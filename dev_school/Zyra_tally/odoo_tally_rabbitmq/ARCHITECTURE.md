# Architecture

## Boundary

Odoo never calls TallyPrime's HTTP/XML gateway directly, and this addon never
imports an XML/HTTP client for Tally. All communication crosses a RabbitMQ
broker:

```
Odoo (this module)  <-- topic exchange "tally.sync" -->  tally_connector (external worker)  <--HTTP/XML-->  TallyPrime
```

`tally_connector` is a separate process (not part of this addon, not
installed by this manifest) that speaks Tally's XML gateway. Its expected
contract is documented in [TALLY_INTEGRATION.md](TALLY_INTEGRATION.md). This
addon's job ends at the RabbitMQ exchange.

## Outbound flow (Odoo -> Tally)

1. A business action happens in Odoo (a customer is created, an invoice is
   posted, ...). The relevant model override (`res.partner`,
   `product.template`, `account.move`, `account.payment`) calls
   `tally.sync.queue.enqueue(...)`.
2. `enqueue()` writes one outbox row (state `pending`) inside the *same*
   database transaction as the business change. If the transaction rolls
   back, the outbox row never existed -- there is no window where Odoo
   commits a change but "forgets" to sync it.
3. `cron_tally_process_outbox` (every minute) picks up `pending`/`retry`
   rows, opens one RabbitMQ connection, and publishes each as a JSON
   envelope (see [API_SCHEMA.md](API_SCHEMA.md)) with a routing key such as
   `odoo.customer.created`. Each row's state transition is committed
   individually -- a broker outage after row 5 of a 100-row batch leaves
   rows 1-5 `queued` and 6-100 `pending`, not the whole batch rolled back.
4. `tally_connector` consumes from the `tally.odoo.*` queues, translates the
   envelope into a Tally XML voucher/master, and posts it to Tally's HTTP
   gateway.

## Inbound flow (Tally -> Odoo)

1. `tally_connector` detects a change in Tally (new/edited ledger, stock
   item, voucher) and publishes an envelope with a `tally.`-prefixed routing
   key (e.g. `tally.customer.created`) to the same exchange.
2. `cron_tally_consume_incoming_*` crons pull from the `tally.incoming.*`
   queues and dispatch to a handler via `services.event_consumer.EventDispatcher`.
3. The handler matches an existing Odoo record (by `tally.sync.mapping`,
   then GSTIN/SKU/name -- see [SYNC_MAPPING.md](SYNC_MAPPING.md)) or creates
   one, writing with the `tally_sync_no_publish` context key set so the
   write does not itself enqueue a new outbound event (loop prevention).
4. If a field's incoming Tally value disagrees with Odoo's current value
   *and* Odoo has a local change for that same record still sitting
   unsynced in the outbox, the field is held back and logged as a
   `tally.sync.conflict` instead of being silently overwritten -- see
   the "Conflicts" section below.

## Reliability

- **Outbox pattern**: every outbound sync is a durable DB row before it is
  ever published, so an Odoo crash or RabbitMQ outage cannot silently drop a
  sync.
- **Idempotency**: every message carries a key `source:model:record_id:event_type`
  (`services/idempotency.py`). `enqueue()` collapses repeated calls for the
  same key while a prior attempt is still unresolved, so rapid edits before
  the first sync goes out do not produce duplicate messages.
- **Retries and dead-lettering**: a publish failure moves a row to `retry`
  (picked up again by the next cron run) until `max_retries` is exceeded, at
  which point it moves to `dead_letter` and stops being auto-retried. On the
  consume side, `RabbitMQClient.consume_batch` tracks retry count in the
  `x-retry-count` message header and rejects (without requeue) into the
  shared DLQ once exhausted.
- **Manual recovery**: the dashboard and the Sync Queue / Failed
  Synchronizations views let an operator retry `retry`/`dead_letter` rows on
  demand (`tally.sync.queue.action_retry`) without waiting for the cron.

## Conflicts

Two-way sync means both sides can change the same record before the other
side's change has been applied. `tally.sync.conflict.detect_and_split()` is
the single choke point for this: given an inbound `vals` dict, it checks
whether the target Odoo record has any outbound outbox entry still
`pending`/`queued`/`retry`. If not, the inbound update is safe to apply
as-is (the common case). If so, only fields that actually disagree between
Odoo's current value and the incoming Tally value are held back as
`tally.sync.conflict` records for manual resolution (Use Odoo Value / Use
Tally Value / Mark Manually Merged / Ignore) -- everything else in the same
update still applies immediately.

## Multi-company

Every model that carries sync state (`tally.integration.config`,
`tally.sync.queue`, `tally.sync.mapping`, `tally.sync.conflict`) is scoped by
`company_id`, and a cron run only ever touches the outbox/config rows of
**the company the cron executes as** (`tally.integration.config` is looked up
by `self.env.company`, and every outbox/mapping query it drives is filtered
to that company). This is correct and tested (see `test_multi_company.py`)
but it is not automatic fan-out across companies: a deployment with more
than one company that each needs its own RabbitMQ sync should give each
company its own `tally.integration.config` record and its own `ir.cron`
entry scoped to that company (via the cron's `Company` field, or a
`sudo(company)` wrapper), rather than expecting the shipped crons to loop
over every company on their own.

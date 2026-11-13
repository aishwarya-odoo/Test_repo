# Troubleshooting

## Nothing is syncing at all

1. Check **Tally Integration > Dashboard**: `connection_status` and
   `sync_enabled`.
2. `sync_enabled` off, or no `tally.integration.config` record for the
   current company at all (`not_configured`), stops both
   `cron_tally_process_outbox` and every `cron_tally_consume_incoming_*`
   cron cold -- they check this before doing anything.
3. Confirm the crons themselves are active: Settings > Technical >
   Automation > Scheduled Actions, search "Tally". All five should be
   active with a 1-minute interval.
4. Click **Test RabbitMQ Connection** on the config record. A failure here
   means Odoo can't even open a connection -- check host/port/credentials
   and that the `rabbitmq` container is up (`docker compose ps`).

## Outbound events stuck in `pending`

- `cron_tally_process_outbox` runs once a minute; give it a cycle.
- Check the Scheduled Action's last run / next run isn't stuck due to a
  DB lock contention on `ir_cron` around exact-minute boundaries (rare, but
  if you're manually triggering `-u` module upgrades at the same moment
  the live server's own cron fires, you can hit a transient "could not
  serialize access due to concurrent update" -- just retry).

## Outbound events stuck in `retry` or moved to `dead_letter`

- Open the entry in **Sync Queue** (or **Failed Synchronizations** for
  `dead_letter` specifically) and read `error_message`.
- A `pika.exceptions.UnroutableError` here means the routing key had no
  bound queue -- check `rabbitmq_exchange` matches what `declare_topology()`
  set up, and that nobody deleted/rebound a queue manually in the RabbitMQ
  management UI.
- Connection-refused / timeout errors mean the broker was unreachable at
  publish time -- these self-heal once the broker is back, but you can use
  **Retry Failed** / **Retry Dead Letter** on the dashboard to force an
  immediate re-attempt instead of waiting for the next cron tick.
- A `dead_letter` entry has exhausted `max_retries` (default 3) and will
  **not** be picked up automatically again -- it must be manually retried
  (which resets it to `pending`) once the underlying issue is fixed.

## Inbound events not creating/updating Odoo records

- Confirm `tally_connector` is actually publishing to the `tally.incoming.*`
  queues with the routing keys in [RABBITMQ.md](RABBITMQ.md) -- check queue
  depths in the RabbitMQ management UI.
- A poison (non-JSON) message is rejected straight to the DLQ and logged as
  a warning (`odoo.addons.odoo_tally_rabbitmq.services.rabbitmq_client`) --
  check the Odoo server log for "Poison message on ... -- cannot parse
  JSON".
- If the record exists in Odoo but didn't get created, check that the
  incoming `data` included the required fields for that event type (see
  [API_SCHEMA.md](API_SCHEMA.md)) -- e.g. a customer event with no `name`
  is dropped with a warning rather than creating an unnamed partner.

## A field isn't updating even though an inbound event arrived

Check **Tally Integration > Sync Conflicts**. If the record had a local
Odoo change still sitting unsynced in the outbox when the inbound event
arrived, the conflicting field(s) were deliberately held back rather than
overwritten -- resolve the conflict (Use Odoo Value / Use Tally Value /
Mark Manually Merged) to apply the value you want. See "Conflicts" in
[ARCHITECTURE.md](ARCHITECTURE.md) for exactly when this triggers.

## A payment came in from Tally but isn't reconciled

Check `tally_reconciliation_state` on the `account.payment` record. It is
only ever `matched` when the inbound event's `data.invoice_references`
contained **exactly one** reference that matched a single open invoice for
that partner. Anything else (missing, multiple, or unmatched references) is
left `pending` on purpose -- see "Payment reconciliation" in
[TALLY_INTEGRATION.md](TALLY_INTEGRATION.md). Reconcile manually from the
payment/invoice as you normally would in Odoo.

## Duplicate customer/vendor/product appeared instead of updating the existing one

This means the inbound event's identity fields didn't match what Odoo has
on file -- see "Identity matching per model" in
[SYNC_MAPPING.md](SYNC_MAPPING.md). Most commonly: the first sync of a
record used a name that doesn't exactly match Odoo's existing record (e.g.
extra whitespace, a legal-entity suffix), and no GSTIN/SKU/barcode was
provided to match on instead. Once a `tally.sync.mapping` row exists
(`tally_id` set), this stops being an issue for future updates to the same
object.

## After editing this module's Python code, changes don't seem to take effect

Odoo does not hot-reload edited `.py` source in an already-running worker
process. After `-u odoo_tally_rabbitmq`, restart the container:

```bash
docker compose restart odoo
```

## Running tests fails with "Address already in use"

The live server is already bound to 8069/8072. Use alternate ports for the
test run:

```bash
--http-port=8169 --gevent-port=8172
```

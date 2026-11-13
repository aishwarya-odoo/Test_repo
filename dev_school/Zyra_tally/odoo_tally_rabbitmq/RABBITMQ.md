# RabbitMQ Topology

Defined in `services/rabbitmq_client.py`; `RabbitMQClient.declare_topology()`
declares all of this idempotently on every connection, so nothing here needs
manual setup in the RabbitMQ management UI (though it's useful for
inspecting queue depths and dead-lettered messages there).

## Exchange

One durable topic exchange: **`tally.sync`** (configurable per company via
`tally.integration.config.rabbitmq_exchange`). Every event, both directions,
publishes here; only the routing key differs.

## Routing keys

- Outbound (Odoo -> Tally): `odoo.<event_type>`, e.g. `odoo.customer.created`,
  `odoo.sales_invoice.created`, `odoo.customer_payment.created`.
- Inbound (Tally -> Odoo): `tally.<event_type>`, e.g. `tally.customer.created`,
  `tally.receipt.created`.

(`services/event_publisher.py:routing_key_for` derives this: `odoo.*` for
Odoo-sourced events, the event type used as-is for Tally-sourced ones.)

## Queues

| Queue | Bound patterns | Consumed by |
|---|---|---|
| `tally.odoo.customer` | `odoo.customer.*` | `tally_connector` |
| `tally.odoo.vendor` | `odoo.vendor.*` | `tally_connector` |
| `tally.odoo.product` | `odoo.product.*` | `tally_connector` |
| `tally.odoo.invoice` | `odoo.sales_invoice.*`, `odoo.purchase_invoice.*` | `tally_connector` |
| `tally.odoo.payment` | `odoo.customer_payment.*`, `odoo.vendor_payment.*` | `tally_connector` |
| `tally.incoming.customer` | `tally.customer.*` | this module (`cron_tally_consume_incoming_partners`) |
| `tally.incoming.vendor` | `tally.vendor.*` | this module (`cron_tally_consume_incoming_partners`) |
| `tally.incoming.product` | `tally.product.*` | this module (`cron_tally_consume_incoming_products`) |
| `tally.incoming.invoice` | `tally.sales_invoice.*`, `tally.purchase_invoice.*` | this module (`cron_tally_consume_incoming_invoices`) |
| `tally.incoming.payment` | `tally.receipt.*`, `tally.payment.*` | this module (`cron_tally_consume_incoming_payments`) |

Every queue is declared with `x-dead-letter-exchange` pointing at the shared
DLX below, so a message that exhausts its retries lands in one place
regardless of which domain queue it came from.

## Dead-lettering

- **`tally.sync.dlx`** -- topic exchange, bound `#` to **`tally.sync.dlq`**.
- A message that fails `max_retries` times (tracked in the `x-retry-count`
  header, not requeue count) is rejected with `requeue=False`, which RabbitMQ
  routes to the DLX/DLQ per the queue's `x-dead-letter-exchange` argument.
- A message whose body isn't valid JSON is rejected immediately (no retry --
  a poison message will never become parseable) and also lands in the DLQ.

## Consumption model

`RabbitMQClient.consume_batch` is pull-based (`basic_get` in a loop, up to
`max_messages`), matching this module's cron-driven design -- there is no
long-lived `basic_consume` subscription anywhere in this addon. A failed
handler call causes the message to be **republished** to the same queue with
an incremented `x-retry-count` header (rather than `basic_nack(requeue=True)`,
which would put it back with no visible retry count) and the original
delivery is acked.

## Publishing guarantees

`RabbitMQClient.publish()` uses `confirm_delivery()` + `mandatory=True`, so
publishing to a routing key with no bound queue raises
`pika.exceptions.UnroutableError` immediately at publish time instead of
silently dropping the message -- a misconfigured routing key or exchange
name fails loudly. All messages are published with `delivery_mode=2`
(persistent).

## Manual inspection

The RabbitMQ management UI (`http://<host>:${RABBITMQ_MGMT_PORT}`, default
`15672`) is useful for watching queue depths, peeking at DLQ contents, and
confirming bindings match the table above.

# Configuration

All configuration lives on one `tally.integration.config` record per
company (**Tally Integration > Configuration**). The unique constraint
`_company_config_unique` enforces exactly one record per `company_id`.

## Fields

| Field | Purpose |
|---|---|
| `tally_company_name` / `tally_company_identifier` | Which Tally company this config corresponds to (for reference/display -- not used to route messages, since that's per-exchange/queue, not per-company-name). |
| `tally_gateway_url` | TallyPrime's own HTTP/XML gateway address (e.g. `http://192.168.2.55:9000/`). This Odoo module never calls it -- it's recorded here as a single source of truth for whoever configures/runs the separate `tally_connector` worker. |
| `rabbitmq_host` / `rabbitmq_port` | Broker address. Inside the Odoo container this is `rabbitmq` / `5672` for this project's docker-compose setup. |
| `rabbitmq_virtual_host` | RabbitMQ vhost, default `/`. |
| `rabbitmq_username` / `rabbitmq_password` | Broker credentials. `rabbitmq_password` is only visible/editable by **Tally Sync Manager** (`group_tally_manager`); readable by nobody else in the UI. |
| `rabbitmq_exchange` | Topic exchange name, default `tally.sync`. Only change this if you are also changing `tally_connector`'s configuration to match -- both sides must agree. |
| `rabbitmq_connection_timeout` / `rabbitmq_heartbeat` | Socket timeout and AMQP heartbeat, in seconds. |
| `sync_enabled` | Master switch. Both `cron_tally_process_outbox` and every `cron_tally_consume_incoming_*` cron check this (via `tally.sync.queue._get_config()`) and do nothing when it is off, even if the crons themselves are active. |
| `customer_sync_enabled`, `vendor_sync_enabled`, `product_sync_enabled`, `invoice_sync_enabled`, `payment_sync_enabled` | Reserved per-entity toggles for finer-grained control. |
| `retry_count`, `retry_delay` | Configured max retry count / delay; the outbox's actual per-row `max_retries` is set at `enqueue()` time (default 3) rather than read live from this field. |
| `connection_status`, `connection_checked_at` | Read-only, set by **Test RabbitMQ Connection**. |
| `tally_connection_status`, `tally_connection_checked_at` | Read-only, set by **Test Tally Connection** -- a diagnostic HTTP GET against `tally_gateway_url` that checks for a `TallyPrime`-flavored response. This is a standalone health check; it is not part of the sync flow (Odoo still never calls Tally during actual sync, only `tally_connector` does). |

## Security groups

- **Tally Sync User** (`group_tally_user`): read access to configuration,
  queue, mappings, conflicts; can resolve conflicts and use the dashboard.
  Cannot see the RabbitMQ password.
- **Tally Sync Manager** (`group_tally_manager`, implies User): full CRUD on
  every model in this module, including the RabbitMQ password field.

## Recommended first-time sequence

1. Create the config record with `sync_enabled` **off**.
2. Fill in the RabbitMQ connection fields and `tally_gateway_url`.
3. Click **Test RabbitMQ Connection** and **Test Tally Connection**. Fix
   connectivity/credentials until both succeed.
4. Turn `sync_enabled` on.
5. Watch **Tally Integration > Dashboard** and **Sync Queue** for the first
   few outbound events to confirm they reach `queued` state.

# tally_connector

The standalone worker that actually speaks TallyPrime's HTTP/XML gateway.
Not an Odoo module -- a plain Python service, run under `supervisord`
inside its own Docker container, alongside (not inside) the Odoo stack.
See `odoo_tally_rabbitmq/ARCHITECTURE.md` for how this fits into the
overall two-way sync design; this file only covers the connector itself.

```
Odoo (odoo_tally_rabbitmq)  <--RabbitMQ-->  tally_connector  <--HTTP/XML-->  TallyPrime
```

## What it does

Two long-running processes (managed by supervisord, restarted
independently if either crashes):

- **outbound**: pulls from the `tally.odoo.*` queues (customer/vendor/
  product/invoice/payment events Odoo published) and pushes each to Tally
  via its XML import gateway. After a successful ledger/stock-item push,
  it reads the record back to grab the GUID Tally assigned and publishes
  a confirmation event to `tally.incoming.*` so Odoo's mapping gets a
  `tally_id`.
- **inbound**: polls Tally's ledger/stock-item/voucher collections for
  anything with `ALTERID` greater than the last one it's seen (Tally has
  no push/webhook mechanism), classifies each into an Odoo-facing event
  type, and publishes to the matching `tally.incoming.*` queue.

## Verified against a real TallyPrime server

Every piece of the XML dialect here was confirmed against a live server,
not written from documentation alone:

- Master creation (`Ledger`, `StockItem`) requires an explicit
  `<NAME.LIST><NAME>...</NAME></NAME.LIST>` in addition to the `NAME`
  attribute -- omitting it is accepted structurally but silently creates
  nothing (`No Valid Names!`).
- Reading data back uses `<TYPE>Collection</TYPE>` with an ad-hoc TDL
  `<COLLECTION>` definition, filtered via a `<SYSTEM TYPE="Formulae">`
  block -- there's no query language beyond that.
- TallyPrime emits a literal, invalid-per-XML-1.0 character reference
  (`&#4;`) as its "field not set" placeholder marker. Python's standard
  parser rejects this outright; `tally_xml._strip_invalid_xml_charrefs`
  sanitizes it before parsing.
- A response's `<CMPINFO>` summary block reuses the same tag names as the
  actual records (a literal `<LEDGER>3</LEDGER>` meaning "3 ledgers
  exist", not a ledger). `parse_collection_response` is scoped to
  `<DATA><COLLECTION>` children only to avoid matching these.

**Not yet verified with a live write**: the Sales/Purchase invoice and
Receipt/Payment voucher import XML (`build_sales_purchase_voucher_xml`,
`build_payment_receipt_voucher_xml`). Ledger and StockItem master
push+read-back were both confirmed end-to-end; vouchers carry real
accounting impact, so that verification was deliberately deferred rather
than done casually. Test one of each in a controlled way before relying
on this in production -- see "Testing" below.

## Known limitations (see file-level docstrings for detail)

- Renaming a customer/vendor/product in Odoo pushes an `ACTION="Alter"` by
  the *new* name; if Tally's ledger was never renamed to match, Tally
  reports "does not exist" and the event dead-letters for manual retry
  rather than silently duplicating.
- A voucher created directly in Tally (never originated in Odoo) has no
  `odoo_id` and isn't matched by Odoo's current inbound invoice handler --
  see the "Known gap" in `odoo_tally_rabbitmq/TALLY_INTEGRATION.md`.
- Inbound payment/receipt events never carry bill allocations, so
  `invoice_references` is always empty -- Odoo correctly leaves those
  "pending reconciliation" rather than guessing.
- One connector instance = one Tally company = one Odoo company. A
  multi-company deployment needs one instance (one set of env vars) per
  company.

## Configuration (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `RABBITMQ_HOST` | `rabbitmq` | Broker host (docker-compose service name). |
| `RABBITMQ_PORT` | `5672` | |
| `RABBITMQ_USER` / `RABBITMQ_PASSWORD` | `odoo` / `odoo` | |
| `RABBITMQ_EXCHANGE` | `tally.sync` | Must match Odoo's `tally.integration.config.rabbitmq_exchange`. |
| `TALLY_GATEWAY_URL` | *(required)* | e.g. `http://192.168.2.55:9000/` |
| `TALLY_COMPANY_NAME` | *(required)* | Exact Tally company name, e.g. `PearlSoft Logistics`. |
| `ODOO_COMPANY_ID` | `1` | Stamped on every envelope this worker publishes. |
| `TALLY_POLL_INTERVAL_SECONDS` | `60` | Inbound poll cadence. |
| `RABBITMQ_CONSUME_INTERVAL_SECONDS` | `15` | Outbound consume cadence. |
| `STATE_FILE_PATH` | `/data/tally_connector_state.json` | ALTERID cursor per collection type; mount a volume here. |

## Running it

```bash
docker compose up -d --build tally_connector
docker compose logs -f tally_connector
```

## Testing

Offline (no network -- run from this directory):
```bash
python3 -m unittest discover -s tests -p "test_*.py"
```

Live, read-only, against the real Tally server (explicit opt-in, since it
talks to a real external system):
```bash
RUN_LIVE_TALLY_TESTS=1 TALLY_GATEWAY_URL=http://192.168.2.55:9000/ \
  TALLY_COMPANY_NAME="PearlSoft Logistics" python3 -m unittest tests.test_live_tally
```

There is deliberately no automated live *write* test -- creating masters
or vouchers in a real company is a one-off, deliberate action (see
`odoo_tally_rabbitmq/TALLY_INTEGRATION.md`), not something a routine test
run should do on every invocation.

# Tally Integration

## What this addon does and does not do

This addon (`odoo_tally_rabbitmq`) only talks to RabbitMQ. It never opens an
HTTP connection to TallyPrime's XML gateway. The actual translation between
this module's JSON envelopes (see [API_SCHEMA.md](API_SCHEMA.md)) and
Tally's XML request/response format is the job of a separate worker process,
referred to throughout this module's code and docs as **`tally_connector`**.
It lives alongside this addon at `addons/tally_connector/` (not an Odoo
module -- a standalone Python service, run under supervisord in its own
container; see that directory's own README.md for how it works and what's
been verified against a real TallyPrime server).

This document specifies the contract `tally_connector` fulfills so that
everything built here (outbox, queue consumers, conflict detection,
dashboard) works correctly with it.

## What `tally_connector` must do

### Consuming Odoo -> Tally events

1. Connect to the same RabbitMQ broker/exchange (`tally.sync` by default).
2. Consume from the five `tally.odoo.*` queues (see
   [RABBITMQ.md](RABBITMQ.md) for the full binding table).
3. For each message, parse the JSON envelope (schema in
   [API_SCHEMA.md](API_SCHEMA.md)), build the corresponding Tally XML
   request (a ledger master for `customer.*`/`vendor.*`, a stock item master
   for `product.*`, a voucher for `sales_invoice.*`/`purchase_invoice.*`/
   `customer_payment.*`/`vendor_payment.*`), and POST it to Tally's HTTP
   gateway (confirmed reachable in this environment at
   `http://<tally-host>:9000/`).
4. Ack the message only after Tally has accepted the request. On a
   Tally-side rejection, nack/reject so this module's own retry/dead-letter
   handling (already implemented in `RabbitMQClient.consume_batch`, which
   `tally_connector` should reuse or mirror) takes over.

### Publishing Tally -> Odoo events

1. Detect changes in Tally (new/edited ledger, stock item, voucher) --
   typically by polling Tally's XML gateway for a company's data on an
   interval, since Tally has no native webhook/push mechanism.
2. Build a JSON envelope matching [API_SCHEMA.md](API_SCHEMA.md), with
   `source: "tally"` and a `tally.`-prefixed event type
   (e.g. `tally.customer.updated`).
3. Publish to the `tally.sync` exchange with routing key
   `tally.<event_type>` so it lands in the matching `tally.incoming.*` queue
   that this module already consumes.
4. Include a `data.tally_id` (Tally's own ledger/voucher GUID or masterid)
   in every payload -- this is what `tally.sync.mapping.find_by_tally_id`
   matches against on the Odoo side, and it is the field that should be
   sent back on every subsequent update for the same Tally object so Odoo
   doesn't create a duplicate mapping.

## Identity matching (how each side finds "the same" record)

Since Tally has no concept of Odoo's internal IDs and vice versa, this
module never assumes IDs line up -- see
[SYNC_MAPPING.md](SYNC_MAPPING.md) for exactly how each model matches.
`tally_connector` should send whatever natural key each side actually has
(GSTIN/VAT for parties, SKU/barcode for stock items, invoice number for
vouchers) so those matching rules have something to work with on a
first-ever sync, before a `tally.sync.mapping` row exists.

## Payment reconciliation

Per the reverse (Tally -> Odoo) payment flow implemented in
`models/account_payment.py`, `tally_connector` should include the invoice's
own reference number(s) in `data.invoice_references` when publishing a
`tally.receipt.created`/`tally.payment.created` event. Odoo only
auto-reconciles when exactly one reference is given and it matches a single
open invoice for that partner -- anything else (no reference, multiple
references, no match) is intentionally left as
`tally_reconciliation_state = "pending"` for manual reconciliation, rather
than guessing.

## Known gap

`account_move.py`'s inbound handler (`_tally_apply_incoming_invoice`) only
updates an *existing* Odoo invoice's sync state/voucher number by
`data.odoo_id` -- it does not create a brand-new Odoo invoice from a Tally
voucher that originated entirely in Tally. If that direction is needed,
extend `_tally_apply_incoming_invoice` to resolve partner/products/taxes/
journal from the envelope and call `account.move.create(...)` when no
`odoo_id` mapping exists, following the same pattern already used for
partners and products.

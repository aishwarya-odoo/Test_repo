# Message Envelope Schema

Every message published to the `tally.sync` exchange, either direction, is a
single JSON object built by `services/message_serializer.py:build_envelope`:

```json
{
  "schema_version": "1.0",
  "message_id": "a1b2c3d4-...",
  "idempotency_key": "odoo:res.partner:245:customer.created",
  "source": "odoo",
  "event_type": "customer.created",
  "timestamp": "2026-09-17T08:00:00Z",
  "company_id": 1,
  "data": { "...": "event-specific payload, see below" }
}
```

| Field | Type | Notes |
|---|---|---|
| `schema_version` | string | Currently always `"1.0"`. Bump this if the envelope shape (not the `data` payload) changes incompatibly. |
| `message_id` | string (UUID) | Also set as the AMQP message's `message_id` property. |
| `idempotency_key` | string | `source:model:record_id:event_type`. See [SYNC_MAPPING.md](SYNC_MAPPING.md) / `services/idempotency.py`. |
| `source` | `"odoo"` \| `"tally"` | Which side produced the event -- used to prevent update loops. |
| `event_type` | string | Bare for Odoo-sourced events (`customer.created`); `tally.`-prefixed for Tally-sourced events (`tally.customer.created`). See routing key derivation in [RABBITMQ.md](RABBITMQ.md). |
| `timestamp` | string | UTC, `%Y-%m-%dT%H:%M:%SZ`. |
| `company_id` | integer | Odoo `res.company` id. |
| `data` | object | Event-specific, see below. |

## Event catalog

### Outbound (`source: "odoo"`)

| `event_type` | Emitted by | `data` shape |
|---|---|---|
| `customer.created` / `customer.updated` | `res.partner` (customer_rank > 0) | `{odoo_id, name, vat, street, street2, city, state, zip, country, phone, email, company_id}` |
| `vendor.created` / `vendor.updated` | `res.partner` (supplier_rank > 0) | same shape as customer |
| `product.created` / `product.updated` | `product.template` (one event per variant) | `{odoo_id, template_id, name, sku, barcode, uom, sales_price, cost, category, active, company_id, hsn?, hsn_description?, taxes?}` |
| `sales_invoice.created` | `account.move` (`out_invoice`/`out_refund`, on posting) | `{odoo_id, reference, move_type, is_credit_note, invoice_date, currency, partner, journal, lines[], amount_untaxed, amount_tax, amount_total, company_id}` |
| `purchase_invoice.created` | `account.move` (`in_invoice`/`in_refund`, on posting) | same shape as `sales_invoice.created` |
| `customer_payment.created` | `account.payment` (`partner_type=customer`, on posting) | `{odoo_id, partner, amount, date, journal, reference, invoice_references[], currency, company_id}` |
| `vendor_payment.created` | `account.payment` (`partner_type=supplier`, on posting) | same shape as `customer_payment.created` |

`lines[]` (invoices) is a list of
`{product, quantity, unit_price, discount, subtotal, taxes: [{name, amount}]}`.

### Inbound (`source: "tally"`, consumed by this module)

| `event_type` | Consumed by | Required `data` fields |
|---|---|---|
| `tally.customer.created` / `tally.customer.updated` | `res.partner._tally_apply_incoming_customer` | `name` (required to create); `tally_id`, `vat`, `street`, `city`, `zip`, `phone`, `email` (optional) |
| `tally.vendor.created` / `tally.vendor.updated` | `res.partner._tally_apply_incoming_vendor` | same as customer |
| `tally.product.created` / `tally.product.updated` | `product.template._tally_apply_incoming_product` | `name` (required to create); `tally_id`, `sku`, `barcode`, `sales_price`, `cost` (optional) |
| `tally.sales_invoice.created` | `account.move._tally_apply_incoming_invoice` | `odoo_id` (must match an existing Odoo invoice -- see the "Known gap" in [TALLY_INTEGRATION.md](TALLY_INTEGRATION.md)); `tally_id`, `voucher_number` (optional) |
| `tally.purchase_invoice.created` | same handler, `move_type="in_invoice"` | same as above |
| `tally.receipt.created` | `account.payment._tally_apply_incoming_payment(..., "customer")` | `partner: {name, vat}`; `amount`; `reference`; `invoice_references: []` (optional, enables auto-reconciliation when exactly one) |
| `tally.payment.created` | `account.payment._tally_apply_incoming_payment(..., "supplier")` | same as `tally.receipt.created` |

Every inbound `data` object should include `tally_id` (or, for invoices,
`odoo_id`) so the corresponding `tally.sync.mapping` row can be found on
subsequent updates instead of re-matching by name/SKU/GSTIN every time.

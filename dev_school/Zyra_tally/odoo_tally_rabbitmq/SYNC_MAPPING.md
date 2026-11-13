# Sync Mapping

## The `tally.sync.mapping` model

One row links one Odoo record to one Tally object, scoped by company:

- `odoo_model` / `odoo_record_id` -- the Odoo side.
- `tally_object_type` -- one of `customer_ledger`, `vendor_ledger`,
  `stock_item`, `sales_voucher`, `purchase_voucher`, `receipt_voucher`,
  `payment_voucher`.
- `tally_id` / `tally_name` -- the Tally side (Tally's own GUID/masterid and
  display name).
- `sync_status`, `last_sync_at`, `last_success_at`, `last_error`, `checksum`.

Unique constraint: one mapping per
`(odoo_model, odoo_record_id, tally_object_type, company_id)`. A partner
that is both a customer and a vendor gets **two** mapping rows (one
`customer_ledger`, one `vendor_ledger`) since Tally itself models those as
separate ledgers.

## Identity matching per model

Every inbound handler follows the same order: **look up the mapping by
`tally_id` first; only fall back to a natural-key search if no mapping
exists yet** (i.e. this is the first time Odoo has heard about this Tally
object). This is what makes repeated updates for the same object update the
same Odoo record instead of drifting to a name/SKU match every time.

| Model | Natural-key fallback (first sync only) |
|---|---|
| `res.partner` (`_tally_find_by_identity`) | GSTIN/VAT exact match, then exact name match among top-level (non-child) partners |
| `product.product` (`_tally_find_by_identity`) | SKU (`default_code`) exact match, then barcode, then exact name |
| `account.move` | Not applicable -- the inbound handler only ever updates an invoice Odoo already created (matched by `data.odoo_id`); see the "Known gap" in [TALLY_INTEGRATION.md](TALLY_INTEGRATION.md) |
| `account.payment` (`_tally_find_invoice_for_reconciliation`) | Matches the *invoice* to reconcile against by exact invoice number, only when exactly one reference was given |

## Why GSTIN/SKU and not name alone

Names collide (two "John Doe"s, a product renamed on one side but not the
other yet); GSTIN and SKU are the identifiers both systems are expected to
carry for a real business record. Name match is the fallback, not the
primary key, precisely because it's the weakest signal available.

## Loop prevention

Every inbound `write()`/`create()` call passes
`with_context(tally_sync_no_publish=True)`. The outbound `create()`/`write()`
overrides on `res.partner`, `product.template`, `account.move`, and
`account.payment` all check this context key and skip `_tally_enqueue()`
when it is set. Without this, applying an inbound update would itself look
like a local change and get re-published right back to Tally.

## Conflicts vs. plain overwrite

Applying an inbound update is not a blind overwrite -- see the "Conflicts"
section of [ARCHITECTURE.md](ARCHITECTURE.md) for when a field is instead
held back as a `tally.sync.conflict`.

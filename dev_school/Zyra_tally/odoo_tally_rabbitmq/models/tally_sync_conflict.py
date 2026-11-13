import logging

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)

_PENDING_OUTBOUND_STATES = ("pending", "queued", "retry")


class TallySyncConflict(models.Model):
    _name = "tally.sync.conflict"
    _description = "Tally Sync Conflict"
    _order = "create_date desc"

    odoo_model = fields.Char(required=True, readonly=True)
    odoo_record_id = fields.Integer(required=True, readonly=True)
    tally_object_type = fields.Selection(
        selection=[
            ("customer_ledger", "Customer Ledger"),
            ("vendor_ledger", "Vendor Ledger"),
            ("stock_item", "Stock Item"),
            ("sales_voucher", "Sales Voucher"),
            ("purchase_voucher", "Purchase Voucher"),
            ("receipt_voucher", "Receipt Voucher"),
            ("payment_voucher", "Payment Voucher"),
        ],
        readonly=True,
    )
    field_name = fields.Char(required=True, readonly=True)
    odoo_value = fields.Char(readonly=True)
    tally_value = fields.Char(readonly=True)
    state = fields.Selection(
        [
            ("pending", "Pending Review"),
            ("resolved_use_odoo", "Resolved -- Used Odoo Value"),
            ("resolved_use_tally", "Resolved -- Used Tally Value"),
            ("resolved_manual", "Resolved -- Manual"),
            ("ignored", "Ignored"),
        ],
        default="pending", required=True, readonly=True, copy=False,
    )
    notes = fields.Text()
    company_id = fields.Many2one(
        comodel_name="res.company", required=True, default=lambda self: self.env.company
    )
    resolved_by = fields.Many2one(comodel_name="res.users", readonly=True, copy=False)
    resolved_date = fields.Datetime(readonly=True, copy=False)
    record_display_name = fields.Char(compute="_compute_record_display_name")

    def _compute_record_display_name(self):
        for conflict in self:
            record = self.env[conflict.odoo_model].browse(conflict.odoo_record_id).exists()
            conflict.record_display_name = record.display_name if record else _("(record no longer exists)")

    @api.model
    def detect_and_split(self, odoo_model, record, tally_object_type, vals, company_id):
        """Splits an inbound Tally update (`vals`) into the subset that is
        safe to apply directly. A field is only held back as a genuine
        conflict when BOTH sides changed it: the incoming Tally value
        differs from what Odoo currently holds, AND Odoo has a local change
        for this same record still sitting unsynced in the outbox. Anything
        else (no local change pending, or the values already agree) is
        applied immediately -- most inbound updates hit this fast path."""
        Queue = self.env["tally.sync.queue"].sudo()
        has_pending_outbound = bool(Queue.search_count([
            ("odoo_model", "=", odoo_model), ("odoo_record_id", "=", record.id),
            ("state", "in", _PENDING_OUTBOUND_STATES),
        ]))
        if not has_pending_outbound:
            return vals

        safe_vals = {}
        for field_name, tally_value in vals.items():
            current_value = record[field_name]
            if hasattr(current_value, "id"):
                current_value = current_value.display_name if current_value else False
            if not current_value or str(current_value) == str(tally_value):
                safe_vals[field_name] = tally_value
                continue
            self.sudo().create({
                "odoo_model": odoo_model,
                "odoo_record_id": record.id,
                "tally_object_type": tally_object_type,
                "field_name": field_name,
                "odoo_value": str(current_value),
                "tally_value": str(tally_value),
                "company_id": company_id,
            })
            _logger.info(
                "Tally sync conflict on %s(%s).%s: odoo=%r tally=%r",
                odoo_model, record.id, field_name, current_value, tally_value,
            )
        return safe_vals

    def _tally_reenqueue_record(self, record):
        try:
            record._tally_enqueue(is_new=False)
        except TypeError:
            record._tally_enqueue()

    def action_use_odoo_value(self):
        for conflict in self.filtered(lambda c: c.state == "pending"):
            record = self.env[conflict.odoo_model].browse(conflict.odoo_record_id).exists()
            if record:
                conflict._tally_reenqueue_record(record)
            conflict.write({
                "state": "resolved_use_odoo", "resolved_by": self.env.user.id,
                "resolved_date": fields.Datetime.now(),
            })

    def action_use_tally_value(self):
        for conflict in self.filtered(lambda c: c.state == "pending"):
            record = self.env[conflict.odoo_model].browse(conflict.odoo_record_id).exists()
            if record:
                record.sudo().with_context(tally_sync_no_publish=True).write({conflict.field_name: conflict.tally_value})
            conflict.write({
                "state": "resolved_use_tally", "resolved_by": self.env.user.id,
                "resolved_date": fields.Datetime.now(),
            })

    def action_ignore(self):
        self.filtered(lambda c: c.state == "pending").write({
            "state": "ignored", "resolved_by": self.env.user.id, "resolved_date": fields.Datetime.now(),
        })

    def action_mark_resolved_manual(self):
        self.filtered(lambda c: c.state == "pending").write({
            "state": "resolved_manual", "resolved_by": self.env.user.id, "resolved_date": fields.Datetime.now(),
        })

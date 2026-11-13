from odoo import api, fields, models


class TallySyncMapping(models.Model):
    _name = "tally.sync.mapping"
    _description = "Odoo <-> Tally Record Mapping"
    _order = "id"

    odoo_model = fields.Char(required=True, index=True)
    odoo_record_id = fields.Integer(required=True, index=True)
    odoo_reference = fields.Char()

    tally_object_type = fields.Selection(
        [
            ("customer_ledger", "Customer Ledger"),
            ("vendor_ledger", "Vendor Ledger"),
            ("stock_item", "Stock Item"),
            ("sales_voucher", "Sales Voucher"),
            ("purchase_voucher", "Purchase Voucher"),
            ("receipt_voucher", "Receipt Voucher"),
            ("payment_voucher", "Payment Voucher"),
        ],
        required=True, index=True,
    )
    tally_id = fields.Char(string="Tally ID", index=True)
    tally_name = fields.Char(string="Tally Name")

    company_id = fields.Many2one(
        comodel_name="res.company", required=True, default=lambda self: self.env.company, index=True
    )

    sync_direction = fields.Selection(
        [("outbound", "Odoo -> Tally"), ("inbound", "Tally -> Odoo")], required=True, default="outbound"
    )
    sync_status = fields.Selection(
        [("synced", "Synced"), ("pending", "Pending"), ("error", "Error")],
        default="pending", required=True,
    )

    last_sync_at = fields.Datetime()
    last_success_at = fields.Datetime()
    last_error = fields.Char()

    checksum = fields.Char(help="Hash of the last-synced payload, used to detect unsynced local changes.")

    _mapping_unique = models.Constraint(
        "UNIQUE(odoo_model, odoo_record_id, tally_object_type, company_id)",
        "Only one mapping is allowed per Odoo record and Tally object type within a company.",
    )

    @api.model
    def get_or_create(self, odoo_model, odoo_record_id, tally_object_type, company_id, odoo_reference=None):
        mapping = self.search([
            ("odoo_model", "=", odoo_model),
            ("odoo_record_id", "=", odoo_record_id),
            ("tally_object_type", "=", tally_object_type),
            ("company_id", "=", company_id),
        ], limit=1)
        if mapping:
            return mapping
        return self.create({
            "odoo_model": odoo_model,
            "odoo_record_id": odoo_record_id,
            "odoo_reference": odoo_reference,
            "tally_object_type": tally_object_type,
            "company_id": company_id,
        })

    @api.model
    def find_by_tally_id(self, tally_object_type, tally_id, company_id):
        return self.search([
            ("tally_object_type", "=", tally_object_type),
            ("tally_id", "=", tally_id),
            ("company_id", "=", company_id),
        ], limit=1)

    def mark_synced(self, tally_id, tally_name=None, checksum=None):
        self.ensure_one()
        vals = {
            "tally_id": tally_id,
            "sync_status": "synced",
            "last_sync_at": fields.Datetime.now(),
            "last_success_at": fields.Datetime.now(),
            "last_error": False,
        }
        if tally_name is not None:
            vals["tally_name"] = tally_name
        if checksum is not None:
            vals["checksum"] = checksum
        self.write(vals)

    def mark_error(self, error_message):
        self.ensure_one()
        self.write({
            "sync_status": "error",
            "last_sync_at": fields.Datetime.now(),
            "last_error": error_message,
        })

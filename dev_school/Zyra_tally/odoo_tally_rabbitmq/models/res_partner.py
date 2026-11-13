import logging

from odoo import _, api, fields, models

from ..services import event_consumer, rabbitmq_client

_logger = logging.getLogger(__name__)

# Fields whose change on a top-level (non-child) customer/vendor partner
# should trigger a re-sync to Tally.
_TALLY_PARTNER_FIELDS = {
    "name", "street", "street2", "city", "state_id", "zip", "country_id",
    "phone", "email", "vat", "customer_rank", "supplier_rank",
}

_MODEL = "res.partner"


class ResPartner(models.Model):
    _inherit = "res.partner"

    tally_sync_state = fields.Selection(
        [("not_synced", "Not Synced"), ("pending", "Pending"), ("synced", "Synced"), ("error", "Error")],
        default="not_synced", readonly=True, copy=False,
    )
    tally_customer_mapping_id = fields.Many2one(
        comodel_name="tally.sync.mapping", compute="_compute_tally_mapping_ids", store=False
    )
    tally_vendor_mapping_id = fields.Many2one(
        comodel_name="tally.sync.mapping", compute="_compute_tally_mapping_ids", store=False
    )

    def _compute_tally_mapping_ids(self):
        Mapping = self.env["tally.sync.mapping"].sudo()
        for partner in self:
            partner.tally_customer_mapping_id = Mapping.search(
                [("odoo_model", "=", _MODEL), ("odoo_record_id", "=", partner.id), ("tally_object_type", "=", "customer_ledger")],
                limit=1,
            )
            partner.tally_vendor_mapping_id = Mapping.search(
                [("odoo_model", "=", _MODEL), ("odoo_record_id", "=", partner.id), ("tally_object_type", "=", "vendor_ledger")],
                limit=1,
            )

    # ------------------------------------------------------------ outbound
    def _tally_is_syncable(self):
        """Only top-level (non-child-contact) customers/vendors get their
        own Tally ledger -- child contacts share the parent's ledger, same
        real-world rule any Tally-integrated ERP has to follow."""
        self.ensure_one()
        return not self.parent_id and (self.customer_rank > 0 or self.supplier_rank > 0)

    def _tally_partner_data(self):
        self.ensure_one()
        return {
            "odoo_id": self.id,
            "name": self.name,
            "vat": self.vat or None,
            "street": self.street or None,
            "street2": self.street2 or None,
            "city": self.city or None,
            "state": self.state_id.name or None,
            "zip": self.zip or None,
            "country": self.country_id.name or None,
            "phone": self.phone or None,
            "email": self.email or None,
            "company_id": self.company_id.id or self.env.company.id,
        }

    def _tally_enqueue(self, is_new):
        Queue = self.env["tally.sync.queue"].sudo()
        for partner in self:
            if not partner._tally_is_syncable():
                continue
            data = partner._tally_partner_data()
            if partner.customer_rank > 0:
                Queue.enqueue(
                    "customer.created" if is_new else "customer.updated",
                    _MODEL, partner.id, data,
                )
            if partner.supplier_rank > 0:
                Queue.enqueue(
                    "vendor.created" if is_new else "vendor.updated",
                    _MODEL, partner.id, data,
                )
            partner.write({"tally_sync_state": "pending"})

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        if not self.env.context.get("tally_sync_no_publish"):
            records._tally_enqueue(is_new=True)
        return records

    def write(self, vals):
        res = super().write(vals)
        if not self.env.context.get("tally_sync_no_publish") and _TALLY_PARTNER_FIELDS.intersection(vals):
            self._tally_enqueue(is_new=False)
        return res

    def button_tally_sync(self):
        self._tally_enqueue(is_new=False)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"title": _("Tally Sync"), "message": _("Queued for sync to Tally."), "type": "success"},
        }

    # -------------------------------------------------------------- inbound
    @api.model
    def _tally_find_by_identity(self, data):
        """Duplicate prevention for inbound creates: match on GSTIN first
        (the one identifier both systems are guaranteed to have for a real
        business), fall back to exact name."""
        vat = data.get("vat")
        if vat:
            match = self.search([("vat", "=", vat)], limit=1)
            if match:
                return match
        name = data.get("name")
        if name:
            return self.search([("name", "=", name), ("parent_id", "=", False)], limit=1)
        return self.browse()

    @api.model
    def _tally_apply_incoming_customer(self, envelope):
        return self._tally_apply_incoming(envelope, "customer_ledger", customer_rank=1)

    @api.model
    def _tally_apply_incoming_vendor(self, envelope):
        return self._tally_apply_incoming(envelope, "vendor_ledger", supplier_rank=1)

    @api.model
    def _tally_apply_incoming(self, envelope, tally_object_type, **rank_vals):
        data = envelope.get("data") or {}
        tally_id = data.get("tally_id") or data.get("odoo_id")  # connector should send its own Tally ledger id here
        company_id = envelope.get("company_id") or self.env.company.id
        Mapping = self.env["tally.sync.mapping"].sudo()

        mapping = Mapping.find_by_tally_id(tally_object_type, tally_id, company_id) if tally_id else Mapping.browse()
        partner = self.browse(mapping.odoo_record_id).exists() if mapping else self.browse()
        if not partner:
            partner = self._tally_find_by_identity(data)

        vals = {
            "name": data.get("name"),
            "vat": data.get("vat"),
            "street": data.get("street"),
            "city": data.get("city"),
            "zip": data.get("zip"),
            "phone": data.get("phone"),
            "email": data.get("email"),
            **rank_vals,
        }
        vals = {k: v for k, v in vals.items() if v}

        ctx_self = self.sudo().with_context(tally_sync_no_publish=True)
        if partner:
            Conflict = self.env["tally.sync.conflict"].sudo()
            safe_vals = Conflict.detect_and_split(_MODEL, partner, tally_object_type, vals, company_id)
            partner.sudo().with_context(tally_sync_no_publish=True).write(safe_vals)
        else:
            if not vals.get("name"):
                _logger.warning("Cannot create partner from Tally event without a name: %s", envelope)
                return True
            partner = ctx_self.create(vals)

        mapping = Mapping.get_or_create(_MODEL, partner.id, tally_object_type, company_id, partner.name)
        mapping.mark_synced(tally_id, tally_name=data.get("name"))
        partner.sudo().write({"tally_sync_state": "synced"})
        return True

    @api.model
    def cron_consume_incoming_partners(self, max_messages=50):
        config = self.env["tally.sync.queue"]._get_config()
        if not config or not config.sync_enabled:
            return
        dispatcher = event_consumer.EventDispatcher()
        dispatcher.register("tally.customer.created", self._tally_apply_incoming_customer)
        dispatcher.register("tally.customer.updated", self._tally_apply_incoming_customer)
        dispatcher.register("tally.vendor.created", self._tally_apply_incoming_vendor)
        dispatcher.register("tally.vendor.updated", self._tally_apply_incoming_vendor)

        rmq_config = rabbitmq_client.RabbitMQConfig.from_record(config)
        try:
            with rabbitmq_client.RabbitMQClient(rmq_config) as client:
                client.declare_topology()
                dispatcher.consume_queue(client, "tally.incoming.customer", max_messages=max_messages)
                self.env.cr.commit()
                dispatcher.consume_queue(client, "tally.incoming.vendor", max_messages=max_messages)
                self.env.cr.commit()
        except Exception:
            _logger.exception("Could not reach RabbitMQ to consume incoming customer/vendor events")

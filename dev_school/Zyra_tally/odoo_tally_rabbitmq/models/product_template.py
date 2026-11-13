import logging

from odoo import _, api, fields, models

from ..services import event_consumer, rabbitmq_client

_logger = logging.getLogger(__name__)

_TALLY_PRODUCT_TEMPLATE_FIELDS = {
    "name", "default_code", "barcode", "uom_id", "list_price", "standard_price",
    "categ_id", "active",
}

_MODEL_TEMPLATE = "product.template"
_MODEL_VARIANT = "product.product"


class ProductTemplate(models.Model):
    _inherit = "product.template"

    tally_sync_state = fields.Selection(
        [("not_synced", "Not Synced"), ("pending", "Pending"), ("synced", "Synced"), ("error", "Error")],
        default="not_synced", readonly=True, copy=False,
    )

    # ------------------------------------------------------------ outbound
    def _tally_variant_data(self, variant):
        """Field access is defensive on purpose: this Odoo build is missing
        at least one field custom modules elsewhere assumed existed
        (res.partner.mobile) -- HSN/GST fields in particular depend on
        whether an Indian fiscal localization is installed, so probe with
        _fields rather than hardcoding an attribute access that might not
        exist on every install."""
        data = {
            "odoo_id": variant.id,
            "template_id": self.id,
            "name": variant.display_name,
            "sku": variant.default_code or None,
            "barcode": variant.barcode or None,
            "uom": self.uom_id.name or None,
            "sales_price": self.list_price,
            "cost": self.standard_price,
            "category": self.categ_id.complete_name or None,
            "active": self.active,
            "company_id": self.env.company.id,
        }
        for optional_field, key in (("l10n_in_hsn_code", "hsn"), ("l10n_in_hsn_description", "hsn_description")):
            if optional_field in self.env["product.template"]._fields:
                data[key] = getattr(self, optional_field, None) or None
        taxes = self.taxes_id
        if taxes:
            data["taxes"] = [{"name": t.name, "amount": t.amount} for t in taxes]
        return data

    def _tally_enqueue(self, is_new):
        Queue = self.env["tally.sync.queue"].sudo()
        for template in self:
            variants = template.product_variant_ids or template.browse()
            if not variants:
                continue
            for variant in variants:
                data = template._tally_variant_data(variant)
                Queue.enqueue(
                    "product.created" if is_new else "product.updated",
                    _MODEL_VARIANT, variant.id, data,
                )
            template.write({"tally_sync_state": "pending"})

    @api.model_create_multi
    def create(self, vals_list):
        # Core product.template.create() performs its own follow-up write()
        # on the new record (variant field propagation), which would
        # otherwise route through our write() override below and fire a
        # spurious product.updated right after product.created. Suppress
        # publishing for that internal call and enqueue exactly once here.
        records = super(ProductTemplate, self.with_context(tally_sync_no_publish=True)).create(vals_list)
        # Rebind to the caller's original environment -- otherwise the
        # suppression context above leaks into every later write() the
        # caller does through this same recordset.
        records = records.with_env(self.env)
        if not self.env.context.get("tally_sync_no_publish"):
            records._tally_enqueue(is_new=True)
        return records

    def write(self, vals):
        res = super().write(vals)
        if not self.env.context.get("tally_sync_no_publish") and _TALLY_PRODUCT_TEMPLATE_FIELDS.intersection(vals):
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
        sku = data.get("sku")
        if sku:
            variant = self.env["product.product"].search([("default_code", "=", sku)], limit=1)
            if variant:
                return variant
        barcode = data.get("barcode")
        if barcode:
            variant = self.env["product.product"].search([("barcode", "=", barcode)], limit=1)
            if variant:
                return variant
        name = data.get("name")
        if name:
            return self.env["product.product"].search([("name", "=", name)], limit=1)
        return self.env["product.product"].browse()

    @api.model
    def _tally_apply_incoming_product(self, envelope):
        data = envelope.get("data") or {}
        tally_id = data.get("tally_id") or data.get("odoo_id")
        company_id = envelope.get("company_id") or self.env.company.id
        Mapping = self.env["tally.sync.mapping"].sudo()

        mapping = Mapping.find_by_tally_id("stock_item", tally_id, company_id) if tally_id else Mapping.browse()
        variant = self.env["product.product"].browse(mapping.odoo_record_id).exists() if mapping else self.env["product.product"].browse()
        if not variant:
            variant = self._tally_find_by_identity(data)

        vals = {
            "name": data.get("name"),
            "default_code": data.get("sku"),
            "barcode": data.get("barcode"),
            "list_price": data.get("sales_price"),
            "standard_price": data.get("cost"),
        }
        vals = {k: v for k, v in vals.items() if v not in (None, "")}

        if variant:
            Conflict = self.env["tally.sync.conflict"].sudo()
            safe_vals = Conflict.detect_and_split(_MODEL_VARIANT, variant, "stock_item", vals, company_id)
            variant.product_tmpl_id.sudo().with_context(tally_sync_no_publish=True).write(safe_vals)
        else:
            if not vals.get("name"):
                _logger.warning("Cannot create product from Tally event without a name: %s", envelope)
                return True
            template = self.sudo().with_context(tally_sync_no_publish=True).create(vals)
            variant = template.product_variant_ids[:1]

        mapping = Mapping.get_or_create(_MODEL_VARIANT, variant.id, "stock_item", company_id, variant.display_name)
        mapping.mark_synced(tally_id, tally_name=data.get("name"))
        variant.product_tmpl_id.sudo().write({"tally_sync_state": "synced"})
        return True

    @api.model
    def cron_consume_incoming_products(self, max_messages=50):
        config = self.env["tally.sync.queue"]._get_config()
        if not config or not config.sync_enabled:
            return
        dispatcher = event_consumer.EventDispatcher()
        dispatcher.register("tally.product.created", self._tally_apply_incoming_product)
        dispatcher.register("tally.product.updated", self._tally_apply_incoming_product)

        rmq_config = rabbitmq_client.RabbitMQConfig.from_record(config)
        try:
            with rabbitmq_client.RabbitMQClient(rmq_config) as client:
                client.declare_topology()
                dispatcher.consume_queue(client, "tally.incoming.product", max_messages=max_messages)
                self.env.cr.commit()
        except Exception:
            _logger.exception("Could not reach RabbitMQ to consume incoming product events")

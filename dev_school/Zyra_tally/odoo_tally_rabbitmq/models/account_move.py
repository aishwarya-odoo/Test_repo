import logging

from odoo import _, api, fields, models

from ..services import event_consumer, rabbitmq_client

_logger = logging.getLogger(__name__)

_MODEL = "account.move"

# move_type -> (event_type, tally_object_type, is_credit_note)
_MOVE_TYPE_MAP = {
    "out_invoice": ("sales_invoice", "sales_voucher", False),
    "out_refund": ("sales_invoice", "sales_voucher", True),
    "in_invoice": ("purchase_invoice", "purchase_voucher", False),
    "in_refund": ("purchase_invoice", "purchase_voucher", True),
}


class AccountMove(models.Model):
    _inherit = "account.move"

    tally_sync_state = fields.Selection(
        [("not_synced", "Not Synced"), ("pending", "Pending"), ("synced", "Synced"), ("error", "Error")],
        default="not_synced", readonly=True, copy=False,
    )
    tally_sync_error = fields.Char(readonly=True, copy=False)
    tally_voucher_number = fields.Char(readonly=True, copy=False)

    def _tally_line_data(self, line):
        return {
            "product": line.product_id.default_code or line.product_id.name or line.name,
            "quantity": line.quantity,
            "unit_price": line.price_unit,
            "discount": line.discount,
            "subtotal": line.price_subtotal,
            "taxes": [{"name": t.name, "amount": t.amount} for t in line.tax_ids],
        }

    def _tally_invoice_data(self):
        self.ensure_one()
        _event_base, _object_type, is_credit_note = _MOVE_TYPE_MAP[self.move_type]
        return {
            "odoo_id": self.id,
            "reference": self.name,
            "move_type": self.move_type,
            "is_credit_note": is_credit_note,
            "invoice_date": self.invoice_date and self.invoice_date.isoformat() or None,
            "currency": self.currency_id.name,
            "partner": {
                "odoo_id": self.partner_id.id,
                "name": self.partner_id.name,
                "vat": self.partner_id.vat or None,
            },
            "journal": self.journal_id.name,
            "lines": [self._tally_line_data(line) for line in self.invoice_line_ids if line.display_type == "product"],
            "amount_untaxed": self.amount_untaxed,
            "amount_tax": self.amount_tax,
            "amount_total": self.amount_total,
            "company_id": self.company_id.id,
        }

    def action_post(self):
        res = super().action_post()
        to_sync = self.filtered(lambda m: m.move_type in _MOVE_TYPE_MAP and m.state == "posted")
        to_sync._tally_enqueue()
        return res

    def _tally_enqueue(self):
        Queue = self.env["tally.sync.queue"].sudo()
        for move in self:
            event_base, _object_type, _is_credit_note = _MOVE_TYPE_MAP[move.move_type]
            Queue.enqueue(f"{event_base}.created", _MODEL, move.id, move._tally_invoice_data())
            move.write({"tally_sync_state": "pending", "tally_sync_error": False})

    def button_tally_sync(self):
        self.filtered(lambda m: m.move_type in _MOVE_TYPE_MAP and m.state == "posted")._tally_enqueue()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"title": _("Tally Sync"), "message": _("Queued for sync to Tally."), "type": "success"},
        }

    # -------------------------------------------------------------- inbound
    @api.model
    def _tally_apply_incoming_invoice(self, envelope, move_type):
        """Records the result of a Tally-side voucher against its Odoo
        mapping. Full reverse creation of an Odoo invoice from a Tally
        voucher (resolving partner/products/taxes/journal from scratch) is
        out of scope for this phase -- this handler covers the acceptance
        path (Tally confirms/updates a voucher that originated in Odoo) via
        the mapping, and logs anything it can't match rather than guessing
        at creating a new accounting entry."""
        data = envelope.get("data") or {}
        tally_id = data.get("tally_id")
        company_id = envelope.get("company_id") or self.env.company.id
        object_type = _MOVE_TYPE_MAP[move_type][1]
        Mapping = self.env["tally.sync.mapping"].sudo()

        odoo_id = data.get("odoo_id")
        move = self.browse(odoo_id).exists() if odoo_id else self.browse()
        if not move:
            _logger.warning(
                "Incoming %s event without a matching Odoo invoice (odoo_id=%s) -- "
                "reverse invoice creation is not implemented, dropping: %s",
                move_type, odoo_id, envelope,
            )
            return True

        mapping = Mapping.get_or_create(_MODEL, move.id, object_type, company_id, move.name)
        mapping.mark_synced(tally_id, tally_name=data.get("voucher_number"))
        move.sudo().write({
            "tally_sync_state": "synced",
            "tally_voucher_number": data.get("voucher_number") or move.tally_voucher_number,
        })
        return True

    @api.model
    def cron_consume_incoming_invoices(self, max_messages=50):
        config = self.env["tally.sync.queue"]._get_config()
        if not config or not config.sync_enabled:
            return
        dispatcher = event_consumer.EventDispatcher()
        dispatcher.register(
            "tally.sales_invoice.created", lambda e: self._tally_apply_incoming_invoice(e, "out_invoice")
        )
        dispatcher.register(
            "tally.purchase_invoice.created", lambda e: self._tally_apply_incoming_invoice(e, "in_invoice")
        )

        rmq_config = rabbitmq_client.RabbitMQConfig.from_record(config)
        try:
            with rabbitmq_client.RabbitMQClient(rmq_config) as client:
                client.declare_topology()
                dispatcher.consume_queue(client, "tally.incoming.invoice", max_messages=max_messages)
                self.env.cr.commit()
        except Exception:
            _logger.exception("Could not reach RabbitMQ to consume incoming invoice events")

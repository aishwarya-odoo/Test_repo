import logging

from odoo import _, api, fields, models

from ..services import event_consumer, rabbitmq_client

_logger = logging.getLogger(__name__)

_MODEL = "account.payment"


class AccountPayment(models.Model):
    _inherit = "account.payment"

    tally_sync_state = fields.Selection(
        [("not_synced", "Not Synced"), ("pending", "Pending"), ("synced", "Synced"), ("error", "Error")],
        default="not_synced", readonly=True, copy=False,
    )
    tally_reconciliation_state = fields.Selection(
        [("not_applicable", "N/A (Outbound from Odoo)"), ("matched", "Matched"), ("pending", "Pending Reconciliation")],
        default="not_applicable", readonly=True, copy=False,
        help="Only meaningful for payments created here from an incoming Tally receipt/payment event.",
    )

    def _tally_event_type(self):
        self.ensure_one()
        return "customer_payment" if self.partner_type == "customer" else "vendor_payment"

    def _tally_payment_data(self):
        self.ensure_one()
        invoice_ids = self._fields.get("reconciled_invoice_ids") and self.reconciled_invoice_ids or self.env["account.move"]
        return {
            "odoo_id": self.id,
            "partner": {
                "odoo_id": self.partner_id.id,
                "name": self.partner_id.name,
                "vat": self.partner_id.vat or None,
            },
            "amount": self.amount,
            "date": self.date and self.date.isoformat() or None,
            "journal": self.journal_id.name,
            "reference": self.memo or self.name,
            "invoice_references": invoice_ids.mapped("name"),
            "currency": self.currency_id.name,
            "company_id": self.company_id.id,
        }

    # account.payment's own `state` never reaches "posted" -- it is a
    # computed field over draft/in_process/paid/canceled/rejected, driven
    # by the underlying move + reconciliation status. The move itself is
    # what actually transitions to "posted", so that is what we key off.
    def action_post(self):
        res = super().action_post()
        self.filtered(lambda p: p.move_id.state == "posted")._tally_enqueue()
        return res

    def _tally_enqueue(self):
        Queue = self.env["tally.sync.queue"].sudo()
        for payment in self:
            Queue.enqueue(f"{payment._tally_event_type()}.created", _MODEL, payment.id, payment._tally_payment_data())
            payment.write({"tally_sync_state": "pending"})

    def button_tally_sync(self):
        self.filtered(lambda p: p.move_id.state == "posted")._tally_enqueue()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"title": _("Tally Sync"), "message": _("Queued for sync to Tally."), "type": "success"},
        }

    # -------------------------------------------------------------- inbound
    @api.model
    def _tally_find_invoice_for_reconciliation(self, partner, data):
        """Only reconciles when the invoice can be identified unambiguously
        by its own reference -- never guesses by amount/date alone. Ambiguous
        or unmatched cases are left 'pending' rather than silently
        reconciled against the wrong invoice."""
        refs = data.get("invoice_references") or []
        if len(refs) != 1:
            return self.env["account.move"].browse()
        move = self.env["account.move"].search([
            ("name", "=", refs[0]), ("partner_id", "=", partner.id), ("state", "=", "posted"),
            ("payment_state", "in", ("not_paid", "partial")),
        ], limit=1)
        return move

    @api.model
    def _tally_apply_incoming_payment(self, envelope, partner_type):
        data = envelope.get("data") or {}
        tally_id = data.get("tally_id")
        company_id = envelope.get("company_id") or self.env.company.id
        Mapping = self.env["tally.sync.mapping"].sudo()

        partner_data = data.get("partner") or {}
        partner = self.env["res.partner"].browse()
        if partner_data.get("vat"):
            partner = self.env["res.partner"].search([("vat", "=", partner_data["vat"])], limit=1)
        if not partner and partner_data.get("name"):
            partner = self.env["res.partner"].search([("name", "=", partner_data["name"])], limit=1)
        if not partner:
            _logger.warning("Cannot create payment from Tally event without a matching partner: %s", envelope)
            return True

        journal = self.env["account.journal"].search(
            [("type", "in", ("bank", "cash")), ("company_id", "=", company_id)], limit=1
        )
        if not journal:
            _logger.warning("No bank/cash journal available to record incoming Tally payment: %s", envelope)
            return True

        payment_type = "inbound" if partner_type == "customer" else "outbound"
        payment = self.sudo().with_context(tally_sync_no_publish=True).create({
            "payment_type": payment_type,
            "partner_type": partner_type,
            "partner_id": partner.id,
            "amount": abs(data.get("amount") or 0.0),
            "date": data.get("date") or fields.Date.context_today(self),
            "journal_id": journal.id,
            "memo": data.get("reference"),
        })
        payment = payment.with_env(self.env)
        payment.action_post()

        invoice = self._tally_find_invoice_for_reconciliation(partner, data)
        if invoice:
            try:
                (payment.move_id.line_ids | invoice.line_ids).filtered(
                    lambda l: not l.reconciled and l.account_id.account_type in ("asset_receivable", "liability_payable")
                ).reconcile()
                payment.tally_reconciliation_state = "matched"
            except Exception:
                _logger.exception("Reconciliation failed for incoming Tally payment %s", envelope)
                payment.tally_reconciliation_state = "pending"
        else:
            payment.tally_reconciliation_state = "pending"

        mapping = Mapping.get_or_create(_MODEL, payment.id, "receipt_voucher" if partner_type == "customer" else "payment_voucher", company_id, payment.name)
        mapping.mark_synced(tally_id)
        payment.write({"tally_sync_state": "synced"})
        return True

    @api.model
    def cron_consume_incoming_payments(self, max_messages=50):
        config = self.env["tally.sync.queue"]._get_config()
        if not config or not config.sync_enabled:
            return
        dispatcher = event_consumer.EventDispatcher()
        dispatcher.register("tally.receipt.created", lambda e: self._tally_apply_incoming_payment(e, "customer"))
        dispatcher.register("tally.payment.created", lambda e: self._tally_apply_incoming_payment(e, "supplier"))

        rmq_config = rabbitmq_client.RabbitMQConfig.from_record(config)
        try:
            with rabbitmq_client.RabbitMQClient(rmq_config) as client:
                client.declare_topology()
                dispatcher.consume_queue(client, "tally.incoming.payment", max_messages=max_messages)
                self.env.cr.commit()
        except Exception:
            _logger.exception("Could not reach RabbitMQ to consume incoming payment events")

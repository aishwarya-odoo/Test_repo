from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestPaymentSync(TransactionCase):
    def setUp(self):
        super().setUp()
        self.Queue = self.env["tally.sync.queue"]
        self.Payment = self.env["account.payment"]
        self.partner = self.env["res.partner"].create({"name": "Payment Test Customer", "vat": "PAYVAT123"})
        self.Queue.search([("odoo_model", "=", "res.partner"), ("odoo_record_id", "=", self.partner.id)]).unlink()
        self.product = self.env["product.product"].create({"name": "Payment Test Item", "list_price": 100})
        self.Queue.search([("odoo_model", "=", "product.product"), ("odoo_record_id", "=", self.product.id)]).unlink()
        self.journal = self.env["account.journal"].search(
            [("type", "in", ("bank", "cash")), ("company_id", "=", self.env.company.id)], limit=1
        )

    def _events_for(self, payment):
        return self.Queue.search([("odoo_model", "=", "account.payment"), ("odoo_record_id", "=", payment.id)])

    def _make_invoice(self, move_type, partner=None):
        move = self.env["account.move"].create({
            "move_type": move_type,
            "partner_id": (partner or self.partner).id,
            "invoice_date": "2026-09-17",
            "invoice_line_ids": [(0, 0, {"product_id": self.product.id, "quantity": 1, "price_unit": 100})],
        })
        move.action_post()
        self.Queue.search([("odoo_model", "=", "account.move"), ("odoo_record_id", "=", move.id)]).unlink()
        return move

    def _make_payment(self, payment_type, partner_type, amount=100):
        return self.Payment.create({
            "payment_type": payment_type,
            "partner_type": partner_type,
            "partner_id": self.partner.id,
            "amount": amount,
            "journal_id": self.journal.id,
        })

    def test_draft_payment_does_not_enqueue(self):
        payment = self._make_payment("inbound", "customer")
        self.assertFalse(self._events_for(payment))
        self.assertEqual(payment.tally_sync_state, "not_synced")

    def test_posting_customer_payment_enqueues_customer_payment_created(self):
        payment = self._make_payment("inbound", "customer")
        payment.action_post()
        events = self._events_for(payment)
        self.assertEqual(events.mapped("event_type"), ["customer_payment.created"])
        self.assertEqual(payment.tally_sync_state, "pending")
        self.assertEqual(events.payload["partner"]["name"], "Payment Test Customer")

    def test_posting_vendor_payment_enqueues_vendor_payment_created(self):
        payment = self._make_payment("outbound", "supplier")
        payment.action_post()
        events = self._events_for(payment)
        self.assertEqual(events.mapped("event_type"), ["vendor_payment.created"])

    def test_incoming_payment_reconciles_when_invoice_identified(self):
        invoice = self._make_invoice("out_invoice")
        envelope = {
            "event_type": "tally.receipt.created",
            "source": "tally",
            "company_id": self.env.company.id,
            "data": {
                "tally_id": "TALLY-RECEIPT-1",
                "partner": {"name": self.partner.name, "vat": self.partner.vat},
                "amount": 100,
                "reference": "R-0001",
                "invoice_references": [invoice.name],
            },
        }
        self.Payment._tally_apply_incoming_payment(envelope, "customer")
        payment = self.Payment.search([("memo", "=", "R-0001")], limit=1)
        self.assertTrue(payment)
        self.assertEqual(payment.move_id.state, "posted")
        self.assertEqual(payment.tally_reconciliation_state, "matched")
        self.assertEqual(payment.tally_sync_state, "synced")
        self.assertIn(invoice.payment_state, ("paid", "in_payment", "partial"))

    def test_incoming_payment_without_identifiable_invoice_is_pending(self):
        envelope = {
            "event_type": "tally.receipt.created",
            "source": "tally",
            "company_id": self.env.company.id,
            "data": {
                "tally_id": "TALLY-RECEIPT-2",
                "partner": {"name": self.partner.name, "vat": self.partner.vat},
                "amount": 50,
                "reference": "R-0002",
                "invoice_references": [],
            },
        }
        self.Payment._tally_apply_incoming_payment(envelope, "customer")
        payment = self.Payment.search([("memo", "=", "R-0002")], limit=1)
        self.assertTrue(payment)
        self.assertEqual(payment.tally_reconciliation_state, "pending")

    def test_incoming_payment_without_matching_partner_does_not_crash(self):
        envelope = {
            "event_type": "tally.receipt.created",
            "source": "tally",
            "company_id": self.env.company.id,
            "data": {
                "tally_id": "TALLY-RECEIPT-3",
                "partner": {"name": "No Such Partner", "vat": "NOPE"},
                "amount": 10,
                "reference": "R-0003",
            },
        }
        result = self.Payment._tally_apply_incoming_payment(envelope, "customer")
        self.assertTrue(result)
        self.assertFalse(self.Payment.search([("memo", "=", "R-0003")]))

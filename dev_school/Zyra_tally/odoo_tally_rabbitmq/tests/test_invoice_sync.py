from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestInvoiceSync(TransactionCase):
    def setUp(self):
        super().setUp()
        self.Queue = self.env["tally.sync.queue"]
        self.partner = self.env["res.partner"].create({"name": "Invoice Test Customer"})
        self.Queue.search([("odoo_model", "=", "res.partner"), ("odoo_record_id", "=", self.partner.id)]).unlink()
        self.product = self.env["product.product"].create({"name": "Test Item", "list_price": 100})
        self.Queue.search([("odoo_model", "=", "product.product"), ("odoo_record_id", "=", self.product.id)]).unlink()

    def _events_for(self, move):
        return self.Queue.search([("odoo_model", "=", "account.move"), ("odoo_record_id", "=", move.id)])

    def _make_invoice(self, move_type, partner=None):
        return self.env["account.move"].create({
            "move_type": move_type,
            "partner_id": (partner or self.partner).id,
            "invoice_date": "2026-09-17",
            "invoice_line_ids": [(0, 0, {"product_id": self.product.id, "quantity": 1, "price_unit": 100})],
        })

    def test_draft_invoice_does_not_enqueue(self):
        move = self._make_invoice("out_invoice")
        self.assertFalse(self._events_for(move))
        self.assertEqual(move.tally_sync_state, "not_synced")

    def test_posting_a_sales_invoice_enqueues_sales_invoice_created(self):
        move = self._make_invoice("out_invoice")
        move.action_post()
        events = self._events_for(move)
        self.assertEqual(events.mapped("event_type"), ["sales_invoice.created"])
        self.assertEqual(move.tally_sync_state, "pending")
        self.assertFalse(events.payload["is_credit_note"])
        self.assertEqual(events.payload["partner"]["name"], "Invoice Test Customer")
        self.assertEqual(len(events.payload["lines"]), 1)

    def test_posting_a_purchase_invoice_enqueues_purchase_invoice_created(self):
        move = self._make_invoice("in_invoice")
        move.action_post()
        events = self._events_for(move)
        self.assertEqual(events.mapped("event_type"), ["purchase_invoice.created"])

    def test_posting_a_credit_note_flags_is_credit_note(self):
        move = self._make_invoice("out_refund")
        move.action_post()
        events = self._events_for(move)
        self.assertEqual(events.mapped("event_type"), ["sales_invoice.created"])
        self.assertTrue(events.payload["is_credit_note"])

    def test_incoming_event_marks_existing_move_synced(self):
        move = self._make_invoice("out_invoice")
        move.action_post()
        envelope = {
            "event_type": "tally.sales_invoice.created",
            "source": "tally",
            "company_id": self.env.company.id,
            "data": {"tally_id": "TALLY-VOUCHER-1", "odoo_id": move.id, "voucher_number": "SV/0001"},
        }
        self.env["account.move"]._tally_apply_incoming_invoice(envelope, "out_invoice")
        move.invalidate_recordset()
        self.assertEqual(move.tally_sync_state, "synced")
        self.assertEqual(move.tally_voucher_number, "SV/0001")
        mapping = self.env["tally.sync.mapping"].search([
            ("odoo_model", "=", "account.move"), ("odoo_record_id", "=", move.id),
        ])
        self.assertEqual(mapping.tally_id, "TALLY-VOUCHER-1")

    def test_incoming_event_without_matching_move_does_not_crash(self):
        envelope = {
            "event_type": "tally.sales_invoice.created",
            "source": "tally",
            "company_id": self.env.company.id,
            "data": {"tally_id": "TALLY-VOUCHER-2", "odoo_id": 999999, "voucher_number": "SV/0002"},
        }
        result = self.env["account.move"]._tally_apply_incoming_invoice(envelope, "out_invoice")
        self.assertTrue(result)

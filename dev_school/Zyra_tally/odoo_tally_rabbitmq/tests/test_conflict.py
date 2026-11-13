from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestSyncConflict(TransactionCase):
    def setUp(self):
        super().setUp()
        self.Queue = self.env["tally.sync.queue"]
        self.Partner = self.env["res.partner"]
        self.Conflict = self.env["tally.sync.conflict"]

    def _events_for(self, partner):
        return self.Queue.search([("odoo_model", "=", "res.partner"), ("odoo_record_id", "=", partner.id)])

    def test_no_conflict_when_no_pending_outbound(self):
        partner = self.Partner.create({"name": "Conflict Free Co", "customer_rank": 1, "city": "Old City"})
        self._events_for(partner).unlink()
        envelope = {
            "event_type": "tally.customer.updated",
            "source": "tally",
            "company_id": self.env.company.id,
            "data": {"tally_id": "TALLY-CONF-1", "name": "Conflict Free Co", "city": "New City"},
        }
        self.Partner._tally_apply_incoming_customer(envelope)
        partner.invalidate_recordset()
        self.assertEqual(partner.city, "New City")
        self.assertFalse(self.Conflict.search([("odoo_record_id", "=", partner.id)]))

    def test_conflict_detected_when_local_change_still_pending(self):
        partner = self.Partner.create({"name": "Conflict Party", "customer_rank": 1, "city": "Odoo City"})
        # Leave the initial customer.created event pending -- it represents
        # a local change Tally has not acknowledged yet.
        self.assertTrue(self._events_for(partner))
        envelope = {
            "event_type": "tally.customer.updated",
            "source": "tally",
            "company_id": self.env.company.id,
            "data": {"tally_id": "TALLY-CONF-2", "name": "Conflict Party", "city": "Tally City"},
        }
        self.Partner._tally_apply_incoming_customer(envelope)
        partner.invalidate_recordset()
        self.assertEqual(partner.city, "Odoo City", "conflicting field must not be silently overwritten")
        conflict = self.Conflict.search([("odoo_record_id", "=", partner.id), ("field_name", "=", "city")])
        self.assertTrue(conflict)
        self.assertEqual(conflict.odoo_value, "Odoo City")
        self.assertEqual(conflict.tally_value, "Tally City")
        self.assertEqual(conflict.state, "pending")

    def test_use_tally_value_applies_and_resolves(self):
        partner = self.Partner.create({"name": "Resolve Tally Co", "customer_rank": 1, "city": "Odoo City"})
        envelope = {
            "event_type": "tally.customer.updated", "source": "tally", "company_id": self.env.company.id,
            "data": {"tally_id": "TALLY-CONF-3", "name": "Resolve Tally Co", "city": "Tally City"},
        }
        self.Partner._tally_apply_incoming_customer(envelope)
        conflict = self.Conflict.search([("odoo_record_id", "=", partner.id), ("field_name", "=", "city")])
        self.assertTrue(conflict)
        conflict.action_use_tally_value()
        partner.invalidate_recordset()
        self.assertEqual(partner.city, "Tally City")
        self.assertEqual(conflict.state, "resolved_use_tally")

    def test_use_odoo_value_reenqueues_and_resolves(self):
        partner = self.Partner.create({"name": "Resolve Odoo Co", "customer_rank": 1, "city": "Odoo City"})
        envelope = {
            "event_type": "tally.customer.updated", "source": "tally", "company_id": self.env.company.id,
            "data": {"tally_id": "TALLY-CONF-4", "name": "Resolve Odoo Co", "city": "Tally City"},
        }
        self.Partner._tally_apply_incoming_customer(envelope)
        conflict = self.Conflict.search([("odoo_record_id", "=", partner.id), ("field_name", "=", "city")])
        self.assertTrue(conflict)
        self._events_for(partner).unlink()
        conflict.action_use_odoo_value()
        partner.invalidate_recordset()
        self.assertEqual(partner.city, "Odoo City", "Odoo's value should win and not be overwritten")
        self.assertEqual(conflict.state, "resolved_use_odoo")
        self.assertEqual(self._events_for(partner).mapped("event_type"), ["customer.updated"])

    def test_ignore_marks_ignored(self):
        partner = self.Partner.create({"name": "Ignore Co", "customer_rank": 1, "city": "Odoo City"})
        envelope = {
            "event_type": "tally.customer.updated", "source": "tally", "company_id": self.env.company.id,
            "data": {"tally_id": "TALLY-CONF-5", "name": "Ignore Co", "city": "Tally City"},
        }
        self.Partner._tally_apply_incoming_customer(envelope)
        conflict = self.Conflict.search([("odoo_record_id", "=", partner.id), ("field_name", "=", "city")])
        self.assertTrue(conflict)
        conflict.action_ignore()
        self.assertEqual(conflict.state, "ignored")

from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestPartnerSync(TransactionCase):
    def setUp(self):
        super().setUp()
        self.Queue = self.env["tally.sync.queue"]
        self.Partner = self.env["res.partner"]

    def _pending_events(self, partner):
        return self.Queue.search([("odoo_model", "=", "res.partner"), ("odoo_record_id", "=", partner.id)])

    def test_creating_a_customer_enqueues_customer_created_only(self):
        partner = self.Partner.create({"name": "ABC Medicals", "customer_rank": 1})
        events = self._pending_events(partner)
        self.assertEqual(events.mapped("event_type"), ["customer.created"])
        self.assertEqual(partner.tally_sync_state, "pending")

    def test_creating_a_vendor_enqueues_vendor_created_only(self):
        partner = self.Partner.create({"name": "Steel Supplier Co", "supplier_rank": 1})
        events = self._pending_events(partner)
        self.assertEqual(events.mapped("event_type"), ["vendor.created"])

    def test_creating_a_customer_and_vendor_enqueues_both(self):
        partner = self.Partner.create({"name": "Dual Party", "customer_rank": 1, "supplier_rank": 1})
        events = self._pending_events(partner)
        self.assertEqual(set(events.mapped("event_type")), {"customer.created", "vendor.created"})

    def test_creating_a_plain_contact_enqueues_nothing(self):
        partner = self.Partner.create({"name": "Just a contact"})
        self.assertFalse(self._pending_events(partner))

    def test_child_contact_of_a_customer_is_not_syncable(self):
        company = self.Partner.create({"name": "Parent Co", "customer_rank": 1})
        self._pending_events(company).unlink()
        child = self.Partner.create({"name": "John Doe", "parent_id": company.id, "customer_rank": 1})
        self.assertFalse(self._pending_events(child), "child contacts share the parent's ledger")

    def test_updating_a_tracked_field_enqueues_customer_updated(self):
        partner = self.Partner.create({"name": "ABC Medicals", "customer_rank": 1})
        self._pending_events(partner).unlink()
        partner.write({"street": "123 Main St"})
        events = self._pending_events(partner)
        self.assertEqual(events.mapped("event_type"), ["customer.updated"])

    def test_updating_an_untracked_field_enqueues_nothing(self):
        partner = self.Partner.create({"name": "ABC Medicals", "customer_rank": 1})
        self._pending_events(partner).unlink()
        partner.write({"comment": "just a note"})
        self.assertFalse(self._pending_events(partner))

    def test_incoming_customer_event_creates_a_new_partner_with_mapping(self):
        envelope = {
            "event_type": "tally.customer.created",
            "source": "tally",
            "company_id": self.env.company.id,
            "data": {"tally_id": "TALLY-CUST-1", "name": "New From Tally", "vat": "32AAAAA0000A1Z5"},
        }
        self.Partner._tally_apply_incoming_customer(envelope)
        partner = self.Partner.search([("name", "=", "New From Tally")], limit=1)
        self.assertTrue(partner)
        self.assertEqual(partner.tally_sync_state, "synced")
        mapping = self.env["tally.sync.mapping"].search([
            ("odoo_model", "=", "res.partner"), ("odoo_record_id", "=", partner.id),
            ("tally_object_type", "=", "customer_ledger"),
        ])
        self.assertEqual(mapping.tally_id, "TALLY-CUST-1")
        self.assertEqual(mapping.sync_status, "synced")

    def test_incoming_customer_event_does_not_create_a_loop(self):
        """Applying an incoming event must not itself enqueue an outbound
        customer.created for the same record -- that's the update loop the
        spec explicitly warns about."""
        envelope = {
            "event_type": "tally.customer.created",
            "source": "tally",
            "company_id": self.env.company.id,
            "data": {"tally_id": "TALLY-CUST-2", "name": "No Loop Party"},
        }
        self.Partner._tally_apply_incoming_customer(envelope)
        partner = self.Partner.search([("name", "=", "No Loop Party")], limit=1)
        self.assertFalse(self._pending_events(partner), "inbound apply must not enqueue an outbound event")

    def test_incoming_customer_event_matches_existing_partner_by_vat(self):
        existing = self.Partner.create({"name": "Existing Co", "customer_rank": 1, "vat": "32BBBBB0000B1Z5"})
        self._pending_events(existing).unlink()
        envelope = {
            "event_type": "tally.customer.updated",
            "source": "tally",
            "company_id": self.env.company.id,
            "data": {"tally_id": "TALLY-CUST-3", "name": "Existing Co", "vat": "32BBBBB0000B1Z5", "city": "Chennai"},
        }
        self.Partner._tally_apply_incoming_customer(envelope)
        existing.invalidate_recordset()
        self.assertEqual(existing.city, "Chennai")
        count = self.Partner.search_count([("vat", "=", "32BBBBB0000B1Z5")])
        self.assertEqual(count, 1, "should update the existing partner, not create a duplicate")

from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestProductSync(TransactionCase):
    def setUp(self):
        super().setUp()
        self.Queue = self.env["tally.sync.queue"]
        self.Template = self.env["product.template"]

    def _events_for_variant(self, variant):
        return self.Queue.search([("odoo_model", "=", "product.product"), ("odoo_record_id", "=", variant.id)])

    def test_creating_a_product_enqueues_product_created_for_its_variant(self):
        template = self.Template.create({"name": "Ophthalmic Crescent Blade", "default_code": "OCB-001"})
        variant = template.product_variant_ids[:1]
        events = self._events_for_variant(variant)
        self.assertEqual(events.mapped("event_type"), ["product.created"])
        self.assertEqual(events.payload["sku"], "OCB-001")
        self.assertEqual(template.tally_sync_state, "pending")

    def test_updating_a_tracked_field_enqueues_product_updated(self):
        template = self.Template.create({"name": "Ophthalmic Crescent Blade", "default_code": "OCB-001"})
        variant = template.product_variant_ids[:1]
        self._events_for_variant(variant).unlink()
        template.write({"list_price": 500})
        events = self._events_for_variant(variant)
        self.assertEqual(events.mapped("event_type"), ["product.updated"])

    def test_updating_an_untracked_field_enqueues_nothing(self):
        template = self.Template.create({"name": "Ophthalmic Crescent Blade", "default_code": "OCB-001"})
        variant = template.product_variant_ids[:1]
        self._events_for_variant(variant).unlink()
        template.write({"description": "just a note"})
        self.assertFalse(self._events_for_variant(variant))

    def test_incoming_product_event_creates_a_new_product_with_mapping(self):
        envelope = {
            "event_type": "tally.product.created",
            "source": "tally",
            "company_id": self.env.company.id,
            "data": {"tally_id": "TALLY-ITEM-1", "name": "New Item From Tally", "sku": "TI-001", "sales_price": 250},
        }
        self.Template._tally_apply_incoming_product(envelope)
        variant = self.env["product.product"].search([("default_code", "=", "TI-001")], limit=1)
        self.assertTrue(variant)
        self.assertEqual(variant.list_price, 250)
        mapping = self.env["tally.sync.mapping"].search([
            ("odoo_model", "=", "product.product"), ("odoo_record_id", "=", variant.id),
            ("tally_object_type", "=", "stock_item"),
        ])
        self.assertEqual(mapping.tally_id, "TALLY-ITEM-1")

    def test_incoming_product_event_does_not_create_a_loop(self):
        envelope = {
            "event_type": "tally.product.created",
            "source": "tally",
            "company_id": self.env.company.id,
            "data": {"tally_id": "TALLY-ITEM-2", "name": "No Loop Item", "sku": "TI-002"},
        }
        self.Template._tally_apply_incoming_product(envelope)
        variant = self.env["product.product"].search([("default_code", "=", "TI-002")], limit=1)
        self.assertFalse(self._events_for_variant(variant), "inbound apply must not enqueue an outbound event")

    def test_incoming_product_event_matches_existing_by_sku(self):
        template = self.Template.create({"name": "Existing Item", "default_code": "TI-003", "list_price": 100})
        variant = template.product_variant_ids[:1]
        self._events_for_variant(variant).unlink()

        envelope = {
            "event_type": "tally.product.updated",
            "source": "tally",
            "company_id": self.env.company.id,
            "data": {"tally_id": "TALLY-ITEM-3", "name": "Existing Item", "sku": "TI-003", "sales_price": 175},
        }
        self.Template._tally_apply_incoming_product(envelope)
        template.invalidate_recordset()
        self.assertEqual(template.list_price, 175)
        count = self.env["product.product"].search_count([("default_code", "=", "TI-003")])
        self.assertEqual(count, 1, "should update the existing variant, not create a duplicate")

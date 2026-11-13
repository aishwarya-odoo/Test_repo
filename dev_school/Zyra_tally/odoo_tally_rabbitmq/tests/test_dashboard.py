from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestSyncDashboard(TransactionCase):
    def setUp(self):
        super().setUp()
        self.Queue = self.env["tally.sync.queue"]
        self.Dashboard = self.env["tally.sync.dashboard"]
        self.Conflict = self.env["tally.sync.conflict"]
        # The module ships one default config per company (data/tally_config_data.xml);
        # drop it here so each test starts from a clean, known config.
        self.env["tally.integration.config"].search([("company_id", "=", self.env.company.id)]).unlink()
        self.config = self.env["tally.integration.config"].create({
            "name": "Dashboard Test Config",
            "company_id": self.env.company.id,
            "rabbitmq_host": "localhost",
            "sync_enabled": True,
            "connection_status": "connected",
        })

    def test_default_get_reflects_queue_and_config_state(self):
        self.Queue.enqueue("customer.created", "res.partner", 1, {"x": 1})
        self.Queue.enqueue("vendor.created", "res.partner", 2, {"x": 1})
        retry_entry = self.Queue.enqueue("product.created", "product.product", 3, {"x": 1})
        retry_entry.state = "retry"
        dl_entry = self.Queue.enqueue("product.updated", "product.product", 4, {"x": 1})
        dl_entry.state = "dead_letter"
        self.Conflict.create({
            "odoo_model": "res.partner", "odoo_record_id": 1, "field_name": "city",
            "odoo_value": "A", "tally_value": "B", "company_id": self.env.company.id,
        })

        dashboard = self.Dashboard.create({})
        self.assertEqual(dashboard.connection_status, "connected")
        self.assertTrue(dashboard.sync_enabled)
        self.assertEqual(dashboard.pending_count, 2)
        self.assertEqual(dashboard.retry_count, 1)
        self.assertEqual(dashboard.dead_letter_count, 1)
        self.assertEqual(dashboard.conflict_count, 1)

    def test_default_get_without_config_reports_not_configured(self):
        self.config.unlink()
        dashboard = self.Dashboard.create({})
        self.assertEqual(dashboard.connection_status, "not_configured")
        self.assertFalse(dashboard.sync_enabled)

    def test_action_retry_failed_resets_retry_entries_to_pending(self):
        entry = self.Queue.enqueue("customer.created", "res.partner", 5, {"x": 1})
        entry.write({"state": "retry", "error_message": "boom"})
        dashboard = self.Dashboard.create({})
        dashboard.action_retry_failed()
        entry.invalidate_recordset()
        self.assertEqual(entry.state, "pending")

    def test_action_retry_dead_letter_resets_dead_letter_entries_to_pending(self):
        entry = self.Queue.enqueue("customer.created", "res.partner", 6, {"x": 1})
        entry.write({"state": "dead_letter", "error_message": "boom"})
        dashboard = self.Dashboard.create({})
        dashboard.action_retry_dead_letter()
        entry.invalidate_recordset()
        self.assertEqual(entry.state, "pending")

    def test_action_test_connection_without_config_shows_warning(self):
        self.config.unlink()
        dashboard = self.Dashboard.create({})
        result = dashboard.action_test_connection()
        self.assertEqual(result["params"]["type"], "warning")

    def test_action_view_conflicts_scopes_domain_to_company_and_pending(self):
        dashboard = self.Dashboard.create({})
        action = dashboard.action_view_conflicts()
        self.assertEqual(action["res_model"], "tally.sync.conflict")
        self.assertIn(("state", "=", "pending"), action["domain"])

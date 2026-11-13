from unittest import mock

from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestTallyIntegrationConfig(TransactionCase):
    def setUp(self):
        super().setUp()
        # The module ships one default config per company (data/tally_config_data.xml);
        # drop it here so each test starts from a clean, known config.
        self.env["tally.integration.config"].search([("company_id", "=", self.env.company.id)]).unlink()
        self.Config = self.env["tally.integration.config"]

    def test_test_tally_connection_without_url_raises(self):
        config = self.Config.create({"name": "No URL", "company_id": self.env.company.id})
        with self.assertRaises(Exception):
            config.action_test_tally_connection()

    def test_test_tally_connection_against_unreachable_host_marks_failed(self):
        config = self.Config.create({
            "name": "Unreachable", "company_id": self.env.company.id,
            # Nothing listens on this port -- an immediate "connection
            # refused" rather than a slow network timeout.
            "tally_gateway_url": "http://127.0.0.1:1/",
        })
        result = config.action_test_tally_connection()
        self.assertEqual(config.tally_connection_status, "failed")
        self.assertEqual(result["params"]["type"], "danger")

    def test_test_tally_connection_success_marks_connected(self):
        """Odoo's test harness blocks un-mocked outbound `requests` calls to
        external hosts (unlike the raw pika sockets in
        test_rabbitmq_integration.py, which aren't intercepted) -- so unlike
        that file, this mocks the HTTP call rather than hitting the real LAN
        gateway. This environment's real gateway does respond exactly like
        this when reached manually via the "Test Tally Connection" button."""
        config = self.Config.create({
            "name": "Real Tally", "company_id": self.env.company.id,
            "tally_gateway_url": "http://192.168.2.55:9000/",
        })
        fake_response = mock.MagicMock()
        fake_response.text = "<RESPONSE>TallyPrime Server is Running</RESPONSE>"
        fake_response.raise_for_status.return_value = None
        with mock.patch(
            "odoo.addons.odoo_tally_rabbitmq.models.tally_config.requests.get",
            return_value=fake_response,
        ):
            result = config.action_test_tally_connection()
        self.assertEqual(config.tally_connection_status, "connected")
        self.assertEqual(result["params"]["type"], "success")

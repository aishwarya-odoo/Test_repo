from unittest import mock

from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestTallySyncQueue(TransactionCase):
    def setUp(self):
        super().setUp()
        self.Queue = self.env["tally.sync.queue"]
        # The module ships one default config per company (data/tally_config_data.xml);
        # drop it here so each test starts from a clean, known config.
        self.env["tally.integration.config"].search([("company_id", "=", self.env.company.id)]).unlink()
        self.config = self.env["tally.integration.config"].create({
            "name": "Test Config",
            "company_id": self.env.company.id,
            "rabbitmq_host": "localhost",
            "sync_enabled": True,
        })

    def test_enqueue_creates_a_pending_entry_with_derived_idempotency_key(self):
        entry = self.Queue.enqueue("customer.created", "res.partner", 245, {"name": "ABC"})
        self.assertEqual(entry.state, "pending")
        self.assertEqual(entry.idempotency_key, "odoo:res.partner:245:customer.created")
        self.assertEqual(entry.payload, {"name": "ABC"})

    def test_enqueue_is_idempotent_for_repeated_calls_before_processing(self):
        first = self.Queue.enqueue("customer.created", "res.partner", 245, {"name": "ABC"})
        second = self.Queue.enqueue("customer.created", "res.partner", 245, {"name": "ABC v2"})
        self.assertEqual(first.id, second.id, "repeated enqueue before processing should collapse into one entry")
        count = self.Queue.search_count([("idempotency_key", "=", first.idempotency_key)])
        self.assertEqual(count, 1)

    def test_enqueue_creates_a_new_entry_once_previous_one_is_resolved(self):
        first = self.Queue.enqueue("customer.created", "res.partner", 245, {"name": "ABC"})
        first.state = "success"
        second = self.Queue.enqueue("customer.created", "res.partner", 245, {"name": "ABC v2"})
        self.assertNotEqual(first.id, second.id, "a new sync after a resolved one should be a new outbox entry")

    def test_cron_process_outbox_does_nothing_when_sync_disabled(self):
        self.config.sync_enabled = False
        entry = self.Queue.enqueue("customer.created", "res.partner", 245, {"name": "ABC"})
        self.Queue.cron_process_outbox()
        entry.invalidate_recordset()
        self.assertEqual(entry.state, "pending")

    def test_cron_process_outbox_publishes_and_marks_queued(self):
        entry = self.Queue.enqueue("customer.created", "res.partner", 245, {"name": "ABC"})
        fake_client = mock.MagicMock()
        fake_client.__enter__.return_value = fake_client
        fake_client.__exit__.return_value = False

        with mock.patch(
            "odoo.addons.odoo_tally_rabbitmq.models.tally_sync_queue.rabbitmq_client.RabbitMQClient",
            return_value=fake_client,
        ), mock.patch.object(self.env.cr, "commit"):
            self.Queue.cron_process_outbox()

        fake_client.declare_topology.assert_called_once()
        self.assertEqual(entry.state, "queued")
        self.assertFalse(entry.error_message)

    def test_cron_process_outbox_marks_retry_on_publish_failure(self):
        entry = self.Queue.enqueue("customer.created", "res.partner", 245, {"name": "ABC"})
        fake_client = mock.MagicMock()
        fake_client.__enter__.return_value = fake_client
        fake_client.__exit__.return_value = False

        with mock.patch(
            "odoo.addons.odoo_tally_rabbitmq.models.tally_sync_queue.event_publisher.publish_event",
            side_effect=RuntimeError("unroutable"),
        ), mock.patch(
            "odoo.addons.odoo_tally_rabbitmq.models.tally_sync_queue.rabbitmq_client.RabbitMQClient",
            return_value=fake_client,
        ), mock.patch.object(self.env.cr, "commit"):
            self.Queue.cron_process_outbox()

        self.assertEqual(entry.state, "retry")
        self.assertEqual(entry.retry_count, 1)
        self.assertTrue(entry.error_message)

    def test_mark_failed_dead_letters_after_max_retries(self):
        entry = self.Queue.enqueue("customer.created", "res.partner", 245, {"name": "ABC"})
        entry.max_retries = 2
        entry.retry_count = 1
        entry._mark_failed("boom")
        self.assertEqual(entry.state, "dead_letter")
        self.assertEqual(entry.retry_count, 2)

    def test_action_retry_resets_dead_letter_to_pending(self):
        entry = self.Queue.enqueue("customer.created", "res.partner", 245, {"name": "ABC"})
        entry.write({"state": "dead_letter", "error_message": "boom"})
        entry.action_retry()
        self.assertEqual(entry.state, "pending")
        self.assertFalse(entry.error_message)

    def test_action_cancel_does_not_touch_successful_entries(self):
        entry = self.Queue.enqueue("customer.created", "res.partner", 245, {"name": "ABC"})
        entry.state = "success"
        entry.action_cancel()
        self.assertEqual(entry.state, "success")

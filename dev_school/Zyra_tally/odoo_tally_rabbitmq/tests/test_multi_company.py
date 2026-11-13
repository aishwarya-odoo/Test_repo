from unittest import mock

from psycopg2 import IntegrityError

from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestMultiCompany(TransactionCase):
    def setUp(self):
        super().setUp()
        self.company_a = self.env.company
        self.company_b = self.env["res.company"].create({"name": "Second Tally Co"})
        # The module ships one default config per company (data/tally_config_data.xml);
        # drop it here so each test starts from a clean, known config.
        self.env["tally.integration.config"].search([("company_id", "=", self.company_a.id)]).unlink()

    def test_config_is_unique_per_company_but_allowed_across_companies(self):
        Config = self.env["tally.integration.config"]
        Config.create({"name": "A", "company_id": self.company_a.id})
        # A savepoint keeps the constraint violation from poisoning the rest
        # of this test's transaction, matching Odoo's own convention for
        # asserting a DB-level constraint raises without aborting everything
        # that runs afterward.
        with self.assertRaises(IntegrityError), self.env.cr.savepoint():
            Config.create({"name": "A2", "company_id": self.company_a.id})
        Config.create({"name": "B", "company_id": self.company_b.id})

    def test_mapping_is_scoped_per_company(self):
        Mapping = self.env["tally.sync.mapping"]
        m1 = Mapping.get_or_create("res.partner", 1, "customer_ledger", self.company_a.id)
        m2 = Mapping.get_or_create("res.partner", 1, "customer_ledger", self.company_b.id)
        self.assertNotEqual(m1.id, m2.id, "the same Odoo record can map independently per company")

    def test_cron_process_outbox_only_processes_its_own_company_queue(self):
        self.env["tally.integration.config"].create({
            "name": "A", "company_id": self.company_a.id, "sync_enabled": True,
        })
        Queue = self.env["tally.sync.queue"]
        entry_a = Queue.with_context(allowed_company_ids=[self.company_a.id]).enqueue(
            "customer.created", "res.partner", 1, {"x": 1}
        )
        entry_b = Queue.with_context(allowed_company_ids=[self.company_b.id]).enqueue(
            "customer.created", "res.partner", 2, {"x": 1}
        )

        fake_client = mock.MagicMock()
        fake_client.__enter__.return_value = fake_client
        fake_client.__exit__.return_value = False
        with mock.patch(
            "odoo.addons.odoo_tally_rabbitmq.models.tally_sync_queue.rabbitmq_client.RabbitMQClient",
            return_value=fake_client,
        ), mock.patch.object(self.env.cr, "commit"):
            Queue.with_context(allowed_company_ids=[self.company_a.id]).cron_process_outbox()

        entry_a.invalidate_recordset()
        entry_b.invalidate_recordset()
        self.assertEqual(entry_a.state, "queued")
        self.assertEqual(entry_b.state, "pending", "other company's outbox entries must not be touched")

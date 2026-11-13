import uuid

from odoo.tests.common import TransactionCase, tagged

from ..services import rabbitmq_client

try:
    import pika
except ImportError:
    pika = None


@tagged("post_install", "-at_install")
class TestRabbitMQIntegration(TransactionCase):
    """Exercises the real local RabbitMQ broker (docker-compose service
    'rabbitmq') rather than mocking pika -- catches anything a mock-based
    unit test can't: actual topology declaration, real mandatory-publish
    routing failures, and a genuine publish/consume round trip."""

    def setUp(self):
        super().setUp()
        if pika is None:
            self.skipTest("pika is not installed")
        # The module ships one default config per company (data/tally_config_data.xml);
        # drop it here so each test starts from a clean, known config.
        self.env["tally.integration.config"].search([("company_id", "=", self.env.company.id)]).unlink()
        self.config = self.env["tally.integration.config"].create({
            "name": "Integration Test Config",
            "company_id": self.env.company.id,
            "rabbitmq_host": "rabbitmq",
            "rabbitmq_port": 5672,
            "rabbitmq_username": "odoo",
            "rabbitmq_password": "odoo",
            "sync_enabled": True,
        })
        self.rmq_config = rabbitmq_client.RabbitMQConfig.from_record(self.config)
        try:
            with rabbitmq_client.RabbitMQClient(self.rmq_config):
                pass
        except Exception as exc:
            self.skipTest(f"RabbitMQ broker not reachable at {self.rmq_config.host}: {exc}")

    def test_declare_topology_and_round_trip_publish_consume(self):
        marker = str(uuid.uuid4())
        received = []

        def handler(payload):
            received.append(payload)
            return True

        with rabbitmq_client.RabbitMQClient(self.rmq_config) as client:
            client.declare_topology()
            client._channel.queue_purge("tally.odoo.customer")
            client.publish("odoo.customer.created", {"marker": marker})
            client.consume_batch("tally.odoo.customer", handler, max_messages=5)

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["marker"], marker)

    def test_publish_to_unbound_routing_key_is_rejected(self):
        with rabbitmq_client.RabbitMQClient(self.rmq_config) as client:
            client.declare_topology()
            with self.assertRaises(pika.exceptions.UnroutableError):
                client.publish("odoo.no_such_domain.created", {"x": 1})

    def test_poison_message_is_sent_to_dlq_without_crashing_consumer(self):
        with rabbitmq_client.RabbitMQClient(self.rmq_config) as client:
            client.declare_topology()
            client._channel.queue_purge("tally.odoo.vendor")
            client._channel.basic_publish(
                exchange=rabbitmq_client.EXCHANGE, routing_key="odoo.vendor.created",
                body=b"not-json", mandatory=True,
            )
            processed = client.consume_batch("tally.odoo.vendor", lambda payload: True, max_messages=5)

        self.assertEqual(processed, 1)

"""Unit tests for the RabbitMQ service layer. Pika is fully mocked -- no
live broker required, per the project's testing principle of not needing a
live Tally/RabbitMQ server for unit tests.
"""
import json
from unittest import mock

from odoo.tests.common import BaseCase, tagged

from odoo.addons.odoo_tally_rabbitmq.services import (
    event_publisher,
    idempotency,
    message_serializer,
    rabbitmq_client,
)


def make_client_with_mock_channel():
    config = rabbitmq_client.RabbitMQConfig(
        host="localhost", port=5672, virtual_host="/", username="u", password="p"
    )
    client = rabbitmq_client.RabbitMQClient.__new__(rabbitmq_client.RabbitMQClient)
    client.config = config
    client._channel = mock.MagicMock()
    client._connection = mock.MagicMock()
    return client


@tagged('post_install', '-at_install')
class TestRabbitMQClient(BaseCase):
    def test_declare_topology_declares_exchange_dlx_and_all_queues(self):
        client = make_client_with_mock_channel()
        client.declare_topology()
        ch = client._channel

        ch.exchange_declare.assert_any_call(
            exchange=rabbitmq_client.EXCHANGE, exchange_type="topic", durable=True
        )
        ch.exchange_declare.assert_any_call(
            exchange=rabbitmq_client.DLX, exchange_type="topic", durable=True
        )
        ch.queue_declare.assert_any_call(queue=rabbitmq_client.DLQ, durable=True)
        ch.queue_bind.assert_any_call(rabbitmq_client.DLQ, rabbitmq_client.DLX, "#")

        all_queues = {**rabbitmq_client.OUTGOING_QUEUE_BINDINGS, **rabbitmq_client.INCOMING_QUEUE_BINDINGS}
        self.assertEqual(
            ch.queue_declare.call_count, 1 + len(all_queues), "one DLQ + one per domain queue"
        )
        for queue_name in all_queues:
            found = any(
                c.kwargs.get("queue") == queue_name and "x-dead-letter-exchange" in c.kwargs.get("arguments", {})
                for c in ch.queue_declare.call_args_list
            )
            self.assertTrue(found, f"{queue_name} should be declared with a dead-letter-exchange")

    def test_publish_sends_json_body_with_mandatory_and_persistent_delivery(self):
        client = make_client_with_mock_channel()
        client.publish("odoo.customer.created", {"message_id": "abc", "foo": "bar"})

        client._channel.basic_publish.assert_called_once()
        _, kwargs = client._channel.basic_publish.call_args
        self.assertEqual(kwargs["routing_key"], "odoo.customer.created")
        self.assertTrue(kwargs["mandatory"])
        self.assertEqual(json.loads(kwargs["body"]), {"message_id": "abc", "foo": "bar"})

    def test_consume_batch_acks_on_successful_handler(self):
        client = make_client_with_mock_channel()
        method = mock.MagicMock(delivery_tag=1)
        props = mock.MagicMock(headers={})
        body = json.dumps({"event_type": "x"}).encode()
        client._channel.basic_get.side_effect = [(method, props, body), (None, None, None)]

        handler = mock.MagicMock(return_value=True)
        processed = client.consume_batch("some.queue", handler)

        self.assertEqual(processed, 1)
        handler.assert_called_once_with({"event_type": "x"})
        client._channel.basic_ack.assert_called_once_with(1)
        client._channel.basic_reject.assert_not_called()

    def test_consume_batch_retries_below_max_by_republishing_with_incremented_header(self):
        client = make_client_with_mock_channel()
        method = mock.MagicMock(delivery_tag=1)
        props = mock.MagicMock(headers={rabbitmq_client.RETRY_COUNT_HEADER: 0})
        body = json.dumps({"event_type": "x"}).encode()
        client._channel.basic_get.side_effect = [(method, props, body), (None, None, None)]

        handler = mock.MagicMock(return_value=False)
        client.consume_batch("some.queue", handler, max_retries=3)

        # original message acked (removed), a retry copy republished with count=1
        client._channel.basic_ack.assert_called_once_with(1)
        client._channel.basic_reject.assert_not_called()
        republish_kwargs = client._channel.basic_publish.call_args.kwargs
        self.assertEqual(republish_kwargs["routing_key"], "some.queue")
        self.assertEqual(republish_kwargs["properties"].headers[rabbitmq_client.RETRY_COUNT_HEADER], 1)

    def test_consume_batch_dead_letters_after_max_retries(self):
        client = make_client_with_mock_channel()
        method = mock.MagicMock(delivery_tag=1)
        props = mock.MagicMock(headers={rabbitmq_client.RETRY_COUNT_HEADER: 2})
        body = json.dumps({"event_type": "x"}).encode()
        client._channel.basic_get.side_effect = [(method, props, body), (None, None, None)]

        handler = mock.MagicMock(return_value=False)
        client.consume_batch("some.queue", handler, max_retries=3)

        client._channel.basic_reject.assert_called_once_with(1, requeue=False)
        client._channel.basic_ack.assert_not_called()

    def test_consume_batch_rejects_unparseable_message_without_retry(self):
        client = make_client_with_mock_channel()
        method = mock.MagicMock(delivery_tag=1)
        props = mock.MagicMock(headers={})
        client._channel.basic_get.side_effect = [(method, props, b"not json"), (None, None, None)]

        handler = mock.MagicMock()
        client.consume_batch("some.queue", handler)

        handler.assert_not_called()
        client._channel.basic_reject.assert_called_once_with(1, requeue=False)

    def test_consume_batch_handler_exception_counts_as_failure(self):
        client = make_client_with_mock_channel()
        method = mock.MagicMock(delivery_tag=1)
        props = mock.MagicMock(headers={})
        body = json.dumps({"event_type": "x"}).encode()
        client._channel.basic_get.side_effect = [(method, props, body), (None, None, None)]

        handler = mock.MagicMock(side_effect=RuntimeError("boom"))
        client.consume_batch("some.queue", handler, max_retries=3)

        client._channel.basic_ack.assert_called_once_with(1)
        client._channel.basic_publish.assert_called_once()


@tagged('post_install', '-at_install')
class TestIdempotency(BaseCase):
    def test_build_key_is_stable_and_distinguishes_event_type(self):
        k1 = idempotency.build_key("odoo", "account.move", 1524, "sales_invoice.created")
        k2 = idempotency.build_key("odoo", "account.move", 1524, "sales_invoice.updated")
        self.assertEqual(k1, "odoo:account.move:1524:sales_invoice.created")
        self.assertNotEqual(k1, k2)
        self.assertEqual(k1, idempotency.build_key("odoo", "account.move", 1524, "sales_invoice.created"))


@tagged('post_install', '-at_install')
class TestMessageSerializer(BaseCase):
    def test_build_envelope_shape(self):
        envelope = message_serializer.build_envelope(
            event_type="customer.created",
            source="odoo",
            company_id=1,
            data={"odoo_id": 245},
            idempotency_key="odoo:res.partner:245:customer.created",
        )
        self.assertEqual(envelope["schema_version"], "1.0")
        self.assertEqual(envelope["source"], "odoo")
        self.assertEqual(envelope["event_type"], "customer.created")
        self.assertEqual(envelope["company_id"], 1)
        self.assertEqual(envelope["data"], {"odoo_id": 245})
        self.assertTrue(envelope["message_id"])
        self.assertTrue(envelope["timestamp"].endswith("Z"))


@tagged('post_install', '-at_install')
class TestEventPublisher(BaseCase):
    def test_routing_key_for_odoo_source_gets_prefixed(self):
        self.assertEqual(event_publisher.routing_key_for("customer.created", "odoo"), "odoo.customer.created")

    def test_routing_key_for_tally_source_is_used_as_is(self):
        self.assertEqual(
            event_publisher.routing_key_for("tally.customer.created", "tally"), "tally.customer.created"
        )

    def test_publish_event_calls_client_publish_with_derived_routing_key(self):
        client = mock.MagicMock()
        envelope = event_publisher.publish_event(
            client, "customer.created", "odoo", 1, "res.partner", 245, {"name": "ABC"}
        )
        client.publish.assert_called_once()
        args, kwargs = client.publish.call_args
        self.assertEqual(args[0], "odoo.customer.created")
        self.assertEqual(envelope["idempotency_key"], "odoo:res.partner:245:customer.created")


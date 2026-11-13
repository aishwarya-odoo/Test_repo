"""RabbitMQ transport layer, mirroring
odoo_tally_rabbitmq/services/rabbitmq_client.py exactly (topology, retry
header, dead-lettering) -- both sides must agree on this, but this project
has no dependency on Odoo, so it's a standalone twin rather than a shared
import. See odoo_tally_rabbitmq/RABBITMQ.md for the full topology reference.
"""
import json
import logging
import uuid

import pika

_logger = logging.getLogger(__name__)

EXCHANGE = "tally.sync"
DLX = "tally.sync.dlx"
DLQ = "tally.sync.dlq"

# Odoo -> Tally (Odoo publishes here; this worker consumes).
OUTGOING_QUEUE_BINDINGS = {
    "tally.odoo.customer": ["odoo.customer.*"],
    "tally.odoo.vendor": ["odoo.vendor.*"],
    "tally.odoo.product": ["odoo.product.*"],
    "tally.odoo.invoice": ["odoo.sales_invoice.*", "odoo.purchase_invoice.*"],
    "tally.odoo.payment": ["odoo.customer_payment.*", "odoo.vendor_payment.*"],
}

# Tally -> Odoo (this worker publishes here; Odoo consumes).
INCOMING_QUEUE_BINDINGS = {
    "tally.incoming.customer": ["tally.customer.*"],
    "tally.incoming.vendor": ["tally.vendor.*"],
    "tally.incoming.product": ["tally.product.*"],
    "tally.incoming.invoice": ["tally.sales_invoice.*", "tally.purchase_invoice.*"],
    "tally.incoming.payment": ["tally.receipt.*", "tally.payment.*"],
}

RETRY_COUNT_HEADER = "x-retry-count"


class RabbitMQClient:
    """Use as a context manager -- one connection per publish batch or
    consume pass, matching the same convention as the Odoo-side client."""

    def __init__(self, config):
        self.config = config
        self._connection = None
        self._channel = None

    def __enter__(self):
        params = pika.ConnectionParameters(
            host=self.config.rabbitmq_host,
            port=self.config.rabbitmq_port,
            virtual_host=self.config.rabbitmq_virtual_host,
            credentials=pika.PlainCredentials(self.config.rabbitmq_username, self.config.rabbitmq_password),
            socket_timeout=self.config.rabbitmq_connection_timeout,
            heartbeat=self.config.rabbitmq_heartbeat,
        )
        self._connection = pika.BlockingConnection(params)
        self._channel = self._connection.channel()
        self._channel.confirm_delivery()
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if self._connection and self._connection.is_open:
                self._connection.close()
        except Exception:
            _logger.exception("Error closing RabbitMQ connection")
        return False

    def declare_topology(self):
        ch = self._channel
        ch.exchange_declare(exchange=self.config.rabbitmq_exchange, exchange_type="topic", durable=True)
        ch.exchange_declare(exchange=DLX, exchange_type="topic", durable=True)
        ch.queue_declare(queue=DLQ, durable=True)
        ch.queue_bind(DLQ, DLX, "#")

        queue_args = {"x-dead-letter-exchange": DLX}
        all_bindings = {**OUTGOING_QUEUE_BINDINGS, **INCOMING_QUEUE_BINDINGS}
        for queue_name, patterns in all_bindings.items():
            ch.queue_declare(queue=queue_name, durable=True, arguments=queue_args)
            for pattern in patterns:
                ch.queue_bind(queue_name, self.config.rabbitmq_exchange, pattern)

    def publish(self, routing_key, message: dict, message_id=None):
        body = json.dumps(message, separators=(",", ":")).encode("utf-8")
        self._channel.basic_publish(
            exchange=self.config.rabbitmq_exchange,
            routing_key=routing_key,
            body=body,
            mandatory=True,
            properties=pika.BasicProperties(
                delivery_mode=2,
                content_type="application/json",
                message_id=message_id or message.get("message_id") or str(uuid.uuid4()),
            ),
        )

    def consume_batch(self, queue, handler, max_messages=50, max_retries=3):
        processed = 0
        for _ in range(max_messages):
            method, props, body = self._channel.basic_get(queue=queue, auto_ack=False)
            if method is None:
                break
            processed += 1
            headers = dict((props.headers or {}) if props else {})
            retry_count = int(headers.get(RETRY_COUNT_HEADER, 0))

            try:
                payload = json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                _logger.exception("Poison message on %s -- cannot parse JSON, sending to DLQ", queue)
                self._channel.basic_reject(method.delivery_tag, requeue=False)
                continue

            try:
                ok = handler(payload)
            except Exception:
                _logger.exception("Handler failed for message on %s (attempt %s)", queue, retry_count + 1)
                ok = False

            if ok:
                self._channel.basic_ack(method.delivery_tag)
            elif retry_count + 1 >= max_retries:
                _logger.warning("Max retries reached for message on %s -- sending to DLQ", queue)
                self._channel.basic_reject(method.delivery_tag, requeue=False)
            else:
                self._republish_with_incremented_retry(queue, body, headers, retry_count + 1)
                self._channel.basic_ack(method.delivery_tag)
        return processed

    def _republish_with_incremented_retry(self, queue, body, headers, new_retry_count):
        new_headers = dict(headers)
        new_headers[RETRY_COUNT_HEADER] = new_retry_count
        self._channel.basic_publish(
            exchange="",
            routing_key=queue,
            body=body,
            properties=pika.BasicProperties(delivery_mode=2, content_type="application/json", headers=new_headers),
        )

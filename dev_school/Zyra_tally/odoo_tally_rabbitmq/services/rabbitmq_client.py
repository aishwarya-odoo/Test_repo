"""RabbitMQ transport layer for the Odoo <-> Tally integration.

Topology:
  * One topic exchange ("tally.sync") carries every event, Odoo->Tally and
    Tally->Odoo alike, distinguished by routing key.
  * Separate queues per domain (customer/vendor/product/invoice/payment) x
    direction (odoo/incoming), each bound to the exchange with the routing
    key patterns for that domain. Keeping queues separate means a slow/broken
    consumer for one domain (e.g. payments) never blocks another (e.g.
    customers).
  * Every queue dead-letters into a shared DLX/DLQ after max_retries.

Odoo never talks to Tally directly from here -- this module only publishes
to and consumes from RabbitMQ. The actual Tally HTTP/XML calls happen in the
separate tally_connector worker.
"""
import json
import logging
import uuid

_logger = logging.getLogger(__name__)

try:
    import pika
except ImportError:
    pika = None

EXCHANGE = "tally.sync"
DLX = "tally.sync.dlx"
DLQ = "tally.sync.dlq"

# Odoo -> Tally (this module publishes here; the external connector consumes).
OUTGOING_QUEUE_BINDINGS = {
    "tally.odoo.customer": ["odoo.customer.*"],
    "tally.odoo.vendor": ["odoo.vendor.*"],
    "tally.odoo.product": ["odoo.product.*"],
    "tally.odoo.invoice": ["odoo.sales_invoice.*", "odoo.purchase_invoice.*"],
    "tally.odoo.payment": ["odoo.customer_payment.*", "odoo.vendor_payment.*"],
}

# Tally -> Odoo (the external connector publishes here; this module consumes).
INCOMING_QUEUE_BINDINGS = {
    "tally.incoming.customer": ["tally.customer.*"],
    "tally.incoming.vendor": ["tally.vendor.*"],
    "tally.incoming.product": ["tally.product.*"],
    "tally.incoming.invoice": ["tally.sales_invoice.*", "tally.purchase_invoice.*"],
    "tally.incoming.payment": ["tally.receipt.*", "tally.payment.*"],
}

RETRY_COUNT_HEADER = "x-retry-count"


class RabbitMQConfig:
    def __init__(self, host, port, virtual_host, username, password,
                 exchange=EXCHANGE, connection_timeout=10, heartbeat=30):
        self.host = host
        self.port = int(port or 5672)
        self.virtual_host = virtual_host or "/"
        self.username = username or ""
        self.password = password or ""
        self.exchange = exchange or EXCHANGE
        self.connection_timeout = connection_timeout or 10
        self.heartbeat = heartbeat or 30

    @classmethod
    def from_record(cls, config_record):
        return cls(
            host=config_record.rabbitmq_host,
            port=config_record.rabbitmq_port,
            virtual_host=config_record.rabbitmq_virtual_host,
            username=config_record.rabbitmq_username,
            password=config_record.sudo().rabbitmq_password,
            exchange=config_record.rabbitmq_exchange,
            connection_timeout=config_record.rabbitmq_connection_timeout,
            heartbeat=config_record.rabbitmq_heartbeat,
        )


class RabbitMQClient:
    """Use as a context manager -- one connection per publish batch or
    consume pass, never held open across Odoo requests/cron runs.

        with RabbitMQClient(config) as client:
            client.declare_topology()
            client.publish("odoo.customer.created", message)
    """

    def __init__(self, config: RabbitMQConfig):
        if pika is None:
            raise RuntimeError(
                "The 'pika' Python package is required for the Tally RabbitMQ integration."
            )
        self.config = config
        self._connection = None
        self._channel = None

    def __enter__(self):
        params = pika.ConnectionParameters(
            host=self.config.host,
            port=self.config.port,
            virtual_host=self.config.virtual_host,
            credentials=pika.PlainCredentials(self.config.username, self.config.password),
            socket_timeout=self.config.connection_timeout,
            heartbeat=self.config.heartbeat,
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
        """Idempotent -- safe to call on every connection. Declares the
        exchange, the shared DLX/DLQ, and every known domain queue with its
        bindings and dead-letter routing."""
        ch = self._channel
        ch.exchange_declare(exchange=self.config.exchange, exchange_type="topic", durable=True)
        ch.exchange_declare(exchange=DLX, exchange_type="topic", durable=True)
        ch.queue_declare(queue=DLQ, durable=True)
        ch.queue_bind(DLQ, DLX, "#")

        queue_args = {"x-dead-letter-exchange": DLX}
        all_bindings = {**OUTGOING_QUEUE_BINDINGS, **INCOMING_QUEUE_BINDINGS}
        for queue_name, patterns in all_bindings.items():
            ch.queue_declare(queue=queue_name, durable=True, arguments=queue_args)
            for pattern in patterns:
                ch.queue_bind(queue_name, self.config.exchange, pattern)

    def publish(self, routing_key, message: dict, message_id=None):
        """Publisher-confirms + mandatory=True: raises pika.exceptions.
        UnroutableError if no queue is bound for this routing key, so a
        misconfigured routing key fails loudly at publish time instead of
        silently dropping the message."""
        body = json.dumps(message, separators=(",", ":")).encode("utf-8")
        self._channel.basic_publish(
            exchange=self.config.exchange,
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
        """Pulls up to max_messages with basic_get (pull, matching the
        cron-driven model used throughout this integration -- no long-lived
        basic_consume subscriptions). handler(dict) -> True acks, anything
        else or a raised exception counts as a failed attempt.

        Retry count travels in the message's x-retry-count header rather
        than a DB row, so it survives across cron runs without extra state.
        Once max_retries is exceeded the message is rejected without
        requeue, landing it in the DLQ via the queue's own dead-letter-
        exchange (set up in declare_topology).
        """
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
                _logger.exception(
                    "Handler failed for message on %s (attempt %s)", queue, retry_count + 1
                )
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
        """basic_nack(requeue=True) puts the message back with no visible
        retry count and no backoff hook -- republishing a copy with an
        incremented header instead keeps the count inspectable and leaves
        room for backoff scheduling later."""
        new_headers = dict(headers)
        new_headers[RETRY_COUNT_HEADER] = new_retry_count
        self._channel.basic_publish(
            exchange="",
            routing_key=queue,
            body=body,
            properties=pika.BasicProperties(
                delivery_mode=2, content_type="application/json", headers=new_headers
            ),
        )

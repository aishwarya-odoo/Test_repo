"""Generic dispatch for incoming (Tally -> Odoo) events: consumes a queue via
a RabbitMQClient and routes each envelope's event_type to a registered
handler. Handlers are registered per-queue by later phases (customer/vendor
sync in Phase 4, product in Phase 5, etc.) rather than hardcoded here, so
this module has no dependency on any Odoo model.
"""
import logging

_logger = logging.getLogger(__name__)


class EventDispatcher:
    def __init__(self):
        self._handlers = {}

    def register(self, event_type, handler):
        """handler(envelope: dict) -> bool (True = ack, False/exception = retry)."""
        self._handlers[event_type] = handler
        return self

    def dispatch(self, envelope: dict) -> bool:
        event_type = envelope.get("event_type")
        handler = self._handlers.get(event_type)
        if handler is None:
            _logger.warning("No handler registered for event_type=%s -- dropping (acked)", event_type)
            return True
        return handler(envelope)

    def consume_queue(self, client, queue, max_messages=50, max_retries=3):
        return client.consume_batch(
            queue, self.dispatch, max_messages=max_messages, max_retries=max_retries
        )

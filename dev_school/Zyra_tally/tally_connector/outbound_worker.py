"""Consumes the tally.odoo.* queues (Odoo -> Tally) and pushes each event
to TallyPrime via sync_engine. Pull-based (basic_get in a loop, like the
Odoo-side cron), not a long-lived basic_consume subscription -- one
connection per pass, matching the RabbitMQClient context-manager contract.
"""
import logging
import time

import rabbitmq_client

_logger = logging.getLogger(__name__)

_QUEUES = list(rabbitmq_client.OUTGOING_QUEUE_BINDINGS.keys())


def run_once(config, sync_engine):
    with rabbitmq_client.RabbitMQClient(config) as client:
        client.declare_topology()
        total = 0
        for queue in _QUEUES:
            total += client.consume_batch(
                queue,
                lambda envelope: sync_engine.handle_outbound_event(envelope, client),
                max_messages=config.max_messages_per_batch,
                max_retries=config.max_retries,
            )
        return total


def run_forever(config, sync_engine):
    _logger.info("Outbound worker starting -- polling every %ss", config.consume_interval_seconds)
    while True:
        try:
            processed = run_once(config, sync_engine)
            if processed:
                _logger.info("Outbound pass processed %s message(s)", processed)
        except Exception:
            _logger.exception("Outbound pass failed -- will retry next interval")
        time.sleep(config.consume_interval_seconds)

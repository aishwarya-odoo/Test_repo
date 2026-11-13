import logging
import uuid

from odoo import _, api, fields, models

from ..services import event_publisher, idempotency, rabbitmq_client

_logger = logging.getLogger(__name__)


class TallySyncQueue(models.Model):
    _name = "tally.sync.queue"
    _description = "Tally Sync Outbox / Queue"
    _order = "id"

    name = fields.Char(compute="_compute_name", store=True)
    event_type = fields.Char(required=True, index=True)
    direction = fields.Selection(
        [("outbound", "Odoo -> Tally"), ("inbound", "Tally -> Odoo")],
        required=True, default="outbound", index=True,
    )

    odoo_model = fields.Char(required=True, index=True)
    odoo_record_id = fields.Integer(required=True, index=True)

    payload = fields.Json(required=True)

    message_id = fields.Char(index=True, copy=False, default=lambda self: str(uuid.uuid4()))
    idempotency_key = fields.Char(required=True, index=True, copy=False)

    state = fields.Selection(
        [
            ("pending", "Pending"),
            ("queued", "Queued"),
            ("processing", "Processing"),
            ("success", "Success"),
            ("failed", "Failed"),
            ("retry", "Retry"),
            ("dead_letter", "Dead Letter"),
            ("cancelled", "Cancelled"),
        ],
        required=True, default="pending", index=True,
    )

    retry_count = fields.Integer(default=0)
    max_retries = fields.Integer(default=3)

    scheduled_at = fields.Datetime(default=fields.Datetime.now)
    processed_at = fields.Datetime()

    error_message = fields.Char()

    company_id = fields.Many2one(
        comodel_name="res.company", required=True, default=lambda self: self.env.company, index=True
    )

    @api.depends("event_type", "odoo_model", "odoo_record_id")
    def _compute_name(self):
        for rec in self:
            rec.name = f"{rec.event_type} [{rec.odoo_model}#{rec.odoo_record_id}]"

    @api.model
    def enqueue(self, event_type, odoo_model, odoo_record_id, data, direction="outbound", max_retries=3):
        """Idempotent outbox insert: if a pending/queued/retry entry already
        exists for this exact idempotency key, return it unchanged instead of
        creating a duplicate -- repeated writes to the same record before the
        first sync goes out collapse into one event, same principle as
        zyra_tally's posting dedup but generalized to any model."""
        source = "odoo" if direction == "outbound" else "tally"
        key = idempotency.build_key(source, odoo_model, odoo_record_id, event_type)
        existing = self.search(
            [("idempotency_key", "=", key), ("state", "in", ("pending", "queued", "retry"))], limit=1
        )
        if existing:
            return existing
        return self.create({
            "event_type": event_type,
            "direction": direction,
            "odoo_model": odoo_model,
            "odoo_record_id": odoo_record_id,
            "payload": data,
            "idempotency_key": key,
            "max_retries": max_retries,
        })

    @api.model
    def _get_config(self):
        config = self.env["tally.integration.config"].search(
            [("company_id", "=", self.env.company.id), ("active", "=", True)], limit=1
        )
        return config

    @api.model
    def cron_process_outbox(self, batch_size=100):
        """Publishes pending outbound entries to RabbitMQ. Never lets a
        RabbitMQ failure roll back the DB state that got us here -- each
        entry's state change is its own commit, so a broker outage just
        leaves entries pending for the next run instead of blocking whatever
        posted the original invoice/payment."""
        config = self._get_config()
        if not config or not config.sync_enabled:
            return
        pending = self.search(
            [("direction", "=", "outbound"), ("state", "in", ("pending", "retry")), ("company_id", "=", config.company_id.id)],
            limit=batch_size,
            order="scheduled_at",
        )
        if not pending:
            return

        rmq_config = rabbitmq_client.RabbitMQConfig.from_record(config)
        try:
            with rabbitmq_client.RabbitMQClient(rmq_config) as client:
                client.declare_topology()
                for entry in pending:
                    try:
                        event_publisher.publish_event(
                            client, entry.event_type, "odoo", entry.company_id.id,
                            entry.odoo_model, entry.odoo_record_id, entry.payload,
                        )
                    except Exception as exc:
                        _logger.exception("Failed to publish outbox entry %s", entry.id)
                        entry._mark_failed(str(exc))
                        self.env.cr.commit()
                        continue
                    entry.write({"state": "queued", "processed_at": fields.Datetime.now(), "error_message": False})
                    self.env.cr.commit()
        except Exception as exc:
            _logger.exception("Could not connect to RabbitMQ to process outbox")
            pending._mark_failed(_("Could not reach RabbitMQ: %s", exc))
            self.env.cr.commit()

    def _mark_failed(self, error_message):
        for entry in self:
            if entry.retry_count + 1 >= entry.max_retries:
                entry.write({
                    "state": "dead_letter", "error_message": error_message,
                    "retry_count": entry.retry_count + 1, "processed_at": fields.Datetime.now(),
                })
            else:
                entry.write({
                    "state": "retry", "error_message": error_message,
                    "retry_count": entry.retry_count + 1,
                })

    def action_retry(self):
        # "retry" entries are already eligible for the next automatic
        # cron_process_outbox pass, but an operator forcing a manual retry
        # from the dashboard/queue UI should still be able to reset them
        # immediately rather than being silently ignored.
        for entry in self:
            if entry.state in ("failed", "retry", "dead_letter", "cancelled"):
                entry.write({"state": "pending", "error_message": False})

    def action_cancel(self):
        for entry in self.filtered(lambda e: e.state not in ("success",)):
            entry.state = "cancelled"

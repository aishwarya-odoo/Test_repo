import logging

import requests

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

try:
    import pika
except ImportError:
    pika = None


class TallyIntegrationConfig(models.Model):
    _name = "tally.integration.config"
    _description = "Tally Integration Configuration"
    _inherit = ["mail.thread"]

    name = fields.Char(required=True, default="Tally Integration")
    company_id = fields.Many2one(
        comodel_name="res.company", required=True, default=lambda self: self.env.company
    )
    active = fields.Boolean(default=True)

    tally_company_name = fields.Char(string="Tally Company Name")
    tally_company_identifier = fields.Char(string="Tally Company Identifier")
    tally_gateway_url = fields.Char(
        string="Tally Gateway URL",
        help="TallyPrime's own HTTP/XML gateway (e.g. http://192.168.2.55:9000/). "
             "This Odoo module never calls it directly -- it is only recorded here "
             "for reference and for the separate tally_connector worker to read.",
    )

    rabbitmq_host = fields.Char(string="RabbitMQ Host", required=True, default="localhost")
    rabbitmq_port = fields.Integer(string="RabbitMQ Port", required=True, default=5672)
    rabbitmq_virtual_host = fields.Char(string="RabbitMQ Virtual Host", default="/")
    rabbitmq_username = fields.Char(string="RabbitMQ Username")
    rabbitmq_password = fields.Char(string="RabbitMQ Password", groups="odoo_tally_rabbitmq.group_tally_manager")
    rabbitmq_exchange = fields.Char(
        string="RabbitMQ Exchange", default="tally.sync",
        help="Topic exchange used for all Odoo <-> Tally routing.",
    )
    rabbitmq_connection_timeout = fields.Integer(string="Connection Timeout (s)", default=10)
    rabbitmq_heartbeat = fields.Integer(string="Heartbeat (s)", default=30)

    sync_enabled = fields.Boolean(string="Synchronization Enabled", default=False)
    customer_sync_enabled = fields.Boolean(string="Sync Customers", default=True)
    vendor_sync_enabled = fields.Boolean(string="Sync Vendors", default=True)
    product_sync_enabled = fields.Boolean(string="Sync Products", default=True)
    invoice_sync_enabled = fields.Boolean(string="Sync Invoices", default=True)
    payment_sync_enabled = fields.Boolean(string="Sync Payments", default=True)

    retry_count = fields.Integer(string="Max Retry Count", default=3)
    retry_delay = fields.Integer(string="Retry Delay (s)", default=30)

    connection_status = fields.Selection(
        [("unknown", "Unknown"), ("connected", "Connected"), ("failed", "Failed")],
        default="unknown", string="RabbitMQ Connection Status", readonly=True, copy=False,
    )
    connection_checked_at = fields.Datetime(readonly=True, copy=False)

    tally_connection_status = fields.Selection(
        [("unknown", "Unknown"), ("connected", "Connected"), ("failed", "Failed")],
        default="unknown", string="Tally Gateway Status", readonly=True, copy=False,
    )
    tally_connection_checked_at = fields.Datetime(readonly=True, copy=False)

    _company_config_unique = models.Constraint(
        "UNIQUE(company_id)",
        "Only one Tally integration configuration is allowed per company.",
    )

    def action_test_rabbitmq_connection(self):
        self.ensure_one()
        if pika is None:
            raise UserError(_("The 'pika' Python package is not installed on the server."))
        params = pika.ConnectionParameters(
            host=self.rabbitmq_host,
            port=self.rabbitmq_port,
            virtual_host=self.rabbitmq_virtual_host or "/",
            credentials=pika.PlainCredentials(
                self.rabbitmq_username or "", self.sudo().rabbitmq_password or ""
            ),
            socket_timeout=self.rabbitmq_connection_timeout or 10,
            heartbeat=self.rabbitmq_heartbeat or 30,
        )
        try:
            connection = pika.BlockingConnection(params)
            connection.close()
        except Exception as exc:
            _logger.warning("Tally RabbitMQ connection test failed for config %s", self.id)
            self.write({"connection_status": "failed", "connection_checked_at": fields.Datetime.now()})
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("RabbitMQ Connection Failed"),
                    "message": str(exc),
                    "type": "danger",
                    "sticky": False,
                },
            }
        self.write({"connection_status": "connected", "connection_checked_at": fields.Datetime.now()})
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("RabbitMQ Connection OK"),
                "message": _("Successfully connected to %(host)s:%(port)s.", host=self.rabbitmq_host, port=self.rabbitmq_port),
                "type": "success",
                "sticky": False,
            },
        }

    def action_test_tally_connection(self):
        """Odoo never talks to Tally during normal sync (that is
        tally_connector's job) -- this button is purely a diagnostic ping
        against the recorded gateway URL, so misconfigured hosts get caught
        from the Odoo config screen instead of only surfacing as a failure
        deep in tally_connector."""
        self.ensure_one()
        if not self.tally_gateway_url:
            raise UserError(_("Set a Tally Gateway URL before testing the connection."))
        try:
            response = requests.get(self.tally_gateway_url, timeout=10)
            response.raise_for_status()
            if "TallyPrime" not in response.text:
                raise ValueError(_("Unexpected response from %s -- doesn't look like a Tally gateway.", self.tally_gateway_url))
        except Exception as exc:
            _logger.warning("Tally gateway connection test failed for config %s", self.id)
            self.write({"tally_connection_status": "failed", "tally_connection_checked_at": fields.Datetime.now()})
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("Tally Connection Failed"),
                    "message": str(exc),
                    "type": "danger",
                    "sticky": False,
                },
            }
        self.write({"tally_connection_status": "connected", "tally_connection_checked_at": fields.Datetime.now()})
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Tally Connection OK"),
                "message": _("Successfully reached the Tally gateway at %s.", self.tally_gateway_url),
                "type": "success",
                "sticky": False,
            },
        }

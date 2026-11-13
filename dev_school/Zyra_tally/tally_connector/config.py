"""Configuration for the tally_connector worker, entirely from environment
variables -- this process has no database of its own and no access to
Odoo's ORM/config records (deliberately: it only ever talks to RabbitMQ and
to Tally's HTTP/XML gateway, per the architecture in
odoo_tally_rabbitmq/ARCHITECTURE.md)."""
import os


def _env(name, default=None, required=False):
    value = os.environ.get(name, default)
    if required and not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


class Config:
    def __init__(self):
        self.rabbitmq_host = _env("RABBITMQ_HOST", "rabbitmq")
        self.rabbitmq_port = int(_env("RABBITMQ_PORT", "5672"))
        self.rabbitmq_virtual_host = _env("RABBITMQ_VHOST", "/")
        self.rabbitmq_username = _env("RABBITMQ_USER", "odoo")
        self.rabbitmq_password = _env("RABBITMQ_PASSWORD", "odoo")
        self.rabbitmq_exchange = _env("RABBITMQ_EXCHANGE", "tally.sync")
        self.rabbitmq_connection_timeout = int(_env("RABBITMQ_CONNECTION_TIMEOUT", "10"))
        self.rabbitmq_heartbeat = int(_env("RABBITMQ_HEARTBEAT", "30"))

        self.tally_gateway_url = _env("TALLY_GATEWAY_URL", required=True)
        self.tally_company_name = _env("TALLY_COMPANY_NAME", required=True)
        self.tally_request_timeout = int(_env("TALLY_REQUEST_TIMEOUT", "30"))

        # company_id to stamp on every envelope this worker publishes --
        # the Odoo side's tally.integration.config is looked up per-company,
        # so a real multi-company deployment needs one connector instance
        # (one set of env vars) per company, matching the "Multi-company"
        # section of ARCHITECTURE.md.
        self.odoo_company_id = int(_env("ODOO_COMPANY_ID", "1"))

        self.poll_interval_seconds = int(_env("TALLY_POLL_INTERVAL_SECONDS", "60"))
        self.consume_interval_seconds = int(_env("RABBITMQ_CONSUME_INTERVAL_SECONDS", "15"))
        self.max_messages_per_batch = int(_env("MAX_MESSAGES_PER_BATCH", "50"))
        self.max_retries = int(_env("MAX_RETRIES", "3"))

        self.state_file_path = _env("STATE_FILE_PATH", "/data/tally_connector_state.json")


config = Config()

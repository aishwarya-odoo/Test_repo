from odoo import _, api, fields, models


class TallySyncDashboard(models.TransientModel):
    _name = "tally.sync.dashboard"
    _description = "Tally Sync Dashboard"

    company_id = fields.Many2one(comodel_name="res.company", readonly=True)
    connection_status = fields.Selection(
        [("not_configured", "Not Configured"), ("unknown", "Unknown"),
         ("connected", "Connected"), ("failed", "Failed")],
        readonly=True,
    )
    sync_enabled = fields.Boolean(readonly=True)
    pending_count = fields.Integer(readonly=True)
    processing_count = fields.Integer(readonly=True)
    success_count = fields.Integer(readonly=True)
    retry_count = fields.Integer(readonly=True)
    dead_letter_count = fields.Integer(readonly=True)
    conflict_count = fields.Integer(readonly=True)

    @api.model
    def default_get(self, fields_list):
        vals = super().default_get(fields_list)
        company = self.env.company
        Queue = self.env["tally.sync.queue"].sudo()
        Config = self.env["tally.integration.config"].sudo()
        Conflict = self.env["tally.sync.conflict"].sudo()

        config = Config.search([("company_id", "=", company.id)], limit=1)

        def count(state):
            return Queue.search_count([("company_id", "=", company.id), ("state", "=", state)])

        vals.update({
            "company_id": company.id,
            "connection_status": config.connection_status if config else "not_configured",
            "sync_enabled": bool(config and config.sync_enabled),
            "pending_count": count("pending") + count("queued"),
            "processing_count": count("processing"),
            "success_count": count("success"),
            "retry_count": count("retry") + count("failed"),
            "dead_letter_count": count("dead_letter"),
            "conflict_count": Conflict.search_count([("company_id", "=", company.id), ("state", "=", "pending")]),
        })
        return vals

    @api.model
    def action_open_dashboard(self):
        """Creates the transient record up front and opens it by res_id
        rather than as an empty "new" record -- opening an uncreated record
        full-page makes Odoo's client show the New/Save/Discard editing bar
        (and, worse, opening it as a dialog instead leaves no real page
        behind it, so navigating into this app from the menu would strand
        the user with nothing to click into). An already-saved, all-readonly
        record behaves like a normal informational page instead."""
        record = self.create(self.default_get(list(self._fields)))
        return {
            "type": "ir.actions.act_window",
            "name": _("Dashboard"),
            "res_model": "tally.sync.dashboard",
            "res_id": record.id,
            "view_mode": "form",
            "target": "current",
        }

    def _reopen(self):
        return self.action_open_dashboard()

    def action_refresh(self):
        return self._reopen()

    def action_sync_now(self):
        self.env["tally.sync.queue"].sudo().cron_process_outbox()
        return self._reopen()

    def action_retry_failed(self):
        Queue = self.env["tally.sync.queue"].sudo()
        Queue.search([("company_id", "=", self.company_id.id), ("state", "in", ("retry", "failed"))]).action_retry()
        return self._reopen()

    def action_retry_dead_letter(self):
        Queue = self.env["tally.sync.queue"].sudo()
        Queue.search([("company_id", "=", self.company_id.id), ("state", "=", "dead_letter")]).action_retry()
        return self._reopen()

    def action_test_connection(self):
        config = self.env["tally.integration.config"].sudo().search(
            [("company_id", "=", self.company_id.id)], limit=1
        )
        if not config:
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {"title": _("Tally Sync"), "message": _("No configuration found for this company."), "type": "warning"},
            }
        return config.action_test_rabbitmq_connection()

    def action_view_queue(self):
        return {
            "type": "ir.actions.act_window", "name": _("Sync Queue"),
            "res_model": "tally.sync.queue", "view_mode": "list,form",
            "domain": [("company_id", "=", self.company_id.id)],
        }

    def action_view_dead_letter(self):
        return {
            "type": "ir.actions.act_window", "name": _("Failed Synchronizations"),
            "res_model": "tally.sync.queue", "view_mode": "list,form",
            "domain": [("company_id", "=", self.company_id.id), ("state", "in", ("retry", "failed", "dead_letter"))],
        }

    def action_view_conflicts(self):
        return {
            "type": "ir.actions.act_window", "name": _("Sync Conflicts"),
            "res_model": "tally.sync.conflict", "view_mode": "list,form",
            "domain": [("company_id", "=", self.company_id.id), ("state", "=", "pending")],
        }

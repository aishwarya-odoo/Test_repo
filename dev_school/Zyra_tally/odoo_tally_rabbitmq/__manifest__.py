{
    "name": "Odoo Tally RabbitMQ Integration",
    "version": "19.0.1.0.0",
    "category": "Accounting/Integration",
    "summary": "Two-way Odoo <-> TallyPrime synchronization over RabbitMQ",
    "description": """
Odoo 19 Community <-> RabbitMQ <-> TallyPrime two-way synchronization platform
for customers, vendors, products, invoices and payments.

Odoo never calls Tally's HTTP/XML API directly -- all communication goes
through RabbitMQ, consumed on the Tally side by a separate connector worker
(see tally_connector/ alongside this module, not part of the Odoo addon
itself).
""",
    "author": "Eyegen Medical LLP",
    "license": "LGPL-3",
    "depends": ["base", "mail", "account", "product", "stock"],
    "external_dependencies": {"python": ["pika"]},
    "data": [
        "security/tally_security.xml",
        "security/ir.model.access.csv",
        "data/ir_cron.xml",
        "data/tally_config_data.xml",
        "views/tally_config_views.xml",
        "views/tally_sync_queue_views.xml",
        "views/tally_mapping_views.xml",
        "views/tally_sync_conflict_views.xml",
        "views/tally_sync_dashboard_views.xml",
        "views/res_partner_views.xml",
        "views/product_views.xml",
        "views/account_move_views.xml",
        "views/account_payment_views.xml",
        "views/tally_menus.xml",
    ],
    "installable": True,
    "application": True,
    "auto_install": False,
}

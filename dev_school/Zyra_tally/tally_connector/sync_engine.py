"""Maps between odoo_tally_rabbitmq's JSON envelopes (see
odoo_tally_rabbitmq/API_SCHEMA.md) and TallyPrime XML, both directions.

Honest about its limits rather than guessing past them:
  * Renaming a customer/vendor/product in Odoo pushes an ACTION="Alter" by
    the *new* name -- if Tally's ledger/item was never renamed to match
    (this worker doesn't track name history), Tally will report it "does
    not exist" and the event dead-letters on the Odoo side for manual
    retry, rather than silently creating a duplicate.
  * A voucher (invoice/receipt/payment) created directly in Tally --
    without ever having originated in Odoo -- has no odoo_id and cannot
    be matched by Odoo's current inbound handler; see the "Known gap" in
    TALLY_INTEGRATION.md. This worker still forwards what Tally has
    (reference/voucher number); Odoo logs and skips it rather than
    guessing at creating a new accounting entry.
  * Payment/receipt bill allocations are not fetched, so
    invoice_references is always empty on inbound payments -- Odoo
    correctly leaves those "pending reconciliation" rather than guessing.
"""
import datetime
import logging
import re
import uuid

_logger = logging.getLogger(__name__)

SCHEMA_VERSION = "1.0"
_TALLY_CODE_SUFFIX_RE = re.compile(r"\s*\[.*?\]\s*$")


def strip_tally_code_suffix(name):
    """Tally auto-appends a bracketed code to a ledger's display NAME (e.g.
    "Acme Corp [ CRD00002]") -- strip it to recover the plain name Odoo
    knows the record by."""
    if not name:
        return name
    return _TALLY_CODE_SUFFIX_RE.sub("", name).strip()


def to_tally_date(iso_date):
    if not iso_date:
        return datetime.date.today().strftime("%Y%m%d")
    return iso_date.replace("-", "")[:8]


def build_envelope(event_type, source, company_id, data):
    return {
        "schema_version": SCHEMA_VERSION,
        "message_id": str(uuid.uuid4()),
        "idempotency_key": f"{source}:{event_type}:{data.get('tally_id') or data.get('odoo_id')}",
        "source": source,
        "event_type": event_type,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "company_id": company_id,
        "data": data,
    }


class SyncEngine:
    def __init__(self, config, tally_client):
        self.config = config
        self.client = tally_client

    # ---------------------------------------------------------- outbound push
    def handle_outbound_event(self, envelope, rabbitmq_client):
        event_type = envelope.get("event_type")
        data = envelope.get("data") or {}
        handler = {
            "customer.created": lambda: self._push_ledger(data, "Sundry Debtors", "Create", rabbitmq_client),
            "customer.updated": lambda: self._push_ledger(data, "Sundry Debtors", "Alter", rabbitmq_client),
            "vendor.created": lambda: self._push_ledger(data, "Sundry Creditors", "Create", rabbitmq_client),
            "vendor.updated": lambda: self._push_ledger(data, "Sundry Creditors", "Alter", rabbitmq_client),
            "product.created": lambda: self._push_stock_item(data, "Create", rabbitmq_client),
            "product.updated": lambda: self._push_stock_item(data, "Alter", rabbitmq_client),
            "sales_invoice.created": lambda: self._push_invoice(data, "Sales", rabbitmq_client),
            "purchase_invoice.created": lambda: self._push_invoice(data, "Purchase", rabbitmq_client),
            "customer_payment.created": lambda: self._push_payment(data, "Receipt", rabbitmq_client),
            "vendor_payment.created": lambda: self._push_payment(data, "Payment", rabbitmq_client),
        }.get(event_type)
        if not handler:
            _logger.warning("No outbound handler for event_type %s -- dropping", event_type)
            return True
        return handler()

    def _push_ledger(self, data, parent, action, rabbitmq_client):
        name = data.get("name")
        if not name:
            _logger.warning("Cannot push ledger without a name: %s", data)
            return False
        address_lines = [data.get(k) for k in ("street", "street2", "city") if data.get(k)]
        self.client.push_ledger(
            name, parent, action=action,
            mobile=data.get("phone"), email=data.get("email"), gstin=data.get("vat"),
            address_lines=address_lines, pincode=data.get("zip"),
        )
        self._confirm_ledger(name, parent, data, rabbitmq_client)
        return True

    def _push_stock_item(self, data, action, rabbitmq_client):
        name = data.get("name")
        if not name:
            _logger.warning("Cannot push stock item without a name: %s", data)
            return False
        self.client.push_stock_item(
            name, data.get("uom") or "Units", action=action, hsn_code=data.get("hsn"),
        )
        self._confirm_stock_item(name, data, rabbitmq_client)
        return True

    def _push_invoice(self, data, voucher_type, rabbitmq_client):
        partner = data.get("partner") or {}
        party_name = partner.get("name")
        if not party_name:
            _logger.warning("Cannot push %s voucher without a partner name: %s", voucher_type, data)
            return False
        line_ledger = "Sales Accounts" if voucher_type == "Sales" else "Purchase Accounts"
        lines = [
            {
                "stock_item": line.get("product") or "N/A",
                "quantity": line.get("quantity") or 0,
                "rate": line.get("unit_price") or 0,
                "amount": line.get("subtotal") or 0,
            }
            for line in (data.get("lines") or [])
        ]
        self.client.push_sales_purchase_voucher(
            voucher_type,
            date_yyyymmdd=to_tally_date(data.get("invoice_date")),
            party_ledger=party_name,
            party_amount=data.get("amount_total") or 0,
            line_ledger=line_ledger,
            lines=lines,
            narration=data.get("reference"),
        )
        return True

    def _push_payment(self, data, voucher_type, rabbitmq_client):
        partner = data.get("partner") or {}
        party_name = partner.get("name")
        if not party_name:
            _logger.warning("Cannot push %s voucher without a partner name: %s", voucher_type, data)
            return False
        self.client.push_payment_receipt_voucher(
            voucher_type,
            date_yyyymmdd=to_tally_date(data.get("date")),
            party_ledger=party_name,
            bank_ledger=data.get("journal") or "Cash",
            amount=data.get("amount") or 0,
            reference=data.get("reference"),
        )
        return True

    def _confirm_ledger(self, name, parent, odoo_data, rabbitmq_client):
        """After a successful push, look the ledger back up to grab the
        GUID Tally assigned it and echo it back to Odoo -- otherwise
        Odoo's tally.sync.mapping never gets a tally_id and every future
        update keeps relying on the GSTIN/name fallback match instead."""
        try:
            match = self.client.find_by_name("ledger", name)
        except Exception:
            _logger.exception("Could not confirm ledger '%s' after push", name)
            return
        if not match:
            _logger.warning("Pushed ledger '%s' but could not find it on read-back", name)
            return
        event_type = "tally.customer.updated" if parent == "Sundry Debtors" else "tally.vendor.updated"
        data = {
            "tally_id": match.get("GUID"),
            "name": name,
            "vat": odoo_data.get("vat"),
            "phone": odoo_data.get("phone"),
            "email": odoo_data.get("email"),
        }
        envelope = build_envelope(event_type, "tally", self.config.odoo_company_id, data)
        rabbitmq_client.publish(event_type, envelope, message_id=envelope["message_id"])

    def _confirm_stock_item(self, name, odoo_data, rabbitmq_client):
        try:
            match = self.client.find_by_name("stock_item", name)
        except Exception:
            _logger.exception("Could not confirm stock item '%s' after push", name)
            return
        if not match:
            _logger.warning("Pushed stock item '%s' but could not find it on read-back", name)
            return
        data = {"tally_id": match.get("GUID"), "name": name, "sku": odoo_data.get("sku")}
        envelope = build_envelope("tally.product.updated", "tally", self.config.odoo_company_id, data)
        rabbitmq_client.publish("tally.product.updated", envelope, message_id=envelope["message_id"])

    # -------------------------------------------------------------- inbound poll
    def poll_and_publish(self, category, state_store, rabbitmq_client):
        since = state_store.get_last_alterid(category)
        records = self.client.poll_changes(category, since)
        published = 0
        max_alterid = since
        for record in records:
            alterid = record.get("ALTERID", 0)
            if not record.get("ISDELETED"):
                for event_type, data in self._classify(category, record):
                    envelope = build_envelope(event_type, "tally", self.config.odoo_company_id, data)
                    rabbitmq_client.publish(event_type, envelope, message_id=envelope["message_id"])
                    published += 1
            max_alterid = max(max_alterid, alterid)
            state_store.set_last_alterid(category, max_alterid)
        return published

    def _classify(self, category, record):
        if category == "ledger":
            parent = record.get("PARENT")
            name = strip_tally_code_suffix(record.get("name"))
            if not name:
                return
            address = record.get("ADDRESS.LIST") or []
            data = {
                "tally_id": record.get("GUID"),
                "name": name,
                "vat": record.get("PARTYGSTIN"),
                "street": address[0] if address else None,
                "street2": " ".join(address[1:]) if len(address) > 1 else None,
                "zip": record.get("PINCODE"),
                "phone": record.get("LEDGERMOBILE"),
                "email": record.get("EMAIL"),
            }
            if parent == "Sundry Debtors":
                yield "tally.customer.updated", data
            elif parent == "Sundry Creditors":
                yield "tally.vendor.updated", data

        elif category == "stock_item":
            name = record.get("name")
            if not name:
                return
            yield "tally.product.updated", {
                "tally_id": record.get("GUID"),
                "name": name,
                "sku": None,
                "barcode": None,
            }

        elif category == "voucher":
            voucher_type = record.get("VOUCHERTYPENAME")
            party_name = strip_tally_code_suffix(record.get("PARTYLEDGERNAME"))
            base = {
                "tally_id": record.get("GUID"),
                "voucher_number": record.get("VOUCHERNUMBER"),
                "reference": record.get("VOUCHERNUMBER"),
            }
            if voucher_type == "Sales":
                yield "tally.sales_invoice.created", {**base, "odoo_id": None}
            elif voucher_type == "Purchase":
                yield "tally.purchase_invoice.created", {**base, "odoo_id": None}
            elif voucher_type == "Receipt":
                yield "tally.receipt.created", {
                    **base, "partner": {"name": party_name},
                    "amount": abs(record.get("AMOUNT", 0)), "invoice_references": [],
                }
            elif voucher_type == "Payment":
                yield "tally.payment.created", {
                    **base, "partner": {"name": party_name},
                    "amount": abs(record.get("AMOUNT", 0)), "invoice_references": [],
                }

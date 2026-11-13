"""HTTP client for TallyPrime's XML gateway -- wraps tally_xml's envelope
builders/parsers with the actual POST call and error handling."""
import logging

import requests

import tally_xml

_logger = logging.getLogger(__name__)


class TallyError(Exception):
    pass


class TallyClient:
    def __init__(self, config):
        self.config = config

    def _post(self, envelope_xml):
        response = requests.post(
            self.config.tally_gateway_url,
            data=envelope_xml.encode("utf-8"),
            headers={"Content-Type": "text/xml"},
            timeout=self.config.tally_request_timeout,
        )
        response.raise_for_status()
        return response.text

    # ------------------------------------------------------------------ push
    def push_ledger(self, name, parent, action="Create", **kwargs):
        inner = tally_xml.build_ledger_xml(name, parent, action=action, **kwargs)
        envelope = tally_xml.build_import_envelope(self.config.tally_company_name, inner)
        result = tally_xml.parse_import_response(self._post(envelope))
        if result["errors"]:
            raise TallyError(f"Tally rejected ledger '{name}': {result['line_error']}")
        return result

    def push_stock_item(self, name, base_units, action="Create", **kwargs):
        inner = tally_xml.build_stock_item_xml(name, base_units, action=action, **kwargs)
        envelope = tally_xml.build_import_envelope(self.config.tally_company_name, inner)
        result = tally_xml.parse_import_response(self._post(envelope))
        if result["errors"]:
            raise TallyError(f"Tally rejected stock item '{name}': {result['line_error']}")
        return result

    def push_sales_purchase_voucher(self, voucher_type, **kwargs):
        inner = tally_xml.build_sales_purchase_voucher_xml(voucher_type, **kwargs)
        envelope = tally_xml.build_import_envelope(self.config.tally_company_name, inner)
        result = tally_xml.parse_import_response(self._post(envelope))
        if result["errors"]:
            raise TallyError(f"Tally rejected {voucher_type} voucher: {result['line_error']}")
        return result

    def push_payment_receipt_voucher(self, voucher_type, **kwargs):
        inner = tally_xml.build_payment_receipt_voucher_xml(voucher_type, **kwargs)
        envelope = tally_xml.build_import_envelope(self.config.tally_company_name, inner)
        result = tally_xml.parse_import_response(self._post(envelope))
        if result["errors"]:
            raise TallyError(f"Tally rejected {voucher_type} voucher: {result['line_error']}")
        return result

    # ------------------------------------------------------------------ poll
    _COLLECTION_SPEC = {
        "ledger": ("Ledger", "LEDGER", [
            "NAME", "PARENT", "GUID", "ALTERID", "LEDGERMOBILE", "EMAIL",
            "PARTYGSTIN", "ADDRESS.LIST", "PINCODE", "ISDELETED",
        ]),
        "stock_item": ("StockItem", "STOCKITEM", [
            "NAME", "GUID", "ALTERID", "BASEUNITS", "GSTHSNCODE", "ISDELETED",
        ]),
        "voucher": ("Voucher", "VOUCHER", [
            "DATE", "VOUCHERTYPENAME", "VOUCHERNUMBER", "PARTYLEDGERNAME",
            "GUID", "ALTERID", "AMOUNT", "ISDELETED",
        ]),
    }

    def poll_changes(self, object_category, since_alterid):
        """Returns raw records for every object of this Tally TYPE with
        ALTERID > since_alterid. Classifying a raw ledger/voucher record
        into a specific Odoo-facing event type (customer vs vendor,
        sales vs purchase, ...) is sync_engine's job, not this client's --
        Tally itself does not separate these into different collections."""
        tally_type, object_tag, fetch_fields = self._COLLECTION_SPEC[object_category]
        envelope = tally_xml.build_collection_envelope(
            self.config.tally_company_name,
            f"Connector{object_category.title().replace('_', '')}Changes",
            tally_type,
            fetch_fields,
            filter_formula=f"$AlterID > {int(since_alterid)}",
        )
        records = tally_xml.parse_collection_response(self._post(envelope), object_tag)
        return records

    def find_by_name(self, object_category, name):
        """Single-record lookup by exact name, used to confirm a push and
        recover the GUID Tally assigned -- much cheaper than polling the
        whole collection just to find one record."""
        tally_type, object_tag, fetch_fields = self._COLLECTION_SPEC[object_category]
        safe_name = name.replace('"', "'")
        envelope = tally_xml.build_collection_envelope(
            self.config.tally_company_name,
            f"Connector{object_category.title().replace('_', '')}ByName",
            tally_type,
            fetch_fields,
            filter_formula=f'$NAME = "{safe_name}"',
        )
        records = tally_xml.parse_collection_response(self._post(envelope), object_tag)
        return records[0] if records else None

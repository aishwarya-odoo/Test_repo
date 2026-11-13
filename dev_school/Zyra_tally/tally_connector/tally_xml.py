"""Builds and parses the XML envelopes for TallyPrime's HTTP/XML gateway.

Everything here was verified against a real TallyPrime server (see
odoo_tally_rabbitmq/TALLY_INTEGRATION.md), not written from documentation
alone -- in particular:

  * Master creation/alteration (Ledger, StockItem) requires an explicit
    <NAME.LIST><NAME>...</NAME></NAME.LIST> in addition to the NAME
    attribute -- omitting it is accepted structurally but fails with a
    generic "No Valid Names!" error and creates nothing.
  * Reading data back uses <TYPE>Collection</TYPE> with an ad-hoc TDL
    <COLLECTION> definition and an optional <FILTER> referencing a
    <SYSTEM TYPE="Formulae"> block -- there is no REST-style query
    language, filtering is done via these formula expressions.
  * Every master/voucher carries a GUID (stable identity) and an ALTERID
    (a monotonically increasing per-object change counter, company-wide)
    -- ALTERID is what incremental polling is built on, since Tally has no
    webhook/push mechanism of its own.
  * Empty/unset fields are omitted from the response entirely rather than
    returned empty; some unset fields render literal placeholder text
    (e.g. "\\x04 Not Found", "\\x04 Not Applicable") instead of being
    omitted -- both cases are normalized to None here.
"""
import re
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

_PLACEHOLDER_RE = re.compile(r"Not Found|Not Applicable", re.IGNORECASE)
_AMOUNT_RE = re.compile(r"-?[0-9][0-9,]*\.?[0-9]*")

# TallyPrime emits literal numeric character references for control
# characters XML 1.0 does not actually allow (verified on the wire: e.g.
# `&#4;` ahead of "Not Found"/"Not Applicable" placeholder text) --
# Python's strict expat-based parser raises on these, so they're stripped
# before parsing rather than left to crash every response containing an
# unset field.
_CHARREF_RE = re.compile(r"&#(x?)([0-9A-Fa-f]+);", re.IGNORECASE)


def _is_valid_xml_char(codepoint):
    return (
        codepoint in (0x09, 0x0A, 0x0D)
        or 0x20 <= codepoint <= 0xD7FF
        or 0xE000 <= codepoint <= 0xFFFD
        or 0x10000 <= codepoint <= 0x10FFFF
    )


def _strip_invalid_xml_charrefs(text):
    def _replace(match):
        is_hex, digits = match.group(1), match.group(2)
        codepoint = int(digits, 16 if is_hex else 10)
        return match.group(0) if _is_valid_xml_char(codepoint) else ""

    return _CHARREF_RE.sub(_replace, text)


def _clean_text(value):
    if value is None:
        return None
    text = value.strip().lstrip("\x04").strip()
    if not text or _PLACEHOLDER_RE.search(text):
        return None
    return text


def _clean_amount(value):
    text = _clean_text(value)
    if text is None:
        return 0.0
    match = _AMOUNT_RE.search(text.replace(",", ""))
    if not match:
        return 0.0
    negative = text.strip().startswith("-")
    amount = float(match.group())
    return -amount if negative and amount > 0 else amount


# --------------------------------------------------------------- import (push)
def build_import_envelope(company_name, tally_message_inner_xml):
    return (
        "<ENVELOPE>"
        "<HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>"
        "<BODY><IMPORTDATA>"
        "<REQUESTDESC><REPORTNAME>Vouchers</REPORTNAME>"
        f"<STATICVARIABLES><SVCURRENTCOMPANY>{escape(company_name)}</SVCURRENTCOMPANY></STATICVARIABLES>"
        "</REQUESTDESC>"
        f'<REQUESTDATA><TALLYMESSAGE xmlns:UDF="TallyUDF">{tally_message_inner_xml}</TALLYMESSAGE></REQUESTDATA>'
        "</IMPORTDATA></BODY></ENVELOPE>"
    )


def parse_import_response(xml_text):
    """Handles two real response shapes seen from this gateway:
    the classic flat one build_import_envelope's request actually gets back
    (<RESPONSE><CREATED>.../<LINEERROR>...</RESPONSE>, sometimes with no
    structured fields at all -- just plain text, e.g. "Unknown Request,
    cannot be processed"), and defensively an ENVELOPE-wrapped one (seen
    only with a different, unused request header style) in case it's ever
    returned here too."""
    root = ET.fromstring(_strip_invalid_xml_charrefs(xml_text))
    if root.tag != "RESPONSE":
        data = root.find(".//DATA")
        root = data if data is not None else root

    def _int(tag):
        el = root.find(tag)
        return int((el.text or "0").strip()) if el is not None and el.text else 0

    line_error_el = root.find("LINEERROR")
    if line_error_el is not None:
        line_error = (line_error_el.text or "").strip()
    elif root.find("CREATED") is None and root.find("ERRORS") is None:
        line_error = (root.text or "").strip() or "Unrecognized response"
    else:
        line_error = None

    errors = _int("ERRORS")
    if line_error and not errors:
        errors = 1

    return {
        "created": _int("CREATED"),
        "altered": _int("ALTERED"),
        "deleted": _int("DELETED"),
        "errors": errors,
        "line_error": line_error,
    }


def _address_xml(address_lines):
    if not address_lines:
        return ""
    items = "".join(f"<ADDRESS>{escape(line)}</ADDRESS>" for line in address_lines if line)
    return f"<ADDRESS.LIST>{items}</ADDRESS.LIST>" if items else ""


def build_ledger_xml(name, parent, action="Create", mobile=None, email=None, gstin=None,
                      address_lines=None, pincode=None):
    """Ledger master -- used for both customer and vendor push (parent group
    is "Sundry Debtors" or "Sundry Creditors", matching this company's
    existing chart of accounts; those groups must already exist in Tally)."""
    parts = [
        f'<LEDGER NAME="{escape(name)}" ACTION="{escape(action)}">',
        f"<NAME.LIST><NAME>{escape(name)}</NAME></NAME.LIST>",
        f"<PARENT>{escape(parent)}</PARENT>",
        _address_xml(address_lines),
    ]
    if mobile:
        parts.append(f"<LEDGERMOBILE>{escape(mobile)}</LEDGERMOBILE>")
    if email:
        parts.append(f"<EMAIL>{escape(email)}</EMAIL>")
    if gstin:
        parts.append(f"<PARTYGSTIN>{escape(gstin)}</PARTYGSTIN>")
    if pincode:
        parts.append(f"<PINCODE>{escape(pincode)}</PINCODE>")
    parts.append("</LEDGER>")
    return "".join(parts)


def build_stock_item_xml(name, base_units, action="Create", hsn_code=None):
    """Stock item master. `base_units` must name a Unit that already exists
    in Tally (e.g. "Units", "L", "Kg") -- this gateway does not create Units
    on the fly, matching how Tally itself requires units to be defined
    before use."""
    parts = [
        f'<STOCKITEM NAME="{escape(name)}" ACTION="{escape(action)}">',
        f"<NAME.LIST><NAME>{escape(name)}</NAME></NAME.LIST>",
        f"<BASEUNITS>{escape(base_units or 'Units')}</BASEUNITS>",
    ]
    if hsn_code:
        parts.append(f"<GSTHSNCODE>{escape(hsn_code)}</GSTHSNCODE>")
    parts.append("</STOCKITEM>")
    return "".join(parts)


def build_sales_purchase_voucher_xml(voucher_type, date_yyyymmdd, party_ledger, party_amount,
                                      line_ledger, lines, narration=None, action="Create"):
    """Sales/Purchase invoice voucher. `lines` is a list of
    {"stock_item": str, "quantity": float, "rate": float, "amount": float}.
    `party_amount` is the total invoice amount (positive); the party ledger
    entry and the sales/purchase ledger entry are opposite signs, per
    Tally's double-entry convention -- this is the same shape observed on
    a real exported Sales voucher (ALLLEDGERENTRIES.LIST with
    ISDEEMEDPOSITIVE toggling which side is debit vs credit).

    This has NOT been verified with a live import (only the read/export
    side of vouchers was confirmed against the real server) -- see the
    "Known gap" note in TALLY_INTEGRATION.md before relying on it for a
    real posting."""
    is_sales = voucher_type == "Sales"
    party_amount_signed = party_amount if is_sales else -party_amount
    line_amount_signed = -party_amount if is_sales else party_amount

    inventory_entries = "".join(
        "<ALLINVENTORYENTRIES.LIST>"
        f"<STOCKITEMNAME>{escape(line['stock_item'])}</STOCKITEMNAME>"
        f"<RATE>{line.get('rate', 0)}</RATE>"
        f"<AMOUNT>{-line['amount'] if is_sales else line['amount']}</AMOUNT>"
        f"<ACTUALQTY>{line.get('quantity', 0)} Units</ACTUALQTY>"
        f"<BILLEDQTY>{line.get('quantity', 0)} Units</BILLEDQTY>"
        "</ALLINVENTORYENTRIES.LIST>"
        for line in lines
    )

    return (
        f'<VOUCHER VCHTYPE="{escape(voucher_type)}" ACTION="{escape(action)}" OBJVIEW="Invoice Voucher View">'
        f"<DATE>{date_yyyymmdd}</DATE>"
        f"<VOUCHERTYPENAME>{escape(voucher_type)}</VOUCHERTYPENAME>"
        f"<PARTYLEDGERNAME>{escape(party_ledger)}</PARTYLEDGERNAME>"
        f"<NARRATION>{escape(narration or '')}</NARRATION>"
        "<ISINVOICE>Yes</ISINVOICE>"
        "<ALLLEDGERENTRIES.LIST>"
        f"<LEDGERNAME>{escape(party_ledger)}</LEDGERNAME>"
        f"<ISDEEMEDPOSITIVE>{'Yes' if is_sales else 'No'}</ISDEEMEDPOSITIVE>"
        f"<AMOUNT>{party_amount_signed}</AMOUNT>"
        "</ALLLEDGERENTRIES.LIST>"
        "<ALLLEDGERENTRIES.LIST>"
        f"<LEDGERNAME>{escape(line_ledger)}</LEDGERNAME>"
        f"<ISDEEMEDPOSITIVE>{'No' if is_sales else 'Yes'}</ISDEEMEDPOSITIVE>"
        f"<AMOUNT>{line_amount_signed}</AMOUNT>"
        "</ALLLEDGERENTRIES.LIST>"
        f"{inventory_entries}"
        "</VOUCHER>"
    )


def build_payment_receipt_voucher_xml(voucher_type, date_yyyymmdd, party_ledger, bank_ledger,
                                       amount, reference=None, action="Create"):
    """Receipt (customer payment) / Payment (vendor payment) voucher.
    NOT verified with a live import -- see the note on
    build_sales_purchase_voucher_xml above; same caveat applies here."""
    is_receipt = voucher_type == "Receipt"
    party_amount_signed = -amount if is_receipt else amount
    bank_amount_signed = amount if is_receipt else -amount

    return (
        f'<VOUCHER VCHTYPE="{escape(voucher_type)}" ACTION="{escape(action)}">'
        f"<DATE>{date_yyyymmdd}</DATE>"
        f"<VOUCHERTYPENAME>{escape(voucher_type)}</VOUCHERTYPENAME>"
        f"<PARTYLEDGERNAME>{escape(party_ledger)}</PARTYLEDGERNAME>"
        f"<NARRATION>{escape(reference or '')}</NARRATION>"
        "<ALLLEDGERENTRIES.LIST>"
        f"<LEDGERNAME>{escape(party_ledger)}</LEDGERNAME>"
        f"<ISDEEMEDPOSITIVE>{'No' if is_receipt else 'Yes'}</ISDEEMEDPOSITIVE>"
        f"<AMOUNT>{party_amount_signed}</AMOUNT>"
        "</ALLLEDGERENTRIES.LIST>"
        "<ALLLEDGERENTRIES.LIST>"
        f"<LEDGERNAME>{escape(bank_ledger)}</LEDGERNAME>"
        f"<ISDEEMEDPOSITIVE>{'Yes' if is_receipt else 'No'}</ISDEEMEDPOSITIVE>"
        f"<AMOUNT>{bank_amount_signed}</AMOUNT>"
        "</ALLLEDGERENTRIES.LIST>"
        "</VOUCHER>"
    )


# -------------------------------------------------------------- export (poll)
def build_collection_envelope(company_name, collection_name, tally_type, fetch_fields,
                               filter_formula=None):
    filter_block = ""
    if filter_formula:
        filter_block = (
            f'<TDLMESSAGE><SYSTEM TYPE="Formulae" NAME="ConnectorFilter">{escape(filter_formula)}</SYSTEM></TDLMESSAGE>'
        )
        filter_tag = "<FILTER>ConnectorFilter</FILTER>"
    else:
        filter_tag = ""

    return (
        "<ENVELOPE>"
        "<HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>"
        f"<TYPE>Collection</TYPE><ID>{escape(collection_name)}</ID></HEADER>"
        "<BODY><DESC>"
        f"<STATICVARIABLES><SVCURRENTCOMPANY>{escape(company_name)}</SVCURRENTCOMPANY></STATICVARIABLES>"
        "<TDL><TDLMESSAGE>"
        f'<COLLECTION NAME="{escape(collection_name)}" ISMODIFY="No">'
        f"<TYPE>{escape(tally_type)}</TYPE>"
        f"<FETCH>{','.join(fetch_fields)}</FETCH>"
        f"{filter_tag}"
        "</COLLECTION>"
        f"</TDLMESSAGE>{filter_block}</TDL>"
        "</DESC></BODY></ENVELOPE>"
    )


def parse_collection_response(xml_text, object_tag):
    """Returns a list of {field_name: value} dicts, one per object node
    (e.g. one per <LEDGER>). NAME/ALTERID/GUID come from the node's own
    attributes/children; every other requested field is read from its
    child element by tag name, with amounts and placeholder text
    normalized.

    Deliberately scoped to <BODY><DATA><COLLECTION> children only, not a
    tree-wide search for `object_tag` -- the response's own <CMPINFO>
    summary block carries same-named count fields (e.g. a literal
    <LEDGER>3</LEDGER> meaning "3 ledgers exist", not a ledger record) that
    a blind root.iter(object_tag) would wrongly pick up as a phantom,
    nameless record."""
    root = ET.fromstring(_strip_invalid_xml_charrefs(xml_text))
    collection = root.find(".//DATA/COLLECTION")
    records = []
    for node in (collection.findall(object_tag) if collection is not None else []):
        record = {"name": node.get("NAME")}
        for child in node:
            tag = child.tag
            if tag in ("LANGUAGENAME.LIST", "ADDRESS.LIST"):
                continue
            type_attr = child.get("TYPE", "")
            if type_attr == "Amount":
                record[tag] = _clean_amount(child.text)
            elif type_attr == "Number":
                text = _clean_text(child.text)
                record[tag] = int(text) if text else 0
            else:
                record[tag] = _clean_text(child.text)
        address_list = node.find("ADDRESS.LIST")
        if address_list is not None:
            record["ADDRESS.LIST"] = [
                a.text for a in address_list.findall("ADDRESS") if a.text
            ]
        records.append(record)
    return records

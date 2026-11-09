"""
Post a saved invoice to the Oracle staging tables (deploy/database/invoice_staging_schema.sql).

Flow: needs_review -> draft -> saved -> posted. Only a "saved" invoice can be posted, and
a posted invoice is read-only in the app. One post writes, in ONE transaction:

    XXDA_INVOICE_HEADERS          one row (status POSTED)
    XXDA_INVOICE_LINES            one row per line item
    XXDA_INVOICE_FIELD_AUDIT      OCR value vs final value for every field
    XXDA_INVOICE_STATUS_HISTORY   the POST step

If anything fails, nothing is written, the invoice stays "saved" and the error is kept
so the user can post it again. Moving the rows on to Oracle EBS (AP open interface) is a
separate step on the database side; the app never touches EBS tables.

The app only INSERTs here. It never creates or changes database objects.
"""

import json
import logging
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

from app import config
from app.errors import ServiceError
from app.services import invoice_data_service, invoice_extraction_service, oracle_db_service

logger = logging.getLogger(__name__)

HEADERS_TABLE = "XXDA_INVOICE_HEADERS"
LINES_TABLE = "XXDA_INVOICE_LINES"
FIELD_AUDIT_TABLE = "XXDA_INVOICE_FIELD_AUDIT"
HISTORY_TABLE = "XXDA_INVOICE_STATUS_HISTORY"

# Constraints of the staging schema that tell the user something specific.
DOCUMENT_UNIQUE_CONSTRAINT = "XXDA_INV_HDR_DOC_UK"
DUPLICATE_CONSTRAINT = "XXDA_INV_HDR_DUP_UK"

# App field -> (staging column, kind, maximum length for text). Kinds: text, date, number.
FIELD_COLUMNS = {
    "vendor_name": ("SELLER_NAME", "text", 240),
    "supplier_ntn": ("SELLER_NTN", "text", 20),
    "supplier_strn": ("SELLER_STRN", "text", 30),
    "supplier_address": ("SELLER_ADDRESS", "text", 500),
    "buyer_name": ("BUYER_NAME", "text", 240),
    "buyer_ntn": ("BUYER_NTN", "text", 20),
    "buyer_strn": ("BUYER_STRN", "text", 30),
    "buyer_address": ("BUYER_ADDRESS", "text", 500),
    "invoice_number": ("SALE_INVOICE_NO", "text", 50),
    "invoice_date": ("INVOICE_DATE", "date", None),
    "due_date": ("DUE_DATE", "date", None),
    "fbr_invoice_number": ("FBR_INVOICE_NO", "text", 100),
    "po_number": ("PO_NUMBER", "text", 50),
    "dc_number": ("DC_NUMBER", "text", 50),
    "gate_pass_number": ("GATE_PASS_NO", "text", 50),
    "contract_number": ("CONTRACT_NO", "text", 50),
    "currency": ("CURRENCY_CODE", "text", 15),
    "subtotal": ("TOTAL_VALUE_EXCL_TAX", "number", None),
    "discount": ("DISCOUNT_AMOUNT", "number", None),
    "sales_tax_rate": ("TAX_RATE", "number", None),
    "tax": ("TOTAL_SALES_TAX", "number", None),
    "further_tax": ("TOTAL_FURTHER_TAX", "number", None),
    "total_due": ("GRAND_TOTAL", "number", None),
    "total_quantity": ("TOTAL_QUANTITY", "number", None),
    "amount_in_words": ("AMOUNT_IN_WORDS", "text", 500),
}

# Line item field -> (staging column, kind, maximum length for text).
LINE_COLUMNS = {
    "hs_code": ("HS_CODE", "text", 12),
    "description": ("DESCRIPTION", "text", 500),
    "quantity": ("QUANTITY", "number", None),
    "uom": ("UOM", "text", 30),
    "rate": ("UNIT_PRICE", "number", None),
    "amount": ("VALUE_EXCL_TAX", "number", None),
    "discount": ("DISCOUNT_AMOUNT", "number", None),
    "tax_rate": ("TAX_RATE", "number", None),
    "sales_tax": ("SALES_TAX", "number", None),
    "further_tax": ("FURTHER_TAX", "number", None),
    "amount_incl_tax": ("TOTAL_VALUE", "number", None),
}

# Every app field must have a column, so nothing the user corrected is left behind.
assert set(FIELD_COLUMNS) == set(invoice_extraction_service.FIELD_KEYS)
assert set(LINE_COLUMNS) == set(invoice_extraction_service.LINE_ITEM_KEYS)


# ---------------------------------------------------------------------------
# Value conversion
# ---------------------------------------------------------------------------
def text_or_none(value):
    text = "" if value is None else str(value).strip()
    return text or None


def to_number(value):
    text = text_or_none(value)
    if text is None:
        return None
    try:
        return Decimal(text.replace(",", ""))
    except InvalidOperation:
        return None


def to_date(value):
    text = text_or_none(value)
    if text is None:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def to_timestamp(value):
    """ISO timestamp from the app (UTC) -> naive UTC datetime for a TIMESTAMP column."""
    text = text_or_none(value)
    if text is None:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
    return moment


def convert(value, kind):
    if kind == "date":
        return to_date(value)
    if kind == "number":
        return to_number(value)
    return text_or_none(value)


def same_value(first, second):
    return (text_or_none(first) or "") == (text_or_none(second) or "")


def average(numbers):
    numbers = [number for number in numbers if isinstance(number, (int, float)) and not isinstance(number, bool)]
    return round(sum(numbers) / len(numbers), 4) if numbers else None


# ---------------------------------------------------------------------------
# Building the rows
# ---------------------------------------------------------------------------
def check_lengths(values, line_items):
    """The staging columns are shorter than the app's limits for some fields: refuse instead of cutting."""
    errors = []
    for key, (column, kind, limit) in FIELD_COLUMNS.items():
        text = text_or_none(values.get(key))
        if kind == "text" and text and len(text) > limit:
            label = invoice_data_service.FIELD_LABELS[key]
            errors.append({"field": key, "message": f"{label} is too long for the database (maximum {limit} characters)."})
    for position, item in enumerate(line_items):
        for key, (column, kind, limit) in LINE_COLUMNS.items():
            text = text_or_none(item.get(key))
            if kind == "text" and text and len(text) > limit:
                label = invoice_data_service.LINE_LABELS[key]
                errors.append({
                    "field": f"line_items.{position}.{key}",
                    "message": f"{label} of line {position + 1} is too long for the database (maximum {limit} characters).",
                })
    if errors:
        raise ServiceError("Some values do not fit the database columns.", 422, "INVALID_INVOICE_DATA", errors)


def line_source(item, extracted_items, position):
    """OCR = unchanged from what OCR read, EDITED = changed, ADDED = a line the user added."""
    if position >= len(extracted_items):
        return "ADDED"
    original = extracted_items[position]
    unchanged = all(same_value(item.get(key), original.get(key)) for key in LINE_COLUMNS)
    return "OCR" if unchanged else "EDITED"


def build_rows(snapshot):
    """Turn the saved invoice and its document metadata into staging rows."""
    metadata = snapshot["metadata"]
    record = snapshot["record"]
    values = record.get("values") or {}
    extracted = record.get("extracted") or {}
    line_items = record.get("line_items") or []
    extracted_items = record.get("extracted_line_items") or []
    pages = record.get("pages") or [snapshot["page"]]
    check_lengths(values, line_items)

    reference = metadata.get("reference_number")
    if not reference:
        raise ServiceError(
            "This document has no reference number yet. Restart the server and try again.", 409, "REFERENCE_MISSING",
        )

    extracted_fields = [key for key in FIELD_COLUMNS if text_or_none((extracted.get(key) or {}).get("value"))]
    corrected_fields = [
        key for key in FIELD_COLUMNS if not same_value(values.get(key), (extracted.get(key) or {}).get("value"))
    ]
    sources = [line_source(item, extracted_items, position) for position, item in enumerate(line_items)]
    lines_corrected = sum(1 for source in sources if source != "OCR") + max(0, len(extracted_items) - len(line_items))

    ocr = metadata.get("ocr") or {}
    models = ocr.get("models") or []
    header = {
        "DOCUMENT_ID": metadata["document_id"],
        "DOCUMENT_REF": reference,
        "FIRST_PAGE": pages[0],
        "LAST_PAGE": pages[-1],
        "SOURCE_FILE": text_or_none(metadata.get("original_filename")),
        "SOURCE_SHA256": text_or_none(metadata.get("sha256")),
        "EXTRACTION_METHOD": "OCR",
        "OCR_CONFIDENCE": average([(extracted.get(key) or {}).get("confidence") for key in extracted_fields]),
        "NEEDS_REVIEW": 0,
        "EXTRACTED_JSON": json.dumps(
            {"fields": extracted, "line_items": extracted_items}, ensure_ascii=False, default=str,
        ),
        "REVIEWED_BY": config.APP_USER_NAME,
        "REVIEWED_AT": to_timestamp(record.get("saved_at")),
        "UPLOADED_AT": to_timestamp(metadata.get("uploaded_at")),
        "PAGE_COUNT": len(metadata.get("pages") or []),
        "OCR_ENGINE": text_or_none(ocr.get("engine")),
        "OCR_MODEL": text_or_none(" / ".join(models))[:100] if models else None,
        "OCR_DPI": (metadata.get("conversion") or {}).get("dpi"),
        "OCR_COMPLETED_AT": to_timestamp(ocr.get("completed_at")),
        "APP_VERSION": config.APP_VERSION,
        "FIELDS_EXTRACTED": len(extracted_fields),
        "FIELDS_CORRECTED": len(corrected_fields),
        "LINES_EXTRACTED": len(extracted_items),
        "LINES_CORRECTED": lines_corrected,
        "POST_ATTEMPTS": int((record.get("posting") or {}).get("attempts") or 0) + 1,
        "STATUS": "POSTED",
        "POSTED_BY": config.APP_USER_NAME,
    }
    for key, (column, kind, _) in FIELD_COLUMNS.items():
        header[column] = convert(values.get(key), kind)
    header["CURRENCY_CODE"] = (header["CURRENCY_CODE"] or "PKR").upper()

    lines = []
    for position, item in enumerate(line_items):
        original = extracted_items[position] if position < len(extracted_items) else {}
        row = {
            "LINE_NO": position + 1,
            "LINE_TYPE": "ITEM",
            "OCR_CONFIDENCE": item.get("confidence") if sources[position] == "OCR" else None,
            "LINE_SOURCE": sources[position],
            "SOURCE_PAGE": item.get("page") or original.get("page"),
            "EXTRACTED_JSON": json.dumps(original, ensure_ascii=False, default=str) if original else None,
        }
        for key, (column, kind, _) in LINE_COLUMNS.items():
            row[column] = convert(item.get(key), kind)
        lines.append(row)

    audit = []
    for key, (column, _, _) in FIELD_COLUMNS.items():
        info = extracted.get(key) or {}
        source = info.get("source") or {}
        audit.append({
            "FIELD_KEY": key,
            "COLUMN_NAME": column,
            "EXTRACTED_VALUE": (text_or_none(info.get("value")) or "")[:4000] or None,
            "FINAL_VALUE": (text_or_none(values.get(key)) or "")[:4000] or None,
            "WAS_CORRECTED": 1 if key in corrected_fields else 0,
            "OCR_CONFIDENCE": info.get("confidence"),
            "SOURCE_PAGE": source.get("page"),
            "SOURCE_LINE": source.get("line"),
        })
    return header, lines, audit


# ---------------------------------------------------------------------------
# SQL (parameterized INSERTs only)
# ---------------------------------------------------------------------------
def insert_sql(table, columns, returning=None):
    names = ", ".join(columns)
    binds = ", ".join(f":{column.lower()}" for column in columns)
    sql = f"INSERT INTO {oracle_db_service.qualified_name(table)} ({names}) VALUES ({binds})"
    if returning:
        sql += f" RETURNING {returning} INTO :new_id"
    return sql


def binds(row):
    return {column.lower(): value for column, value in row.items()}


def write_rows(header, lines, audit):
    """Insert all rows in one transaction. Returns the new staging INVOICE_ID."""

    def work(cursor):
        new_id = cursor.var(int)
        cursor.execute(insert_sql(HEADERS_TABLE, list(header), "INVOICE_ID"), dict(binds(header), new_id=new_id))
        value = new_id.getvalue()
        invoice_id = int(value[0] if isinstance(value, list) else value)

        if lines:
            columns = ["INVOICE_ID", *lines[0]]
            cursor.executemany(insert_sql(LINES_TABLE, columns), [dict(binds(row), invoice_id=invoice_id) for row in lines])
        if audit:
            columns = ["INVOICE_ID", *audit[0]]
            cursor.executemany(
                insert_sql(FIELD_AUDIT_TABLE, columns), [dict(binds(row), invoice_id=invoice_id) for row in audit],
            )
        history = {
            "INVOICE_ID": invoice_id,
            "FROM_STATUS": None,
            "TO_STATUS": "POSTED",
            "ACTION": "POST" if header["POST_ATTEMPTS"] == 1 else "REPOST",
            "MESSAGE": None,
            "CHANGED_BY": config.APP_USER_NAME,
            "CHANGED_FROM": config.APP_NAME[:100],
        }
        cursor.execute(insert_sql(HISTORY_TABLE, list(history)), binds(history))
        return invoice_id

    return oracle_db_service.run_in_transaction(work)


def find_posted_id(document_id, first_page):
    """Staging INVOICE_ID of an invoice already posted (used when a previous post committed but the app did not hear back)."""
    rows = oracle_db_service.fetch_all(
        f"SELECT INVOICE_ID FROM {oracle_db_service.qualified_name(HEADERS_TABLE)} "
        "WHERE DOCUMENT_ID = :document_id AND FIRST_PAGE = :first_page",
        {"document_id": document_id, "first_page": first_page},
    )
    return int(rows[0]["invoice_id"]) if rows else None


# ---------------------------------------------------------------------------
# Public function
# ---------------------------------------------------------------------------
def post_invoice(document_id, page=None):
    """Post the saved invoice that contains the page. Returns the invoice data with status "posted"."""
    if not oracle_db_service.is_configured():
        raise ServiceError(
            "The database is not set up. Set ORACLE_ENABLED, ORACLE_USER, ORACLE_PASSWORD and ORACLE_DSN in .env.",
            503, "ORACLE_NOT_CONFIGURED",
        )
    snapshot = invoice_data_service.posting_snapshot(document_id, page)
    record = snapshot["record"]
    first_page = record["pages"][0]
    header, lines, audit = build_rows(snapshot)

    try:
        staging_id = write_rows(header, lines, audit)
    except ServiceError as error:
        constraint = (error.details or {}).get("constraint") if isinstance(error.details, dict) else None
        if constraint == DOCUMENT_UNIQUE_CONSTRAINT:
            # Already in the database (an earlier post committed): mark it posted instead of failing.
            staging_id = find_posted_id(document_id, first_page)
            logger.info("Document %s: invoice on page %s was already posted (id %s)", document_id, first_page, staging_id)
            return invoice_data_service.record_posting_result(document_id, first_page, record.get("saved_at"), staging_id)
        if constraint == DUPLICATE_CONSTRAINT:
            error = ServiceError(
                "This supplier's invoice number is already in the database. Check for a duplicate invoice.",
                409, "INVOICE_DUPLICATE",
            )
        invoice_data_service.record_posting_result(
            document_id, first_page, record.get("saved_at"), error=error.message,
        )
        raise error

    logger.info(
        "Document %s: invoice on pages %s posted to the database (staging id %s, %d line(s))",
        document_id, record["pages"], staging_id, len(lines),
    )
    return invoice_data_service.record_posting_result(document_id, first_page, record.get("saved_at"), staging_id)

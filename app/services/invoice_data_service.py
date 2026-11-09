"""
Reviewed invoice data of a document: split into invoices, extract, validate and save.

A document (for example a scanned batch of 160 pages) can hold many invoices.
Each invoice is one page or several consecutive pages; the review screen shows
the invoice of the page that is open.

Stored per document in storage/documents/<id>/invoice_data.json:
    version       2
    ocr_signature identifies the OCR results the invoices were made from
    page_count    number of pages when the invoices were made
    page_breaks   {page: true | false}: the user's own "starts a new invoice" choices
    invoices      {first page: invoice record}

Invoice record:
    pages                the page numbers of the invoice
    status               needs_review | draft | saved | posted (sent to the database, read-only)
    posting              result of "Post to database": staging_id, posted_at, attempts, last_error
    values               the current (possibly corrected) field values
    line_items           the current (possibly corrected) line items
    extracted            what was read from the OCR text, with source line and confidence
    extracted_line_items what was read from the OCR text
    ocr_ready            True when at least one page of the invoice has OCR text

Rules:
- Where an invoice starts is found from the OCR text (invoice_extraction_service.split_into_invoices).
  The user can correct it ("start a new invoice here" / "join with the previous invoice").
- Data is extracted automatically the first time it is requested after OCR.
- If OCR runs again, "extracted" is refreshed. The values are replaced only
  while the user has not saved anything, so corrections are never lost.
- An invoice the user saved (draft or saved) keeps its pages when OCR changes.
- "Save as draft" keeps whatever is entered. "Save changes" also requires the
  mandatory fields.
"""

import copy
import logging
import re
import threading
from datetime import date

from app import config
from app.errors import ServiceError
from app.services import (
    document_metadata_service,
    document_validation_service,
    invoice_extraction_service,
    ocr_results_service,
    oracle_db_service,
    storage_service,
)

logger = logging.getLogger(__name__)

INVOICE_DATA_FILENAME = "invoice_data.json"
STORE_VERSION = 2
FIELD_KEYS = invoice_extraction_service.FIELD_KEYS
LINE_ITEM_KEYS = invoice_extraction_service.LINE_ITEM_KEYS
LINE_TEXT_KEYS = invoice_extraction_service.LINE_TEXT_KEYS
LINE_NUMBER_KEYS = invoice_extraction_service.LINE_NUMBER_KEYS
DATE_KEYS = ("invoice_date", "due_date")
MONEY_KEYS = ("subtotal", "discount", "tax", "further_tax", "total_due")
NUMBER_KEYS = ("sales_tax_rate", "total_quantity")
LONG_TEXT_KEYS = ("supplier_address", "buyer_address", "amount_in_words")
TEXT_KEYS = tuple(key for key in FIELD_KEYS if key not in DATE_KEYS + MONEY_KEYS + NUMBER_KEYS + ("currency",))
REQUIRED_KEYS = ("vendor_name", "invoice_number", "invoice_date", "due_date", "total_due")
USER_STATUSES = ("draft", "saved")
# Statuses whose invoice keeps its pages when OCR changes. "posted" = sent to the database (read-only).
KEPT_STATUSES = ("draft", "saved", "posted")
FIELD_LABELS = {
    "vendor_name": "Vendor name",
    "supplier_ntn": "Supplier NTN",
    "supplier_strn": "Supplier STRN",
    "supplier_address": "Supplier address",
    "buyer_name": "Buyer name",
    "buyer_ntn": "Buyer NTN",
    "buyer_strn": "Buyer STRN",
    "buyer_address": "Buyer address",
    "invoice_number": "Invoice number",
    "invoice_date": "Invoice date",
    "due_date": "Due date",
    "fbr_invoice_number": "FBR invoice number",
    "po_number": "PO number",
    "dc_number": "DC number",
    "gate_pass_number": "Gate pass number",
    "contract_number": "Contract number",
    "currency": "Currency",
    "subtotal": "Subtotal",
    "discount": "Discount",
    "sales_tax_rate": "Sales tax %",
    "tax": "Sales tax",
    "further_tax": "Further tax",
    "total_due": "Total due",
    "total_quantity": "Total quantity",
    "amount_in_words": "Amount in words",
}
LINE_LABELS = {
    "description": "Description",
    "hs_code": "HS code",
    "uom": "UOM",
    "quantity": "Quantity",
    "rate": "Rate",
    "amount": "Value excl. tax",
    "tax_rate": "Tax %",
    "sales_tax": "Sales tax",
    "further_tax": "Further tax",
    "discount": "Discount",
    "amount_incl_tax": "Value incl. tax",
}
MAX_TEXT_LENGTH = 200
MAX_LONG_TEXT_LENGTH = 500
MAX_CODE_LENGTH = 50
MAX_DESCRIPTION_LENGTH = 500
NUMBER_PATTERN = re.compile(r"^-?\d{1,12}(?:\.\d{1,4})?$")
CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")

_lock = threading.RLock()


def invoice_data_path(document_id):
    """Path of invoice_data.json inside the document folder."""
    return storage_service.get_document_dir(document_id) / INVOICE_DATA_FILENAME


def compute_ocr_signature(document_id):
    """Changes whenever an OCR result file is written; 'none' when there are no results."""
    ocr_dir = storage_service.get_ocr_dir(document_id)
    parts = []
    if ocr_dir.is_dir():
        for path in sorted(ocr_dir.glob("page_*.json")):
            stat = path.stat()
            parts.append(f"{path.name}:{stat.st_mtime_ns}:{stat.st_size}")
    return "|".join(parts) or "none"


def read_stored(document_id):
    """Read invoice_data.json. Return None if it does not exist or cannot be read."""
    path = invoice_data_path(document_id)
    if not path.is_file():
        return None
    try:
        return storage_service.read_json(path)
    except (OSError, ValueError) as error:
        logger.error("Document %s: unreadable invoice data: %s", document_id, error)
        return None


def write_stored(document_id, store):
    """Save invoice_data.json (written safely through a temporary file)."""
    storage_service.write_json_atomic(invoice_data_path(document_id), store)


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------
def values_from_extraction(extraction):
    """Take only the field values out of an extraction result (the starting values the user can correct)."""
    return {key: extraction["fields"][key]["value"] for key in FIELD_KEYS}


def clean_extracted_items(items):
    """Keep only the line item fields that are stored (the columns, confidence and page)."""
    return [
        dict(
            {key: item.get(key, "") for key in LINE_ITEM_KEYS},
            confidence=item.get("confidence"),
            page=item.get("page"),
        )
        for item in items
    ]


def empty_extraction():
    """An extraction with empty fields. Used when there is no OCR text yet."""
    return {
        "fields": {
            key: {"value": "", "confidence": None, "needs_check": False, "source": None}
            for key in FIELD_KEYS
        },
        "line_items": [],
        "date_order": config.INVOICE_DATE_ORDER,
    }


def new_record(extraction, pages, ocr_ready):
    """Create the stored record of one invoice, with the extracted data copied as the starting values."""
    return {
        "pages": pages,
        "status": "needs_review",
        "ocr_ready": ocr_ready,
        "date_order": extraction["date_order"],
        "extracted": extraction["fields"],
        "extracted_line_items": clean_extracted_items(extraction["line_items"]),
        "values": values_from_extraction(extraction),
        "line_items": clean_extracted_items(extraction["line_items"]),
        "extracted_at": storage_service.utc_now(),
        "saved_at": None,
    }


def empty_store():
    """A new, empty invoice_data.json structure."""
    return {"version": STORE_VERSION, "ocr_signature": None, "page_count": None, "page_breaks": {}, "invoices": {}}


def load_store(document_id):
    """The stored invoices, or None. Data saved before invoices were split per page is kept as the first invoice."""
    stored = read_stored(document_id)
    if stored is None:
        return None
    if stored.get("version") == STORE_VERSION:
        return stored
    store = empty_store()
    if stored.get("status") in KEPT_STATUSES:
        record = {key: stored.get(key) for key in (
            "status", "values", "line_items", "extracted", "extracted_line_items",
            "date_order", "extracted_at", "saved_at",
        )}
        record["pages"] = None  # Not known: takes the pages found for page 1.
        store["invoices"]["1"] = record
    return store


def sorted_records(store):
    """All invoice records in page order."""
    return [store["invoices"][key] for key in sorted(store["invoices"], key=int)]


def record_for_page(store, page_number):
    """Find the invoice record that contains this page. Return None if there is none."""
    for record in sorted_records(store):
        if page_number in (record.get("pages") or ()):
            return record
    return None


# ---------------------------------------------------------------------------
# Splitting the document into invoices
# ---------------------------------------------------------------------------
def load_page_results(document_id, metadata):
    """{page number: OCR result with text, or None when the page has not been read}."""
    results = {}
    for page in metadata.get("pages", []):
        result = ocr_results_service.read_page_result_file(document_id, page["page_number"])
        ok = result and result.get("status") in ("completed", "no_text")
        results[page["page_number"]] = result if ok else None
    return results


def forced_page_breaks(store):
    """Invoices the user saved keep their pages; the user's own page breaks win over everything."""
    forced = {}
    for record in store["invoices"].values():
        pages = record.get("pages")
        if record.get("status") in KEPT_STATUSES and pages:
            forced[pages[0]] = True
            forced.update({page: False for page in pages[1:]})
            forced[pages[-1] + 1] = True
    forced.update({int(page): bool(starts) for page, starts in store.get("page_breaks", {}).items()})
    return forced


def refresh_invoices(document_id, metadata, store, signature):
    """Split the pages into invoices and extract each invoice's data from its pages."""
    results = load_page_results(document_id, metadata)
    page_numbers = sorted(results)
    pages = [
        (number, invoice_extraction_service.page_signals(results[number]) if results[number] else None)
        for number in page_numbers
    ]
    groups = invoice_extraction_service.split_into_invoices(pages, forced_page_breaks(store))

    invoices = {}
    for group in groups:
        group_results = [results[number] for number in group if results[number]]
        extraction = (
            invoice_extraction_service.extract_invoice_data(group_results) if group_results else empty_extraction()
        )
        key = str(group[0])
        old = store["invoices"].get(key)
        if old and old.get("status") in KEPT_STATUSES:
            record = dict(old)
            record["extracted"] = extraction["fields"]
            record["extracted_line_items"] = clean_extracted_items(extraction["line_items"])
            record["date_order"] = extraction["date_order"]
            record["extracted_at"] = storage_service.utc_now()
            record["pages"] = group
            record["ocr_ready"] = bool(group_results)
        else:
            record = new_record(extraction, group, bool(group_results))
        invoices[key] = record

    store["invoices"] = invoices
    store["ocr_signature"] = signature
    store["page_count"] = len(page_numbers)
    store["rules_version"] = invoice_extraction_service.RULES_VERSION
    store["version"] = STORE_VERSION
    store.setdefault("page_breaks", {})
    return store


def current_store(document_id, metadata):
    """
    The invoices of the document, refreshed when the OCR results changed.
    Returns (store, changed). Nothing is written while the document has no OCR results and no saved data.
    """
    store = load_store(document_id)
    signature = compute_ocr_signature(document_id)
    page_count = len(metadata.get("pages", []))
    if (
        store is not None
        and store.get("ocr_signature") == signature
        and store.get("page_count") == page_count
        and store.get("rules_version") == invoice_extraction_service.RULES_VERSION
    ):
        return store, False
    persist = store is not None or signature != "none"
    store = refresh_invoices(document_id, metadata, store or empty_store(), signature)
    if persist:
        write_stored(document_id, store)
        logger.info("Document %s: %d invoice(s) found", document_id, len(store["invoices"]))
    return store, persist


def review_summary(store):
    """Count the invoices of the document by status (saved, draft, needs review)."""
    records = sorted_records(store)
    posted = sum(1 for record in records if record["status"] == "posted")
    saved = sum(1 for record in records if record["status"] == "saved") + posted  # posted invoices were saved first
    draft = sum(1 for record in records if record["status"] == "draft")
    return {
        "invoices": len(records), "saved": saved, "draft": draft, "posted": posted,
        "needs_review": len(records) - saved - draft,
    }


def update_review_status(document_id, metadata, store):
    """Document list status: saved when every invoice is saved, draft when some work was saved."""
    summary = review_summary(store)
    if summary["invoices"] and summary["saved"] == summary["invoices"]:
        status = "saved"
    elif summary["saved"] or summary["draft"]:
        status = "draft"
    else:
        status = "needs_review"
    counts = {key: summary[key] for key in ("invoices", "saved", "draft")}
    if metadata.get("data_status") == status and metadata.get("data_counts") == counts:
        return

    def apply(data):
        data["data_status"] = status
        data["data_counts"] = counts

    document_metadata_service.update_metadata(document_id, apply)


def resolve_page(metadata, raw_page):
    """Validated page number (default 1), or None for a document without pages."""
    pages = metadata.get("pages", [])
    if not pages:
        return None
    if raw_page is None:
        return 1
    return document_validation_service.validate_page_number(raw_page, len(pages))


def complete_items(items):
    """Line items with every column (items saved before a column existed get an empty value)."""
    return [dict({key: "" for key in LINE_ITEM_KEYS}, **item) for item in items or []]


def build_response(document_id, store, page_number):
    """Build the data the review screen needs for the invoice that contains the given page."""
    records = sorted_records(store)
    record = record_for_page(store, page_number) if page_number else None
    if record is None:
        record = new_record(empty_extraction(), [], False)
    pages = record["pages"]
    return {
        "document_id": document_id,
        "page": page_number,
        "status": record["status"],
        "ocr_ready": bool(record.get("ocr_ready")),
        "values": {key: (record.get("values") or {}).get(key, "") for key in FIELD_KEYS},
        "extracted": record["extracted"],
        "line_items": complete_items(record["line_items"]),
        "extracted_line_items": complete_items(record["extracted_line_items"]),
        "date_order": record.get("date_order"),
        "extracted_at": record.get("extracted_at"),
        "saved_at": record.get("saved_at"),
        "invoice": {
            "index": records.index(record) + 1 if record in records else None,
            "count": len(records),
            "pages": pages,
            "first_pages": [item["pages"][0] for item in records],
            "statuses": [item["status"] for item in records],  # same order as first_pages
        },
        "summary": review_summary(store),
        "posting": posting_info(record),
    }


def posting_info(record):
    """What the review screen needs for the "Post to database" button."""
    posting = record.get("posting") or {}
    return {
        "enabled": oracle_db_service.is_configured(),
        "posted": record.get("status") == "posted",
        "posted_at": posting.get("posted_at"),
        "posted_by": posting.get("posted_by"),
        "staging_id": posting.get("staging_id"),
        "attempts": posting.get("attempts", 0),
        "last_error": posting.get("last_error"),
        "last_attempt_at": posting.get("last_attempt_at"),
    }


def refuse_if_posted(record, action):
    """Posted invoices are read-only: the database copy must stay the same as the file."""
    if record is not None and record.get("status") == "posted":
        raise ServiceError(
            f"This invoice was posted to the database and cannot be {action}.", 409, "INVOICE_POSTED",
        )


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------
def get_invoice_data(document_id, page=None):
    """The invoice that contains the page (default: page 1), extracted from the OCR text when needed."""
    with _lock:
        metadata = document_metadata_service.load_metadata(document_id)
        page_number = resolve_page(metadata, page)
        store, changed = current_store(document_id, metadata)
        if changed:
            update_review_status(document_id, metadata, store)
        return build_response(document_id, store, page_number)


def re_extract_invoice_data(document_id, page=None):
    """Throw away the values of the page's invoice and read them from the OCR text again."""
    with _lock:
        metadata = document_metadata_service.load_metadata(document_id)
        page_number = resolve_page(metadata, page)
        store, _ = current_store(document_id, metadata)
        record = record_for_page(store, page_number) if page_number else None
        refuse_if_posted(record, "read again")
        if record is None or not record.get("ocr_ready"):
            raise ServiceError("OCR results are not available yet. Run OCR first.", 409, "OCR_NOT_READY")
        # Without a saved status the invoice no longer holds its pages, and its values are read again.
        record["status"] = "needs_review"
        store = refresh_invoices(document_id, metadata, store, compute_ocr_signature(document_id))
        write_stored(document_id, store)
        update_review_status(document_id, metadata, store)
        logger.info("Document %s: invoice on page %s re-extracted on request", document_id, page_number)
        return build_response(document_id, store, page_number)


def set_page_break(document_id, payload):
    """
    JSON body: {"page": 5, "starts_invoice": true | false}

    true: a new invoice starts on this page. false: this page belongs to the invoice before it.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("starts_invoice"), bool):
        raise ServiceError('Send {"page": <number>, "starts_invoice": true | false}.', 422, "INVALID_PAGE_BREAK")
    with _lock:
        metadata = document_metadata_service.load_metadata(document_id)
        page_number = resolve_page(metadata, payload.get("page"))
        if page_number is None:
            raise ServiceError("This document has no page images yet.", 409, "PAGES_NOT_READY")
        if page_number == 1 and not payload["starts_invoice"]:
            raise ServiceError("The first page always starts an invoice.", 422, "INVALID_PAGE_BREAK")
        store, _ = current_store(document_id, metadata)
        refuse_if_posted(record_for_page(store, page_number), "split or joined")
        if not payload["starts_invoice"]:
            refuse_if_posted(record_for_page(store, page_number - 1), "split or joined")
        store["page_breaks"][str(page_number)] = payload["starts_invoice"]
        store = refresh_invoices(document_id, metadata, store, compute_ocr_signature(document_id))
        write_stored(document_id, store)
        update_review_status(document_id, metadata, store)
        logger.info(
            "Document %s: page %s %s", document_id, page_number,
            "starts a new invoice" if payload["starts_invoice"] else "joined to the previous invoice",
        )
        return build_response(document_id, store, page_number)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def text_of(value):
    """Convert a value to trimmed text. None becomes ""."""
    return "" if value is None else str(value).strip()


def clean_values(raw_values, errors):
    """Check and clean the field values sent by the review screen. Problems are added to the errors list."""
    values = {}
    for key in FIELD_KEYS:
        text = text_of(raw_values.get(key))
        label = FIELD_LABELS[key]
        limit = MAX_LONG_TEXT_LENGTH if key in LONG_TEXT_KEYS else MAX_TEXT_LENGTH
        if key in TEXT_KEYS and len(text) > limit:
            errors.append({"field": key, "message": f"{label} is too long (maximum {limit} characters)."})
        elif key in DATE_KEYS and text:
            try:
                date.fromisoformat(text)
            except ValueError:
                errors.append({"field": key, "message": f"{label} must be a valid date."})
        elif key in MONEY_KEYS and text and not NUMBER_PATTERN.match(text):
            errors.append({"field": key, "message": f"{label} must be a number such as 1234.50."})
        elif key in NUMBER_KEYS and text and not NUMBER_PATTERN.match(text):
            errors.append({"field": key, "message": f"{label} must be a number."})
        elif key == "currency" and text:
            text = text.upper()
            if not CURRENCY_PATTERN.match(text):
                errors.append({"field": key, "message": "Currency must be a 3-letter code such as USD."})
        values[key] = text
    return values


def clean_line_items(raw_items, errors):
    """Check and clean the line items sent by the review screen. Problems are added to the errors list."""
    if not isinstance(raw_items, list):
        errors.append({"field": "line_items", "message": "Line items must be a list."})
        return []
    if len(raw_items) > invoice_extraction_service.MAX_LINE_ITEMS:
        errors.append({
            "field": "line_items",
            "message": f"A maximum of {invoice_extraction_service.MAX_LINE_ITEMS} line items is allowed.",
        })
        return []

    items = []
    for position, raw in enumerate(raw_items, start=1):
        if not isinstance(raw, dict):
            errors.append({"field": f"line_items.{position - 1}", "message": f"Line item {position} is not valid."})
            continue
        item = {}
        for key in LINE_ITEM_KEYS:
            text = text_of(raw.get(key))
            limit = MAX_DESCRIPTION_LENGTH if key == "description" else MAX_CODE_LENGTH
            if key in LINE_TEXT_KEYS and len(text) > limit:
                errors.append({
                    "field": f"line_items.{position - 1}.{key}",
                    "message": f"{LINE_LABELS[key]} of line {position} is too long (maximum {limit} characters).",
                })
            elif key in LINE_NUMBER_KEYS and text and not NUMBER_PATTERN.match(text):
                errors.append({
                    "field": f"line_items.{position - 1}.{key}",
                    "message": f"{LINE_LABELS[key]} of line {position} must be a number.",
                })
            item[key] = text
        confidence = raw.get("confidence")
        item["confidence"] = confidence if isinstance(confidence, (int, float)) and not isinstance(confidence, bool) else None
        page = raw.get("page")
        item["page"] = page if isinstance(page, int) and not isinstance(page, bool) else None
        if any(item[key] for key in LINE_ITEM_KEYS):
            items.append(item)
    return items


def save_invoice_data(document_id, payload):
    """
    Validate and store the user's values for the invoice that contains payload["page"] (default 1).

    status: "draft" or "saved". payload["pages"] (optional): the pages the form was showing;
    if the invoice's pages changed meanwhile the save is refused, so values never land on the wrong invoice.
    """
    if not isinstance(payload, dict):
        raise ServiceError("Send the invoice data as a JSON object.", 422, "INVALID_INVOICE_DATA")
    status = payload.get("status")
    if status not in USER_STATUSES:
        raise ServiceError('Status must be "draft" or "saved".', 422, "INVALID_INVOICE_DATA")
    raw_values = payload.get("values")
    if not isinstance(raw_values, dict):
        raise ServiceError("Send the field values as a JSON object.", 422, "INVALID_INVOICE_DATA")

    errors = []
    values = clean_values(raw_values, errors)
    line_items = clean_line_items(payload.get("line_items", []), errors)
    if status == "saved":
        for key in REQUIRED_KEYS:
            if not values[key] and not any(error["field"] == key for error in errors):
                errors.append({"field": key, "message": f"{FIELD_LABELS[key]} is required."})
    if errors:
        raise ServiceError("Some values are not valid.", 422, "INVALID_INVOICE_DATA", errors)

    with _lock:
        metadata = document_metadata_service.load_metadata(document_id)
        page_number = resolve_page(metadata, payload.get("page"))
        if page_number is None:
            raise ServiceError("This document has no page images yet.", 409, "PAGES_NOT_READY")
        store, _ = current_store(document_id, metadata)
        record = record_for_page(store, page_number)
        refuse_if_posted(record, "changed")
        expected_pages = payload.get("pages")
        if expected_pages is not None and expected_pages != record["pages"]:
            raise ServiceError(
                "The pages of this invoice changed after the form was opened. Reload the page and try again.",
                409,
                "INVOICE_PAGES_CHANGED",
                {"pages": record["pages"]},
            )
        record["values"] = values
        record["line_items"] = line_items
        record["status"] = status
        record["saved_at"] = storage_service.utc_now()
        write_stored(document_id, store)
        update_review_status(document_id, metadata, store)
        logger.info("Document %s: invoice on pages %s saved as %s", document_id, record["pages"], status)
        return build_response(document_id, store, page_number)


def parse_value(document_id, kind, text, page=None):
    """Read a value of the given kind from text (used when the user clicks text on the invoice)."""
    if kind not in ("text", "code", "date", "amount", "quantity", "percent"):
        raise ServiceError("This value cannot be read from the invoice.", 422, "INVALID_VALUE_KIND")
    text = text_of(text)
    if not text or len(text) > 1000:
        raise ServiceError("There is no text to read.", 422, "INVALID_VALUE")
    metadata = document_metadata_service.load_metadata(document_id)  # 404 for unknown documents
    order = config.INVOICE_DATE_ORDER
    store = load_store(document_id)
    if store is not None and store.get("version") == STORE_VERSION:
        page_number = resolve_page(metadata, page)
        record = record_for_page(store, page_number) if page_number else None
        order = (record or {}).get("date_order") or order
    parsed = invoice_extraction_service.parse_value(kind, text, order)
    if parsed is None:
        messages = {
            "date": "Could not read a date from this text.",
            "amount": "Could not read an amount from this text.",
            "quantity": "Could not read a number from this text.",
            "percent": "Could not read a percentage (such as 18%) from this text.",
            "code": "Could not read a number or code from this text.",
            "text": "This text is too short to use.",
        }
        raise ServiceError(messages[kind], 422, "VALUE_NOT_READABLE")
    return {"value": parsed["value"], "ambiguous": parsed["ambiguous"]}


def posted_count(document_id):
    """How many invoices of the document were posted to the database."""
    with _lock:
        store = load_store(document_id)
        if store is None:
            return 0
        return sum(1 for record in store["invoices"].values() if record.get("status") == "posted")


def delete_warning(document_id):
    """Guard for document_delete_service: deleting a document with posted invoices needs a confirmation."""
    count = posted_count(document_id)
    if not count:
        return None
    return {
        "code": "DOCUMENT_HAS_POSTED",
        "message": (
            f"{count} invoice(s) of this document were already posted to the database. "
            "Deleting the document does not remove them from the database."
        ),
        "details": {"posted": count},
    }


# ---------------------------------------------------------------------------
# Posting to the database (used by invoice_posting_service)
# ---------------------------------------------------------------------------
def posting_snapshot(document_id, page):
    """
    A copy of the saved invoice on this page, with the document metadata, ready to be posted.
    Only invoices with status "saved" can be posted.
    """
    with _lock:
        metadata = document_metadata_service.load_metadata(document_id)
        page_number = resolve_page(metadata, page)
        if page_number is None:
            raise ServiceError("This document has no page images yet.", 409, "PAGES_NOT_READY")
        store, _ = current_store(document_id, metadata)
        record = record_for_page(store, page_number)
        if record is None:
            raise ServiceError("No invoice was found on this page.", 404, "NOT_FOUND")
        refuse_if_posted(record, "posted again")
        if record.get("status") != "saved":
            raise ServiceError(
                'Only saved invoices can be posted. Use "Save changes" first.', 409, "INVOICE_NOT_SAVED",
            )
        records = sorted_records(store)
        return {
            "metadata": copy.deepcopy(metadata),
            "record": copy.deepcopy(record),
            "page": page_number,
            "index": records.index(record) + 1,
            "count": len(records),
        }


def record_posting_result(document_id, page, saved_at, staging_id=None, error=None):
    """
    Store the result of a post. Success: status "posted" and the staging row id.
    Failure: the error is kept and the invoice stays "saved" so it can be posted again.
    """
    with _lock:
        metadata = document_metadata_service.load_metadata(document_id)
        store, _ = current_store(document_id, metadata)
        record = record_for_page(store, page)
        if record is None:
            raise ServiceError("No invoice was found on this page.", 404, "NOT_FOUND")
        posting = dict(record.get("posting") or {})
        posting["attempts"] = int(posting.get("attempts") or 0) + 1
        posting["last_attempt_at"] = storage_service.utc_now()
        if error is None:
            if record.get("saved_at") != saved_at:
                logger.warning("Document %s: invoice on page %s changed while it was posted", document_id, page)
            record["status"] = "posted"
            posting.update({
                "staging_id": staging_id,
                "posted_at": posting["last_attempt_at"],
                "posted_by": config.APP_USER_NAME,
                "last_error": None,
            })
        else:
            posting["last_error"] = error
        record["posting"] = posting
        write_stored(document_id, store)
        update_review_status(document_id, metadata, store)
        return build_response(document_id, store, page)

"""
Reviewed invoice data of a document: load, extract, validate and save.

Stored per document in storage/documents/<id>/invoice_data.json:
    status               needs_review | draft | saved
    values               the current (possibly corrected) field values
    line_items           the current (possibly corrected) line items
    extracted            what was read from the OCR text, with source line and confidence
    extracted_line_items what was read from the OCR text
    ocr_signature        identifies the OCR results the extraction was made from

Rules:
- Data is extracted automatically the first time it is requested after OCR.
- If OCR runs again, "extracted" is refreshed. The values are replaced only
  while the user has not saved anything, so corrections are never lost.
- "Save as draft" keeps whatever is entered. "Save changes" also requires the
  mandatory fields.
"""

import logging
import re
import threading
from datetime import date

from app import config
from app.errors import ServiceError
from app.services import (
    document_metadata_service,
    invoice_extraction_service,
    ocr_results_service,
    storage_service,
)

logger = logging.getLogger(__name__)

INVOICE_DATA_FILENAME = "invoice_data.json"
FIELD_KEYS = invoice_extraction_service.FIELD_KEYS
TEXT_KEYS = ("vendor_name", "invoice_number", "po_number")
DATE_KEYS = ("invoice_date", "due_date")
MONEY_KEYS = ("subtotal", "tax", "total_due")
REQUIRED_KEYS = ("vendor_name", "invoice_number", "invoice_date", "due_date", "total_due")
FIELD_LABELS = {
    "vendor_name": "Vendor name",
    "invoice_number": "Invoice number",
    "invoice_date": "Invoice date",
    "due_date": "Due date",
    "po_number": "PO number",
    "currency": "Currency",
    "subtotal": "Subtotal",
    "tax": "Tax",
    "total_due": "Total due",
}
MAX_TEXT_LENGTH = 200
MAX_DESCRIPTION_LENGTH = 500
NUMBER_PATTERN = re.compile(r"^-?\d{1,12}(?:\.\d{1,4})?$")
CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")

_lock = threading.RLock()


def invoice_data_path(document_id):
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
    path = invoice_data_path(document_id)
    if not path.is_file():
        return None
    try:
        return storage_service.read_json(path)
    except (OSError, ValueError) as error:
        logger.error("Document %s: unreadable invoice data: %s", document_id, error)
        return None


def write_stored(document_id, stored):
    storage_service.write_json_atomic(invoice_data_path(document_id), stored)


def set_data_status(document_id, status):
    def apply(metadata):
        metadata["data_status"] = status

    document_metadata_service.update_metadata(document_id, apply)


def run_extraction(document_id, metadata):
    """Extract fields from the stored OCR results of the document."""
    results = []
    for page in metadata.get("pages", []):
        result = ocr_results_service.read_page_result_file(document_id, page["page_number"])
        if result and result.get("status") in ("completed", "no_text"):
            results.append(result)
    return invoice_extraction_service.extract_invoice_data(results)


def values_from_extraction(extraction):
    return {key: extraction["fields"][key]["value"] for key in FIELD_KEYS}


def clean_extracted_items(items):
    return [
        {
            "description": item["description"],
            "quantity": item["quantity"],
            "rate": item["rate"],
            "amount": item["amount"],
            "confidence": item.get("confidence"),
            "page": item.get("page"),
        }
        for item in items
    ]


def new_stored(extraction, signature):
    return {
        "status": "needs_review",
        "ocr_signature": signature,
        "date_order": extraction["date_order"],
        "extracted": extraction["fields"],
        "extracted_line_items": clean_extracted_items(extraction["line_items"]),
        "values": values_from_extraction(extraction),
        "line_items": clean_extracted_items(extraction["line_items"]),
        "extracted_at": storage_service.utc_now(),
        "saved_at": None,
    }


def empty_extraction():
    return {
        "fields": {
            key: {"value": "", "confidence": None, "needs_check": False, "source": None}
            for key in FIELD_KEYS
        },
        "line_items": [],
        "date_order": config.INVOICE_DATE_ORDER,
    }


def build_response(document_id, stored, ocr_ready):
    return {
        "document_id": document_id,
        "status": stored["status"],
        "ocr_ready": ocr_ready,
        "values": {key: stored["values"].get(key, "") for key in FIELD_KEYS},
        "extracted": stored["extracted"],
        "line_items": stored["line_items"],
        "extracted_line_items": stored["extracted_line_items"],
        "date_order": stored.get("date_order"),
        "extracted_at": stored.get("extracted_at"),
        "saved_at": stored.get("saved_at"),
    }


def get_invoice_data(document_id):
    """Return the invoice data of a document, extracting it from the OCR text when needed."""
    with _lock:
        metadata = document_metadata_service.load_metadata(document_id)
        stored = read_stored(document_id)
        signature = compute_ocr_signature(document_id)
        ocr_ready = signature != "none"

        if stored is None:
            if not ocr_ready:
                return build_response(document_id, new_stored(empty_extraction(), "none"), False)
            stored = new_stored(run_extraction(document_id, metadata), signature)
            write_stored(document_id, stored)
            set_data_status(document_id, "needs_review")
            logger.info("Document %s: invoice data extracted", document_id)
        elif stored.get("ocr_signature") != signature:
            extraction = run_extraction(document_id, metadata)
            stored["extracted"] = extraction["fields"]
            stored["extracted_line_items"] = clean_extracted_items(extraction["line_items"])
            stored["date_order"] = extraction["date_order"]
            stored["ocr_signature"] = signature
            stored["extracted_at"] = storage_service.utc_now()
            if stored["status"] == "needs_review":
                stored["values"] = values_from_extraction(extraction)
                stored["line_items"] = clean_extracted_items(extraction["line_items"])
            write_stored(document_id, stored)
            logger.info("Document %s: invoice data re-extracted after OCR changed", document_id)
        return build_response(document_id, stored, ocr_ready)


def re_extract_invoice_data(document_id):
    """Throw away the current values and read them from the OCR text again."""
    with _lock:
        metadata = document_metadata_service.load_metadata(document_id)
        signature = compute_ocr_signature(document_id)
        if signature == "none":
            raise ServiceError("OCR results are not available yet. Run OCR first.", 409, "OCR_NOT_READY")
        stored = new_stored(run_extraction(document_id, metadata), signature)
        write_stored(document_id, stored)
        set_data_status(document_id, "needs_review")
        logger.info("Document %s: invoice data re-extracted on request", document_id)
        return build_response(document_id, stored, True)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def text_of(value):
    return "" if value is None else str(value).strip()


def clean_values(raw_values, errors):
    values = {}
    for key in FIELD_KEYS:
        text = text_of(raw_values.get(key))
        label = FIELD_LABELS[key]
        if key in TEXT_KEYS and len(text) > MAX_TEXT_LENGTH:
            errors.append({"field": key, "message": f"{label} is too long (maximum {MAX_TEXT_LENGTH} characters)."})
        elif key in DATE_KEYS and text:
            try:
                date.fromisoformat(text)
            except ValueError:
                errors.append({"field": key, "message": f"{label} must be a valid date."})
        elif key in MONEY_KEYS and text and not NUMBER_PATTERN.match(text):
            errors.append({"field": key, "message": f"{label} must be a number such as 1234.50."})
        elif key == "currency" and text:
            text = text.upper()
            if not CURRENCY_PATTERN.match(text):
                errors.append({"field": key, "message": "Currency must be a 3-letter code such as USD."})
        values[key] = text
    return values


def clean_line_items(raw_items, errors):
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
        item = {"description": text_of(raw.get("description"))}
        if len(item["description"]) > MAX_DESCRIPTION_LENGTH:
            errors.append({
                "field": f"line_items.{position - 1}.description",
                "message": f"Description of line {position} is too long (maximum {MAX_DESCRIPTION_LENGTH} characters).",
            })
        for key in ("quantity", "rate", "amount"):
            text = text_of(raw.get(key))
            if text and not NUMBER_PATTERN.match(text):
                errors.append({
                    "field": f"line_items.{position - 1}.{key}",
                    "message": f"{key.capitalize()} of line {position} must be a number.",
                })
            item[key] = text
        confidence = raw.get("confidence")
        item["confidence"] = confidence if isinstance(confidence, (int, float)) and not isinstance(confidence, bool) else None
        page = raw.get("page")
        item["page"] = page if isinstance(page, int) and not isinstance(page, bool) else None
        if item["description"] or item["quantity"] or item["rate"] or item["amount"]:
            items.append(item)
    return items


def save_invoice_data(document_id, payload):
    """Validate and store the user's values. status: "draft" or "saved"."""
    if not isinstance(payload, dict):
        raise ServiceError("Send the invoice data as a JSON object.", 422, "INVALID_INVOICE_DATA")
    status = payload.get("status")
    if status not in ("draft", "saved"):
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
        signature = compute_ocr_signature(document_id)
        stored = read_stored(document_id)
        if stored is None:
            extraction = run_extraction(document_id, metadata) if signature != "none" else empty_extraction()
            stored = new_stored(extraction, signature)
        stored["values"] = values
        stored["line_items"] = line_items
        stored["status"] = status
        stored["saved_at"] = storage_service.utc_now()
        write_stored(document_id, stored)
        set_data_status(document_id, status)
        logger.info("Document %s: invoice data saved as %s", document_id, status)
        return build_response(document_id, stored, signature != "none")


def parse_value(document_id, kind, text):
    """Read a value of the given kind from text (used when the user clicks text on the invoice)."""
    if kind not in ("text", "code", "date", "amount", "quantity"):
        raise ServiceError("This value cannot be read from the invoice.", 422, "INVALID_VALUE_KIND")
    text = text_of(text)
    if not text or len(text) > 1000:
        raise ServiceError("There is no text to read.", 422, "INVALID_VALUE")
    document_metadata_service.load_metadata(document_id)  # 404 for unknown documents
    stored = read_stored(document_id)
    order = (stored or {}).get("date_order") or config.INVOICE_DATE_ORDER
    parsed = invoice_extraction_service.parse_value(kind, text, order)
    if parsed is None:
        messages = {
            "date": "Could not read a date from this text.",
            "amount": "Could not read an amount from this text.",
            "quantity": "Could not read a number from this text.",
            "code": "Could not read a number or code from this text.",
            "text": "This text is too short to use.",
        }
        raise ServiceError(messages[kind], 422, "VALUE_NOT_READABLE")
    return {"value": parsed["value"], "ambiguous": parsed["ambiguous"]}

"""Invoice data API: autofilled invoice fields that the user can review, correct and save.

A document can hold several invoices. Every call works on the invoice that contains
the given page (default: page 1).
"""

from typing import Optional

from fastapi import APIRouter, Body

from app.api.api_responses import success
from app.errors import ServiceError
from app.services import invoice_data_service

router = APIRouter(prefix="/api/documents", tags=["Invoice data"])


@router.get("/{document_id}/invoice-data")
def get_invoice_data(document_id: str, page: Optional[str] = None):
    """Fields and line items of the invoice on this page, read from the OCR text, or the user's saved values."""
    return success(invoice_data_service.get_invoice_data(document_id, page))


@router.put("/{document_id}/invoice-data")
def save_invoice_data(document_id: str, payload: dict = Body(default=None)):
    """
    JSON body: {"status": "draft" | "saved", "values": {...}, "line_items": [...], "page": 1, "pages": [1, 2]}

    "page" picks the invoice (default 1). "pages" (optional) is what the form showed; a
    mismatch is refused with INVOICE_PAGES_CHANGED.
    "saved" requires vendor name, invoice number, invoice date, due date and total due.
    """
    data = invoice_data_service.save_invoice_data(document_id, payload)
    message = "Draft saved." if data["status"] == "draft" else "Changes saved."
    return success(data, message)


@router.post("/{document_id}/invoice-data/extract")
def extract_invoice_data(document_id: str, payload: Optional[dict] = Body(default=None)):
    """Optional JSON body: {"page": 3}. Discard the invoice's values and read them from the OCR text again."""
    page = payload.get("page") if isinstance(payload, dict) else None
    return success(
        invoice_data_service.re_extract_invoice_data(document_id, page), "Values read from the invoice again."
    )


@router.post("/{document_id}/invoice-data/page-break")
def set_page_break(document_id: str, payload: dict = Body(default=None)):
    """JSON body: {"page": 5, "starts_invoice": true | false}. Correct where an invoice starts."""
    data = invoice_data_service.set_page_break(document_id, payload)
    starts = isinstance(payload, dict) and payload.get("starts_invoice")
    return success(data, "A new invoice starts on this page." if starts else "Page joined to the previous invoice.")


@router.post("/{document_id}/invoice-data/parse-value")
def parse_value(document_id: str, payload: dict = Body(default=None)):
    """JSON body: {"kind": "text|code|date|amount|quantity", "text": "...", "page": 1}"""
    if not isinstance(payload, dict):
        raise ServiceError('Send a JSON body such as {"kind": "date", "text": "15 Oct 2023"}.', 422, "INVALID_VALUE")
    return success(invoice_data_service.parse_value(
        document_id, payload.get("kind"), payload.get("text"), payload.get("page")
    ))

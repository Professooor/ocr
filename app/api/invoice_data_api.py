"""Invoice data API: autofilled invoice fields that the user can review, correct and save."""

from fastapi import APIRouter, Body

from app.api.api_responses import success
from app.errors import ServiceError
from app.services import invoice_data_service

router = APIRouter(prefix="/api/documents", tags=["Invoice data"])


@router.get("/{document_id}/invoice-data")
def get_invoice_data(document_id: str):
    """Fields and line items read from the OCR text, or the user's saved values."""
    return success(invoice_data_service.get_invoice_data(document_id))


@router.put("/{document_id}/invoice-data")
def save_invoice_data(document_id: str, payload: dict = Body(default=None)):
    """
    JSON body: {"status": "draft" | "saved", "values": {...}, "line_items": [...]}

    "saved" requires vendor name, invoice number, invoice date, due date and total due.
    """
    data = invoice_data_service.save_invoice_data(document_id, payload)
    message = "Draft saved." if data["status"] == "draft" else "Changes saved."
    return success(data, message)


@router.post("/{document_id}/invoice-data/extract")
def extract_invoice_data(document_id: str):
    """Discard the current values and read them from the OCR text again."""
    return success(invoice_data_service.re_extract_invoice_data(document_id), "Values read from the invoice again.")


@router.post("/{document_id}/invoice-data/parse-value")
def parse_value(document_id: str, payload: dict = Body(default=None)):
    """JSON body: {"kind": "text|code|date|amount|quantity", "text": "..."}"""
    if not isinstance(payload, dict):
        raise ServiceError('Send a JSON body such as {"kind": "date", "text": "15 Oct 2023"}.', 422, "INVALID_VALUE")
    return success(invoice_data_service.parse_value(document_id, payload.get("kind"), payload.get("text")))

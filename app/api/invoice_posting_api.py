"""Invoice posting API: send a saved invoice to the Oracle staging tables ("Post to database")."""

from fastapi import APIRouter, Body

from app.api.api_responses import success
from app.services import invoice_posting_service

router = APIRouter(prefix="/api/documents", tags=["Invoice posting"])


@router.post("/{document_id}/invoice-data/post")
def post_invoice(document_id: str, payload: dict = Body(default=None)):
    """JSON body: {"page": 3}. Post the saved invoice that contains the page. It becomes read-only."""
    page = payload.get("page") if isinstance(payload, dict) else None
    data = invoice_posting_service.post_invoice(document_id, page)
    return success(data, "Invoice posted to the database.")

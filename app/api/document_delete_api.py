"""Document delete API: remove a whole document from storage."""

from fastapi import APIRouter

from app.api.api_responses import success
from app.services import document_delete_service

router = APIRouter(prefix="/api/documents", tags=["Document delete"])


@router.delete("/{document_id}")
def delete_document(document_id: str, confirm: bool = False):
    """Delete the document and all its files. 409 when OCR is running, or when a guard asks for confirm=true."""
    return success(document_delete_service.delete_document(document_id, confirm), "Document deleted.")

"""OCR processing API: start OCR for a document and report its progress."""

from fastapi import APIRouter

from app.api.api_responses import success
from app.services import ocr_processing_service

router = APIRouter(prefix="/api/documents", tags=["OCR processing"])


@router.post("/{document_id}/ocr", status_code=202)
def start_ocr(document_id: str):
    """Queue OCR for every page that has no successful result yet."""
    submitted, summary = ocr_processing_service.submit_ocr(document_id)
    if submitted:
        return success(summary, "OCR started.")
    return success(summary, "OCR has already completed for every page.")


@router.get("/{document_id}/ocr/status")
def get_ocr_status(document_id: str):
    return success(ocr_processing_service.get_ocr_status(document_id))

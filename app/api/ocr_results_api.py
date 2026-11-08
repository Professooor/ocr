"""OCR results API: return stored OCR results by document and page number."""

from fastapi import APIRouter

from app.api.api_responses import success
from app.services import ocr_results_service

router = APIRouter(prefix="/api/documents", tags=["OCR results"])


@router.get("/{document_id}/ocr/results")
def get_document_results(document_id: str):
    return success(ocr_results_service.get_document_results(document_id))


@router.get("/{document_id}/pages/{page_number}/ocr")
def get_page_result(document_id: str, page_number: str):
    return success(ocr_results_service.get_page_result(document_id, page_number))

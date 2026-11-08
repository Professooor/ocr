"""Document Review API: documents, pages and OCR results for the review screen."""

from fastapi import APIRouter

from app.api.api_responses import success
from app.services import document_review_service

router = APIRouter(prefix="/api/review", tags=["Document review"])


@router.get("/documents")
def list_documents():
    return success(document_review_service.list_review_documents())


@router.get("/documents/{document_id}")
def get_document(document_id: str):
    return success(document_review_service.get_review_document(document_id))


@router.get("/documents/{document_id}/pages/{page_number}")
def get_page(document_id: str, page_number: str):
    return success(document_review_service.get_review_page(document_id, page_number))

"""PDF processing API: (re)convert a stored PDF into page images with a chosen DPI."""

from fastapi import APIRouter, Body

from app.api.api_responses import success
from app.errors import ServiceError
from app.services import pdf_processing_service

router = APIRouter(prefix="/api/documents", tags=["PDF processing"])


@router.post("/{document_id}/convert")
def convert_document(document_id: str, payload: dict = Body(default=None)):
    """
    JSON body: {"dpi": 150}

    Existing page images are reused when they were created with the same DPI.
    A different DPI creates new page images and clears old OCR results.
    """
    if not isinstance(payload, dict):
        raise ServiceError("Send a JSON body such as {\"dpi\": 150}.", 422, "INVALID_DPI")
    result = pdf_processing_service.convert_pdf_to_images(document_id, payload.get("dpi"))
    message = "Existing page images reused." if result["reused"] else "Page images created."
    return success(result, message)

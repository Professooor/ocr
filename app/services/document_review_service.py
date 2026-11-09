"""
Document Review: combine document details, page images and OCR results.

This service only reads data. It never starts OCR or page conversion.
"""

from app.services import (
    document_metadata_service,
    ocr_results_service,
    page_image_service,
)


def list_review_documents():
    """Return a summary of every stored document, newest first."""
    return [
        document_metadata_service.build_document_summary(metadata)
        for metadata in document_metadata_service.list_all_metadata()
    ]


def get_review_document(document_id):
    """Return one document summary together with a summary of each of its pages."""
    metadata = document_metadata_service.load_metadata(document_id)
    return {
        "document": document_metadata_service.build_document_summary(metadata),
        "pages": [
            document_metadata_service.build_page_summary(metadata, page)
            for page in metadata.get("pages", [])
        ],
    }


def get_review_page(document_id, raw_page_number):
    """Return one page with its OCR result and a short message that explains why there is no text (if that is the case)."""
    metadata = document_metadata_service.load_metadata(document_id)
    page = page_image_service.get_page_record(metadata, raw_page_number)
    result = ocr_results_service.read_page_result_file(document_id, page["page_number"])

    if result is None:
        status = page.get("ocr_status", "pending")
        message = {
            "pending": "OCR has not been run for this page yet.",
            "failed": page.get("ocr_error") or "OCR failed for this page.",
        }.get(status, "No OCR result is available for this page.")
    elif result["status"] == "no_text":
        message = "OCR found no readable text on this page."
    elif result["status"] == "failed":
        message = result.get("error") or "OCR failed for this page."
    else:
        message = None

    return {
        "document_id": document_id,
        "page_count": len(metadata.get("pages", [])),
        "page": document_metadata_service.build_page_summary(metadata, page),
        "ocr_result": result,
        "message": message,
    }

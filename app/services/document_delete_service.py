"""
Delete a whole document: its folder in storage (original file, page images, OCR results, saved data).

Rules:
- A document that is being read by OCR (queued or running) cannot be deleted. Wait or let it finish.
- Other modules can register a "guard" with set_guard(). The guard gets the document ID and returns
  a short text when deleting needs an extra confirmation (nothing otherwise). The caller must then
  send confirm=True. This keeps this module generic.
- Nothing outside the document's own folder is touched, and the database is never changed.
"""

import logging

from app.errors import ServiceError
from app.services import document_metadata_service, storage_service

logger = logging.getLogger(__name__)

_guard = None


def set_guard(function):
    """Register guard(document_id) -> None | {"code": ..., "message": ..., "details": ...}."""
    global _guard
    _guard = function


def delete_document(document_id, confirm=False):
    """Delete the document folder. Returns {"document_id", "reference_number", "original_filename"}."""
    metadata = document_metadata_service.load_metadata(document_id)  # 400 / 404 for bad or unknown IDs
    if (metadata.get("ocr") or {}).get("status") in ("queued", "running"):
        raise ServiceError(
            "OCR is still running for this document. Wait until it has finished, then delete it.",
            409, "OCR_IN_PROGRESS",
        )
    if _guard is not None and not confirm:
        warning = _guard(document_id)
        if warning:
            raise ServiceError(warning["message"], 409, warning["code"], warning.get("details"))

    storage_service.remove_path(storage_service.get_document_dir(document_id))
    logger.info(
        "Document %s (%s, %s) deleted",
        document_id, metadata.get("reference_number"), metadata.get("original_filename"),
    )
    return {
        "document_id": document_id,
        "reference_number": metadata.get("reference_number"),
        "original_filename": metadata.get("original_filename"),
    }

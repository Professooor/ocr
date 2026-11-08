"""
Read and write document processing metadata (document.json).

Metadata is changed by upload, page conversion and the background OCR job, so
every read and write goes through one lock. The application is designed to run
as a single process (one Uvicorn worker), which makes a thread lock sufficient.
"""

import logging
import threading

from app.errors import ServiceError
from app.services import storage_service
from app import config

logger = logging.getLogger(__name__)

_metadata_lock = threading.RLock()


def save_metadata(document_id, metadata):
    with _metadata_lock:
        metadata["updated_at"] = storage_service.utc_now()
        storage_service.write_json_atomic(storage_service.get_metadata_path(document_id), metadata)


def load_metadata(document_id):
    path = storage_service.get_metadata_path(document_id)
    with _metadata_lock:
        if not path.exists():
            raise ServiceError("Document not found.", 404, "DOCUMENT_NOT_FOUND")
        try:
            return storage_service.read_json(path)
        except (OSError, ValueError) as error:
            logger.error("Could not read metadata for document %s: %s", document_id, error)
            raise ServiceError("Document metadata could not be read.", 500, "METADATA_ERROR")


def update_metadata(document_id, change_function):
    """Load metadata, apply change_function(metadata) and save it, as one locked step."""
    with _metadata_lock:
        metadata = load_metadata(document_id)
        change_function(metadata)
        save_metadata(document_id, metadata)
        return metadata


def list_all_metadata():
    """Return metadata for every stored document, newest first."""
    documents = []
    if not config.DOCUMENTS_DIR.exists():
        return documents
    for folder in config.DOCUMENTS_DIR.iterdir():
        if not folder.is_dir() or not storage_service.DOCUMENT_ID_PATTERN.match(folder.name):
            continue
        try:
            documents.append(load_metadata(folder.name))
        except ServiceError:
            logger.warning("Skipping document folder without readable metadata: %s", folder.name)
    documents.sort(key=lambda item: item.get("uploaded_at", ""), reverse=True)
    return documents


def build_page_image_url(metadata, page):
    """Image URL with a version parameter so browsers reload images after re-conversion."""
    version = f"{metadata['conversion'].get('dpi') or 'native'}-{metadata['conversion'].get('completed_at', '')}"
    version = "".join(ch for ch in version if ch.isalnum() or ch == "-")
    return (
        f"/api/documents/{metadata['document_id']}/pages/{page['page_number']}/image"
        f"?v={version}"
    )


def build_document_summary(metadata):
    """Public document information returned by the API (no internal paths)."""
    conversion = metadata.get("conversion", {})
    pages = metadata.get("pages", [])
    return {
        "document_id": metadata["document_id"],
        "original_filename": metadata.get("original_filename"),
        "document_kind": metadata.get("document_kind"),
        "file_type": metadata.get("file_type"),
        "file_size_bytes": metadata.get("file_size_bytes"),
        "uploaded_at": metadata.get("uploaded_at"),
        "updated_at": metadata.get("updated_at"),
        "page_count": len(pages),
        "conversion": {
            "status": conversion.get("status"),
            "dpi": conversion.get("dpi"),
            "source": conversion.get("source"),
            "completed_at": conversion.get("completed_at"),
            "duration_seconds": conversion.get("duration_seconds"),
        },
        "ocr": build_ocr_summary(metadata),
        "data_status": metadata.get("data_status"),
    }


def build_ocr_summary(metadata):
    ocr = metadata.get("ocr", {})
    pages = metadata.get("pages", [])
    counts = {"pending": 0, "completed": 0, "no_text": 0, "failed": 0}
    for page in pages:
        status = page.get("ocr_status", "pending")
        counts[status] = counts.get(status, 0) + 1
    return {
        "status": ocr.get("status", "not_started"),
        "engine": ocr.get("engine"),
        "models": ocr.get("models"),
        "queued_at": ocr.get("queued_at"),
        "started_at": ocr.get("started_at"),
        "completed_at": ocr.get("completed_at"),
        "current_page": ocr.get("current_page"),
        "error": ocr.get("error"),
        "pages_total": len(pages),
        "pages_completed": counts.get("completed", 0),
        "pages_no_text": counts.get("no_text", 0),
        "pages_failed": counts.get("failed", 0),
        "pages_pending": counts.get("pending", 0),
    }


def build_page_summary(metadata, page):
    return {
        "page_number": page["page_number"],
        "image_url": build_page_image_url(metadata, page),
        "width": page.get("width"),
        "height": page.get("height"),
        "dpi": page.get("dpi"),
        "ocr_status": page.get("ocr_status", "pending"),
        "ocr_error": page.get("ocr_error"),
    }

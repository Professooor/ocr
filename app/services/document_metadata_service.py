"""
Read and write document processing metadata (document.json).

Every document also gets a readable reference number, DOC-<year>-<4-digit sequence>
(for example DOC-2026-0001), shown to users next to the internal document ID. The
internal ID stays the folder name and the API key; the reference number is display only.

Metadata is changed by upload, page conversion and the background OCR job, so
every read and write goes through one lock. The application is designed to run
as a single process (one Uvicorn worker), which makes a thread lock sufficient.
"""

import logging
import re
import threading

from app.errors import ServiceError
from app.services import storage_service
from app import config

logger = logging.getLogger(__name__)

_metadata_lock = threading.RLock()

REFERENCE_PREFIX = "DOC"
REFERENCE_PATTERN = re.compile(r"^" + REFERENCE_PREFIX + r"-(\d{4})-(\d{4,})$")


def save_metadata(document_id, metadata):
    """Write document.json (under the lock) and update its "updated_at" time."""
    with _metadata_lock:
        metadata["updated_at"] = storage_service.utc_now()
        storage_service.write_json_atomic(storage_service.get_metadata_path(document_id), metadata)


def load_metadata(document_id):
    """Read document.json. Raises a 404 ServiceError when the document does not exist."""
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


def upload_year(metadata):
    """Year of the upload (from "uploaded_at"), used in the reference number."""
    uploaded = str(metadata.get("uploaded_at") or "")
    return uploaded[:4] if re.match(r"^\d{4}", uploaded) else storage_service.utc_now()[:4]


def next_reference_number(year, documents):
    """The next free reference number of the year: one more than the highest already given."""
    highest = 0
    for metadata in documents:
        match = REFERENCE_PATTERN.match(str(metadata.get("reference_number") or ""))
        if match and match.group(1) == year:
            highest = max(highest, int(match.group(2)))
    return f"{REFERENCE_PREFIX}-{year}-{highest + 1:04d}"


def create_metadata(document_id, metadata):
    """Save the metadata of a new document, with the next reference number of its upload year."""
    with _metadata_lock:
        metadata["reference_number"] = next_reference_number(upload_year(metadata), list_all_metadata())
        save_metadata(document_id, metadata)
        return metadata


def assign_missing_reference_numbers():
    """Give a reference number to documents stored before reference numbers existed (oldest first)."""
    with _metadata_lock:
        documents = list_all_metadata()
        missing = [item for item in documents if not item.get("reference_number")]
        for metadata in sorted(missing, key=lambda item: item.get("uploaded_at", "")):
            metadata["reference_number"] = next_reference_number(upload_year(metadata), documents)
            save_metadata(metadata["document_id"], metadata)
            logger.info("Document %s: reference number %s", metadata["document_id"], metadata["reference_number"])
        return len(missing)


def build_page_image_url(metadata, page):
    """Image URL with a version parameter so browsers reload images after re-conversion."""
    # The page's own DPI is part of the version: a page read again in more detail gets a new image.
    version = (
        f"{metadata['conversion'].get('dpi') or 'native'}-{page.get('dpi') or ''}-"
        f"{metadata['conversion'].get('completed_at', '')}"
    )
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
        "reference_number": metadata.get("reference_number"),
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
        "data_counts": metadata.get("data_counts"),
    }


def build_ocr_summary(metadata):
    """OCR status of the document plus how many pages are completed, empty, failed or pending."""
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
    """Public information about one page (image URL, size, DPI and OCR status)."""
    return {
        "page_number": page["page_number"],
        "image_url": build_page_image_url(metadata, page),
        "width": page.get("width"),
        "height": page.get("height"),
        "dpi": page.get("dpi"),
        "ocr_status": page.get("ocr_status", "pending"),
        "ocr_error": page.get("ocr_error"),
    }

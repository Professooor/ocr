"""
Document upload: receive one file, validate it, store it and create page images.

Steps:
1. Validate the file extension and the DPI value (before reading the file).
2. Stream the upload to a temporary file (no size limit).
3. Validate the real file content (magic bytes, can it be opened, page count).
4. Create the document folder and move the file to original/original.<ext>.
5. Create page images (PDF: render with the selected DPI; image: convert to PNG).

If any step after step 4 fails, the new document folder is removed, so a failed
upload never leaves a half-processed document behind.
"""

import hashlib
import logging
import os

from app.errors import ServiceError
from app.services import (
    document_metadata_service,
    document_validation_service,
    page_image_service,
    pdf_processing_service,
    storage_service,
)

logger = logging.getLogger(__name__)

CHUNK_SIZE = 1024 * 1024


def save_upload_to_temp(upload_file, extension):
    """Copy the uploaded file to a temporary file in chunks. Return (path, size, sha256).

    There is no size limit; the file is streamed, so memory use stays small.
    """
    temp_path = storage_service.new_temp_path(extension)
    size = 0
    digest = hashlib.sha256()
    try:
        upload_file.file.seek(0)
        with open(temp_path, "wb") as target:
            while True:
                chunk = upload_file.file.read(CHUNK_SIZE)
                if not chunk:
                    break
                size += len(chunk)
                digest.update(chunk)
                target.write(chunk)
    except Exception:
        storage_service.remove_path(temp_path)
        raise

    if size == 0:
        storage_service.remove_path(temp_path)
        raise ServiceError("The selected file is empty.", 400, "EMPTY_FILE")
    return temp_path, size, digest.hexdigest()


def cleanup_failed_upload(temp_path, document_id):
    """Remove files of a failed upload. Cleanup problems are logged, never raised,
    so they cannot hide the original error from the user."""
    targets = [temp_path]
    if document_id is not None:
        targets.append(storage_service.get_document_dir(document_id))
    for target in targets:
        try:
            storage_service.remove_path(target)
        except OSError as error:
            logger.warning("Could not remove %s after a failed upload: %s", target, error)
    if document_id is not None:
        logger.info("Upload failed; removed document folder %s", document_id)


def upload_document(upload_file, raw_dpi):
    """Store one uploaded document and create its page images. Return the document summary."""
    extension = document_validation_service.validate_extension(upload_file.filename)
    dpi = document_validation_service.validate_dpi(raw_dpi, allow_missing=True)

    temp_path, size, sha256 = save_upload_to_temp(upload_file, extension)
    document_id = None
    try:
        content = document_validation_service.validate_file_content(temp_path, extension)

        document_id = storage_service.new_document_id()
        original_dir = storage_service.get_original_dir(document_id)
        original_dir.mkdir(parents=True)
        storage_service.get_pages_dir(document_id).mkdir()
        storage_service.get_ocr_dir(document_id).mkdir()
        stored_filename = f"original{extension}"
        os.replace(temp_path, original_dir / stored_filename)

        metadata = {
            "document_id": document_id,
            "original_filename": storage_service.safe_display_filename(upload_file.filename),
            "stored_filename": stored_filename,
            "file_extension": extension,
            "file_type": content["file_type"],
            "document_kind": content["document_kind"],
            "file_size_bytes": size,
            "sha256": sha256,
            "uploaded_at": storage_service.utc_now(),
            "requested_dpi": dpi if content["document_kind"] == "pdf" else None,
            "conversion": {"status": "pending"},
            "pages": [],
            "ocr": {"status": "not_started"},
        }
        document_metadata_service.create_metadata(document_id, metadata)

        if content["document_kind"] == "pdf":
            pdf_processing_service.convert_pdf_to_images(document_id, dpi)
        else:
            page_image_service.create_pages_from_image(document_id)
    except Exception:
        cleanup_failed_upload(temp_path, document_id)
        raise

    metadata = document_metadata_service.load_metadata(document_id)
    logger.info(
        "Document %s (%s) uploaded: %s, %d bytes, %d page(s), DPI %s",
        document_id,
        metadata.get("reference_number"),
        metadata["file_type"],
        size,
        len(metadata["pages"]),
        metadata["conversion"].get("dpi"),
    )
    return document_metadata_service.build_document_summary(metadata)

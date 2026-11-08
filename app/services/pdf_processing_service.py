"""
Convert every page of a PDF document into a PNG image with PyMuPDF.

Memory safety:
- Pages are rendered one at a time; each page image is saved and released
  before the next page is rendered.
- Before rendering, the pixel size of the page at the selected DPI is checked
  against MAX_PAGE_PIXELS, so a very large page cannot exhaust memory.

Re-use rules:
- If the document was already converted with the same DPI and every page image
  still exists, the existing images are reused.
- If the DPI changes, new images are rendered into a staging folder and only
  replace the old images after every page succeeded. OCR results belong to the
  old images, so they are removed at the same time.
"""

import logging
import math
import os
import time

import fitz  # PyMuPDF

from app import config
from app.errors import ServiceError
from app.services import document_metadata_service, document_validation_service, storage_service

logger = logging.getLogger(__name__)


def pages_are_reusable(metadata, dpi):
    conversion = metadata.get("conversion", {})
    pages = metadata.get("pages", [])
    if conversion.get("status") != "completed" or conversion.get("dpi") != dpi or not pages:
        return False
    pages_dir = storage_service.get_pages_dir(metadata["document_id"])
    return all((pages_dir / page["image_filename"]).is_file() for page in pages)


def render_pdf_pages(pdf_path, output_dir, dpi):
    """Render all pages of pdf_path into output_dir. Return the list of page records."""
    pages = []
    zoom = dpi / 72.0
    document = fitz.open(pdf_path, filetype="pdf")
    try:
        for page_index in range(document.page_count):
            page_number = page_index + 1
            page = document.load_page(page_index)
            expected_width = math.ceil(page.rect.width * zoom)
            expected_height = math.ceil(page.rect.height * zoom)
            if expected_width * expected_height > config.MAX_PAGE_PIXELS:
                largest_dpi = int(72.0 * math.sqrt(config.MAX_PAGE_PIXELS / (page.rect.width * page.rect.height)))
                raise ServiceError(
                    f"Page {page_number} is too large to render at {dpi} DPI "
                    f"({expected_width} x {expected_height} pixels). "
                    f"Use {max(config.PDF_DPI_MIN, largest_dpi)} DPI or lower.",
                    413,
                    "PAGE_TOO_LARGE",
                )

            pixmap = page.get_pixmap(dpi=dpi, alpha=False)
            filename = storage_service.page_image_filename(page_number)
            pixmap.save(str(output_dir / filename))
            pages.append(
                {
                    "page_number": page_number,
                    "image_filename": filename,
                    "width": pixmap.width,
                    "height": pixmap.height,
                    "dpi": dpi,
                    "ocr_status": "pending",
                    "ocr_error": None,
                }
            )
            # Release the page image memory before rendering the next page.
            pixmap = None
            page = None
    finally:
        document.close()
    return pages


def convert_pdf_to_images(document_id, raw_dpi):
    """
    Convert a stored PDF document into page images.

    Returns {"document_id", "dpi", "page_count", "reused"}.
    """
    dpi = document_validation_service.validate_dpi(raw_dpi, allow_missing=False)
    metadata = document_metadata_service.load_metadata(document_id)

    if metadata.get("document_kind") != "pdf":
        raise ServiceError("Only PDF documents can be converted with a DPI value.", 400, "NOT_A_PDF")
    if metadata.get("ocr", {}).get("status") in ("queued", "running"):
        raise ServiceError(
            "OCR is running for this document. Wait until it finishes.", 409, "OCR_IN_PROGRESS"
        )
    if pages_are_reusable(metadata, dpi):
        logger.info("Document %s: reusing %d page images at %d DPI", document_id, len(metadata["pages"]), dpi)
        return {"document_id": document_id, "dpi": dpi, "page_count": len(metadata["pages"]), "reused": True}

    pdf_path = storage_service.get_original_dir(document_id) / metadata["stored_filename"]
    if not pdf_path.is_file():
        raise ServiceError("The original document file is missing.", 500, "ORIGINAL_MISSING")

    pages_dir = storage_service.get_pages_dir(document_id)
    staging_dir = storage_service.get_document_dir(document_id) / f"pages_staging_{storage_service.new_document_id()}"
    staging_dir.mkdir(parents=True)
    started = time.time()
    try:
        pages = render_pdf_pages(pdf_path, staging_dir, dpi)
    except ServiceError:
        storage_service.remove_path(staging_dir)
        raise
    except Exception as error:
        storage_service.remove_path(staging_dir)
        logger.exception("Document %s: PDF conversion failed", document_id)
        raise ServiceError(
            "The PDF could not be converted to images. The file may be damaged.",
            422,
            "CONVERSION_FAILED",
        ) from error
    duration = round(time.time() - started, 2)

    def apply_conversion(data):
        # Runs under the metadata lock, so OCR cannot be queued while the
        # page folders are swapped, and two conversions cannot swap at once.
        if data.get("ocr", {}).get("status") in ("queued", "running"):
            raise ServiceError(
                "OCR started for this document during conversion. Try again when it finishes.",
                409,
                "OCR_IN_PROGRESS",
            )
        # Every page succeeded: replace old page images and old OCR results.
        storage_service.remove_path(pages_dir)
        os.replace(staging_dir, pages_dir)
        ocr_dir = storage_service.get_ocr_dir(document_id)
        storage_service.remove_path(ocr_dir)
        ocr_dir.mkdir(parents=True, exist_ok=True)

        data["pages"] = pages
        data["conversion"] = {
            "status": "completed",
            "source": "pdf_render",
            "dpi": dpi,
            "completed_at": storage_service.utc_now(),
            "duration_seconds": duration,
            "error": None,
        }
        data["ocr"] = {"status": "not_started"}

    try:
        document_metadata_service.update_metadata(document_id, apply_conversion)
    finally:
        if staging_dir.exists():
            storage_service.remove_path(staging_dir)
    logger.info("Document %s: converted %d page(s) at %d DPI in %.2fs", document_id, len(pages), dpi, duration)
    return {"document_id": document_id, "dpi": dpi, "page_count": len(pages), "reused": False}

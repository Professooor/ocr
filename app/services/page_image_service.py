"""
Page images: create them from uploaded image files and look them up.

PDF pages are rendered by pdf_processing_service. Image uploads (PNG, JPEG,
BMP, TIFF) are already raster images, so DPI does not apply to them: each image
(or each TIFF frame) is stored as one PNG page image at its native resolution.
"""

import logging
import time

from PIL import Image, ImageOps, ImageSequence

from app.errors import ServiceError
from app.services import document_metadata_service, document_validation_service, storage_service

logger = logging.getLogger(__name__)


def to_rgb(image):
    """Convert any Pillow image mode to RGB; transparent areas become white."""
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba_image = image.convert("RGBA")
        background = Image.new("RGB", rgba_image.size, (255, 255, 255))
        background.paste(rgba_image, mask=rgba_image.getchannel("A"))
        return background
    if image.mode.startswith("I;16"):
        image = image.point(lambda value: value * (1 / 256)).convert("L")
    return image.convert("RGB")


def create_pages_from_image(document_id):
    """Create PNG page images from an uploaded image document."""
    metadata = document_metadata_service.load_metadata(document_id)
    source_path = storage_service.get_original_dir(document_id) / metadata["stored_filename"]
    pages_dir = storage_service.get_pages_dir(document_id)
    pages_dir.mkdir(parents=True, exist_ok=True)

    started = time.time()
    pages = []
    try:
        with Image.open(source_path) as image:
            for frame_index, frame in enumerate(ImageSequence.Iterator(image)):
                page_number = frame_index + 1
                page_image = to_rgb(ImageOps.exif_transpose(frame))
                filename = storage_service.page_image_filename(page_number)
                page_image.save(pages_dir / filename, format="PNG")
                pages.append(
                    {
                        "page_number": page_number,
                        "image_filename": filename,
                        "width": page_image.width,
                        "height": page_image.height,
                        "dpi": None,
                        "ocr_status": "pending",
                        "ocr_error": None,
                    }
                )
                page_image.close()
    except Exception as error:
        logger.exception("Document %s: image page creation failed", document_id)
        raise ServiceError("The image could not be processed.", 422, "CONVERSION_FAILED") from error

    duration = round(time.time() - started, 2)

    def apply_pages(data):
        data["pages"] = pages
        data["conversion"] = {
            "status": "completed",
            "source": "image",
            "dpi": None,
            "completed_at": storage_service.utc_now(),
            "duration_seconds": duration,
            "error": None,
        }
        data["ocr"] = {"status": "not_started"}

    document_metadata_service.update_metadata(document_id, apply_pages)
    logger.info("Document %s: created %d page image(s) from image upload", document_id, len(pages))
    return {"document_id": document_id, "dpi": None, "page_count": len(pages), "reused": False}


def get_page_record(metadata, raw_page_number):
    pages = metadata.get("pages", [])
    if not pages:
        raise ServiceError("This document has no page images yet.", 409, "PAGES_NOT_READY")
    page_number = document_validation_service.validate_page_number(raw_page_number, len(pages))
    return pages[page_number - 1]


def list_pages(document_id):
    metadata = document_metadata_service.load_metadata(document_id)
    return {
        "document_id": document_id,
        "page_count": len(metadata.get("pages", [])),
        "dpi": metadata.get("conversion", {}).get("dpi"),
        "pages": [
            document_metadata_service.build_page_summary(metadata, page)
            for page in metadata.get("pages", [])
        ],
    }


def get_page_image_path(document_id, raw_page_number):
    metadata = document_metadata_service.load_metadata(document_id)
    page = get_page_record(metadata, raw_page_number)
    image_path = storage_service.get_pages_dir(document_id) / page["image_filename"]
    if not image_path.is_file():
        logger.error("Document %s: page image missing: %s", document_id, image_path)
        raise ServiceError("The page image file is missing.", 404, "PAGE_IMAGE_MISSING")
    return image_path

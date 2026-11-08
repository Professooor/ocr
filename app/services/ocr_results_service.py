"""
OCR results: convert raw PaddleOCR output into structured JSON and store it.

One JSON file is stored per page (ocr/page_0001.json, ...). Each file contains
the recognised lines with their confidence, polygon and bounding box, the full
page text and the processing status of that page.

Page statuses:
    completed   text was recognised
    no_text     OCR ran successfully but found no readable text
    failed      OCR raised an error for this page (see "error")
"""

import logging

from app.errors import ServiceError
from app.services import (
    document_metadata_service,
    page_image_service,
    storage_service,
)

logger = logging.getLogger(__name__)


def to_plain(value):
    """Convert numpy arrays / numpy numbers into plain Python lists and numbers."""
    if hasattr(value, "tolist"):
        return value.tolist()
    if isinstance(value, (list, tuple)):
        return [to_plain(item) for item in value]
    return value


def build_page_result(document_id, page, raw_result, duration_seconds, engine_info):
    """Build the stored result for one page from a PaddleOCR 3.x OCRResult."""
    texts = list(raw_result.get("rec_texts") or [])
    scores = to_plain(raw_result.get("rec_scores"))
    polygons = to_plain(raw_result.get("rec_polys"))
    boxes = to_plain(raw_result.get("rec_boxes"))
    scores = scores if isinstance(scores, list) else []
    polygons = polygons if isinstance(polygons, list) else []
    boxes = boxes if isinstance(boxes, list) else []

    lines = []
    for index, text in enumerate(texts):
        confidence = float(scores[index]) if index < len(scores) else None
        polygon = polygons[index] if index < len(polygons) else None
        box = boxes[index] if index < len(boxes) else None
        lines.append(
            {
                "line_number": index + 1,
                "text": str(text),
                "confidence": round(confidence, 4) if confidence is not None else None,
                "polygon": [[int(round(x)), int(round(y))] for x, y in polygon] if polygon else None,
                "box": [int(round(v)) for v in box] if box else None,
            }
        )

    readable_lines = [line for line in lines if line["text"].strip()]
    confidences = [line["confidence"] for line in readable_lines if line["confidence"] is not None]
    return {
        "document_id": document_id,
        "page_number": page["page_number"],
        "status": "completed" if readable_lines else "no_text",
        "image_filename": page["image_filename"],
        "image_width": page.get("width"),
        "image_height": page.get("height"),
        "dpi": page.get("dpi"),
        "engine": engine_info,
        "processed_at": storage_service.utc_now(),
        "duration_seconds": round(duration_seconds, 2),
        "line_count": len(readable_lines),
        "average_confidence": round(sum(confidences) / len(confidences), 4) if confidences else None,
        "text": "\n".join(line["text"] for line in readable_lines),
        "lines": readable_lines,
        "error": None,
    }


def build_failed_result(document_id, page, error_message, engine_info):
    return {
        "document_id": document_id,
        "page_number": page["page_number"],
        "status": "failed",
        "image_filename": page["image_filename"],
        "image_width": page.get("width"),
        "image_height": page.get("height"),
        "dpi": page.get("dpi"),
        "engine": engine_info,
        "processed_at": storage_service.utc_now(),
        "duration_seconds": None,
        "line_count": 0,
        "average_confidence": None,
        "text": "",
        "lines": [],
        "error": error_message,
    }


def save_page_result(document_id, page_number, result):
    path = storage_service.get_ocr_dir(document_id) / storage_service.ocr_result_filename(page_number)
    storage_service.write_json_atomic(path, result)


def read_page_result_file(document_id, page_number):
    """Return the stored result for one page, or None if there is no result."""
    path = storage_service.get_ocr_dir(document_id) / storage_service.ocr_result_filename(page_number)
    if not path.is_file():
        return None
    try:
        return storage_service.read_json(path)
    except (OSError, ValueError) as error:
        logger.error("Document %s page %s: unreadable OCR result: %s", document_id, page_number, error)
        return None


def get_page_result(document_id, raw_page_number):
    metadata = document_metadata_service.load_metadata(document_id)
    page = page_image_service.get_page_record(metadata, raw_page_number)
    result = read_page_result_file(document_id, page["page_number"])
    if result is None:
        raise ServiceError(
            f"No OCR result is available for page {page['page_number']} yet.",
            404,
            "OCR_RESULT_NOT_FOUND",
        )
    return result


def get_document_results(document_id):
    """Return OCR results for every page of a document, in page order."""
    metadata = document_metadata_service.load_metadata(document_id)
    pages = []
    for page in metadata.get("pages", []):
        result = read_page_result_file(document_id, page["page_number"])
        if result is None:
            result = {
                "document_id": document_id,
                "page_number": page["page_number"],
                "status": page.get("ocr_status", "pending"),
                "text": "",
                "lines": [],
                "line_count": 0,
                "average_confidence": None,
                "error": page.get("ocr_error"),
            }
        pages.append(result)
    return {
        "document_id": document_id,
        "ocr": document_metadata_service.build_ocr_summary(metadata),
        "pages": pages,
    }

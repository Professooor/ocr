"""
Validation rules for documents, DPI values, document IDs and page numbers.

The backend is the final authority for every rule. The frontend repeats the
same checks only to give faster feedback.
"""

import logging
import re

import fitz  # PyMuPDF
from PIL import Image

from app import config
from app.errors import ServiceError

logger = logging.getLogger(__name__)

# Pillow raises DecompressionBombError above twice this value; we also check it ourselves.
Image.MAX_IMAGE_PIXELS = config.MAX_PAGE_PIXELS

INTEGER_PATTERN = re.compile(r"^[0-9]+$")


# ---------------------------------------------------------------------------
# DPI
# ---------------------------------------------------------------------------
def validate_dpi(raw_value, allow_missing=True):
    """
    Return a valid integer DPI between PDF_DPI_MIN and PDF_DPI_MAX.

    Accepted: an int, or a string of digits (for example "150").
    Rejected: empty strings, decimals ("75.5"), signs ("+75"), exponents ("1e2"),
    booleans, floats and values outside the allowed range.
    A missing value (None) uses the default DPI when allow_missing is True.
    """
    message = (
        f"DPI must be a whole number from {config.PDF_DPI_MIN} to {config.PDF_DPI_MAX}."
    )
    if raw_value is None:
        if allow_missing:
            return config.PDF_DPI_DEFAULT
        raise ServiceError(f"DPI is required. {message}", 422, "INVALID_DPI")

    if isinstance(raw_value, bool):
        raise ServiceError(message, 422, "INVALID_DPI")
    if isinstance(raw_value, int):
        dpi = raw_value
    elif isinstance(raw_value, str):
        text = raw_value.strip()
        if not INTEGER_PATTERN.match(text):
            raise ServiceError(message, 422, "INVALID_DPI")
        dpi = int(text)
    else:
        raise ServiceError(message, 422, "INVALID_DPI")

    if dpi < config.PDF_DPI_MIN or dpi > config.PDF_DPI_MAX:
        raise ServiceError(message, 422, "INVALID_DPI")
    return dpi


# ---------------------------------------------------------------------------
# Page numbers
# ---------------------------------------------------------------------------
def validate_page_number(raw_value, page_count):
    text = str(raw_value).strip()
    if not INTEGER_PATTERN.match(text) or int(text) < 1:
        raise ServiceError("Page number must be a whole number of 1 or more.", 400, "INVALID_PAGE_NUMBER")
    page_number = int(text)
    if page_number > page_count:
        raise ServiceError(
            f"Page {page_number} does not exist. This document has {page_count} page(s).",
            404,
            "PAGE_NOT_FOUND",
        )
    return page_number


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------
def get_extension(filename):
    name = str(filename or "").replace("\\", "/").split("/")[-1]
    if "." not in name:
        return ""
    return "." + name.rsplit(".", 1)[-1].lower()


def validate_extension(filename):
    """Return the lowercase extension (for example ".pdf") or raise ServiceError."""
    if not filename or not str(filename).strip():
        raise ServiceError("Please select a document to upload.", 400, "NO_FILE")
    extension = get_extension(filename)
    if extension not in config.ALLOWED_EXTENSIONS:
        allowed = ", ".join(sorted(ext.lstrip(".").upper() for ext in config.ALLOWED_EXTENSIONS))
        raise ServiceError(
            f"Unsupported file type. Allowed types: {allowed}.", 415, "UNSUPPORTED_FILE_TYPE"
        )
    return extension


def detect_file_type(header):
    """Detect the real file type from the first bytes of the file."""
    if b"%PDF-" in header[:1024]:
        return "pdf"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if header.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if header.startswith(b"BM"):
        return "bmp"
    if header.startswith(b"II*\x00") or header.startswith(b"MM\x00*"):
        return "tiff"
    return None


def validate_file_content(path, extension):
    """
    Check that the file content matches its extension and can be opened.

    Returns a dict with "file_type", "document_kind" and "page_count".
    """
    with open(path, "rb") as handle:
        header = handle.read(2048)

    expected_type = config.ALLOWED_EXTENSIONS[extension]
    detected_type = detect_file_type(header)
    if detected_type is None:
        raise ServiceError(
            "The file content is not a supported PDF or image file.", 415, "INVALID_FILE_CONTENT"
        )
    if detected_type != expected_type:
        raise ServiceError(
            f"The file extension is {extension} but the file content is {detected_type.upper()}.",
            415,
            "FILE_TYPE_MISMATCH",
        )

    if detected_type == "pdf":
        page_count = validate_pdf(path)
        return {"file_type": "pdf", "document_kind": "pdf", "page_count": page_count}

    page_count = validate_image(path)
    return {"file_type": detected_type, "document_kind": "image", "page_count": page_count}


def validate_pdf(path):
    # Open from memory: if MuPDF fails on a damaged file it must not keep a
    # handle on the temporary file (Windows could not delete it otherwise).
    try:
        with open(path, "rb") as handle:
            document = fitz.open(stream=handle.read(), filetype="pdf")
    except Exception as error:
        logger.info("PDF could not be opened: %s", error)
        raise ServiceError("The PDF file is damaged or cannot be read.", 422, "CORRUPTED_PDF")
    try:
        if document.needs_pass:
            raise ServiceError(
                "Password-protected PDF files are not supported.", 422, "PASSWORD_PROTECTED_PDF"
            )
        page_count = document.page_count
        if page_count < 1:
            raise ServiceError("The PDF file has no pages.", 422, "EMPTY_PDF")
        # Loading the first page catches many structurally broken files early.
        document.load_page(0)
        return page_count
    except ServiceError:
        raise
    except Exception as error:
        logger.info("PDF validation failed: %s", error)
        raise ServiceError("The PDF file is damaged or cannot be read.", 422, "CORRUPTED_PDF")
    finally:
        document.close()


def validate_image(path):
    """Verify an image file and return its number of frames (pages)."""
    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            frame_count = getattr(image, "n_frames", 1)
            width, height = image.size
            if width < 1 or height < 1:
                raise ServiceError("The image has no content.", 422, "CORRUPTED_IMAGE")
            if width * height > config.MAX_PAGE_PIXELS:
                raise ServiceError(
                    f"The image is too large to process in memory ({width} x {height} pixels). "
                    "Increase MAX_PAGE_PIXELS in .env if the server has enough memory.",
                    413,
                    "IMAGE_TOO_LARGE",
                )
            image.load()
            return frame_count
    except ServiceError:
        raise
    except Image.DecompressionBombError:
        raise ServiceError("The image is too large to process safely.", 413, "IMAGE_TOO_LARGE")
    except Exception as error:
        logger.info("Image validation failed: %s", error)
        raise ServiceError("The image file is damaged or cannot be read.", 422, "CORRUPTED_IMAGE")


def get_validation_rules():
    extensions = sorted(config.ALLOWED_EXTENSIONS)
    return {
        "allowed_extensions": extensions,
        "max_upload_bytes": None,  # No file size limit.
        "max_page_pixels": config.MAX_PAGE_PIXELS,
        "dpi": {
            "min": config.PDF_DPI_MIN,
            "max": config.PDF_DPI_MAX,
            "default": config.PDF_DPI_DEFAULT,
        },
    }

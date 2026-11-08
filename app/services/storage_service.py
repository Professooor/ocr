"""
Storage layout and safe file helpers.

Every document gets its own folder named after its document ID:

    storage/
        documents/
            <document_id>/
                document.json        processing metadata
                original/            the uploaded file (original.<ext>)
                pages/               page images (page_0001.png, page_0002.png, ...)
                ocr/                 OCR results (page_0001.json, page_0002.json, ...)
        temp/                        temporary upload files
    logs/                            application logs

The user's filename is only kept as metadata; it is never used to build a path.
Document IDs are 32 lowercase hexadecimal characters, which prevents path
traversal through the document ID.
"""

import json
import logging
import os
import re
import shutil
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app import config
from app.errors import ServiceError

logger = logging.getLogger(__name__)

DOCUMENT_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
METADATA_FILENAME = "document.json"
ORIGINAL_FOLDER = "original"
PAGES_FOLDER = "pages"
OCR_FOLDER = "ocr"


def ensure_storage_dirs():
    for folder in (config.DOCUMENTS_DIR, config.TEMP_DIR, config.LOG_DIR):
        folder.mkdir(parents=True, exist_ok=True)


def new_document_id():
    return uuid.uuid4().hex


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def check_document_id(document_id):
    if not isinstance(document_id, str) or not DOCUMENT_ID_PATTERN.match(document_id):
        raise ServiceError("Invalid document ID.", 400, "INVALID_DOCUMENT_ID")
    return document_id


def get_document_dir(document_id):
    return config.DOCUMENTS_DIR / check_document_id(document_id)


def get_original_dir(document_id):
    return get_document_dir(document_id) / ORIGINAL_FOLDER


def get_pages_dir(document_id):
    return get_document_dir(document_id) / PAGES_FOLDER


def get_ocr_dir(document_id):
    return get_document_dir(document_id) / OCR_FOLDER


def get_metadata_path(document_id):
    return get_document_dir(document_id) / METADATA_FILENAME


def page_image_filename(page_number):
    return f"page_{page_number:04d}.png"


def ocr_result_filename(page_number):
    return f"page_{page_number:04d}.json"


def safe_display_filename(filename):
    """Return a cleaned filename for display only (never used as a path)."""
    name = str(filename or "")
    # Keep only the last path component from either Windows or Linux paths.
    name = name.replace("\\", "/").split("/")[-1]
    name = unicodedata.normalize("NFC", name)
    name = "".join(ch for ch in name if ch.isprintable() and ch not in '<>:"|?*')
    name = name.strip().strip(".")
    if len(name) > 200:
        stem, dot, extension = name.rpartition(".")
        name = (stem[: 200 - len(extension) - 1] + dot + extension) if dot else name[:200]
    return name or "document"


def is_inside_storage(path):
    try:
        Path(path).resolve().relative_to(config.STORAGE_DIR)
        return True
    except ValueError:
        return False


def remove_path(path):
    """Delete a file or folder, but only inside the storage folder."""
    path = Path(path)
    if not path.exists():
        return
    if not is_inside_storage(path):
        raise RuntimeError(f"Refusing to delete a path outside storage: {path}")
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def new_temp_path(suffix=""):
    config.TEMP_DIR.mkdir(parents=True, exist_ok=True)
    return config.TEMP_DIR / f"{uuid.uuid4().hex}{suffix}"


def write_json_atomic(path, data):
    """Write JSON to a temporary file first, then replace the target file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    os.replace(temp_path, path)


def read_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def cleanup_temp_dir(max_age_seconds=24 * 3600):
    """Delete temporary upload files left behind by an interrupted request."""
    if not config.TEMP_DIR.exists():
        return 0
    removed = 0
    now = datetime.now().timestamp()
    for item in config.TEMP_DIR.iterdir():
        try:
            if now - item.stat().st_mtime > max_age_seconds:
                remove_path(item)
                removed += 1
        except OSError as error:
            logger.warning("Could not remove temporary file %s: %s", item, error)
    return removed

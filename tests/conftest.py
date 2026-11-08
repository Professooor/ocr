"""
Shared test setup.

Storage and logs are redirected to a temporary folder before the application
is imported, so tests never touch real documents. Test PDFs and images are
generated in memory; no user files are used.
"""

import io
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="document_automation_tests_"))
os.environ["STORAGE_DIR"] = str(TEST_DATA_DIR / "storage")
os.environ["LOG_DIR"] = str(TEST_DATA_DIR / "logs")
os.environ["ORACLE_ENABLED"] = "false"

import fitz  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

from main import app  # noqa: E402


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as test_client:
        yield test_client
    shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)


def make_pdf(page_sizes=((595, 842),), texts=None):
    """Create a PDF in memory. Each page can have its own size and text."""
    document = fitz.open()
    for index, (width, height) in enumerate(page_sizes):
        page = document.new_page(width=width, height=height)
        text = texts[index] if texts else f"Test page {index + 1}"
        if text:
            page.insert_text((50, 100), text, fontsize=20)
    data = document.tobytes()
    document.close()
    return data


def make_image(image_format="PNG", size=(400, 300), mode="RGB", frames=1):
    images = [Image.new(mode, size, color=(255, 255, 255) if mode == "RGB" else None) for _ in range(frames)]
    buffer = io.BytesIO()
    if frames > 1:
        images[0].save(buffer, format=image_format, save_all=True, append_images=images[1:])
    else:
        images[0].save(buffer, format=image_format)
    return buffer.getvalue()


def upload(client, filename, content, dpi=None, content_type="application/octet-stream"):
    data = {} if dpi is None else {"dpi": str(dpi)}
    return client.post(
        "/api/documents/upload",
        files={"file": (filename, content, content_type)},
        data=data,
    )


def run_ocr(client, document_id):
    """Start OCR through the API and wait until the OCR worker is idle."""
    from app.services import ocr_processing_service

    response = client.post(f"/api/documents/{document_id}/ocr")
    ocr_processing_service.wait_until_idle()
    return response


def stored_document_count():
    documents_dir = TEST_DATA_DIR / "storage" / "documents"
    if not documents_dir.exists():
        return 0
    return sum(1 for item in documents_dir.iterdir() if item.is_dir())

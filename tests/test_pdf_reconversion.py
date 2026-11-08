"""PDF re-conversion: reuse images for the same DPI, regenerate for a new DPI."""

import pytest

from app import config
from tests.conftest import make_image, make_pdf, upload


def upload_pdf(client, dpi=75):
    response = upload(client, "reconvert.pdf", make_pdf(((300, 400), (300, 400))), dpi=dpi)
    assert response.status_code == 201, response.text
    return response.json()["data"]["document_id"]


def test_same_dpi_reuses_existing_images(client):
    document_id = upload_pdf(client, dpi=100)
    image_path = config.DOCUMENTS_DIR / document_id / "pages" / "page_0001.png"
    modified_before = image_path.stat().st_mtime_ns

    response = client.post(f"/api/documents/{document_id}/convert", json={"dpi": 100})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["reused"] is True
    assert image_path.stat().st_mtime_ns == modified_before


def test_new_dpi_regenerates_images_and_clears_ocr(client):
    document_id = upload_pdf(client, dpi=75)
    ocr_dir = config.DOCUMENTS_DIR / document_id / "ocr"
    (ocr_dir / "page_0001.json").write_text("{}", encoding="utf-8")

    response = client.post(f"/api/documents/{document_id}/convert", json={"dpi": 150})
    assert response.status_code == 200, response.text
    assert response.json()["data"] == {"document_id": document_id, "dpi": 150, "page_count": 2, "reused": False}

    pages = client.get(f"/api/documents/{document_id}/pages").json()["data"]
    assert pages["dpi"] == 150
    assert all(page["dpi"] == 150 for page in pages["pages"])
    assert pages["pages"][0]["width"] == 625  # ceil(300 * 150 / 72)
    assert all(page["ocr_status"] == "pending" for page in pages["pages"])
    assert not (ocr_dir / "page_0001.json").exists()
    assert "150-" in pages["pages"][0]["image_url"]
    leftovers = [p.name for p in (config.DOCUMENTS_DIR / document_id).iterdir() if p.name.startswith("pages_staging")]
    assert leftovers == []


@pytest.mark.parametrize("payload", [{"dpi": 49}, {"dpi": 301}, {"dpi": "abc"}, {"dpi": 150.0}, {"dpi": None}, {}, {"dpi": True}])
def test_convert_rejects_invalid_dpi(client, payload):
    document_id = upload_pdf(client)
    response = client.post(f"/api/documents/{document_id}/convert", json=payload)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "INVALID_DPI"


def test_convert_without_body_rejected(client):
    document_id = upload_pdf(client)
    response = client.post(f"/api/documents/{document_id}/convert")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_DPI"


def test_convert_image_document_rejected(client):
    response = upload(client, "picture.png", make_image())
    document_id = response.json()["data"]["document_id"]
    response = client.post(f"/api/documents/{document_id}/convert", json={"dpi": 150})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "NOT_A_PDF"


def test_failed_reconversion_keeps_existing_pages(client):
    document_id = upload(client, "keep.pdf", make_pdf(((3000, 3000),)), dpi=50).json()["data"]["document_id"]
    response = client.post(f"/api/documents/{document_id}/convert", json={"dpi": 300})
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "PAGE_TOO_LARGE"
    pages = client.get(f"/api/documents/{document_id}/pages").json()["data"]
    assert pages["dpi"] == 50
    assert client.get(pages["pages"][0]["image_url"]).status_code == 200

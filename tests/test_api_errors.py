"""Invalid IDs, page numbers and routes return the standard JSON error format."""

import pytest

from tests.conftest import make_pdf, upload

UNKNOWN_ID = "0" * 32


@pytest.fixture(scope="module")
def document_id(client):
    response = upload(client, "errors.pdf", make_pdf(((300, 300), (300, 300))), dpi=75)
    return response.json()["data"]["document_id"]


@pytest.mark.parametrize(
    "path",
    [
        "/api/documents/{id}/pages",
        "/api/documents/{id}/pages/1/image",
        "/api/documents/{id}/pages/1/ocr",
        "/api/documents/{id}/ocr/status",
        "/api/documents/{id}/ocr/results",
        "/api/review/documents/{id}",
        "/api/review/documents/{id}/pages/1",
        "/api/documents/{id}/invoice-data",
    ],
)
@pytest.mark.parametrize("bad_id", ["abc", "..%2F..%2Fetc", "ZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZ", "0" * 33])
def test_invalid_document_id(client, path, bad_id):
    response = client.get(path.replace("{id}", bad_id))
    assert response.status_code in (400, 404), response.text
    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] in ("INVALID_DOCUMENT_ID", "NOT_FOUND")


def test_unknown_document_id(client):
    response = client.get(f"/api/review/documents/{UNKNOWN_ID}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DOCUMENT_NOT_FOUND"


@pytest.mark.parametrize("page", ["0", "-1", "abc", "1.5"])
def test_invalid_page_number(client, document_id, page):
    response = client.get(f"/api/documents/{document_id}/pages/{page}/image")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_PAGE_NUMBER"


def test_page_number_out_of_range(client, document_id):
    response = client.get(f"/api/documents/{document_id}/pages/3/image")
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "PAGE_NOT_FOUND"
    assert "2 page" in body["error"]["message"]


def test_ocr_result_missing_before_ocr(client, document_id):
    response = client.get(f"/api/documents/{document_id}/pages/1/ocr")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "OCR_RESULT_NOT_FOUND"


def test_unknown_route_uses_standard_error_format(client):
    response = client.get("/api/does-not-exist")
    assert response.status_code == 404
    assert response.json() == {
        "success": False,
        "error": {"code": "NOT_FOUND", "message": "The requested resource was not found.", "details": None},
    }


def test_wrong_method(client):
    response = client.put("/api/documents/upload")  # DELETE /api/documents/{id} exists now, so use PUT
    assert response.status_code == 405
    assert response.json()["success"] is False


def test_validation_rules_endpoint(client):
    rules = client.get("/api/documents/validation-rules").json()["data"]
    assert rules["dpi"] == {"min": 50, "max": 300, "default": 75}
    assert ".pdf" in rules["allowed_extensions"]


def test_health_endpoint(client):
    body = client.get("/api/health").json()
    assert body["success"] is True
    data = body["data"]
    assert data["status"] == "ok"
    assert data["python_version"] == "3.11.9"
    assert data["ocr"]["engine"] in ("onnxruntime", "paddle")
    assert data["oracle"] == {"enabled": False, "configured": False}

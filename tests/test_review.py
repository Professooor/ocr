"""Document Review API uses stored document, page and OCR data."""

from app.services import ocr_processing_service
from tests.conftest import make_pdf, run_ocr, upload
from tests.test_ocr import FakeEngine


def test_review_document_and_pages(client, monkeypatch):
    response = upload(client, "review.pdf", make_pdf(((300, 400), (300, 400))), dpi=60)
    document_id = response.json()["data"]["document_id"]

    listing = client.get("/api/review/documents").json()["data"]
    assert document_id in [item["document_id"] for item in listing]

    review = client.get(f"/api/review/documents/{document_id}").json()["data"]
    assert review["document"]["original_filename"] == "review.pdf"
    assert review["document"]["conversion"]["dpi"] == 60
    assert [page["page_number"] for page in review["pages"]] == [1, 2]
    assert review["pages"][0]["image_url"].startswith(f"/api/documents/{document_id}/pages/1/image")

    # Before OCR: page info is returned with a clear message and no result.
    page = client.get(f"/api/review/documents/{document_id}/pages/2").json()["data"]
    assert page["page"]["page_number"] == 2
    assert page["ocr_result"] is None
    assert page["message"] == "OCR has not been run for this page yet."

    monkeypatch.setattr(ocr_processing_service, "get_engine", lambda: FakeEngine(fail_on=["page_0002.png"]))
    run_ocr(client, document_id)

    page_one = client.get(f"/api/review/documents/{document_id}/pages/1").json()["data"]
    assert page_one["ocr_result"]["text"] == "sample text"
    assert page_one["message"] is None

    page_two = client.get(f"/api/review/documents/{document_id}/pages/2").json()["data"]
    assert page_two["ocr_result"]["status"] == "failed"
    assert page_two["message"] == "OCR failed for this page (RuntimeError)."

    review = client.get(f"/api/review/documents/{document_id}").json()["data"]
    assert review["document"]["ocr"]["status"] == "partial"
    assert [p["ocr_status"] for p in review["pages"]] == ["completed", "failed"]

"""
OCR processing and OCR results.

The tests marked "real_ocr" run the real PaddleOCR 3.x engine with the
configured models. They are skipped automatically when the models have not
been prepared (scripts/prepare_ocr_models.py).
"""

import time

import pytest

from app.services import ocr_processing_service
from tests.conftest import make_pdf, run_ocr, upload

models_ready = ocr_processing_service.get_engine_info().get("models_ready") is not False
real_ocr = pytest.mark.skipif(not models_ready, reason="OCR models are not prepared")

EXPECTED_LINES = ["DOCUMENT AUTOMATION TEST", "Reference number 4821-7730"]


def upload_text_pdf(client, texts, dpi=150):
    sizes = [(595, 842)] * len(texts)
    response = upload(client, "ocr_test.pdf", make_pdf(sizes, texts), dpi=dpi)
    assert response.status_code == 201, response.text
    return response.json()["data"]["document_id"]


@real_ocr
def test_real_ocr_page_by_page(client):
    document_id = upload_text_pdf(client, [EXPECTED_LINES[0], "", EXPECTED_LINES[1]])

    # run_ocr waits until the OCR worker has finished.
    response = run_ocr(client, document_id)
    assert response.status_code == 202, response.text

    status = client.get(f"/api/documents/{document_id}/ocr/status").json()["data"]
    assert status["status"] == "completed"
    assert [p["ocr_status"] for p in status["pages"]] == ["completed", "no_text", "completed"]
    assert status["pages_completed"] == 2 and status["pages_no_text"] == 1

    page_one = client.get(f"/api/documents/{document_id}/pages/1/ocr").json()["data"]
    assert page_one["page_number"] == 1
    assert page_one["status"] == "completed"
    assert page_one["text"] == EXPECTED_LINES[0]
    line = page_one["lines"][0]
    assert 0.5 < line["confidence"] <= 1.0
    assert len(line["polygon"]) == 4 and len(line["box"]) == 4
    assert page_one["dpi"] == 150
    assert page_one["engine"]["name"] == "PaddleOCR"

    page_three = client.get(f"/api/documents/{document_id}/pages/3/ocr").json()["data"]
    assert page_three["text"] == EXPECTED_LINES[1]

    blank = client.get(f"/api/documents/{document_id}/pages/2/ocr").json()["data"]
    assert blank["status"] == "no_text" and blank["lines"] == []

    results = client.get(f"/api/documents/{document_id}/ocr/results").json()["data"]
    assert [p["page_number"] for p in results["pages"]] == [1, 2, 3]

    # Running OCR again does nothing because every page already has a result.
    again = run_ocr(client, document_id).json()
    assert again["message"] == "OCR has already completed for every page."


class FakeEngine:
    """Stands in for PaddleOCR. Fails for the page image names in fail_on."""

    def __init__(self, fail_on=()):
        self.fail_on = set(fail_on)
        self.calls = []

    def predict(self, image_path):
        self.calls.append(image_path)
        if any(image_path.endswith(name) for name in self.fail_on):
            raise RuntimeError("simulated OCR failure")
        return [{"rec_texts": ["sample text"], "rec_scores": [0.95],
                 "rec_polys": [[[1, 2], [30, 2], [30, 12], [1, 12]]], "rec_boxes": [[1, 2, 30, 12]]}]


def test_failed_page_does_not_lose_other_pages(client, monkeypatch):
    document_id = upload_text_pdf(client, ["a", "b", "c"], dpi=50)
    failing_engine = FakeEngine(fail_on=["page_0002.png"])
    monkeypatch.setattr(ocr_processing_service, "get_engine", lambda: failing_engine)

    run_ocr(client, document_id)
    status = client.get(f"/api/documents/{document_id}/ocr/status").json()["data"]
    assert status["status"] == "partial"
    assert [p["ocr_status"] for p in status["pages"]] == ["completed", "failed", "completed"]
    assert status["pages"][1]["ocr_error"] == "OCR failed for this page (RuntimeError)."

    # Retry processes only the failed page.
    working_engine = FakeEngine()
    monkeypatch.setattr(ocr_processing_service, "get_engine", lambda: working_engine)
    run_ocr(client, document_id)
    assert len(working_engine.calls) == 1 and working_engine.calls[0].endswith("page_0002.png")
    status = client.get(f"/api/documents/{document_id}/ocr/status").json()["data"]
    assert status["status"] == "completed"


def test_engine_load_failure_marks_pages_failed(client, monkeypatch):
    document_id = upload_text_pdf(client, ["a", "b"], dpi=50)

    def broken_engine():
        raise ocr_processing_service.ServiceError("OCR model files are missing.", 503, "OCR_MODELS_MISSING")

    monkeypatch.setattr(ocr_processing_service, "get_engine", broken_engine)
    run_ocr(client, document_id)
    status = client.get(f"/api/documents/{document_id}/ocr/status").json()["data"]
    assert status["status"] == "failed"
    assert status["error"] == "OCR model files are missing."
    assert all(p["ocr_status"] == "failed" for p in status["pages"])


def test_ocr_cannot_start_twice(client, monkeypatch):
    document_id = upload_text_pdf(client, ["a"], dpi=50)
    should_run, _ = ocr_processing_service.queue_ocr(document_id)
    assert should_run is True
    response = run_ocr(client, document_id)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "OCR_IN_PROGRESS"

    # Conversion is blocked while OCR is queued or running.
    response = client.post(f"/api/documents/{document_id}/convert", json={"dpi": 100})
    assert response.status_code == 409

    monkeypatch.setattr(ocr_processing_service, "get_engine", lambda: FakeEngine())
    ocr_processing_service.run_ocr_job(document_id)
    assert client.get(f"/api/documents/{document_id}/ocr/status").json()["data"]["status"] == "completed"


class SlowEngine(FakeEngine):
    def predict(self, image_path):
        time.sleep(0.8)
        return super().predict(image_path)


def test_ocr_runs_in_worker_without_blocking_requests(client, monkeypatch):
    document_id = upload_text_pdf(client, ["a", "b", "c"], dpi=50)
    monkeypatch.setattr(ocr_processing_service, "get_engine", lambda: SlowEngine())

    started = time.time()
    response = client.post(f"/api/documents/{document_id}/ocr")
    assert response.status_code == 202
    assert response.json()["data"]["status"] == "queued"
    assert time.time() - started < 1.0  # The request does not wait for OCR.

    # While OCR runs, other requests are answered immediately.
    request_started = time.time()
    assert client.get("/api/health").status_code == 200
    status = client.get(f"/api/documents/{document_id}/ocr/status").json()["data"]
    assert time.time() - request_started < 1.0
    assert status["status"] in ("queued", "running")

    ocr_processing_service.wait_until_idle()
    status = client.get(f"/api/documents/{document_id}/ocr/status").json()["data"]
    assert status["status"] == "completed"
    assert status["pages_completed"] == 3


def test_interrupted_job_is_recovered_at_startup(client):
    document_id = upload_text_pdf(client, ["a"], dpi=50)
    ocr_processing_service.queue_ocr(document_id)
    assert ocr_processing_service.recover_interrupted_jobs() >= 1
    status = client.get(f"/api/documents/{document_id}/ocr/status").json()["data"]
    assert status["status"] == "interrupted"
    assert "Run OCR again" in status["error"]

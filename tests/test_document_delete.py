"""Delete a document: files are removed, running OCR blocks it, posted invoices need a confirmation."""

from app.services import document_metadata_service, ocr_processing_service, storage_service
from tests.conftest import make_pdf, run_ocr, stored_document_count, upload
from tests.test_invoice_posting import fake_oracle, post, saved_invoice  # noqa: F401  (fixtures)


def new_document(client, name="to_delete.pdf"):
    return upload(client, name, make_pdf(((595, 842),)), dpi=75).json()["data"]["document_id"]


def delete(client, document_id, confirm=False):
    return client.delete(f"/api/documents/{document_id}" + ("?confirm=true" if confirm else ""))


def test_deleting_a_document_removes_its_folder_and_list_entry(client):
    document_id = new_document(client)
    folder = storage_service.get_document_dir(document_id)
    assert folder.exists()
    before = stored_document_count()

    response = delete(client, document_id)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["document_id"] == document_id
    assert not folder.exists()
    assert stored_document_count() == before - 1
    listed = client.get("/api/review/documents").json()["data"]
    assert document_id not in [item["document_id"] for item in listed]
    assert client.get(f"/api/review/documents/{document_id}").status_code == 404


def test_deleting_an_unknown_or_invalid_document_fails(client):
    assert delete(client, "0" * 32).status_code == 404
    assert delete(client, "not-an-id").status_code == 400


def test_a_document_in_ocr_cannot_be_deleted(client):
    document_id = new_document(client, "busy.pdf")

    def mark_running(data):
        data["ocr"] = {"status": "running"}

    document_metadata_service.update_metadata(document_id, mark_running)
    response = delete(client, document_id)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "OCR_IN_PROGRESS"
    assert storage_service.get_document_dir(document_id).exists()

    def mark_done(data):
        data["ocr"] = {"status": "not_started"}

    document_metadata_service.update_metadata(document_id, mark_done)
    assert delete(client, document_id).status_code == 200


def test_document_with_posted_invoices_needs_a_confirmation(client, saved_invoice, fake_oracle):  # noqa: F811
    assert post(client, saved_invoice).status_code == 200

    response = delete(client, saved_invoice)
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "DOCUMENT_HAS_POSTED"
    assert error["details"] == {"posted": 1}
    assert storage_service.get_document_dir(saved_invoice).exists()

    assert delete(client, saved_invoice, confirm=True).status_code == 200
    assert not storage_service.get_document_dir(saved_invoice).exists()


def test_document_without_posted_invoices_needs_no_confirmation(client, saved_invoice):  # noqa: F811
    assert delete(client, saved_invoice).status_code == 200

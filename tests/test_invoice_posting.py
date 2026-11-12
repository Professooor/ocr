"""Post to database: saved invoices go to the Oracle staging tables in one transaction (Oracle is faked)."""

from datetime import date
from decimal import Decimal

import pytest

from app.errors import ServiceError
from app.services import invoice_posting_service, ocr_processing_service, oracle_db_service
from tests.conftest import make_pdf, run_ocr, upload
from tests.test_invoice_data import InvoiceEngine


class FakeVar:
    def __init__(self, value):
        self.value = value

    def getvalue(self):
        return [self.value]


class FakeCursor:
    """Records the statements instead of sending them to Oracle."""

    def __init__(self, new_id=501):
        self.new_id = new_id
        self.statements = []

    def var(self, kind):
        return FakeVar(self.new_id)

    def execute(self, sql, binds):
        self.statements.append((sql, [binds]))

    def executemany(self, sql, rows):
        self.statements.append((sql, list(rows)))

    def rows_for(self, table):
        return [row for sql, rows in self.statements if f"INTO {table} " in sql for row in rows]


@pytest.fixture()
def fake_oracle(monkeypatch):
    """Oracle "configured", every transaction runs against a FakeCursor."""
    cursor = FakeCursor()
    state = {"cursor": cursor, "error": None, "posted_id": None}

    def run_in_transaction(work):
        if state["error"]:
            raise state["error"]
        return work(cursor)

    monkeypatch.setattr(oracle_db_service, "is_configured", lambda: True)
    monkeypatch.setattr(oracle_db_service, "run_in_transaction", run_in_transaction)
    monkeypatch.setattr(
        oracle_db_service, "fetch_all",
        lambda sql, binds=None: [{"invoice_id": state["posted_id"]}] if state["posted_id"] else [],
    )
    return state


@pytest.fixture()
def saved_invoice(client, monkeypatch):
    """A one-page document after (fake) OCR, with its invoice saved as "saved"."""
    document_id = upload(client, "acme_invoice.pdf", make_pdf(((595, 842),)), dpi=150).json()["data"]["document_id"]
    monkeypatch.setattr(ocr_processing_service, "get_engine", lambda: InvoiceEngine())
    run_ocr(client, document_id)
    data = client.get(f"/api/documents/{document_id}/invoice-data").json()["data"]
    values = dict(data["values"], supplier_ntn="1234567-8", vendor_name="Acme Supplies (Pvt) Ltd")
    response = client.put(
        f"/api/documents/{document_id}/invoice-data",
        json={"status": "saved", "values": values, "line_items": data["line_items"]},
    )
    assert response.status_code == 200, response.text
    return document_id


def post(client, document_id, page=1):
    return client.post(f"/api/documents/{document_id}/invoice-data/post", json={"page": page})


def test_posting_needs_a_configured_database(client, saved_invoice):
    response = post(client, saved_invoice)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "ORACLE_NOT_CONFIGURED"


def test_only_saved_invoices_can_be_posted(client, monkeypatch, fake_oracle):
    document_id = upload(client, "draft.pdf", make_pdf(((595, 842),)), dpi=150).json()["data"]["document_id"]
    monkeypatch.setattr(ocr_processing_service, "get_engine", lambda: InvoiceEngine())
    run_ocr(client, document_id)
    response = post(client, document_id)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVOICE_NOT_SAVED"
    assert fake_oracle["cursor"].statements == []


def test_saved_invoice_is_posted_in_one_transaction(client, saved_invoice, fake_oracle):
    response = post(client, saved_invoice)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["status"] == "posted"
    assert data["posting"]["posted"] is True
    assert data["posting"]["staging_id"] == 501
    assert data["posting"]["attempts"] == 1
    assert data["summary"]["posted"] == 1
    assert data["invoice"]["statuses"] == ["posted"]  # Used by "Post all saved"

    cursor = fake_oracle["cursor"]
    header = cursor.rows_for("XXDA_INVOICE_HEADERS")[0]
    assert header["document_id"] == saved_invoice
    assert header["document_ref"].startswith("DOC-")
    assert header["seller_name"] == "Acme Supplies (Pvt) Ltd"
    assert header["seller_ntn"] == "1234567-8"
    assert header["sale_invoice_no"] == "IVN-10012"
    assert header["invoice_date"] == date(2023, 10, 15)
    assert header["grand_total"] == Decimal("550.00")
    assert header["currency_code"] == "USD"
    assert header["status"] == "POSTED"
    assert header["fields_corrected"] == 2  # Vendor name and NTN were changed by the user
    assert "RETURNING INVOICE_ID INTO :new_id" in cursor.statements[0][0]

    lines = cursor.rows_for("XXDA_INVOICE_LINES")
    assert len(lines) == 1
    assert lines[0]["invoice_id"] == 501 and lines[0]["line_no"] == 1
    assert lines[0]["value_excl_tax"] == Decimal("500.00")
    assert lines[0]["line_source"] == "OCR"

    audit = {row["field_key"]: row for row in cursor.rows_for("XXDA_INVOICE_FIELD_AUDIT")}
    assert len(audit) == len(invoice_posting_service.FIELD_COLUMNS)
    assert audit["vendor_name"]["was_corrected"] == 1
    assert audit["vendor_name"]["extracted_value"] == "Acme Supplies Ltd"
    assert audit["invoice_number"]["was_corrected"] == 0

    history = cursor.rows_for("XXDA_INVOICE_STATUS_HISTORY")
    assert history[0]["to_status"] == "POSTED" and history[0]["action"] == "POST"


def test_posted_invoice_is_read_only(client, saved_invoice, fake_oracle):
    assert post(client, saved_invoice).status_code == 200
    data = client.get(f"/api/documents/{saved_invoice}/invoice-data").json()["data"]
    assert data["status"] == "posted"

    response = client.put(
        f"/api/documents/{saved_invoice}/invoice-data",
        json={"status": "saved", "values": data["values"], "line_items": data["line_items"]},
    )
    assert response.status_code == 409 and response.json()["error"]["code"] == "INVOICE_POSTED"
    response = client.post(f"/api/documents/{saved_invoice}/invoice-data/extract", json={"page": 1})
    assert response.status_code == 409 and response.json()["error"]["code"] == "INVOICE_POSTED"
    response = post(client, saved_invoice)
    assert response.status_code == 409 and response.json()["error"]["code"] == "INVOICE_POSTED"


def test_failed_post_keeps_the_invoice_saved_with_the_error(client, saved_invoice, fake_oracle):
    fake_oracle["error"] = ServiceError("Writing to the database failed.", 500, "ORACLE_WRITE_FAILED")
    response = post(client, saved_invoice)
    assert response.status_code == 500
    data = client.get(f"/api/documents/{saved_invoice}/invoice-data").json()["data"]
    assert data["status"] == "saved"
    assert data["posting"]["attempts"] == 1
    assert data["posting"]["last_error"] == "Writing to the database failed."

    fake_oracle["error"] = None  # Database back: posting again works.
    response = post(client, saved_invoice)
    assert response.status_code == 200
    assert response.json()["data"]["posting"]["attempts"] == 2
    header = fake_oracle["cursor"].rows_for("XXDA_INVOICE_HEADERS")[0]
    assert header["post_attempts"] == 2
    assert fake_oracle["cursor"].rows_for("XXDA_INVOICE_STATUS_HISTORY")[0]["action"] == "REPOST"


def test_duplicate_supplier_invoice_number_is_refused(client, saved_invoice, fake_oracle):
    fake_oracle["error"] = ServiceError(
        "constraint", 409, "ORACLE_CONSTRAINT_VIOLATED", {"constraint": "XXDA_INV_HDR_DUP_UK"},
    )
    response = post(client, saved_invoice)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVOICE_DUPLICATE"
    data = client.get(f"/api/documents/{saved_invoice}/invoice-data").json()["data"]
    assert data["status"] == "saved"


def test_invoice_already_in_the_database_is_marked_posted(client, saved_invoice, fake_oracle):
    fake_oracle["error"] = ServiceError(
        "constraint", 409, "ORACLE_CONSTRAINT_VIOLATED", {"constraint": "XXDA_INV_HDR_DOC_UK"},
    )
    fake_oracle["posted_id"] = 777
    response = post(client, saved_invoice)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["status"] == "posted" and data["posting"]["staging_id"] == 777


def test_values_too_long_for_the_database_are_refused(client, monkeypatch, saved_invoice, fake_oracle):
    data = client.get(f"/api/documents/{saved_invoice}/invoice-data").json()["data"]
    values = dict(data["values"], supplier_ntn="1" * 25)
    client.put(
        f"/api/documents/{saved_invoice}/invoice-data",
        json={"status": "saved", "values": values, "line_items": data["line_items"]},
    )
    response = post(client, saved_invoice)
    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["field"] == "supplier_ntn"
    assert fake_oracle["cursor"].statements == []


def test_constraint_name_is_read_from_the_oracle_message():
    message = "ORA-00001: unique constraint (XXDA.XXDA_INV_HDR_DOC_UK) violated"
    assert oracle_db_service.constraint_name(message) == "XXDA_INV_HDR_DOC_UK"
    assert oracle_db_service.constraint_name("ORA-12899: value too large") is None

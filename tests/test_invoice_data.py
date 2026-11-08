"""Invoice data API: autofill from OCR, review, validation, saving, export."""

import io

import pytest
from openpyxl import load_workbook

from app.services import ocr_processing_service
from tests.conftest import make_pdf, run_ocr, upload
from tests.sample_invoice import make_invoice_pdf

ocr_ready = ocr_processing_service.get_engine_info().get("models_ready") is not False

INVOICE_LINES = [
    ("Acme Supplies Ltd", (800, 100, 1150, 125)),
    ("Date Issued", (519, 317, 615, 335)), ("15 October 2023", (519, 345, 661, 365)),
    ("Invoice Number", (769, 317, 897, 334)), ("IVN-10012", (768, 345, 862, 366)),
    ("Due Date", (519, 420, 590, 440)), ("14 November 2023", (519, 448, 680, 470)),
    ("DESCRIPTION", (80, 600, 230, 620)), ("RATE", (700, 600, 760, 620)),
    ("QTY", (800, 600, 850, 620)), ("AMOUNT", (1000, 600, 1100, 620)),
    ("Services", (80, 650, 180, 670)), ("$50.00", (690, 650, 760, 670)),
    ("10", (820, 650, 850, 670)), ("$500.00", (1000, 650, 1100, 670)),
    ("Subtotal", (800, 900, 880, 920)), ("$500.00", (1000, 900, 1100, 920)),
    ("Tax", (800, 930, 840, 950)), ("$50.00", (1030, 930, 1100, 950)),
    ("Total", (800, 960, 850, 980)), ("$550.00", (1000, 960, 1100, 980)),
]


class InvoiceEngine:
    """Stands in for PaddleOCR: returns the lines of a typical invoice with box positions."""

    def __init__(self, lines=None):
        self.lines = INVOICE_LINES if lines is None else lines

    def predict(self, image_path):
        boxes = [list(box) for _, box in self.lines]
        polys = [[[l, t], [r, t], [r, b], [l, b]] for l, t, r, b in boxes]
        return [{
            "rec_texts": [text for text, _ in self.lines],
            "rec_scores": [0.99] * len(self.lines),
            "rec_polys": polys,
            "rec_boxes": boxes,
        }]


@pytest.fixture()
def invoice_document(client, monkeypatch):
    """An uploaded one-page document that has been through (fake) OCR."""
    document_id = upload(client, "acme_invoice.pdf", make_pdf(((595, 842),)), dpi=150).json()["data"]["document_id"]
    monkeypatch.setattr(ocr_processing_service, "get_engine", lambda: InvoiceEngine())
    run_ocr(client, document_id)
    return document_id


def get_data(client, document_id):
    response = client.get(f"/api/documents/{document_id}/invoice-data")
    assert response.status_code == 200, response.text
    return response.json()["data"]


def put_data(client, document_id, status, values=None, line_items=None):
    return client.put(
        f"/api/documents/{document_id}/invoice-data",
        json={"status": status, "values": values or {}, "line_items": line_items or []},
    )


def test_data_is_extracted_automatically_after_ocr(client, invoice_document):
    data = get_data(client, invoice_document)
    assert data["status"] == "needs_review"
    assert data["ocr_ready"] is True
    assert data["values"] == {
        "vendor_name": "Acme Supplies Ltd",
        "invoice_number": "IVN-10012",
        "invoice_date": "2023-10-15",
        "due_date": "2023-11-14",
        "po_number": "",
        "currency": "USD",
        "subtotal": "500.00",
        "tax": "50.00",
        "total_due": "550.00",
    }
    assert data["extracted"]["invoice_number"]["source"] == {"page": 1, "line": 4}
    assert data["extracted"]["vendor_name"]["needs_check"] is True  # Found without a label
    assert data["line_items"] == [{
        "description": "Services", "quantity": "10", "rate": "50.00", "amount": "500.00",
        "confidence": 0.99, "page": 1,
    }]


def test_before_ocr_the_form_is_empty(client):
    document_id = upload(client, "not_read.pdf", make_pdf(), dpi=50).json()["data"]["document_id"]
    data = get_data(client, document_id)
    assert data["ocr_ready"] is False
    assert data["status"] == "needs_review"
    assert all(value == "" for value in data["values"].values())
    assert data["line_items"] == []


def test_save_changes_requires_the_mandatory_fields(client, invoice_document):
    response = put_data(client, invoice_document, "saved", {"vendor_name": "Acme", "invoice_number": "X1"})
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "INVALID_INVOICE_DATA"
    assert sorted(item["field"] for item in error["details"]) == ["due_date", "invoice_date", "total_due"]
    assert all("required" in item["message"] for item in error["details"])
    # Nothing was stored.
    assert get_data(client, invoice_document)["status"] == "needs_review"


def test_draft_needs_no_mandatory_fields(client, invoice_document):
    response = put_data(client, invoice_document, "draft", {"vendor_name": "Half done"})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["status"] == "draft"
    assert data["values"]["vendor_name"] == "Half done"
    assert data["values"]["invoice_number"] == ""
    assert data["saved_at"]


@pytest.mark.parametrize(
    "values,field",
    [
        ({"invoice_date": "15/10/2023"}, "invoice_date"),
        ({"due_date": "2023-02-30"}, "due_date"),
        ({"total_due": "1,810.00"}, "total_due"),
        ({"subtotal": "abc"}, "subtotal"),
        ({"tax": "12.12345"}, "tax"),
        ({"currency": "dollars"}, "currency"),
        ({"vendor_name": "x" * 201}, "vendor_name"),
    ],
)
def test_invalid_values_are_rejected(client, invoice_document, values, field):
    response = put_data(client, invoice_document, "draft", values)
    assert response.status_code == 422, response.text
    assert [item["field"] for item in response.json()["error"]["details"]] == [field]


def test_invalid_line_items_are_rejected(client, invoice_document):
    items = [{"description": "ok", "quantity": "1", "rate": "x", "amount": "5.00"}]
    response = put_data(client, invoice_document, "draft", {}, items)
    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["field"] == "line_items.0.rate"
    response = put_data(client, invoice_document, "draft", {}, "not a list")
    assert response.status_code == 422


@pytest.mark.parametrize("payload", [None, [], {"values": {}}, {"status": "approved", "values": {}}, {"status": "draft"}])
def test_invalid_payloads_are_rejected(client, invoice_document, payload):
    response = client.put(f"/api/documents/{invoice_document}/invoice-data", json=payload)
    assert response.status_code == 422
    # A body that is not a JSON object is already refused by the request validation.
    assert response.json()["error"]["code"] in ("INVALID_INVOICE_DATA", "VALIDATION_ERROR")


def test_corrections_are_saved_and_survive_new_ocr(client, invoice_document, monkeypatch):
    data = get_data(client, invoice_document)
    values = dict(data["values"], vendor_name="Acme Supplies Limited", total_due="560.00", currency="usd")
    items = [
        {"description": "Services (corrected)", "quantity": "10", "rate": "50.00", "amount": "500.00"},
        {"description": "", "quantity": "", "rate": "", "amount": ""},       # empty rows are dropped
        {"description": "Delivery", "quantity": "1", "rate": "10.00", "amount": "10.00"},
    ]
    saved = put_data(client, invoice_document, "saved", values, items).json()["data"]
    assert saved["status"] == "saved"
    assert saved["values"]["vendor_name"] == "Acme Supplies Limited"
    assert saved["values"]["currency"] == "USD"
    assert [i["description"] for i in saved["line_items"]] == ["Services (corrected)", "Delivery"]
    # The value read by OCR is still known, to show what was edited.
    assert saved["extracted"]["vendor_name"]["value"] == "Acme Supplies Ltd"

    # Reading the invoice again must not overwrite what the user saved.
    other = [("Different Vendor", (800, 100, 1150, 125)), ("Total", (800, 960, 850, 980)), ("$9.00", (1000, 960, 1100, 980))]
    monkeypatch.setattr(ocr_processing_service, "get_engine", lambda: InvoiceEngine(other))
    client.post(f"/api/documents/{invoice_document}/pages/1/ocr")  # No effect: page already done
    from app.services import ocr_results_service, storage_service
    result = ocr_results_service.read_page_result_file(invoice_document, 1)
    result["lines"] = result["lines"][:1]
    result["lines"][0]["text"] = "Different Vendor"
    storage_service.write_json_atomic(
        storage_service.get_ocr_dir(invoice_document) / storage_service.ocr_result_filename(1), result
    )
    after = get_data(client, invoice_document)
    assert after["status"] == "saved"
    assert after["values"]["vendor_name"] == "Acme Supplies Limited"
    assert after["extracted"]["vendor_name"]["value"] == "Different Vendor"  # extraction refreshed


def test_unsaved_data_follows_new_ocr_results(client, invoice_document):
    from app.services import ocr_results_service, storage_service
    first = get_data(client, invoice_document)
    assert first["values"]["invoice_number"] == "IVN-10012"
    result = ocr_results_service.read_page_result_file(invoice_document, 1)
    result["lines"][4]["text"] = "IVN-99999"
    storage_service.write_json_atomic(
        storage_service.get_ocr_dir(invoice_document) / storage_service.ocr_result_filename(1), result
    )
    assert get_data(client, invoice_document)["values"]["invoice_number"] == "IVN-99999"


def test_re_extract_discards_corrections(client, invoice_document):
    put_data(client, invoice_document, "draft", {"vendor_name": "Typed by hand"})
    response = client.post(f"/api/documents/{invoice_document}/invoice-data/extract")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["status"] == "needs_review"
    assert data["values"]["vendor_name"] == "Acme Supplies Ltd"


def test_re_extract_needs_ocr_results(client):
    document_id = upload(client, "no_ocr_yet.pdf", make_pdf(), dpi=50).json()["data"]["document_id"]
    response = client.post(f"/api/documents/{document_id}/invoice-data/extract")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "OCR_NOT_READY"


@pytest.mark.parametrize(
    "kind,text,expected",
    [
        ("amount", "Total: $1,234.50", "1234.50"),
        ("date", "Date: 14 November 2023", "2023-11-14"),
        ("code", "Invoice IVN-10012", "IVN-10012"),
        ("text", "Acme Supplies", "Acme Supplies"),
        ("quantity", "Qty 12", "12"),
    ],
)
def test_parse_value_endpoint(client, invoice_document, kind, text, expected):
    response = client.post(f"/api/documents/{invoice_document}/invoice-data/parse-value", json={"kind": kind, "text": text})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["value"] == expected


@pytest.mark.parametrize(
    "payload,code",
    [
        ({"kind": "date", "text": "no date"}, "VALUE_NOT_READABLE"),
        ({"kind": "amount", "text": "abc"}, "VALUE_NOT_READABLE"),
        ({"kind": "select", "text": "USD"}, "INVALID_VALUE_KIND"),
        ({"kind": "date", "text": "  "}, "INVALID_VALUE"),
        ({"kind": "date"}, "INVALID_VALUE"),
    ],
)
def test_parse_value_errors(client, invoice_document, payload, code):
    response = client.post(f"/api/documents/{invoice_document}/invoice-data/parse-value", json=payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == code


def test_document_summary_shows_review_status(client, invoice_document):
    def status():
        listing = client.get("/api/review/documents").json()["data"]
        return next(item for item in listing if item["document_id"] == invoice_document)["data_status"]

    assert status() is None
    values = get_data(client, invoice_document)["values"]
    assert status() == "needs_review"
    put_data(client, invoice_document, "draft", {"vendor_name": "A"})
    assert status() == "draft"
    assert put_data(client, invoice_document, "saved", values).status_code == 200
    assert status() == "saved"


def test_excel_export_contains_the_corrected_invoice_data(client, invoice_document):
    values = dict(get_data(client, invoice_document)["values"], vendor_name="Acme Supplies Limited", tax="")
    items = [{"description": "Services", "quantity": "10", "rate": "50.00", "amount": "500.00"}]
    put_data(client, invoice_document, "saved", values, items)

    workbook = load_workbook(io.BytesIO(client.get(f"/api/documents/{invoice_document}/export/excel").content))
    assert workbook.sheetnames[0] == "Invoice data"
    sheet = workbook["Invoice data"]
    rows = {sheet.cell(row=r, column=1).value: [sheet.cell(row=r, column=c).value for c in (2, 3, 4, 5)] for r in range(5, 14)}
    assert sheet["B2"].value == "Saved"
    assert rows["Vendor name"][:2] == ["Acme Supplies Limited", "Edited"]
    assert rows["Invoice number"][:2] == ["IVN-10012", "Autofilled"]
    assert rows["Invoice date"][0].strftime("%Y-%m-%d") == "2023-10-15"
    assert rows["Subtotal"][0] == 500.0
    assert rows["Tax"][:2] == [None, "Cleared"]
    assert rows["Total due"][0] == 550.0
    line_item_row = next(r for r in range(14, 25) if sheet.cell(row=r, column=1).value == "Services")
    assert [sheet.cell(row=line_item_row, column=c).value for c in (2, 3, 4)] == [10.0, 50.0, 500.0]


@pytest.mark.skipif(not ocr_ready, reason="OCR models are not prepared")
@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({}, {"vendor_name": "NORTHWIND TRADERS LTD", "invoice_number": "IVN-10012", "invoice_date": "2023-10-15",
              "due_date": "2023-11-14", "po_number": "", "subtotal": "1758.00", "tax": "52.00", "total_due": "1810.00"}),
        ({"vendor": "Blue Harbor Supplies Inc", "invoice_number": "INV-2024-0099", "invoice_date": "25/12/2024",
          "due_date": "31/01/2025", "po_number": "PO-77123", "subtotal": "64.50", "tax": "5.16", "total": "69.66",
          "items": [("Widget A", "", "12.50", "4", "50.00"), ("Widget B", "", "7.25", "2", "14.50")]},
         {"vendor_name": "BLUE HARBOR SUPPLIES INC", "invoice_number": "INV-2024-0099", "invoice_date": "2024-12-25",
          "due_date": "2025-01-31", "po_number": "PO-77123", "subtotal": "64.50", "tax": "5.16", "total_due": "69.66"}),
    ],
)
def test_real_ocr_on_a_sample_invoice(client, overrides, expected):
    pdf = make_invoice_pdf(**overrides)
    document_id = upload(client, "sample_invoice.pdf", pdf, dpi=150).json()["data"]["document_id"]
    run_ocr(client, document_id)
    data = get_data(client, document_id)
    for key, value in expected.items():
        assert data["values"][key] == value, key
    assert data["values"]["currency"] == "USD"
    assert len(data["line_items"]) == len(overrides.get("items", [None] * 3))
    first = data["line_items"][0]
    assert (first["quantity"], first["rate"], first["amount"]) in (("10", "50.00", "500.00"), ("4", "12.50", "50.00"))

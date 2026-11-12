"""Invoice data API: autofill from OCR, review, validation, saving, documents with several invoices."""

import pytest

from app.services import invoice_data_service, ocr_processing_service
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
    expected = {key: "" for key in invoice_data_service.FIELD_KEYS}
    expected.update({
        "vendor_name": "Acme Supplies Ltd",
        "invoice_number": "IVN-10012",
        "invoice_date": "2023-10-15",
        "due_date": "2023-11-14",
        "currency": "USD",
        "subtotal": "500.00",
        "sales_tax_rate": "10",  # Worked out from tax / subtotal
        "tax": "50.00",
        "total_due": "550.00",
    })
    assert data["values"] == expected
    assert data["extracted"]["invoice_number"]["source"] == {"page": 1, "line": 4}
    assert data["extracted"]["vendor_name"]["needs_check"] is True  # Found without a label
    item = {key: "" for key in invoice_data_service.LINE_ITEM_KEYS}
    item.update({"description": "Services", "quantity": "10", "rate": "50.00", "amount": "500.00",
                 "confidence": 0.99, "page": 1})
    assert data["line_items"] == [item]


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
        ({"further_tax": "zero"}, "further_tax"),
        ({"sales_tax_rate": "18%"}, "sales_tax_rate"),
        ({"buyer_address": "x" * 501}, "buyer_address"),
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


def test_tax_columns_of_line_items_are_saved(client, invoice_document):
    values = {"vendor_name": "Grandsons Industries", "buyer_name": "Sapphire Textile Mills Limited",
              "supplier_ntn": "2541416-0", "fbr_invoice_number": "2541416DIWE6L6E417248", "further_tax": "0.00"}
    items = [{"description": "Label", "hs_code": "5807.9000", "uom": "PCS", "quantity": "9180", "rate": "10.00",
              "amount": "91800.00", "tax_rate": "18", "sales_tax": "16524.00", "amount_incl_tax": "108324.00"}]
    data = put_data(client, invoice_document, "draft", values, items).json()["data"]
    assert data["values"]["buyer_name"] == "Sapphire Textile Mills Limited"
    assert data["values"]["fbr_invoice_number"] == "2541416DIWE6L6E417248"
    saved = data["line_items"][0]
    assert (saved["hs_code"], saved["uom"], saved["tax_rate"], saved["sales_tax"]) == ("5807.9000", "PCS", "18", "16524.00")
    assert saved["further_tax"] == "" and saved["discount"] == ""

    bad = [dict(items[0], sales_tax="sixteen")]
    response = put_data(client, invoice_document, "draft", values, bad)
    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["field"] == "line_items.0.sales_tax"


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
        {"description": "", "quantity": "", "rate": "", "amount": ""},       # Empty rows are dropped
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
    assert after["extracted"]["vendor_name"]["value"] == "Different Vendor"  # Extraction refreshed


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


# Documents with several invoices
def invoice_page(vendor, number=None, total=None, extra=()):
    lines = [(vendor, (80, 60, 500, 100))]
    if number:
        lines += [("Invoice Number", (769, 317, 897, 334)), (number, (768, 345, 900, 366))]
    if total:
        lines += [("Total", (800, 960, 850, 980)), (total, (1000, 960, 1100, 980))]
    return lines + list(extra)


# Page 1-2: INV-100 and its terms page. Page 3-4: INV-200 printed on both pages. Page 5: "Page 1 of 1", no number.
BATCH_PAGES = {
    1: invoice_page("Acme Supplies Ltd", "INV-100", "$550.00"),
    2: [("Terms and conditions apply", (80, 100, 600, 130))],
    3: invoice_page("Bright Paper Co", "INV-200", "$90.00"),
    4: invoice_page("Bright Paper Co", "INV-200", extra=[("Delivery notes", (80, 400, 300, 430))]),
    5: invoice_page("Carbon Ink Ltd", total="$40.00", extra=[("Page 1 of 1", (500, 1100, 600, 1120))]),
}


class BatchEngine:
    """Returns different invoice lines for each page image (page_0001.png, page_0002.png, ...)."""

    def predict(self, image_path):
        page_number = int(str(image_path).rsplit("page_", 1)[1].split(".")[0])
        return InvoiceEngine(BATCH_PAGES[page_number]).predict(image_path)


@pytest.fixture()
def batch_document(client, monkeypatch):
    document_id = upload(client, "batch.pdf", make_pdf(((595, 842),) * 5), dpi=50).json()["data"]["document_id"]
    monkeypatch.setattr(ocr_processing_service, "get_engine", lambda: BatchEngine())
    run_ocr(client, document_id)
    return document_id


def get_page(client, document_id, page):
    response = client.get(f"/api/documents/{document_id}/invoice-data", params={"page": page})
    assert response.status_code == 200, response.text
    return response.json()["data"]


def put_page(client, document_id, page, status, values=None, pages=None):
    payload = {"status": status, "values": values or {}, "line_items": [], "page": page}
    if pages is not None:
        payload["pages"] = pages
    return client.put(f"/api/documents/{document_id}/invoice-data", json=payload)


def page_break(client, document_id, page, starts):
    return client.post(
        f"/api/documents/{document_id}/invoice-data/page-break", json={"page": page, "starts_invoice": starts}
    )


def test_the_form_shows_the_invoice_of_the_open_page(client, batch_document):
    first = get_page(client, batch_document, 2)
    assert first["invoice"]["pages"] == [1, 2]
    assert first["invoice"]["index"] == 1
    assert first["invoice"]["count"] == 3
    assert first["invoice"]["first_pages"] == [1, 3, 5]
    assert first["values"]["invoice_number"] == "INV-100"
    assert first["values"]["total_due"] == "550.00"

    second = get_page(client, batch_document, 4)
    assert second["invoice"]["pages"] == [3, 4]
    assert second["values"]["invoice_number"] == "INV-200"
    assert second["values"]["total_due"] == "90.00"

    third = get_page(client, batch_document, 5)
    assert third["invoice"]["pages"] == [5]
    assert third["values"]["total_due"] == "40.00"
    assert get_page(client, batch_document, 1)["summary"] == {"invoices": 3, "saved": 0, "draft": 0, "posted": 0, "needs_review": 3}


def test_each_invoice_is_saved_separately(client, batch_document):
    values = dict(get_page(client, batch_document, 3)["values"], vendor_name="Bright Paper Company",
                  invoice_date="2023-10-15", due_date="2023-11-14")
    response = put_page(client, batch_document, 3, "saved", values, pages=[3, 4])
    assert response.status_code == 200, response.text
    assert response.json()["data"]["summary"]["saved"] == 1

    assert get_page(client, batch_document, 4)["values"]["vendor_name"] == "Bright Paper Company"
    first = get_page(client, batch_document, 1)
    assert first["status"] == "needs_review"
    assert first["values"]["invoice_number"] == "INV-100"

    listing = client.get("/api/review/documents").json()["data"]
    item = next(item for item in listing if item["document_id"] == batch_document)
    assert item["data_status"] == "draft"  # Some, not all, invoices are saved.
    assert item["data_counts"] == {"invoices": 3, "saved": 1, "draft": 0}


def test_save_is_refused_when_the_invoice_pages_changed(client, batch_document):
    response = put_page(client, batch_document, 3, "draft", {"vendor_name": "X"}, pages=[3])
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVOICE_PAGES_CHANGED"
    assert get_page(client, batch_document, 3)["status"] == "needs_review"


def test_page_breaks_can_be_corrected(client, batch_document):
    data = page_break(client, batch_document, 2, True).json()["data"]
    assert data["invoice"]["pages"] == [2]
    assert data["invoice"]["first_pages"] == [1, 2, 3, 5]

    data = page_break(client, batch_document, 3, False).json()["data"]
    assert data["invoice"]["pages"] == [2, 3, 4]
    assert data["invoice"]["first_pages"] == [1, 2, 5]

    response = page_break(client, batch_document, 1, False)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_PAGE_BREAK"
    assert client.post(f"/api/documents/{batch_document}/invoice-data/page-break", json={"page": 2}).status_code == 422
    assert page_break(client, batch_document, 9, True).status_code == 404


def test_a_saved_invoice_keeps_its_pages_when_ocr_changes(client, batch_document):
    from app.services import ocr_results_service, storage_service
    assert put_page(client, batch_document, 3, "draft", {"vendor_name": "Kept"}).status_code == 200

    # Page 4 now reads as a different invoice; without the saved draft it would split off.
    result = ocr_results_service.read_page_result_file(batch_document, 4)
    result["lines"][2]["text"] = "INV-999"
    storage_service.write_json_atomic(
        storage_service.get_ocr_dir(batch_document) / storage_service.ocr_result_filename(4), result
    )
    data = get_page(client, batch_document, 4)
    assert data["invoice"]["pages"] == [3, 4]
    assert data["values"]["vendor_name"] == "Kept"


def test_re_extract_resets_only_the_open_invoice(client, batch_document):
    put_page(client, batch_document, 1, "draft", {"vendor_name": "Typed on invoice 1"})
    put_page(client, batch_document, 3, "draft", {"vendor_name": "Typed on invoice 2"})
    response = client.post(f"/api/documents/{batch_document}/invoice-data/extract", json={"page": 4})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["values"]["vendor_name"] == "Bright Paper Co"
    assert get_page(client, batch_document, 1)["values"]["vendor_name"] == "Typed on invoice 1"


@pytest.mark.parametrize("page,status,code", [("9", 404, "PAGE_NOT_FOUND"), ("abc", 400, "INVALID_PAGE_NUMBER")])
def test_invalid_page_is_rejected(client, batch_document, page, status, code):
    response = client.get(f"/api/documents/{batch_document}/invoice-data", params={"page": page})
    assert response.status_code == status
    assert response.json()["error"]["code"] == code


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

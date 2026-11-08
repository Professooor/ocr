"""Rules that read invoice fields from OCR text and box positions."""

from decimal import Decimal

import pytest

from app.services import invoice_extraction_service as extraction


def make_page(entries, width=1240, height=1755):
    """OCR result of one page from (text, (left, top, right, bottom)) entries."""
    return {
        "page_number": 1,
        "image_width": width,
        "image_height": height,
        "lines": [
            {"line_number": i + 1, "text": text, "confidence": 0.99, "box": list(box)}
            for i, (text, box) in enumerate(entries)
        ],
    }


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text,order,expected,ambiguous",
    [
        ("2023-10-15", "dmy", "2023-10-15", False),
        ("15/10/2023", "dmy", "2023-10-15", False),
        ("10/15/2023", "dmy", "2023-10-15", False),   # 15 cannot be a month
        ("03/04/2025", "dmy", "2025-04-03", True),
        ("03/04/2025", "mdy", "2025-03-04", True),
        ("05/05/2025", "dmy", "2025-05-05", False),   # same either way
        ("15 October 2023", "dmy", "2023-10-15", False),
        ("1st Oct 2023", "dmy", "2023-10-01", False),
        ("Oct 15, 2023", "dmy", "2023-10-15", False),
        ("October 15th 2023", "dmy", "2023-10-15", False),
        ("15-Oct-23", "dmy", "2023-10-15", False),
        ("26/3/2021", "dmy", "2021-03-26", False),
    ],
)
def test_parse_dates(text, order, expected, ambiguous):
    found = extraction.parse_dates(text, order)
    assert found[0]["iso"] == expected
    assert found[0]["ambiguous"] is ambiguous


@pytest.mark.parametrize("text", ["32/13/2023", "99/99/9999", "no date here", "INV-10012", "1,234.56"])
def test_parse_dates_rejects_non_dates(text):
    assert extraction.parse_dates(text, "dmy") == []


def test_date_order_is_learned_from_the_document():
    assert extraction.detect_date_order(["01/02/2024", "25/12/2024"]) == "dmy"
    assert extraction.detect_date_order(["01/02/2024", "12/25/2024"]) == "mdy"
    assert extraction.detect_date_order(["nothing"]) in ("dmy", "mdy")


# ---------------------------------------------------------------------------
# Amounts
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text,expected",
    [
        ("$1,758.00", ["1758.00"]),
        ("Total: 1.234,56 EUR", ["1234.56"]),
        ("USD 12,50", ["12.50"]),
        ("Rs. 1,25,000", ["125000"]),
        ("Tax (10%): $82.00", ["82.00"]),          # the percentage is not an amount
        ("-$179.84", ["-179.84"]),
        ("Services 50.00 10 500.00", ["50.00", "10", "500.00"]),
        ("Invoice IVN-10012", []),                 # codes are not amounts
        ("Due 26/03/2021 total 40", ["40"]),       # dates are not amounts
    ],
)
def test_find_amounts(text, expected):
    found = [str(value) for value in extraction.find_amounts(text, "dmy")]
    assert found == [str(Decimal(item)) for item in expected]


def test_amount_formatting():
    assert extraction.format_amount(Decimal("1758")) == "1758.00"
    assert extraction.format_amount(Decimal("12.5")) == "12.50"
    assert extraction.format_amount(Decimal("12.345")) == "12.345"
    assert extraction.format_quantity(Decimal("10")) == "10"
    assert extraction.format_quantity(Decimal("1.50")) == "1.5"


@pytest.mark.parametrize(
    "texts,expected",
    [
        (["Total $50.00"], "USD"),
        (["Total €50,00"], "EUR"),
        (["Total £50.00"], "GBP"),
        (["Amount due PKR 5,000"], "PKR"),
        (["Rs. 5,000"], "PKR"),
        (["Total CAD 40 $"], "CAD"),   # an explicit code beats the dollar sign
        (["Total 50"], ""),
    ],
)
def test_detect_currency(texts, expected):
    assert extraction.detect_currency(texts) == expected


# ---------------------------------------------------------------------------
# Fields
# ---------------------------------------------------------------------------
def test_value_on_the_same_line_as_the_label():
    page = make_page([
        ("Invoice Number: INV-2024-77", (100, 100, 500, 125)),
        ("Invoice Date: 03/12/2024", (100, 140, 500, 165)),
        ("Due Date: 25/12/2024", (100, 180, 500, 205)),
        ("PO Number: PO-5500", (100, 220, 500, 245)),
        ("Subtotal: $1,000.00", (100, 260, 500, 285)),
        ("Tax (10%): $100.00", (100, 300, 500, 325)),
        ("Total Due: $1,100.00", (100, 340, 500, 365)),
    ])
    fields = extraction.extract_invoice_data([page])["fields"]
    assert fields["invoice_number"]["value"] == "INV-2024-77"
    assert fields["invoice_date"]["value"] == "2024-12-03"
    assert fields["due_date"]["value"] == "2024-12-25"
    assert fields["po_number"]["value"] == "PO-5500"
    assert fields["subtotal"]["value"] == "1000.00"
    assert fields["tax"]["value"] == "100.00"
    assert fields["total_due"]["value"] == "1100.00"
    assert fields["invoice_number"]["source"] == {"page": 1, "line": 0}
    assert fields["currency"]["value"] == "USD"


def test_value_to_the_right_of_the_label():
    page = make_page([
        ("Invoice No.", (100, 100, 230, 125)),
        ("A-1009", (400, 101, 500, 126)),
        ("Total", (100, 300, 170, 325)),
        ("$250.00", (400, 300, 500, 325)),
    ])
    fields = extraction.extract_invoice_data([page])["fields"]
    assert fields["invoice_number"]["value"] == "A-1009"
    assert fields["total_due"]["value"] == "250.00"


def test_value_below_the_label():
    page = make_page([
        ("Date Issued", (100, 100, 230, 120)),
        ("Invoice Number", (400, 100, 560, 120)),
        ("Amount Due", (700, 100, 840, 120)),
        ("15 October 2023", (100, 130, 260, 152)),
        ("IVN-10012", (400, 130, 500, 152)),
        ("$1,810.00", (700, 130, 800, 152)),
    ])
    fields = extraction.extract_invoice_data([page])["fields"]
    assert fields["invoice_date"]["value"] == "2023-10-15"
    assert fields["invoice_number"]["value"] == "IVN-10012"
    assert fields["total_due"]["value"] == "1810.00"


def test_amount_due_is_preferred_over_a_plain_total():
    page = make_page([
        ("Total", (100, 100, 170, 125)), ("$900.00", (400, 100, 500, 125)),
        ("Amount Due", (100, 200, 250, 225)), ("$850.00", (400, 200, 500, 225)),
        ("Deposit Due", (100, 300, 250, 325)), ("$100.00", (400, 300, 500, 325)),
    ])
    assert extraction.extract_invoice_data([page])["fields"]["total_due"]["value"] == "850.00"


def test_subtotal_is_not_taken_as_the_total():
    page = make_page([
        ("Subtotal", (100, 100, 220, 125)), ("$800.00", (400, 100, 500, 125)),
        ("Total", (100, 200, 170, 225)), ("$900.00", (400, 200, 500, 225)),
    ])
    fields = extraction.extract_invoice_data([page])["fields"]
    assert fields["subtotal"]["value"] == "800.00"
    assert fields["total_due"]["value"] == "900.00"


def test_po_box_and_tax_id_are_not_mistaken_for_fields():
    page = make_page([
        ("P.O. Box 4410", (100, 100, 300, 125)),
        ("Tax ID: 77-1234567", (100, 140, 400, 165)),
        ("Tax Invoice", (100, 180, 300, 205)),
    ])
    fields = extraction.extract_invoice_data([page])["fields"]
    assert fields["po_number"]["value"] == ""
    assert fields["tax"]["value"] == ""


def test_missing_fields_are_empty_not_invented():
    page = make_page([("Thank you for your business", (100, 100, 600, 125))])
    data = extraction.extract_invoice_data([page])
    assert data["line_items"] == []
    for key in ("invoice_number", "invoice_date", "due_date", "po_number", "subtotal", "tax", "total_due"):
        assert data["fields"][key]["value"] == ""
        assert data["fields"][key]["source"] is None


def test_fallbacks_are_flagged_for_checking():
    page = make_page([
        ("ACME PLUMBING SUPPLIES", (100, 60, 600, 100)),
        ("INVOICE", (100, 120, 360, 190)),
        ("Reference INV-4421", (100, 300, 400, 325)),
        ("12/03/2024", (100, 340, 260, 365)),
    ])
    fields = extraction.extract_invoice_data([page])["fields"]
    assert fields["vendor_name"]["value"] == "ACME PLUMBING SUPPLIES"
    assert fields["vendor_name"]["needs_check"] is True
    assert fields["invoice_number"]["value"] == "INV-4421"
    assert fields["invoice_number"]["needs_check"] is True
    assert fields["invoice_date"]["value"] == "2024-03-12"
    assert fields["invoice_date"]["needs_check"] is True


def test_low_confidence_and_ambiguous_dates_need_checking():
    page = make_page([("Invoice Date: 03/04/2025", (100, 100, 500, 125))])
    page["lines"][0]["confidence"] = 0.6
    fields = extraction.extract_invoice_data([page])["fields"]
    assert fields["invoice_date"]["needs_check"] is True
    clear = make_page([("Invoice Date: 25/04/2025", (100, 100, 500, 125))])
    assert extraction.extract_invoice_data([clear])["fields"]["invoice_date"]["needs_check"] is False


def test_labeled_vendor():
    page = make_page([
        ("INVOICE", (100, 60, 360, 130)),
        ("Vendor: Delta Office Furniture Co", (100, 300, 600, 325)),
    ])
    assert extraction.extract_invoice_data([page])["fields"]["vendor_name"]["value"] == "Delta Office Furniture Co"


# ---------------------------------------------------------------------------
# Line items
# ---------------------------------------------------------------------------
TABLE_HEADER = [
    ("DESCRIPTION", (80, 600, 230, 620)),
    ("RATE", (700, 600, 760, 620)),
    ("QTY", (800, 600, 850, 620)),
    ("AMOUNT", (1000, 600, 1100, 620)),
]


def test_line_items_are_read_by_column_position():
    page = make_page(TABLE_HEADER + [
        ("Services", (80, 650, 180, 670)), ("$50.00", (690, 650, 760, 670)),
        ("10", (820, 650, 850, 670)), ("$500.00", (1000, 650, 1100, 670)),
        ("Cost of labor services", (80, 674, 300, 690)),
        ("Consulting", (80, 720, 200, 740)), ("$75.00", (690, 720, 760, 740)),
        ("15", (820, 720, 850, 740)), ("$1,125.00", (985, 720, 1100, 740)),
        ("Subtotal", (800, 800, 900, 820)), ("$1,625.00", (1000, 800, 1100, 820)),
        ("Thank you", (80, 900, 200, 920)),
    ])
    items = extraction.extract_invoice_data([page])["line_items"]
    assert [(i["description"], i["quantity"], i["rate"], i["amount"]) for i in items] == [
        ("Services - Cost of labor services", "10", "50.00", "500.00"),
        ("Consulting", "15", "75.00", "1125.00"),
    ]


def test_line_item_row_kept_in_one_ocr_line_is_split():
    page = make_page(TABLE_HEADER + [
        ("Widget A 12.50 4 50.00", (80, 650, 1100, 670)),
    ])
    items = extraction.extract_invoice_data([page])["line_items"]
    assert [(i["description"], i["quantity"], i["rate"], i["amount"]) for i in items] == [
        ("Widget A", "4", "12.50", "50.00"),
    ]


def test_no_table_means_no_line_items():
    page = make_page([("Invoice Number: A1", (100, 100, 400, 125))])
    assert extraction.extract_invoice_data([page])["line_items"] == []


def test_parse_value_kinds():
    assert extraction.parse_value("amount", "Total: $1,234.50", "dmy")["value"] == "1234.50"
    assert extraction.parse_value("date", "Issued 03/04/2025", "mdy") == {"value": "2025-03-04", "ambiguous": True}
    assert extraction.parse_value("code", "No. IVN-10012", "dmy")["value"] == "IVN-10012"
    assert extraction.parse_value("text", "  Delta   Traders  ", "dmy")["value"] == "Delta Traders"
    assert extraction.parse_value("quantity", "x 12", "dmy")["value"] == "12"
    assert extraction.parse_value("date", "no date", "dmy") is None

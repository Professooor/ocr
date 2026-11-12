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


# Dates
@pytest.mark.parametrize(
    "text,order,expected,ambiguous",
    [
        ("2023-10-15", "dmy", "2023-10-15", False),
        ("15/10/2023", "dmy", "2023-10-15", False),
        ("10/15/2023", "dmy", "2023-10-15", False),   # 15 cannot be a month
        ("03/04/2025", "dmy", "2025-04-03", True),
        ("03/04/2025", "mdy", "2025-03-04", True),
        ("05/05/2025", "dmy", "2025-05-05", False),   # Same either way
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


# Amounts
@pytest.mark.parametrize(
    "text,expected",
    [
        ("$1,758.00", ["1758.00"]),
        ("Total: 1.234,56 EUR", ["1234.56"]),
        ("USD 12,50", ["12.50"]),
        ("Rs. 1,25,000", ["125000"]),
        ("Tax (10%): $82.00", ["82.00"]),          # The percentage is not an amount
        ("-$179.84", ["-179.84"]),
        ("Services 50.00 10 500.00", ["50.00", "10", "500.00"]),
        ("Invoice IVN-10012", []),                 # Codes are not amounts
        ("Due 26/03/2021 total 40", ["40"]),       # Dates are not amounts
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
        (["Total CAD 40 $"], "CAD"),   # An explicit code beats the dollar sign
        (["Total 50"], ""),
    ],
)
def test_detect_currency(texts, expected):
    assert extraction.detect_currency(texts) == expected


# Fields
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


# Line items
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


# Splitting a document into invoices
def signals(number="", own_data=False, marker=None):
    return {"invoice_number": number, "has_own_data": own_data, "page_marker": marker}


def test_split_into_invoices_by_number_marker_and_own_data():
    pages = [
        (1, signals("INV1", True)),
        (2, signals()),                      # Terms page: belongs to INV1
        (3, signals("INV2", True)),
        (4, signals("INV2")),                # Same number printed again
        (5, signals(own_data=True)),         # No number read, but its own date/total
        (6, signals("INV9", True, marker=2)),  # "Page 2 of 2" wins over the number
        (7, signals("INV2", marker=1)),      # "Page 1" starts a new invoice even with a known number
    ]
    assert extraction.split_into_invoices(pages) == [[1, 2], [3, 4], [5, 6], [7]]


def test_split_into_invoices_unread_pages_and_forced_breaks():
    pages = [(1, signals("A", True)), (2, None), (3, signals()), (4, signals())]
    # An unread page is its own invoice, and the page after it starts a new one.
    assert extraction.split_into_invoices(pages) == [[1], [2], [3, 4]]
    # The user's choices win over the rules; page 1 always starts.
    assert extraction.split_into_invoices(pages, {1: False, 2: False, 3: False, 4: True}) == [[1, 2, 3], [4]]


@pytest.mark.parametrize(
    "text,marker",
    [("Page 1", 1), ("Page 2 of 3", 2), ("page no. 4", 4), ("PAGE: 2/5", 2), ("Pages", None), ("Total 100", None)],
)
def test_page_signals_read_the_page_marker(text, marker):
    page = make_page([("Invoice Number", (100, 100, 250, 120)), ("INV-0012", (100, 130, 220, 150)),
                      (text, (500, 1600, 640, 1620))])
    result = extraction.page_signals(page)
    assert result["page_marker"] == marker
    assert result["invoice_number"] == "INV0012"


# Patterns seen on scanned Pakistani sales tax invoices
@pytest.mark.parametrize(
    "token,expected",
    [("89.747.84", "89747.84"), ("334,554,00", "334554.00"), ("1.234.567", "1234567"), ("1,372,881", "1372881")],
)
def test_ocr_separator_mistakes_are_read_as_amounts(token, expected):
    assert extraction.parse_number_token(token) == Decimal(expected)


def test_registration_numbers_are_not_amounts():
    assert extraction.find_amounts("GST.1206520500964 NTN.0698478-9", "dmy") == []


def test_totals_row_uses_subtotal_plus_tax_not_the_quantity():
    page = make_page([
        ("A.R ENTERPRISES", (265, 55, 371, 67)),
        ("Sales Tax Invoice", (254, 121, 383, 138)),
        ("Invoice No. 3325", (419, 145, 507, 159)),
        ("Total", (182, 594, 206, 606)), ("50.00", (336, 594, 361, 606)),
        ("31,200.00 5,616.00", (407, 594, 491, 607)), ("0.00", (503, 595, 526, 606)),
        ("36,816.00", (533, 595, 575, 606)),
        ("NTN : 0698478", (64, 247, 129, 258)),
    ], width=638, height=825)
    fields = extraction.extract_invoice_data([page])["fields"]
    assert (fields["subtotal"]["value"], fields["tax"]["value"], fields["total_due"]["value"]) == (
        "31200.00", "5616.00", "36816.00")
    assert fields["vendor_name"]["value"] == "A.R ENTERPRISES"  # Not the "Sales Tax Invoice" title
    assert fields["currency"]["value"] == "PKR"


def test_ocr_spelling_of_total_and_a_rate_next_to_the_tax():
    page = make_page([
        ("Grand Totai:-", (148, 665, 203, 676)), ("1,000.00", (228, 665, 261, 676)),
        ("175,000.00", (360, 667, 401, 678)), ("31,500.00", (415, 666, 456, 680)),
        ("0.00", (481, 668, 502, 680)), ("206,500.00", (517, 669, 559, 680)),
    ], width=638, height=825)
    fields = extraction.extract_invoice_data([page])["fields"]
    assert fields["total_due"]["value"] == "206500.00"
    assert fields["tax"]["value"] == "31500.00"

    page = make_page([
        ("Retail Value (PKR)", (315, 306, 383, 320)), ("20,000", (547, 307, 575, 322)),
        ("GST %", (314, 323, 342, 338)), ("18.00", (473, 325, 499, 340)), ("3,600.00", (537, 326, 576, 340)),
        ("Total Amount", (315, 344, 373, 358)), ("23,600.00", (533, 346, 575, 360)),
    ], width=638, height=825)
    fields = extraction.extract_invoice_data([page])["fields"]
    assert (fields["subtotal"]["value"], fields["tax"]["value"], fields["total_due"]["value"]) == (
        "20000.00", "3600.00", "23600.00")


def test_a_missing_part_is_worked_out_from_printed_amounts():
    page = make_page([
        ("Total :", (346, 393, 380, 407)), ("26.224", (396, 394, 430, 405)), ("26,880.00", (537, 394, 584, 405)),
        ("Sales Tax :", (328, 413, 378, 424)), ("18 %", (403, 412, 432, 426)), ("4,838.40", (542, 412, 585, 425)),
        ("Further Tax :", (318, 433, 378, 444)), ("0.00", (560, 430, 586, 445)),
        ("Total Amount :", (306, 453, 377, 464)), ("31,718.40", (537, 452, 584, 463)),
    ], width=638, height=825)
    fields = extraction.extract_invoice_data([page])["fields"]
    assert (fields["subtotal"]["value"], fields["tax"]["value"], fields["total_due"]["value"]) == (
        "26880.00", "4838.40", "31718.40")


def test_supplier_block_next_to_the_buyer_block():
    page = make_page([
        ("Supplier", (45, 137, 82, 151)), ("Buyer", (357, 136, 383, 148)),
        ("Name :", (44, 154, 76, 168)), ("Al-Khair Royal (Private) Limited", (107, 154, 234, 167)),
        ("Name :", (355, 151, 387, 165)), ("Sapphire Textile Mills Limited", (420, 150, 539, 165)),
    ], width=638, height=825)
    assert extraction.extract_invoice_data([page])["fields"]["vendor_name"]["value"] == "Al-Khair Royal (Private) Limited"


def test_vendor_is_never_a_badge_or_the_buyer_and_wrapped_names_are_joined():
    page = make_page([
        ("Ashraf Mechanical Works", (198, 36, 444, 61)),
        ("DIGITAL", (398, 170, 479, 207)),
        ("Buyer/Customer: Sapphire Textile Mills Ltd", (92, 236, 273, 251)),
    ], width=638, height=825)
    assert extraction.extract_invoice_data([page])["fields"]["vendor_name"]["value"] == "Ashraf Mechanical Works"

    page = make_page([
        ("Apex chemical", (429, 35, 585, 65)), ("Technologies pvt Ltd", (366, 60, 584, 92)),
        ("89-Q. JUHAR TOWN, Lahore", (364, 87, 585, 105)),
    ], width=638, height=825)
    assert extraction.extract_invoice_data([page])["fields"]["vendor_name"]["value"] == "Apex chemical Technologies pvt Ltd"


def test_supplier_invoice_number_comes_before_the_fbr_number():
    page = make_page([
        ("Digital Invoice #3310473758337DIHBJV90194820", (196, 168, 460, 184)),
        ("Inv No. 792", (416, 229, 467, 240)),
        ("PO No 220589", (416, 288, 477, 298)),
    ], width=638, height=825)
    fields = extraction.extract_invoice_data([page])["fields"]
    assert fields["invoice_number"]["value"] == "792"
    assert fields["po_number"]["value"] == "220589"

    only_fbr = make_page([("FBR Invoice No: 3520112202325DI33XMT3872003", (153, 212, 375, 228))])
    assert extraction.extract_invoice_data([only_fbr])["fields"]["invoice_number"]["value"] == "3520112202325DI33XMT3872003"


@pytest.mark.parametrize("text,expected", [("P/O #: 223621", "223621"), ("PO,# 217999", "217999"), ("INVOICE NO: 1403.00", None)])
def test_po_and_invoice_number_spellings(text, expected):
    fields = extraction.extract_invoice_data([make_page([(text, (100, 100, 400, 120))])])["fields"]
    if expected:
        assert fields["po_number"]["value"] == expected
    else:
        assert fields["invoice_number"]["value"] == "1403"


def test_supporting_documents_belong_to_the_invoice_before_them():
    undertaking = make_page([("AMAL TRADERS", (270, 54, 527, 92)), ("UNDERTAKING", (280, 171, 370, 184)),
                             ("Date 13-08-2025", (88, 398, 255, 414))], width=638, height=825)
    assert extraction.page_signals(undertaking)["attachment"] is True
    invoice = make_page([("AMAL TRADERS", (130, 29, 269, 48)), ("SALES TAX INVOICE", (130, 59, 267, 76)),
                         ("Gate Pass # : 85", (379, 198, 428, 211))], width=638, height=825)
    assert extraction.page_signals(invoice)["attachment"] is False


# Line items on scanned sales tax invoices: quantity x rate = amount
STN_HEADER = [
    ("Sr.No", (71, 209, 98, 224)), ("DESCRIPTION", (158, 213, 215, 223)), ("QTY", (282, 211, 305, 228)),
    ("RATE", (329, 213, 358, 228)), ("AMOUNT Ed. GST", (372, 210, 427, 221)), ("Sale Tax 18%", (439, 214, 495, 230)),
    ("Total Incl. GST", (504, 216, 565, 230)),
]


def items_of(entries):
    page = make_page(STN_HEADER + entries, width=638, height=825)
    return [(i["description"], i["quantity"], i["rate"], i["amount"])
            for i in extraction.extract_invoice_data([page])["line_items"]]


def test_items_with_serial_unit_and_tax_columns():
    assert items_of([
        ("1", (81, 231, 91, 242)), ("Ball point picasso rip", (105, 231, 184, 243)), ("120", (274, 233, 293, 246)),
        ("pcs", (300, 234, 318, 247)), ("24.00", (331, 234, 356, 246)), ("2880.00", (400, 235, 436, 249)),
        ("518.40", (472, 237, 501, 251)), ("3398.40", (533, 236, 569, 252)),
        ("2", (81, 246, 90, 257)), ("Note Pad Ring Binder", (107, 246, 190, 257)), ("24", (276, 248, 290, 260)),
        ("pcs", (299, 247, 319, 263)), ("140.00", (330, 249, 358, 262)), ("3360.00", (397, 250, 436, 264)),
        ("604.80", (468, 251, 501, 265)), ("3964.80", (533, 252, 569, 266)),
        ("19", (74, 263, 88, 275)), ("0.00", (409, 264, 430, 276)), ("0.00", (476, 264, 498, 276)),
        ("Total", (327, 668, 351, 680)), ("6240.00", (388, 670, 429, 681)),
    ]) == [("Ball point picasso rip", "120", "24.00", "2880.00"), ("Note Pad Ring Binder", "24", "140.00", "3360.00")]


def test_items_with_glued_amounts_tariff_code_and_units():
    assert items_of([
        ("1 4822.1000", (63, 308, 115, 319)), ("Paper Tube 3x130-10MM", (152, 308, 253, 321)),
        ("Numbers,", (292, 306, 324, 317)), ("50.00", (336, 309, 361, 321)), ("624.00", (375, 310, 406, 321)),
        ("31,200.005,616.00", (405, 309, 491, 322)), ("36,816.00", (534, 310, 575, 321)),
    ]) == [("Paper Tube 3x130-10MM", "50", "624.00", "31200.00")]


def test_item_split_over_two_rows_and_unreadable_quantity():
    # "18.00" and the description sit a little higher than the rate and amount; the amount / rate gives
    # the quantity when its cell is unreadable ("1:10.00").
    assert items_of([
        ("18.00", (88, 270, 113, 280)), ("7 PLY CTN 12X10X17 INCH", (126, 268, 210, 280)),
        ("161.00", (337, 284, 363, 295)), ("2,898", (403, 285, 424, 297)), ("522", (476, 285, 495, 297)),
        ("1:10.00", (82, 316, 110, 327)), ("3 PLY SHEET 9X11.5 INCH", (119, 316, 202, 328)),
        ("15.00", (335, 318, 357, 330)), ("1,950", (398, 319, 420, 331)), ("351", (493, 319, 511, 332)),
        ("2,301", (550, 320, 572, 332)),
    ]) == [("7 PLY CTN 12X10X17 INCH", "18", "161.00", "2898.00"), ("3 PLY SHEET 9X11.5 INCH", "130", "15.00", "1950.00")]


# Scans that are turned, tilted or continued
def test_a_page_scanned_on_its_side_is_turned_upright():
    # Landscape invoice scanned standing: text runs top to bottom, the page top is at the right edge.
    width, height = 638, 825
    words = [("Invoice No. : 6530", 132, 80, 150, 161), ("Item Name", 200, 300, 214, 360),
             ("Quantity", 391, 380, 403, 430), ("Rate", 392, 451, 404, 474), ("Amount", 390, 620, 404, 660),
             ("ZIP 0026 INCH #3", 330, 120, 343, 200), ("719.000", 332, 404, 343, 436),
             ("19.00", 331, 466, 346, 494), ("13,661.00", 333, 620, 347, 660), ("Total Weight", 166, 379, 184, 481)]
    page = {"page_number": 1, "image_width": width, "image_height": height, "lines": [
        {"text": text, "confidence": 0.99, "box": [left, top, right, bottom]}
        for text, left, top, right, bottom in words  # Tall, narrow boxes
    ]}
    lines = extraction.collect_lines([page])
    assert lines[0]["page_width"] == height and lines[0]["page_height"] == width
    by_text = {line["text"]: line for line in lines}
    assert by_text["Quantity"]["t"] < by_text["719.000"]["t"]  # The header is above the item after turning
    assert abs(by_text["719.000"]["yc"] - by_text["13,661.00"]["yc"]) < 10  # One row


def test_rows_of_a_tilted_scan_stay_together():
    entries = []
    for row, y in enumerate((360, 385, 410)):
        for column, x in enumerate((100, 200, 300, 400, 500)):
            drift = -x * 0.02  # The scan rises 2 px every 100 px
            entries.append((f"{row}{column}", (x - 20, y + drift - 5, x + 20, y + drift + 5)))
    page = make_page(entries, width=638, height=825)
    rows = extraction.group_rows(extraction.collect_lines([page]))
    assert [len(row["lines"]) for row in rows] == [5, 5, 5]


def test_a_page_that_continues_the_items_table_belongs_to_the_invoice_before():
    page = make_page([
        ("PJN#-161704 FLANNELETTE", (130, 29, 190, 40)), ("7,080", (315, 40, 337, 52)), ("38.00", (340, 41, 365, 52)),
        ("269,040.00", (361, 41, 404, 51)), ("48,427.20", (427, 40, 463, 54)), ("317,467.20", (533, 42, 572, 53)),
        ("FBR Invoice No. : 3806337DIEUW719672055", (100, 300, 400, 312)),
    ], width=638, height=825)
    assert extraction.page_signals(page)["continues_table"] is True
    first = make_page([("SALES TAX INVOICE", (250, 30, 390, 50)), ("1,210", (300, 60, 330, 70)),
                       ("38.00", (340, 60, 365, 70)), ("45,980.00", (370, 60, 410, 70))], width=638, height=825)
    assert extraction.page_signals(first)["continues_table"] is False


def test_pages_with_unreadable_invoice_details_need_more_detail():
    def read(entries):
        return dict(make_page(entries, width=638, height=825), status="completed")

    unreadable = read([
        ("ALES TAX IVVQACE", (254, 74, 296, 81)), ("Iewolse#", (360, 198, 383, 205)), ("17lal-26", (390, 204, 417, 214)),
        ("ITEM DESCRIPTION", (221, 225, 266, 232)), ("AMOUNT", (365, 221, 388, 228)), ("22,400.00", (363, 286, 390, 295)),
    ])
    assert extraction.needs_more_detail(unreadable) is True

    readable = read([
        ("Sales Tax Invoice", (254, 121, 383, 138)), ("Invoice No. 3325", (419, 145, 507, 159)),
        ("Date: 24-Jun-2026", (58, 142, 173, 157)), ("Sub Total:", (401, 404, 448, 421)), ("31,200.00", (531, 409, 577, 419)),
        ("GST:", (423, 418, 449, 433)), ("5,616.00", (531, 421, 577, 432)),
        ("Total:", (409, 436, 448, 451)), ("Rs. 36,816.00", (481, 437, 576, 452)),
    ])
    assert extraction.needs_more_detail(readable) is False

    letter = read([("UNDERTAKING", (280, 171, 370, 184)), ("Dear Sir,", (90, 241, 134, 256)),
                        ("goods mentioned in the Invoice", (89, 266, 400, 286)), ("Yours faithfully,", (86, 439, 166, 456)),
                        ("Authorized Signature", (86, 489, 196, 505))])
    assert extraction.needs_more_detail(letter) is False


# Supplier and buyer details, references, tax columns (FBR sales tax invoices)
def fbr_sales_tax_invoice():
    """A Pakistani sales tax invoice: supplier block at the top, a purchaser block, a taxed item."""
    return make_page([
        ("GRANDSONS INDUSTRIES", (300, 80, 900, 130)),
        ("Factory: Plot # 9-10, Block-3, Industrial Area, Karachi", (320, 140, 900, 160)),
        ("SALES TAX REG. NO. 02-04-5605-004-46", (320, 180, 700, 200)),
        ("N.T.N. 2541416-0", (760, 180, 960, 200)),
        ("Sales Tax Invoice", (150, 260, 450, 290)),
        ("Invoice No. : 6314-25-26", (650, 240, 1000, 260)),
        ("FBR Invoice No. : 2541416DIWE6L6E417248", (650, 265, 1150, 285)),
        ("Date : 23-Jun-2026", (650, 290, 900, 310)),
        ("Purchaser's Details", (450, 340, 700, 360)),
        ("Name : Sapphire Textile Mills Limited", (100, 380, 600, 400)),
        ("Address : Room No 307, Cotton Exchange Building", (100, 410, 600, 430)),
        ("NTN : 0698478-9", (700, 380, 950, 400)),
        ("GST No. : 12-06-5205-009-64", (700, 410, 1000, 430)),
        ("PO No. : 220538", (700, 440, 900, 460)),
        ("DC # : 4471", (700, 470, 900, 490)),
        ("Description Of Goods", (250, 520, 450, 540)),
        ("HS Code", (480, 520, 560, 540)),
        ("Quantity", (580, 520, 680, 540)),
        ("Rate", (700, 520, 760, 540)),
        ("Value Excluding Sales Tax", (780, 520, 900, 540)),
        ("Sales Tax", (920, 520, 1000, 540)),
        ("Value Including", (1020, 520, 1150, 540)),
        ("Label No:025 A", (120, 570, 300, 590)),
        ("5807.9000", (480, 570, 560, 590)),
        ("9,180.00 Pcs", (580, 570, 680, 590)),
        ("10.0000", (700, 570, 770, 590)),
        ("91,800.00", (790, 570, 890, 590)),
        ("18%", (900, 570, 930, 590)),
        ("16,524.00", (930, 570, 1000, 590)),
        ("108,324.00", (1030, 570, 1140, 590)),
        ("Total Value Rs.", (650, 900, 820, 920)),
        ("91,800.00", (1000, 900, 1120, 920)),
        ("Sales Tax @ 18 %", (650, 930, 820, 950)),
        ("16,524.00", (1000, 930, 1120, 950)),
        ("Further S.Tax @ %", (650, 960, 820, 980)),
        ("0.00", (1000, 960, 1120, 980)),
        ("Total Amount Rs.", (650, 990, 820, 1010)),
        ("108,324.00", (1000, 990, 1120, 1010)),
        ("Total Weight : 78.03 Kg", (100, 900, 400, 920)),
        ("In Words : One Hundred Eight Thousand Three Hundred Twenty-Four Rupees Only", (100, 1040, 900, 1060)),
    ])


def test_supplier_and_buyer_details_are_kept_apart():
    fields = extraction.extract_invoice_data([fbr_sales_tax_invoice()])["fields"]
    assert fields["vendor_name"]["value"] == "GRANDSONS INDUSTRIES"
    assert fields["supplier_strn"]["value"] == "02-04-5605-004-46"
    assert fields["supplier_ntn"]["value"] == "2541416-0"
    assert fields["buyer_name"]["value"] == "Sapphire Textile Mills Limited"
    assert fields["buyer_ntn"]["value"] == "0698478-9"
    assert fields["buyer_strn"]["value"] == "12-06-5205-009-64"
    assert fields["buyer_address"]["value"].startswith("Room No 307")


def test_references_and_tax_amounts_are_read():
    fields = extraction.extract_invoice_data([fbr_sales_tax_invoice()])["fields"]
    assert fields["invoice_number"]["value"] == "6314-25-26"
    assert fields["fbr_invoice_number"]["value"] == "2541416DIWE6L6E417248"
    assert fields["po_number"]["value"] == "220538"
    assert fields["dc_number"]["value"] == "4471"
    assert fields["sales_tax_rate"]["value"] == "18"
    assert fields["tax"]["value"] == "16524.00"
    assert fields["further_tax"]["value"] == "0.00"
    assert fields["total_due"]["value"] == "108324.00"
    assert fields["total_quantity"]["value"] == "78.03"
    assert fields["amount_in_words"]["value"].startswith("One Hundred Eight Thousand")


def test_item_row_tax_columns_are_read():
    items = extraction.extract_invoice_data([fbr_sales_tax_invoice()])["line_items"]
    assert len(items) == 1
    item = items[0]
    assert item["description"] == "Label No:025 A"
    assert item["hs_code"] == "5807.9000"
    assert item["uom"] == "PCS"
    assert (item["quantity"], item["rate"], item["amount"]) == ("9180", "10.00", "91800.00")
    assert item["tax_rate"] == "18"
    assert item["sales_tax"] == "16524.00"
    assert item["amount_incl_tax"] == "108324.00"


@pytest.mark.parametrize("value,expected", [("2541416-0", True), ("12-06-5205-009-64", True),
                                             ("3,576.96", False), ("576.96", False), ("12345", False)])
def test_registration_numbers_are_not_amounts(value, expected):
    assert extraction.is_registration_number(value) is expected


def test_percentages_are_read():
    assert extraction.parse_value("percent", "Sales Tax @ 18 %", "dmy")["value"] == "18"
    assert extraction.parse_value("percent", "GST (17.5%)", "dmy")["value"] == "17.5"
    assert extraction.parse_value("percent", "no rate", "dmy") is None

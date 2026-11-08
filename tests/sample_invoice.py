"""
Generate sample invoice PDFs for tests and demos.

The layout follows a typical invoice: title and vendor at the top, a row of
labels with values underneath, a line items table, and totals on the right.
"""

import fitz  # PyMuPDF

PAGE_WIDTH, PAGE_HEIGHT = 595, 842
DARK = (0.1, 0.1, 0.2)
MUTED = (0.4, 0.4, 0.5)
ACCENT = (0.25, 0.25, 0.8)

DEFAULT_INVOICE = {
    "vendor": "Northwind Traders Ltd",
    "invoice_number": "IVN-10012",
    "invoice_date": "15 October 2023",
    "due_date": "14 November 2023",
    "po_number": "",
    "currency_symbol": "$",
    "items": [
        ("Services", "Cost of labor services", "50.00", "10", "500.00"),
        ("Consulting", "Consultant for your business", "75.00", "15", "1,125.00"),
        ("Materials", "", "133.00", "1", "133.00"),
    ],
    "subtotal": "1,758.00",
    "tax": "52.00",
    "total": "1,810.00",
}


def right_text(page, x_right, y, text, size, font="helv", color=DARK):
    width = fitz.get_text_length(text, fontname=font, fontsize=size)
    page.insert_text((x_right - width, y), text, fontname=font, fontsize=size, color=color)


def make_invoice_pdf(**overrides):
    """Return the bytes of a one-page invoice PDF. Keys in overrides replace DEFAULT_INVOICE."""
    data = dict(DEFAULT_INVOICE, **overrides)
    money = data["currency_symbol"]
    document = fitz.open()
    page = document.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)

    page.insert_text((40, 80), "INVOICE", fontname="hebo", fontsize=34, color=DARK)

    # Vendor block, top right
    y = 62
    right_text(page, 555, y, data["vendor"].upper(), 11, "hebo")
    for line in ("1234 Your Street", "City, California 90210", "United States", "1-888-123-4567"):
        y += 13
        right_text(page, 555, y, line, 9, color=MUTED)

    # Billed to
    page.insert_text((40, 160), "Billed To", fontname="hebo", fontsize=9, color=ACCENT)
    for offset, line in enumerate(("Your Client", "1234 Clients Street", "City, California", "90210")):
        page.insert_text((40, 174 + offset * 12), line, fontsize=9, color=MUTED)

    # Labels with values underneath
    for x, label, value in (
        (250, "Date Issued", data["invoice_date"]),
        (370, "Invoice Number", data["invoice_number"]),
        (480, "Amount Due", f"{money}{data['total']}"),
    ):
        page.insert_text((x, 160), label, fontname="hebo", fontsize=8, color=ACCENT)
        page.insert_text((x, 174), value, fontsize=9, color=DARK)
    page.insert_text((250, 204), "Due Date", fontname="hebo", fontsize=8, color=ACCENT)
    page.insert_text((250, 218), data["due_date"], fontsize=9, color=DARK)
    if data["po_number"]:
        page.insert_text((370, 204), "PO Number", fontname="hebo", fontsize=8, color=ACCENT)
        page.insert_text((370, 218), data["po_number"], fontsize=9, color=DARK)

    # Line items table
    top = 290
    page.draw_line((40, top - 14), (555, top - 14), color=ACCENT, width=1.5)
    page.insert_text((40, top), "DESCRIPTION", fontname="hebo", fontsize=8, color=ACCENT)
    right_text(page, 380, top, "RATE", 8, "hebo", ACCENT)
    right_text(page, 450, top, "QTY", 8, "hebo", ACCENT)
    right_text(page, 555, top, "AMOUNT", 8, "hebo", ACCENT)
    y = top + 26
    for title, detail, rate, quantity, amount in data["items"]:
        page.insert_text((40, y), title, fontsize=10, color=DARK)
        right_text(page, 380, y, f"{money}{rate}", 10)
        right_text(page, 450, y, quantity, 10)
        right_text(page, 555, y, f"{money}{amount}", 10)
        if detail:
            page.insert_text((40, y + 12), detail, fontsize=7.5, color=MUTED)
        y += 38

    # Totals
    y += 6
    for label, value, bold in (
        ("Subtotal", data["subtotal"], False),
        ("Tax", data["tax"], False),
        ("Total", data["total"], True),
    ):
        font = "hebo" if bold else "helv"
        page.insert_text((400, y), label, fontname=font, fontsize=10, color=DARK)
        right_text(page, 555, y, f"{money}{value}", 10, font)
        y += 20

    page.insert_text((40, y + 40), "Notes", fontname="hebo", fontsize=9, color=ACCENT)
    page.insert_text((40, y + 54), "Thank you for your business!", fontsize=9, color=MUTED)
    page.insert_text((40, y + 80), "Terms", fontname="hebo", fontsize=9, color=ACCENT)
    page.insert_text((40, y + 94), "Please pay within 30 days.", fontsize=9, color=MUTED)

    data_bytes = document.tobytes()
    document.close()
    return data_bytes

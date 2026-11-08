"""
Export a document's OCR data to an Excel workbook (.xlsx) with openpyxl.

Sheets:
    Invoice data  the reviewed invoice fields and line items (the user's corrections)
    Summary    document details and one row per page (status, lines, confidence)
    Lines      one row per recognised text line (page, line, text, confidence, box)
    Page text  the full recognised text of each page

Safety:
- OCR text is untrusted input. Every text cell is stored as plain text, so a
  line such as "=HYPERLINK(...)" can never become an Excel formula.
- Characters that are not allowed in Excel files (control characters) are
  removed, and cell text is limited to Excel's 32,767 character maximum.
"""

import io
import logging
import re
from datetime import datetime, timezone
from urllib.parse import quote

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from app import config
from app.services import (
    document_metadata_service,
    invoice_data_service,
    ocr_processing_service,
    ocr_results_service,
)

logger = logging.getLogger(__name__)

EXCEL_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
EXCEL_MAX_CELL_LENGTH = 32767
LOW_CONFIDENCE = 0.8

HEADER_FONT = Font(bold=True, color="FFFFFF")
HEADER_FILL = PatternFill("solid", fgColor="4F46E5")
TITLE_FONT = Font(bold=True, size=16, color="1E1B4B")
LABEL_FONT = Font(bold=True, color="475569")
LOW_FILL = PatternFill("solid", fgColor="FEF3C7")
FAILED_FILL = PatternFill("solid", fgColor="FEE2E2")
THIN_BORDER = Border(bottom=Side(style="thin", color="E2E8F0"))
WRAP_TOP = Alignment(wrap_text=True, vertical="top")
TOP = Alignment(vertical="top")

STATUS_LABELS = {
    "not_started": "Not started",
    "queued": "Queued",
    "running": "Running",
    "completed": "Completed",
    "partial": "Partly completed",
    "failed": "Failed",
    "interrupted": "Interrupted",
    "pending": "Pending",
    "no_text": "No text found",
    "needs_review": "Needs review",
    "draft": "Draft",
    "saved": "Saved",
}
MONEY_FORMAT = "#,##0.00"


def clean_text(value):
    """Remove characters Excel cannot store and respect the cell length limit."""
    text = ILLEGAL_CHARACTERS_RE.sub("", str(value or ""))
    if len(text) > EXCEL_MAX_CELL_LENGTH:
        text = text[: EXCEL_MAX_CELL_LENGTH - 15] + " [truncated]"
    return text


def put_text(sheet, row, column, value):
    """Write a cell as plain text (never a formula)."""
    cell = sheet.cell(row=row, column=column)
    cell.value = clean_text(value)
    cell.data_type = "s"
    return cell


def put_value(sheet, row, column, value, number_format=None):
    """Write a number, date or empty cell."""
    cell = sheet.cell(row=row, column=column, value=value)
    if number_format:
        cell.number_format = number_format
    return cell


def to_excel_datetime(iso_text):
    """Excel has no time zones: convert an ISO timestamp to a naive UTC datetime."""
    if not iso_text:
        return None
    try:
        moment = datetime.fromisoformat(iso_text)
    except ValueError:
        return None
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
    return moment


def write_table_header(sheet, row, headers, widths):
    for column, (header, width) in enumerate(zip(headers, widths), start=1):
        cell = put_text(sheet, row, column, header)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(vertical="center")
        sheet.column_dimensions[get_column_letter(column)].width = width
    sheet.row_dimensions[row].height = 22


def load_page_results(document_id, metadata):
    """Return [(page, result_or_None)] in page order."""
    return [
        (page, ocr_results_service.read_page_result_file(document_id, page["page_number"]))
        for page in metadata.get("pages", [])
    ]


def build_summary_sheet(sheet, metadata, page_results):
    ocr = document_metadata_service.build_ocr_summary(metadata)
    all_confidences = [
        line["confidence"]
        for _, result in page_results if result
        for line in result.get("lines", []) if line.get("confidence") is not None
    ]
    total_lines = sum(result.get("line_count", 0) for _, result in page_results if result)
    engine = ocr_processing_service.engine_summary()

    put_text(sheet, 1, 1, "Document OCR export").font = TITLE_FONT
    sheet.row_dimensions[1].height = 26

    details = [
        ("File name", metadata.get("original_filename"), None),
        ("Document ID", metadata["document_id"], None),
        ("File type", str(metadata.get("file_type", "")).upper(), None),
        ("Pages", len(metadata.get("pages", [])), None),
        ("PDF conversion DPI", metadata.get("conversion", {}).get("dpi") or "Original resolution", None),
        ("Uploaded (UTC)", to_excel_datetime(metadata.get("uploaded_at")), "yyyy-mm-dd hh:mm"),
        ("OCR status", STATUS_LABELS.get(ocr["status"], ocr["status"]), None),
        ("Pages with text", ocr["pages_completed"], None),
        ("Pages without text", ocr["pages_no_text"], None),
        ("Pages failed", ocr["pages_failed"], None),
        ("Pages pending", ocr["pages_pending"], None),
        ("Recognised lines", total_lines, None),
        ("Average confidence",
         sum(all_confidences) / len(all_confidences) if all_confidences else None, "0.0%"),
        ("OCR engine", f"{engine['name']} {engine['version'] or ''} ({engine['engine']}, "
                       f"{engine['text_detection_model']} / {engine['text_recognition_model']})", None),
        ("Exported (UTC)", datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0), "yyyy-mm-dd hh:mm"),
    ]
    row = 3
    for label, value, number_format in details:
        put_text(sheet, row, 1, label).font = LABEL_FONT
        if isinstance(value, str):
            put_text(sheet, row, 2, value)
        else:
            put_value(sheet, row, 2, value, number_format)
        sheet.cell(row=row, column=2).alignment = Alignment(horizontal="left")
        row += 1

    row += 1
    put_text(sheet, row, 1, "Pages").font = Font(bold=True, size=13, color="1E1B4B")
    row += 1
    header_row = row
    write_table_header(
        sheet, header_row,
        ["Page", "OCR status", "Lines", "Avg. confidence", "OCR time (s)", "Image width (px)",
         "Image height (px)", "DPI", "Error"],
        [22, 44, 10, 16, 14, 17, 18, 8, 50],
    )
    for page, result in page_results:
        row += 1
        status = result["status"] if result else page.get("ocr_status", "pending")
        put_value(sheet, row, 1, page["page_number"])
        put_text(sheet, row, 2, STATUS_LABELS.get(status, status))
        put_value(sheet, row, 3, result.get("line_count") if result else None)
        put_value(sheet, row, 4, result.get("average_confidence") if result else None, "0.0%")
        put_value(sheet, row, 5, result.get("duration_seconds") if result else None, "0.00")
        put_value(sheet, row, 6, page.get("width"))
        put_value(sheet, row, 7, page.get("height"))
        put_value(sheet, row, 8, page.get("dpi"))
        put_text(sheet, row, 9, (result or {}).get("error") or page.get("ocr_error") or "")
        if status == "failed":
            for column in range(1, 10):
                sheet.cell(row=row, column=column).fill = FAILED_FILL

    sheet.freeze_panes = sheet.cell(row=header_row + 1, column=1)
    if row > header_row:
        sheet.auto_filter.ref = f"A{header_row}:I{row}"


def to_number(text):
    """Excel number from a stored numeric string, or None when empty or not a number."""
    try:
        return float(text) if text not in ("", None) else None
    except ValueError:
        return None


def field_origin(key, value, extracted):
    """Where a value came from: read from the invoice, corrected by the user, or typed in."""
    original = (extracted.get(key) or {}).get("value", "")
    if not value:
        return "Not found" if not original else "Cleared"
    if not original:
        return "Entered manually"
    return "Autofilled" if value == original else "Edited"


def build_invoice_sheet(sheet, data):
    """First sheet: the reviewed invoice fields and line items (what the user corrected)."""
    values, extracted = data["values"], data["extracted"]
    put_text(sheet, 1, 1, "Invoice data").font = TITLE_FONT
    sheet.row_dimensions[1].height = 26
    put_text(sheet, 2, 1, "Review status").font = LABEL_FONT
    put_text(sheet, 2, 2, STATUS_LABELS.get(data["status"], data["status"]))

    write_table_header(sheet, 4, ["Field", "Value", "Origin", "Confidence", "Please verify"], [24, 40, 18, 13, 14])
    row = 4
    for key in invoice_data_service.FIELD_KEYS:
        row += 1
        value = values.get(key, "")
        put_text(sheet, row, 1, invoice_data_service.FIELD_LABELS[key]).font = LABEL_FONT
        if key in invoice_data_service.DATE_KEYS and value:
            cell = put_value(sheet, row, 2, datetime.fromisoformat(value), "yyyy-mm-dd")
        elif key in invoice_data_service.MONEY_KEYS and to_number(value) is not None:
            cell = put_value(sheet, row, 2, to_number(value), MONEY_FORMAT)
        else:
            cell = put_text(sheet, row, 2, value)
        cell.alignment = Alignment(horizontal="left")
        info = extracted.get(key) or {}
        put_text(sheet, row, 3, field_origin(key, value, extracted))
        put_value(sheet, row, 4, info.get("confidence"), "0.0%")
        put_text(sheet, row, 5, "Yes" if info.get("needs_check") else "")
        if info.get("needs_check"):
            sheet.cell(row=row, column=5).fill = LOW_FILL

    row += 2
    put_text(sheet, row, 1, "Line items").font = Font(bold=True, size=13, color="1E1B4B")
    row += 1
    header_row = row
    write_table_header(sheet, header_row, ["Description", "Quantity", "Rate", "Amount"], [24, 40, 18, 13])
    for item in data["line_items"]:
        row += 1
        put_text(sheet, row, 1, item.get("description")).alignment = WRAP_TOP
        put_value(sheet, row, 2, to_number(item.get("quantity")), "General")
        put_value(sheet, row, 3, to_number(item.get("rate")), MONEY_FORMAT)
        put_value(sheet, row, 4, to_number(item.get("amount")), MONEY_FORMAT)
    # The Description column is narrow in the top table, so give the sheet readable widths.
    sheet.column_dimensions["A"].width = 30
    sheet.column_dimensions["B"].width = 42
    if row > header_row:
        sheet.auto_filter.ref = f"A{header_row}:D{row}"


def build_lines_sheet(sheet, page_results):
    write_table_header(
        sheet, 1,
        ["Page", "Line", "Text", "Confidence", "Left (px)", "Top (px)", "Right (px)", "Bottom (px)"],
        [8, 8, 80, 13, 11, 11, 11, 12],
    )
    row = 1
    for page, result in page_results:
        if not result:
            continue
        for line in result.get("lines", []):
            row += 1
            box = line.get("box") or [None, None, None, None]
            put_value(sheet, row, 1, page["page_number"])
            put_value(sheet, row, 2, line.get("line_number"))
            put_text(sheet, row, 3, line.get("text")).alignment = WRAP_TOP
            confidence = line.get("confidence")
            confidence_cell = put_value(sheet, row, 4, confidence, "0.0%")
            if confidence is not None and confidence < LOW_CONFIDENCE:
                confidence_cell.fill = LOW_FILL
            for offset, value in enumerate(box[:4]):
                put_value(sheet, row, 5 + offset, value)
    sheet.freeze_panes = "A2"
    if row > 1:
        sheet.auto_filter.ref = f"A1:H{row}"


def build_page_text_sheet(sheet, page_results):
    write_table_header(sheet, 1, ["Page", "OCR status", "Text"], [8, 16, 110])
    row = 1
    for page, result in page_results:
        row += 1
        status = result["status"] if result else page.get("ocr_status", "pending")
        put_value(sheet, row, 1, page["page_number"]).alignment = TOP
        put_text(sheet, row, 2, STATUS_LABELS.get(status, status)).alignment = TOP
        put_text(sheet, row, 3, (result or {}).get("text") or "").alignment = WRAP_TOP
    sheet.freeze_panes = "A2"


def export_filename(metadata):
    """'<original name without extension>_ocr.xlsx'."""
    original = metadata.get("original_filename") or "document"
    stem = original.rsplit(".", 1)[0] if "." in original else original
    stem = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", stem).strip(" ._") or "document"
    return f"{stem[:120]}_ocr.xlsx"


def content_disposition(filename):
    """Attachment header with an ASCII fallback and the UTF-8 name (RFC 6266 / 5987)."""
    ascii_name = filename.encode("ascii", "ignore").decode("ascii").replace('"', "") or "document_ocr.xlsx"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


def build_excel_export(document_id):
    """Create the workbook. Returns (filename, xlsx_bytes)."""
    metadata = document_metadata_service.load_metadata(document_id)
    page_results = load_page_results(document_id, metadata)

    invoice_data = invoice_data_service.get_invoice_data(document_id)

    workbook = Workbook()
    build_invoice_sheet(workbook.active, invoice_data)
    workbook.active.title = "Invoice data"
    summary = workbook.create_sheet("Summary")
    build_summary_sheet(summary, metadata, page_results)
    build_lines_sheet(workbook.create_sheet("Lines"), page_results)
    build_page_text_sheet(workbook.create_sheet("Page text"), page_results)
    workbook.properties.title = f"OCR export - {clean_text(metadata.get('original_filename'))}"
    workbook.properties.creator = config.APP_NAME

    buffer = io.BytesIO()
    workbook.save(buffer)
    workbook.close()
    logger.info("Document %s: Excel export created (%d bytes)", document_id, buffer.tell())
    return export_filename(metadata), buffer.getvalue()

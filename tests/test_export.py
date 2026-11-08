"""Excel export: workbook structure, contents and safety."""

import io
import zipfile
from urllib.parse import unquote

from openpyxl import load_workbook

from app.services import document_export_service, ocr_processing_service
from tests.conftest import make_pdf, run_ocr, upload

EXCEL_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
RISKY_TEXT = ["Report title", "=1+1", "Bell\x07char", "+cmd|' /C calc'!A0"]


class ExportEngine:
    """Fake OCR engine: page 2 fails, other pages return risky text."""

    def predict(self, image_path):
        if image_path.endswith("page_0002.png"):
            raise RuntimeError("simulated failure")
        return [{
            "rec_texts": RISKY_TEXT,
            "rec_scores": [0.99, 0.95, 0.5, 0.9],
            "rec_polys": [[[1, 2], [30, 2], [30, 12], [1, 12]]] * 4,
            "rec_boxes": [[1, 2, 30, 12]] * 4,
        }]


def summary_values(sheet):
    """Read the label/value block at the top of the Summary sheet."""
    values = {}
    for row in range(3, 30):
        label = sheet.cell(row=row, column=1).value
        if not label:
            break
        values[label] = sheet.cell(row=row, column=2).value
    return values


def test_excel_export_contents(client, monkeypatch):
    response = upload(client, "Résumé report.pdf", make_pdf(((300, 400),) * 3), dpi=60)
    document_id = response.json()["data"]["document_id"]
    monkeypatch.setattr(ocr_processing_service, "get_engine", lambda: ExportEngine())
    run_ocr(client, document_id)

    response = client.get(f"/api/documents/{document_id}/export/excel")
    assert response.status_code == 200
    assert response.headers["content-type"] == EXCEL_TYPE
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert unquote(disposition.split("filename*=UTF-8''")[1]) == "Résumé report_ocr.xlsx"

    workbook = load_workbook(io.BytesIO(response.content))
    assert workbook.sheetnames == ["Invoice data", "Summary", "Lines", "Page text"]

    summary = summary_values(workbook["Summary"])
    assert summary["File name"] == "Résumé report.pdf"
    assert summary["Document ID"] == document_id
    assert summary["Pages"] == 3
    assert summary["PDF conversion DPI"] == 60
    assert summary["OCR status"] == "Partly completed"
    assert summary["Pages with text"] == 2
    assert summary["Pages failed"] == 1
    assert summary["Recognised lines"] == 8

    lines = list(workbook["Lines"].iter_rows(min_row=2, values_only=True))
    assert len(lines) == 8
    assert [row[0] for row in lines] == [1, 1, 1, 1, 3, 3, 3, 3]
    assert [row[1] for row in lines[:4]] == [1, 2, 3, 4]
    assert [row[2] for row in lines[:4]] == ["Report title", "=1+1", "Bellchar", "+cmd|' /C calc'!A0"]
    assert lines[0][3] == 0.99
    assert lines[0][4:8] == (1, 2, 30, 12)

    page_text = workbook["Page text"]
    assert [page_text.cell(row=row, column=2).value for row in (2, 3, 4)] == ["Completed", "Failed", "Completed"]
    assert page_text.cell(row=2, column=3).value.startswith("Report title\n=1+1")


def test_formula_like_text_is_never_a_formula(client, monkeypatch):
    document_id = upload(client, "risky.pdf", make_pdf(), dpi=50).json()["data"]["document_id"]
    monkeypatch.setattr(ocr_processing_service, "get_engine", lambda: ExportEngine())
    run_ocr(client, document_id)

    content = client.get(f"/api/documents/{document_id}/export/excel").content
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        for name in archive.namelist():
            if name.startswith("xl/worksheets/sheet"):
                assert "<f>" not in archive.read(name).decode("utf-8"), name


def test_export_before_ocr(client):
    document_id = upload(client, "not_read_yet.pdf", make_pdf(((300, 400),) * 2), dpi=50).json()["data"]["document_id"]
    response = client.get(f"/api/documents/{document_id}/export/excel")
    assert response.status_code == 200
    workbook = load_workbook(io.BytesIO(response.content))
    assert summary_values(workbook["Summary"])["OCR status"] == "Not started"
    assert workbook["Lines"].max_row == 1  # Header only
    page_text = workbook["Page text"]
    assert [page_text.cell(row=row, column=2).value for row in (2, 3)] == ["Pending", "Pending"]


def test_export_invalid_and_unknown_document(client):
    response = client.get("/api/documents/not-valid/export/excel")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_DOCUMENT_ID"
    response = client.get(f"/api/documents/{'0' * 32}/export/excel")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DOCUMENT_NOT_FOUND"


def test_export_filename_and_header():
    assert document_export_service.export_filename({"original_filename": "scan.page.png"}) == "scan.page_ocr.xlsx"
    assert document_export_service.export_filename({"original_filename": 'a"b<c>.pdf'}) == "a_b_c_ocr.xlsx"
    assert document_export_service.export_filename({"original_filename": ""}) == "document_ocr.xlsx"
    header = document_export_service.content_disposition("Über report_ocr.xlsx")
    assert 'filename="ber report_ocr.xlsx"' in header
    assert "filename*=UTF-8''%C3%9Cber%20report_ocr.xlsx" in header


def test_clean_text_limits():
    assert document_export_service.clean_text("a\x00b\x1fc") == "abc"
    long_text = document_export_service.clean_text("x" * 40000)
    assert len(long_text) <= 32767 and long_text.endswith("[truncated]")

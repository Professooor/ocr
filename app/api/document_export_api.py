"""Document export API: download a document's OCR data as an Excel workbook."""

from fastapi import APIRouter
from fastapi.responses import Response

from app.services import document_export_service

router = APIRouter(prefix="/api/documents", tags=["Document export"])


@router.get("/{document_id}/export/excel")
def export_excel(document_id: str):
    """
    Returns an .xlsx file with three sheets: Summary, Lines and Page text.
    Works at any time; pages without OCR results are listed with their status.
    """
    filename, content = document_export_service.build_excel_export(document_id)
    return Response(
        content=content,
        media_type=document_export_service.EXCEL_MEDIA_TYPE,
        headers={
            "Content-Disposition": document_export_service.content_disposition(filename),
            "Cache-Control": "no-store",
        },
    )

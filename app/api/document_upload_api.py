"""Document upload API: upload one document and create its page images."""

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from app.api.api_responses import success
from app.errors import ServiceError
from app.services import document_upload_service

router = APIRouter(prefix="/api/documents", tags=["Document upload"])


@router.post("/upload", status_code=201)
async def upload_document(request: Request):
    """
    Upload exactly one document (multipart/form-data).

    Fields:
        file  the PDF or image file (exactly one)
        dpi   PDF conversion DPI, whole number 50-300.
              Omitted -> default 75. Present but empty or invalid -> rejected.

    The form is read directly (not with FastAPI Form parameters) because
    FastAPI treats an empty form value as "missing", and an explicitly empty
    DPI must be rejected instead of silently using the default.
    """
    async with request.form(max_files=5, max_fields=20) as form:
        files = [value for value in form.getlist("file") if isinstance(value, UploadFile)]
        if len(files) == 0:
            raise ServiceError("Please select a document to upload.", 400, "NO_FILE")
        if len(files) > 1:
            raise ServiceError("Upload one document at a time.", 400, "MULTIPLE_FILES")

        # None when the field is absent; validated by the upload service.
        dpi = form.get("dpi")

        # Page conversion is CPU work, so it runs in a worker thread.
        document = await run_in_threadpool(document_upload_service.upload_document, files[0], dpi)
    return success(document, "Document uploaded and page images created.")

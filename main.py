"""
Document Automation - FastAPI application entry point.

This file only creates the application and registers its parts.
Business logic lives in app/services, HTTP endpoints in app/api.

Run (development):
    Windows: .\\.venv\\Scripts\\python.exe main.py
    Linux:   .venv/bin/python main.py
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import config
from app.api import (
    document_delete_api,
    document_review_api,
    document_upload_api,
    document_validation_api,
    frontend_api,
    health_api,
    invoice_data_api,
    invoice_posting_api,
    ocr_processing_api,
    ocr_results_api,
    page_image_api,
    pdf_processing_api,
)
from app.api.error_handlers import register_error_handlers
from app.logging_setup import setup_logging
from app.services import (
    document_delete_service,
    document_metadata_service,
    invoice_data_service,
    invoice_extraction_service,
    ocr_processing_service,
    storage_service,
)

setup_logging()
logger = logging.getLogger("main")

# Keep the OCR layer generic: the invoice rules choose which pages are read again at OCR_DETAIL_DPI.
ocr_processing_service.set_detail_check(
    lambda document_id, page_result: invoice_extraction_service.needs_more_detail(page_result)
)


# Deleting a document that holds invoices already sent to the database needs an extra confirmation.
document_delete_service.set_guard(invoice_data_service.delete_warning)


@asynccontextmanager
async def lifespan(app):
    """Runs when the server starts (prepare storage, resume interrupted OCR jobs) and when it stops."""
    storage_service.ensure_storage_dirs()
    storage_service.cleanup_temp_dir()
    document_metadata_service.assign_missing_reference_numbers()
    ocr_processing_service.recover_interrupted_jobs()
    logger.info(
        "%s %s started (env=%s, storage=%s, ocr_engine=%s)",
        config.APP_NAME,
        config.APP_VERSION,
        config.APP_ENV,
        config.STORAGE_DIR,
        config.OCR_ENGINE,
    )
    yield
    logger.info("%s stopped", config.APP_NAME)


app = FastAPI(title=config.APP_NAME, version=config.APP_VERSION, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(config.STATIC_DIR)), name="static")
register_error_handlers(app)

for api_module in (
    frontend_api,
    health_api,
    document_validation_api,
    document_upload_api,
    pdf_processing_api,
    page_image_api,
    ocr_processing_api,
    ocr_results_api,
    document_review_api,
    document_delete_api,
    invoice_data_api,
    invoice_posting_api,
):
    app.include_router(api_module.router)


if __name__ == "__main__":
    import uvicorn

    # One worker: OCR jobs, the OCR model and the metadata lock live in this process.
    uvicorn.run("main:app", host=config.APP_HOST, port=config.APP_PORT, workers=1)

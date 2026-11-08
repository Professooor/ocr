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
    document_export_api,
    document_review_api,
    document_upload_api,
    document_validation_api,
    frontend_api,
    health_api,
    invoice_data_api,
    ocr_processing_api,
    ocr_results_api,
    page_image_api,
    pdf_processing_api,
)
from app.api.error_handlers import register_error_handlers
from app.logging_setup import setup_logging
from app.services import ocr_processing_service, storage_service

setup_logging()
logger = logging.getLogger("main")


@asynccontextmanager
async def lifespan(app):
    storage_service.ensure_storage_dirs()
    storage_service.cleanup_temp_dir()
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
    invoice_data_api,
    document_export_api,
):
    app.include_router(api_module.router)


if __name__ == "__main__":
    import uvicorn

    # One worker: OCR jobs, the OCR model and the metadata lock live in this process.
    uvicorn.run("main:app", host=config.APP_HOST, port=config.APP_PORT, workers=1)

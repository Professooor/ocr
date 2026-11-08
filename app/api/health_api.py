"""Health API: quick status check for monitoring and deployment verification."""

import platform
import sys

from fastapi import APIRouter

from app import config
from app.api.api_responses import success
from app.services import ocr_processing_service, oracle_db_service

router = APIRouter(prefix="/api", tags=["Health"])


@router.get("/health")
def health():
    """Does not load the OCR engine and does not connect to Oracle."""
    storage_ok = config.DOCUMENTS_DIR.is_dir() and config.TEMP_DIR.is_dir()
    return success(
        {
            "status": "ok" if storage_ok else "degraded",
            "app_version": config.APP_VERSION,
            "python_version": platform.python_version(),
            "platform": sys.platform,
            "storage_ready": storage_ok,
            "ocr": ocr_processing_service.get_engine_info(),
            "oracle": {"enabled": config.ORACLE_ENABLED, "configured": oracle_db_service.is_configured()},
        }
    )

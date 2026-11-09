"""Document validation API: publish the upload and DPI rules used by the backend."""

from fastapi import APIRouter

from app.api.api_responses import success
from app.services import document_validation_service

router = APIRouter(prefix="/api/documents", tags=["Document validation"])


@router.get("/validation-rules")
def get_validation_rules():
    """Return the allowed file types, size limit and DPI limits so the upload page can show them."""
    return success(document_validation_service.get_validation_rules())

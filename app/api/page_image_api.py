"""Page image API: list the pages of a document and return single page images."""

from fastapi import APIRouter
from fastapi.responses import FileResponse

from app.api.api_responses import success
from app.services import page_image_service

router = APIRouter(prefix="/api/documents", tags=["Page images"])


@router.get("/{document_id}/pages")
def list_pages(document_id: str):
    return success(page_image_service.list_pages(document_id))


@router.get("/{document_id}/pages/{page_number}/image")
def get_page_image(document_id: str, page_number: str):
    image_path = page_image_service.get_page_image_path(document_id, page_number)
    return FileResponse(
        image_path,
        media_type="image/png",
        headers={"Cache-Control": "private, max-age=3600"},
    )

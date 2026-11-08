"""HTML pages: Upload and Document Review."""

from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

from app import config
from app.services import ocr_processing_service

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(config.TEMPLATES_DIR))


def get_ocr_engine_label():
    """Short engine description for the page footer, for example
    'PaddleOCR 3.7.0 · onnxruntime · PP-OCRv6 small'."""
    version = ocr_processing_service.package_version("paddleocr") or "not installed"
    model = config.OCR_TEXT_RECOGNITION_MODEL.replace("_rec", "").replace("_", " ")
    return f"PaddleOCR {version} · {config.OCR_ENGINE} · {model}"


def render_page(request, template_name, context=None):
    page_context = {
        "app_name": config.APP_NAME,
        "app_version": config.APP_VERSION,
        "root_path": request.scope.get("root_path", ""),
        "ocr_engine_label": get_ocr_engine_label(),
    }
    page_context.update(context or {})
    return templates.TemplateResponse(request, template_name, page_context)


@router.get("/")
def upload_page(request: Request):
    return render_page(
        request,
        "upload.html",
        {
            "dpi_min": config.PDF_DPI_MIN,
            "dpi_max": config.PDF_DPI_MAX,
            "dpi_default": config.PDF_DPI_DEFAULT,
            "allowed_extensions": sorted(config.ALLOWED_EXTENSIONS),
        },
    )


@router.get("/review")
def review_list_page(request: Request):
    return render_page(request, "review.html", {"document_id": ""})


@router.get("/review/{document_id}")
def review_document_page(request: Request, document_id: str):
    # The ID is only placed in a data attribute (auto-escaped); review.js loads
    # the document through the API, which validates the ID.
    return render_page(request, "review.html", {"document_id": document_id})

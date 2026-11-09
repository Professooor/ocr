"""
Exception handlers: turn every error into the standard JSON error format.

Internal details (tracebacks, file paths, credentials) are written to the log
only. The browser receives a short, understandable message.
"""

import logging

from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.api_responses import error_response
from app.errors import ServiceError

logger = logging.getLogger(__name__)

HTTP_ERROR_CODES = {
    400: "BAD_REQUEST",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    413: "REQUEST_TOO_LARGE",
}


async def handle_service_error(request, error):
    """A service raised ServiceError: return its message and status code. Only server errors (5xx) are logged here."""
    if error.status_code >= 500:
        logger.error("%s %s -> %s %s", request.method, request.url.path, error.code, error.message)
    return error_response(error.status_code, error.code, error.message, error.details)


async def handle_http_error(request, error):
    """Handle normal HTTP errors such as 404 (page or file not found) and 405."""
    code = HTTP_ERROR_CODES.get(error.status_code, "HTTP_ERROR")
    message = "The requested resource was not found." if error.status_code == 404 else str(error.detail)
    return error_response(error.status_code, code, message)


async def handle_validation_error(request, error):
    """Handle invalid request data (wrong type, missing field) with a list of the problems."""
    problems = []
    for item in error.errors():
        location = ".".join(str(part) for part in item.get("loc", []) if part not in ("body", "query", "path"))
        problems.append({"field": location, "message": item.get("msg")})
    return error_response(422, "VALIDATION_ERROR", "The request contains invalid values.", problems)


async def handle_unexpected_error(request, error):
    """Last safety net: log the full traceback, but send only a generic message to the browser."""
    logger.exception("Unexpected error on %s %s", request.method, request.url.path)
    return error_response(500, "INTERNAL_ERROR", "An unexpected error occurred. Please check the application log.")


def register_error_handlers(app):
    """Connect all handlers above to the FastAPI app. Called once from main.py."""
    app.add_exception_handler(ServiceError, handle_service_error)
    app.add_exception_handler(StarletteHTTPException, handle_http_error)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(Exception, handle_unexpected_error)

"""
Consistent JSON response format for every API endpoint.

Success: {"success": true,  "message": "...", "data": {...}}
Error:   {"success": false, "error": {"code": "...", "message": "...", "details": ...}}
"""

from fastapi.responses import JSONResponse


def success(data=None, message=None):
    """Build the standard success response. The frontend JavaScript relies on this shape."""
    return {"success": True, "message": message, "data": data}


def error_response(status_code, code, message, details=None):
    """Build the standard JSON error response with the given HTTP status code."""
    return JSONResponse(
        status_code=status_code,
        content={
            "success": False,
            "error": {"code": code, "message": message, "details": details},
        },
    )

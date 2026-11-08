"""
Consistent JSON response format for every API endpoint.

Success: {"success": true,  "message": "...", "data": {...}}
Error:   {"success": false, "error": {"code": "...", "message": "...", "details": ...}}
"""

from fastapi.responses import JSONResponse


def success(data=None, message=None):
    return {"success": True, "message": message, "data": data}


def error_response(status_code, code, message, details=None):
    return JSONResponse(
        status_code=status_code,
        content={
            "success": False,
            "error": {"code": code, "message": message, "details": details},
        },
    )

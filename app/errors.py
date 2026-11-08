"""
The single application error type.

Services raise ServiceError with a user-friendly message. The API layer turns
it into a JSON error response with the given HTTP status code. Unexpected
exceptions are logged with their traceback and returned as a generic message,
so internal details never reach the browser.
"""


class ServiceError(Exception):
    def __init__(self, message, status_code=400, code="BAD_REQUEST", details=None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code
        self.details = details

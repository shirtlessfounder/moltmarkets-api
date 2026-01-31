"""
Standardized error responses for MoltMarkets API.

Provides:
- Error code constants for machine-readable error classification
- error_response() helper that returns a JSONResponse with standard body
- APIError exception for use in dependencies/helpers where return isn't possible
- Exception handlers to register on the FastAPI app

Standard error body:
    {"error": "Human-readable message", "code": "ERROR_CODE", "detail": {...}}
"""

from typing import Optional
from fastapi import Request, HTTPException
from fastapi.responses import JSONResponse
import logging

logger = logging.getLogger(__name__)


# =============================================================================
# Error Code Constants
# =============================================================================

class ErrorCode:
    """Machine-readable error codes for API consumers."""

    # Authentication / Authorization
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN = "FORBIDDEN"

    # Resource not found
    MARKET_NOT_FOUND = "MARKET_NOT_FOUND"
    USER_NOT_FOUND = "USER_NOT_FOUND"
    AGENT_NOT_FOUND = "AGENT_NOT_FOUND"
    COMMENT_NOT_FOUND = "COMMENT_NOT_FOUND"
    NOT_FOUND = "NOT_FOUND"

    # Input validation
    INVALID_INPUT = "INVALID_INPUT"

    # Business logic
    INSUFFICIENT_BALANCE = "INSUFFICIENT_BALANCE"
    MARKET_CLOSED = "MARKET_CLOSED"
    ALREADY_EXISTS = "ALREADY_EXISTS"
    ALREADY_RESOLVED = "ALREADY_RESOLVED"
    ALREADY_CLAIMED = "ALREADY_CLAIMED"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    INSUFFICIENT_SHARES = "INSUFFICIENT_SHARES"
    ZERO_SHARES = "ZERO_SHARES"
    NO_POSITION = "NO_POSITION"
    MARKET_DURATION_EXCEEDED = "MARKET_DURATION_EXCEEDED"
    CLAIM_REQUIRED = "CLAIM_REQUIRED"

    # Committee resolution
    COMMITTEE_WINDOW_ACTIVE = "COMMITTEE_WINDOW_ACTIVE"
    NOT_COMMITTEE_MEMBER = "NOT_COMMITTEE_MEMBER"

    # Rate limiting
    RATE_LIMITED = "RATE_LIMITED"

    # Server errors
    INTERNAL_ERROR = "INTERNAL_ERROR"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    BAD_GATEWAY = "BAD_GATEWAY"
    GATEWAY_TIMEOUT = "GATEWAY_TIMEOUT"


# =============================================================================
# error_response helper
# =============================================================================

def error_response(
    status_code: int,
    message: str,
    code: str,
    detail: Optional[dict] = None,
    headers: Optional[dict] = None,
) -> JSONResponse:
    """Return a standardized JSON error response.

    Body format:
        {"error": "message", "code": "ERROR_CODE"}
        {"error": "message", "code": "ERROR_CODE", "detail": {...}}
    """
    body: dict = {"error": message, "code": code}
    if detail is not None:
        body["detail"] = detail
    return JSONResponse(status_code=status_code, content=body, headers=headers)


# =============================================================================
# APIError exception (for dependencies / helpers where return isn't possible)
# =============================================================================

class APIError(Exception):
    """Custom exception that carries structured error info.

    Use in dependency functions (get_current_user, require_auth, etc.)
    and helper functions (_validate_uuid, fetch_tweet, etc.) where
    ``return error_response(...)`` isn't possible.

    The registered exception handler converts this to the standard format.
    """

    def __init__(
        self,
        status_code: int,
        message: str,
        code: str,
        detail: Optional[dict] = None,
        headers: Optional[dict] = None,
    ):
        self.status_code = status_code
        self.message = message
        self.code = code
        self.detail = detail
        self.headers = headers
        super().__init__(message)


# =============================================================================
# Exception Handlers (register on the FastAPI app)
# =============================================================================

async def api_error_handler(request: Request, exc: APIError) -> JSONResponse:
    """Handle APIError exceptions → standard error format."""
    return error_response(
        status_code=exc.status_code,
        message=exc.message,
        code=exc.code,
        detail=exc.detail,
        headers=exc.headers,
    )


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """Handle any remaining HTTPException → standard error format.

    Maps common status codes to error codes for backward compatibility
    with any HTTPExceptions raised by FastAPI itself (e.g. 422 validation).
    """
    status_to_code = {
        400: ErrorCode.INVALID_INPUT,
        401: ErrorCode.UNAUTHORIZED,
        403: ErrorCode.FORBIDDEN,
        404: ErrorCode.NOT_FOUND,
        405: ErrorCode.INVALID_INPUT,
        422: ErrorCode.INVALID_INPUT,
        429: ErrorCode.RATE_LIMITED,
        500: ErrorCode.INTERNAL_ERROR,
        502: ErrorCode.BAD_GATEWAY,
        503: ErrorCode.SERVICE_UNAVAILABLE,
        504: ErrorCode.GATEWAY_TIMEOUT,
    }
    code = status_to_code.get(exc.status_code, ErrorCode.INTERNAL_ERROR)
    message = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    headers = getattr(exc, "headers", None)
    return error_response(
        status_code=exc.status_code,
        message=message,
        code=code,
        headers=headers,
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all for unhandled exceptions → INTERNAL_ERROR."""
    logger.exception("Unhandled exception: %s", exc)
    return error_response(
        status_code=500,
        message="An unexpected error occurred.",
        code=ErrorCode.INTERNAL_ERROR,
    )

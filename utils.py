"""
Small shared utility functions (pagination, validation, cache headers).

Extracted from deps.py — see issue #128.
"""

import uuid
from datetime import datetime
from typing import Optional

from errors import APIError, ErrorCode

# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

PAGINATION_DEFAULT_LIMIT = 50
PAGINATION_MAX_LIMIT = 100


def clamp_pagination(limit: Optional[int], offset: Optional[int]) -> tuple:
    """Clamp and validate pagination parameters."""
    if limit is None or limit < 1:
        limit = PAGINATION_DEFAULT_LIMIT
    if limit > PAGINATION_MAX_LIMIT:
        limit = PAGINATION_MAX_LIMIT
    if offset is None or offset < 0:
        offset = 0
    return limit, offset


# ---------------------------------------------------------------------------
# UUID validation
# ---------------------------------------------------------------------------

def validate_uuid(value: str, param_name: str = "id") -> None:
    """Validate that a string is a valid UUID.  Raises APIError(400) if not."""
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError):
        raise APIError(
            status_code=400,
            message=f"Invalid {param_name}: '{value}' is not a valid UUID",
            code=ErrorCode.INVALID_INPUT,
        )


# ---------------------------------------------------------------------------
# Cache header helper
# ---------------------------------------------------------------------------

def set_cache_headers(response, etag: str, last_modified: datetime) -> None:
    """Set ETag, Last-Modified, and Cache-Control headers."""
    response.headers["ETag"] = etag
    response.headers["Last-Modified"] = last_modified.strftime("%a, %d %b %Y %H:%M:%S GMT")
    response.headers["Cache-Control"] = "public, max-age=5"

"""
MoltMarkets API — Structured JSON Logging.

Provides:
- JSON structured logging via structlog
- Request ID middleware for distributed tracing
- Request/response logging middleware
- Configured log levels: DEBUG, INFO, WARNING, ERROR

Usage:
    from logger import get_logger, configure_logging
    
    configure_logging()  # Call once at startup
    logger = get_logger(__name__)
    logger.info("message", key="value")
"""

import logging
import os
import sys
import time
import uuid
from contextvars import ContextVar
from typing import Callable

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

# ---------------------------------------------------------------------------
# Context Variables for Request Tracing
# ---------------------------------------------------------------------------

request_id_ctx: ContextVar[str | None] = ContextVar("request_id", default=None)
user_id_ctx: ContextVar[str | None] = ContextVar("user_id", default=None)


def get_request_id() -> str | None:
    """Get the current request ID from context."""
    return request_id_ctx.get()


def get_user_id() -> str | None:
    """Get the current user ID from context."""
    return user_id_ctx.get()


# ---------------------------------------------------------------------------
# Structlog Processors
# ---------------------------------------------------------------------------

def add_request_context(
    logger: logging.Logger,
    method_name: str,
    event_dict: dict,
) -> dict:
    """Add request context (request_id, user_id) to log events."""
    request_id = request_id_ctx.get()
    user_id = user_id_ctx.get()
    
    if request_id:
        event_dict["request_id"] = request_id
    if user_id:
        event_dict["user_id"] = user_id
    
    return event_dict


def add_service_context(
    logger: logging.Logger,
    method_name: str,
    event_dict: dict,
) -> dict:
    """Add service-level context to log events."""
    event_dict["service"] = "moltmarkets-api"
    return event_dict


# ---------------------------------------------------------------------------
# Logging Configuration
# ---------------------------------------------------------------------------

def configure_logging(level: str | None = None) -> None:
    """Configure structlog for JSON structured logging.
    
    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR). 
               Defaults to LOG_LEVEL env var or INFO.
    """
    if level is None:
        level = os.getenv("LOG_LEVEL", "INFO").upper()
    
    log_level = getattr(logging, level, logging.INFO)
    
    # Determine output format based on environment
    # Use JSON in production, pretty console output in development
    is_dev = os.getenv("DEBUG", "").lower() in ("1", "true", "yes")
    
    # Shared processors for both dev and prod
    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
        add_service_context,
        add_request_context,
    ]
    
    if is_dev:
        # Development: colored console output
        processors = shared_processors + [
            structlog.dev.ConsoleRenderer(colors=True),
        ]
    else:
        # Production: JSON output
        processors = shared_processors + [
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ]
    
    # Configure structlog
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    
    # Configure standard library logging to route through structlog
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
    )
    
    # Reduce noise from third-party libraries
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.error").setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Get a structured logger instance.
    
    Args:
        name: Logger name (typically __name__).
    
    Returns:
        Configured structlog logger.
    """
    return structlog.get_logger(name)


# ---------------------------------------------------------------------------
# Request ID Middleware
# ---------------------------------------------------------------------------

class RequestIDMiddleware(BaseHTTPMiddleware):
    """Middleware to inject and propagate request IDs for distributed tracing.
    
    - Checks for incoming X-Request-ID header
    - Generates UUID if not present
    - Stores in context var for logger access
    - Returns X-Request-ID in response headers
    """
    
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Get or generate request ID
        request_id = request.headers.get("X-Request-ID")
        if not request_id:
            request_id = str(uuid.uuid4())
        
        # Store in context
        token = request_id_ctx.set(request_id)
        
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            request_id_ctx.reset(token)


# ---------------------------------------------------------------------------
# Request/Response Logging Middleware
# ---------------------------------------------------------------------------

class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Middleware to log incoming requests and outgoing responses.
    
    Logs:
    - Request: method, path, query params, client IP
    - Response: status code, duration
    
    Excludes health check endpoints to reduce noise.
    """
    
    # Paths to exclude from logging (health checks, metrics)
    EXCLUDED_PATHS = {"/health", "/healthz", "/ready", "/metrics", "/favicon.ico"}
    
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Skip logging for excluded paths
        if request.url.path in self.EXCLUDED_PATHS:
            return await call_next(request)
        
        logger = get_logger("http")
        
        # Extract request info
        method = request.method
        path = request.url.path
        query = str(request.query_params) if request.query_params else None
        client_ip = self._get_client_ip(request)
        
        # Log request
        logger.info(
            "request_started",
            method=method,
            path=path,
            query=query,
            client_ip=client_ip,
        )
        
        # Process request and measure duration
        start_time = time.perf_counter()
        
        try:
            response = await call_next(request)
            duration_ms = (time.perf_counter() - start_time) * 1000
            
            # Log response
            log_method = logger.warning if response.status_code >= 400 else logger.info
            log_method(
                "request_completed",
                method=method,
                path=path,
                status_code=response.status_code,
                duration_ms=round(duration_ms, 2),
            )
            
            return response
            
        except Exception as exc:
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.error(
                "request_failed",
                method=method,
                path=path,
                duration_ms=round(duration_ms, 2),
                error=str(exc),
                exc_info=True,
            )
            raise
    
    def _get_client_ip(self, request: Request) -> str:
        """Extract client IP, respecting X-Forwarded-For header."""
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            # First IP in the chain is the original client
            return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"


# ---------------------------------------------------------------------------
# User Context Helper
# ---------------------------------------------------------------------------

def set_user_context(user_id: str) -> None:
    """Set the current user ID in logging context.
    
    Call this after authentication to include user_id in subsequent logs.
    """
    user_id_ctx.set(user_id)

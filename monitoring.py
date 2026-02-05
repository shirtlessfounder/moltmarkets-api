"""
Error monitoring and analytics with Sentry.

Sentry is initialized automatically if SENTRY_DSN environment variable is set.
Analytics events are tracked for registration funnel.

GitHub Issue: #176
"""

import os
import logging
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

# Sentry initialization flag
_sentry_initialized = False


def init_sentry() -> bool:
    """Initialize Sentry if DSN is configured.
    
    Returns True if Sentry was initialized, False otherwise.
    """
    global _sentry_initialized
    
    sentry_dsn = os.getenv("SENTRY_DSN")
    if not sentry_dsn:
        logger.info("SENTRY_DSN not set, error monitoring disabled")
        return False
    
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration
        
        environment = os.getenv("ENVIRONMENT", "production")
        release = os.getenv("RELEASE_VERSION", "unknown")
        
        sentry_sdk.init(
            dsn=sentry_dsn,
            environment=environment,
            release=release,
            integrations=[
                FastApiIntegration(transaction_style="endpoint"),
                StarletteIntegration(transaction_style="endpoint"),
            ],
            # Capture 100% of transactions for performance monitoring
            traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.1")),
            # Send 100% of errors
            sample_rate=1.0,
            # Enable profiling for performance insights
            profiles_sample_rate=float(os.getenv("SENTRY_PROFILES_SAMPLE_RATE", "0.1")),
            # Don't send PII
            send_default_pii=False,
        )
        
        _sentry_initialized = True
        logger.info(f"Sentry initialized for environment: {environment}")
        return True
        
    except ImportError:
        logger.warning("sentry-sdk not installed, error monitoring disabled")
        return False
    except Exception as e:
        logger.error(f"Failed to initialize Sentry: {e}")
        return False


def capture_exception(error: Exception, context: Optional[Dict[str, Any]] = None) -> None:
    """Capture an exception to Sentry with optional context."""
    if not _sentry_initialized:
        return
    
    try:
        import sentry_sdk
        with sentry_sdk.push_scope() as scope:
            if context:
                for key, value in context.items():
                    scope.set_extra(key, value)
            sentry_sdk.capture_exception(error)
    except Exception as e:
        logger.error(f"Failed to capture exception to Sentry: {e}")


def capture_message(message: str, level: str = "info", context: Optional[Dict[str, Any]] = None) -> None:
    """Capture a message to Sentry."""
    if not _sentry_initialized:
        return
    
    try:
        import sentry_sdk
        with sentry_sdk.push_scope() as scope:
            if context:
                for key, value in context.items():
                    scope.set_extra(key, value)
            sentry_sdk.capture_message(message, level=level)
    except Exception as e:
        logger.error(f"Failed to capture message to Sentry: {e}")


# =============================================================================
# Analytics Events for Registration Funnel
# =============================================================================

class AnalyticsEvent:
    """Analytics event types for tracking."""
    REGISTRATION_STARTED = "registration.started"
    REGISTRATION_COMPLETED = "registration.completed"
    REGISTRATION_FAILED = "registration.failed"
    CLAIM_PAGE_VIEWED = "claim.page_viewed"
    CLAIM_STARTED = "claim.started"
    CLAIM_COMPLETED = "claim.completed"
    CLAIM_FAILED = "claim.failed"


def track_event(event_name: str, properties: Optional[Dict[str, Any]] = None) -> None:
    """Track an analytics event.
    
    Events are sent to Sentry as breadcrumbs and can be exported
    to analytics platforms via Sentry's integrations.
    """
    if not _sentry_initialized:
        # Log locally even if Sentry isn't configured
        logger.info(f"Analytics event: {event_name}", extra={"properties": properties or {}})
        return
    
    try:
        import sentry_sdk
        sentry_sdk.add_breadcrumb(
            category="analytics",
            message=event_name,
            level="info",
            data=properties or {},
        )
        
        # Also set as a tag for easier filtering
        sentry_sdk.set_tag(f"event.{event_name.replace('.', '_')}", "true")
        
    except Exception as e:
        logger.error(f"Failed to track event {event_name}: {e}")


def track_registration_started(username: str, client_ip: str) -> None:
    """Track when a registration attempt starts."""
    track_event(AnalyticsEvent.REGISTRATION_STARTED, {
        "username": username,
        "client_ip_hash": hash(client_ip) % 10000,  # Anonymized
    })


def track_registration_completed(user_id: str, username: str, is_sandbox: bool) -> None:
    """Track successful registration."""
    track_event(AnalyticsEvent.REGISTRATION_COMPLETED, {
        "user_id": user_id,
        "username": username,
        "is_sandbox": is_sandbox,
    })


def track_registration_failed(username: str, error_type: str, error_message: str) -> None:
    """Track failed registration attempt."""
    track_event(AnalyticsEvent.REGISTRATION_FAILED, {
        "username": username,
        "error_type": error_type,
        "error_message": error_message,
    })
    
    # Also capture as a Sentry message for alerting
    capture_message(
        f"Registration failed: {error_type}",
        level="warning",
        context={"username": username, "error": error_message}
    )


def track_claim_page_viewed(user_id: str) -> None:
    """Track when claim page is viewed."""
    track_event(AnalyticsEvent.CLAIM_PAGE_VIEWED, {
        "user_id": user_id,
    })


def track_claim_completed(user_id: str, username: str) -> None:
    """Track successful claim."""
    track_event(AnalyticsEvent.CLAIM_COMPLETED, {
        "user_id": user_id,
        "username": username,
    })


def track_claim_failed(user_id: str, error_type: str, error_message: str) -> None:
    """Track failed claim attempt."""
    track_event(AnalyticsEvent.CLAIM_FAILED, {
        "user_id": user_id,
        "error_type": error_type,
        "error_message": error_message,
    })

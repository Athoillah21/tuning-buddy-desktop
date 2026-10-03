"""
Template context shared by every page.
"""
from django.conf import settings

from .clients import ServiceError, ServiceUnavailable
from .middleware import get_ai_status


def ai_status(request):
    """Expose AI readiness for the sidebar status dot."""
    status = getattr(request, "ai_status", None)
    if status is None:
        try:
            status = get_ai_status()
        except (ServiceUnavailable, ServiceError):
            status = None
    return {"ai_status": status, "desktop_mode": settings.DEPLOYMENT_MODE == "desktop"}

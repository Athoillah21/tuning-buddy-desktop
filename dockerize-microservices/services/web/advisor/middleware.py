"""
Gate: the app can only be used once at least one AI provider has passed its health check.
"""
from django.contrib import messages
from django.core.cache import cache
from django.http import JsonResponse
from django.shortcuts import redirect, render

from .clients import AIServiceClient, ServiceError, ServiceUnavailable

AI_STATUS_CACHE_KEY = "advisor:ai_status"
AI_STATUS_CACHE_SECONDS = 30

# Paths that must work while the app is locked
EXEMPT_PATH_PREFIXES = ("/settings/ai/", "/static/", "/admin/", "/healthz/")

NOT_READY_MESSAGE = "Add and verify at least one AI provider to start using Tuning Buddy."


def get_ai_status(force_refresh: bool = False) -> dict:
    """AI readiness from the AI service, cached briefly so every page view doesn't hit it."""
    status = None if force_refresh else cache.get(AI_STATUS_CACHE_KEY)
    if status is None:
        status = AIServiceClient().status()
        # Only cache "ready": a locked app should unlock the moment a provider passes its check
        if status.get("ready"):
            cache.set(AI_STATUS_CACHE_KEY, status, AI_STATUS_CACHE_SECONDS)
        else:
            cache.delete(AI_STATUS_CACHE_KEY)
    return status


def clear_ai_status_cache() -> None:
    cache.delete(AI_STATUS_CACHE_KEY)


class AIReadyMiddleware:

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path.startswith(EXEMPT_PATH_PREFIXES):
            return self.get_response(request)

        wants_json = request.path.startswith("/api/") or request.headers.get("x-requested-with") == "XMLHttpRequest"

        try:
            status = get_ai_status()
        except (ServiceUnavailable, ServiceError) as e:
            if wants_json:
                return JsonResponse({"success": False, "error": str(e)}, status=503)
            return render(request, "advisor/service_unavailable.html", {"error": str(e)}, status=503)

        request.ai_status = status
        if not status.get("ready"):
            if wants_json:
                return JsonResponse({"success": False, "error": NOT_READY_MESSAGE}, status=503)
            messages.warning(request, NOT_READY_MESSAGE)
            return redirect("advisor:ai_settings")

        return self.get_response(request)

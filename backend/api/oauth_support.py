"""Shared OAuth callback helpers used by provider and Home Assistant connect flows."""

from __future__ import annotations

import json
from html import escape
from typing import Literal
from urllib.parse import urlparse

from fastapi import HTTPException
from fastapi.responses import HTMLResponse

from core.auth.device_service import device_auth_service

OAuthKind = Literal["bespoke", "composio"]


def assert_allowed_oauth_origin(origin: str, request_origin: str | None = None) -> str:
    cleaned = origin.strip().rstrip("/")
    if not cleaned:
        raise HTTPException(status_code=400, detail="OAuth origin is required.")
    if device_auth_service.origin_allowed(cleaned):
        return cleaned
    if request_origin and cleaned == request_origin.strip().rstrip("/"):
        parsed = urlparse(cleaned)
        if parsed.scheme == "https" or (
            parsed.scheme == "http"
            and device_auth_service.is_loopback_host(parsed.hostname)
        ):
            return cleaned
    raise HTTPException(status_code=400, detail="OAuth origin is not allowlisted.")


async def publish_oauth_changed(
    *,
    app: str,
    success: bool,
    loaded: bool = False,
    kind: OAuthKind = "bespoke",
) -> None:
    """Notify connected clients that an OAuth flow finished."""
    from core.config import settings
    from services.events import Event, EventType, event_bus

    await event_bus.publish(
        Event(
            type=EventType.AUTH_OAUTH_CHANGED,
            source="auth",
            data={
                "owner_id": settings.DEFAULT_USER_ID,
                "app": app,
                "success": success,
                "loaded": loaded,
                "kind": kind,
            },
        )
    )


def render_oauth_callback_page(
    title: str,
    message: str,
    success: bool,
    app_name: str = "",
    loaded: bool = False,
) -> str:
    """Render a minimal HTML page that the user can close after OAuth."""
    icon = "✓" if success else "✗"
    color = "#4ade80" if success else "#f87171"
    payload = json.dumps(
        {
            "type": "jarvis:oauth_callback",
            "success": success,
            "app": app_name or "",
            "loaded": loaded,
        }
    ).replace("<", "\\u003c")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>JARV1S — {escape(title)}</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      background: #0a0a0f;
      color: #e2e8f0;
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 100vh;
    }}
    .card {{
      background: #141420;
      border: 1px solid #1e1e2e;
      border-radius: 16px;
      padding: 40px 48px;
      max-width: 400px;
      text-align: center;
    }}
    .icon {{
      font-size: 48px;
      color: {color};
      margin-bottom: 20px;
    }}
    h1 {{ font-size: 20px; font-weight: 600; margin-bottom: 12px; color: #f1f5f9; }}
    p {{ font-size: 14px; color: #94a3b8; line-height: 1.6; }}
    .close-hint {{
      margin-top: 24px;
      font-size: 12px;
      color: #475569;
    }}
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">{icon}</div>
    <h1>{escape(title)}</h1>
    <p>{escape(message)}</p>
    <p class="close-hint">You can close this window.</p>
  </div>
  <script>
    const payload = {payload};
    if (window.opener) {{
      window.opener.postMessage(payload, window.location.origin);
      setTimeout(() => window.close(), 300);
    }}
  </script>
</body>
</html>"""


def oauth_callback_response(
    *,
    title: str,
    message: str,
    success: bool,
    app_name: str = "",
    loaded: bool = False,
    status_code: int = 200,
) -> HTMLResponse:
    return HTMLResponse(
        render_oauth_callback_page(
            title=title,
            message=message,
            success=success,
            app_name=app_name,
            loaded=loaded,
        ),
        status_code=status_code,
    )

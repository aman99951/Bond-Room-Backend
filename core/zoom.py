import base64
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import timezone as dt_timezone

from django.conf import settings
from django.utils import timezone


_TOKEN_CACHE = {
    "access_token": None,
    "expires_at": 0,
}


class ZoomError(Exception):
    pass


def _get_zoom_config():
    account_id = os.environ.get("ZOOM_ACCOUNT_ID", "").strip()
    client_id = os.environ.get("ZOOM_CLIENT_ID", "").strip()
    client_secret = os.environ.get("ZOOM_CLIENT_SECRET", "").strip()
    user_id = os.environ.get("ZOOM_USER_ID", "me").strip() or "me"
    if not account_id or not client_id or not client_secret:
        return None
    return {
        "account_id": account_id,
        "client_id": client_id,
        "client_secret": client_secret,
        "user_id": user_id,
    }


def zoom_is_configured():
    return _get_zoom_config() is not None


def _request_zoom_token(config):
    query = urllib.parse.urlencode(
        {"grant_type": "account_credentials", "account_id": config["account_id"]}
    )
    token_url = f"https://zoom.us/oauth/token?{query}"
    basic = base64.b64encode(
        f"{config['client_id']}:{config['client_secret']}".encode("utf-8")
    ).decode("utf-8")
    request = urllib.request.Request(token_url, method="POST")
    request.add_header("Authorization", f"Basic {basic}")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload.get("access_token"), payload.get("expires_in", 0)
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8"))
        except Exception:
            detail = {"message": exc.reason}
        raise ZoomError(detail.get("message") or "Unable to authorize Zoom.") from exc


def get_zoom_access_token():
    config = _get_zoom_config()
    if not config:
        return None
    now = time.time()
    if _TOKEN_CACHE["access_token"] and _TOKEN_CACHE["expires_at"] > now + 30:
        return _TOKEN_CACHE["access_token"]

    token, expires_in = _request_zoom_token(config)
    if not token:
        return None
    _TOKEN_CACHE["access_token"] = token
    _TOKEN_CACHE["expires_at"] = now + int(expires_in or 0)
    return token


def _format_start_time(value):
    if value is None:
        return None
    if timezone.is_naive(value):
        value = timezone.make_aware(value, timezone=timezone.get_current_timezone())
    return value.astimezone(dt_timezone.utc).isoformat()


def create_zoom_meeting(session):
    config = _get_zoom_config()
    if not config:
        return None
    access_token = get_zoom_access_token()
    if not access_token:
        return None

    duration = session.duration_minutes
    if not duration and session.scheduled_start and session.scheduled_end:
        diff = session.scheduled_end - session.scheduled_start
        duration = max(15, int(diff.total_seconds() / 60))

    payload = {
        "topic": f"Mentor Session #{session.id}",
        "type": 2,
        "start_time": _format_start_time(session.scheduled_start),
        "duration": duration or 45,
        "timezone": session.timezone or settings.TIME_ZONE,
        "settings": {
            "join_before_host": True,
            "waiting_room": False,
            "approval_type": 2,
            "audio": "both",
            "host_video": True,
            "participant_video": True,
            "auto_recording": "cloud",
        },
    }

    user_id = config["user_id"]
    url = f"https://api.zoom.us/v2/users/{user_id}/meetings"
    request = urllib.request.Request(url, method="POST")
    request.add_header("Authorization", f"Bearer {access_token}")
    request.add_header("Content-Type", "application/json")
    body = json.dumps(payload).encode("utf-8")
    try:
        with urllib.request.urlopen(request, data=body, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8"))
        except Exception:
            detail = {"message": exc.reason}
        raise ZoomError(detail.get("message") or "Unable to create Zoom meeting.") from exc
    return {
        "join_url": data.get("join_url"),
        "host_join_url": data.get("start_url"),
    }


def maybe_attach_zoom_links(session):
    if not session:
        return None
    if session.join_url and session.host_join_url:
        return None
    if session.status not in {"approved", "scheduled"}:
        return None
    try:
        meeting = create_zoom_meeting(session)
    except ZoomError as exc:
        return {"error": str(exc)}
    if not meeting:
        return None
    join_url = meeting.get("join_url") or ""
    host_url = meeting.get("host_join_url") or ""
    if not join_url and not host_url:
        return None
    return {
        "join_url": join_url,
        "host_join_url": host_url,
    }

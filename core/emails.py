import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils import timezone


logger = logging.getLogger(__name__)


def send_mentor_welcome_email(mentor) -> bool:
    recipient = str(getattr(mentor, "email", "") or "").strip()
    if not recipient:
        return False

    mentor_name = f"{getattr(mentor, 'first_name', '')} {getattr(mentor, 'last_name', '')}".strip()
    subject = "Welcome to Bond Room Mentor Community"

    public_base_url = str(getattr(settings, "PUBLIC_BASE_URL", "") or "").strip().rstrip("/")
    dashboard_url = f"{public_base_url}/mentor-dashboard" if public_base_url else ""

    context = {
        "mentor_name": mentor_name or "Mentor",
        "dashboard_url": dashboard_url,
        "support_email": getattr(settings, "DEFAULT_FROM_EMAIL", "").strip(),
    }
    html_body = render_to_string("emails/mentor_welcome.html", context)
    text_body = render_to_string("emails/mentor_welcome.txt", context)

    try:
        message = EmailMultiAlternatives(
            subject=subject,
            body=text_body,
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "") or None,
            to=[recipient],
        )
        message.attach_alternative(html_body, "text/html")
        message.send(fail_silently=False)
        return True
    except Exception:
        logger.exception("Failed to send mentor welcome email to %s", recipient)
        return False


def send_mentee_welcome_email(mentee) -> bool:
    recipient = str(getattr(mentee, "email", "") or "").strip()
    if not recipient:
        return False

    mentee_name = f"{getattr(mentee, 'first_name', '')} {getattr(mentee, 'last_name', '')}".strip()
    subject = "Welcome to Bond Room"

    public_base_url = str(getattr(settings, "PUBLIC_BASE_URL", "") or "").strip().rstrip("/")
    dashboard_url = f"{public_base_url}/dashboard" if public_base_url else ""

    context = {
        "mentee_name": mentee_name or "Student",
        "dashboard_url": dashboard_url,
        "support_email": getattr(settings, "DEFAULT_FROM_EMAIL", "").strip(),
    }
    html_body = render_to_string("emails/mentee_welcome.html", context)
    text_body = render_to_string("emails/mentee_welcome.txt", context)

    try:
        message = EmailMultiAlternatives(
            subject=subject,
            body=text_body,
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "") or None,
            to=[recipient],
        )
        message.attach_alternative(html_body, "text/html")
        message.send(fail_silently=False)
        return True
    except Exception:
        logger.exception("Failed to send mentee welcome email to %s", recipient)
        return False


def send_admin_safety_alert_email(
    *,
    session,
    speaker_role: str,
    warning_count: int,
    warning_limit_before_disconnect: int,
    disconnect_on_warning: int,
    reason: str = "",
) -> bool:
    try:
        from .models import AdminAccount
    except Exception:
        logger.exception("Unable to import AdminAccount for safety alert email")
        return False

    recipients = []
    for row in AdminAccount.objects.select_related("user").all():
        email = str(getattr(getattr(row, "user", None), "email", "") or "").strip()
        if email and email not in recipients:
            recipients.append(email)
    fallback_admin_email = str(getattr(settings, "ADMIN_ALERT_EMAIL", "") or "").strip()
    if fallback_admin_email and fallback_admin_email not in recipients:
        recipients.append(fallback_admin_email)
    if not recipients:
        return False

    session_id = getattr(session, "id", None)
    subject = f"[Bond Room] Safety alert for session {session_id}"
    body_lines = [
        "A meeting has triggered safety warnings and requires admin review.",
        "",
        f"Session ID: {session_id}",
        f"Speaker role: {speaker_role}",
        f"Warnings count: {warning_count}",
        f"Warning limit before disconnect: {warning_limit_before_disconnect}",
        f"Disconnect on warning: {disconnect_on_warning}",
        f"Session status: {getattr(session, 'status', '')}",
        f"Event time (UTC): {timezone.now().isoformat()}",
    ]
    reason_text = str(reason or "").strip()
    if reason_text:
        body_lines.extend(["", f"Reason: {reason_text}"])
    body = "\n".join(body_lines)

    try:
        message = EmailMultiAlternatives(
            subject=subject,
            body=body,
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "") or None,
            to=recipients,
        )
        message.send(fail_silently=False)
        return True
    except Exception:
        logger.exception("Failed to send admin safety alert email for session %s", session_id)
        return False

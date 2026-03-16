import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string


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

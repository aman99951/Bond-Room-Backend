import json
import os
import time
import uuid
import hashlib
import hmac
import base64
from datetime import timedelta
from decimal import Decimal
import re
import urllib.request
import urllib.error

from django.contrib.auth import get_user_model
from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import Avg, Count, Q, Sum
from django.contrib.auth.models import update_last_login
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.generics import GenericAPIView
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.settings import api_settings
from rest_framework_simplejwt.tokens import RefreshToken

from .location_catalog import get_cities_for_state, get_states
from .models import (
    AdminAccount,
    ContactOtpRequest,
    DonationTransaction,
    MatchRecommendation,
    Mentor,
    MentorAvailabilitySlot,
    MentorContactVerification,
    MentorIdentityVerification,
    MentorOnboardingStatus,
    MentorProfile,
    MentorTrainingQuizAttempt,
    MentorTrainingProgress,
    MentorWallet,
    Mentee,
    MenteePreferences,
    MenteeRequest,
    ParentConsentVerification,
    PayoutTransaction,
    Session,
    SessionAbuseIncident,
    SessionDisposition,
    SessionFeedback,
    SessionIssueReport,
    SessionMeetingSignal,
    SessionRecording,
    SiteSetting,
    TrainingModule,
    UserProfile,
    VolunteerEvent,
    VolunteerEventRegistration,
)
from .onboarding import (
    sync_mentor_onboarding_training_status,
)
from .permissions import (
    ROLE_ADMIN,
    ROLE_MENTEE,
    ROLE_MENTOR,
    IsAdminRole,
    IsAuthenticatedWithAppRole,
    IsMenteeOrAdminRole,
    IsMentorOrAdminRole,
    user_role,
)
from .serializers import (
    AdminOnboardingDecisionSerializer,
    AdminRegisterSerializer,
    DonationTransactionSerializer,
    MatchRecommendationSerializer,
    MentorAvailabilitySlotSerializer,
    MentorContactOtpSendSerializer,
    MentorContactOtpVerifySerializer,
    MentorContactVerificationSerializer,
    MentorIdentityVerificationSerializer,
    MentorOnboardingStatusSerializer,
    MentorProfileSerializer,
    MentorRegisterSerializer,
    MentorSerializer,
    MentorTrainingProgressSerializer,
    MentorWalletSerializer,
    MobileLoginOtpVerifySerializer,
    MenteePreferencesSerializer,
    MenteeRegisterSerializer,
    MenteeRequestSerializer,
    MenteeSerializer,
    ParentConsentVerificationSerializer,
    ParentOtpSendSerializer,
    ParentOtpVerifySerializer,
    PayoutTransactionSerializer,
    SessionDispositionActionSerializer,
    SessionDispositionSerializer,
    SessionAbuseIncidentSerializer,
    SessionFeedbackSerializer,
    SessionIssueReportSerializer,
    SessionMeetingSignalSerializer,
    SessionRecordingSerializer,
    SessionSerializer,
    TrainingModuleSerializer,
    TrainingQuizAbandonSerializer,
    TrainingQuizStartSerializer,
    TrainingQuizSubmitSerializer,
    TrainingVideoWatchSerializer,
    VolunteerEventRegistrationSerializer,
    VolunteerEventSerializer,
    build_absolute_media_url,
    generate_otp,
    hash_otp,
    otp_expiry,
)
from .quiz import (
    clean_question_text,
    evaluate_quiz_attempt,
    generate_training_quiz_questions,
)
from .abuse_monitoring import classify_abuse, classify_behavior_signal, classify_video_behavior_frame
from .signals import generate_recommendations_for_request
from .emails import (
    send_admin_safety_alert_email,
    send_mentee_welcome_email,
    send_mentor_welcome_email,
    send_volunteer_registration_confirmation_email,
)

try:
    import boto3
except Exception:
    boto3 = None

try:
    from botocore.config import Config as BotoConfig
except Exception:
    BotoConfig = None

TRAINING_QUIZ_PASS_MARK = 7
User = get_user_model()

class SixPerPagePagination(PageNumberPagination):
    page_size = 6
    page_size_query_param = "page_size"
    max_page_size = 24


class SessionRecordsPagination(PageNumberPagination):
    page_size = 10
    page_size_query_param = "page_size"
    max_page_size = 50


def current_mentee_id(request):
    if not request.user.is_authenticated:
        return None
    return Mentee.objects.filter(email=request.user.email).values_list("id", flat=True).first()


def current_mentor_id(request):
    if not request.user.is_authenticated:
        return None
    return Mentor.objects.filter(email=request.user.email).values_list("id", flat=True).first()


def require_role(request, allowed_roles):
    role = user_role(request.user)
    if role not in allowed_roles:
        raise PermissionDenied("You do not have permission to access this endpoint.")


def deny_if_mentee_mutation(request):
    if user_role(request.user) == ROLE_MENTEE:
        raise PermissionDenied("Mentee users have read-only access for this endpoint.")


def resolve_session_participant_role(request, session):
    role = user_role(request.user)
    if role == ROLE_ADMIN:
        return "admin"
    if role == ROLE_MENTOR and session.mentor_id == current_mentor_id(request):
        return "mentor"
    if role == ROLE_MENTEE and session.mentee_id == current_mentee_id(request):
        return "mentee"
    raise PermissionDenied("You can only access your own sessions.")


def build_meeting_room_path(session_id, participant_role):
    if participant_role == "mentor":
        return f"/mentor-meeting-room?sessionId={session_id}"
    return f"/mentee-meeting-room?sessionId={session_id}"


def build_role_meeting_paths(session_id):
    return {
        "mentee": f"/mentee-meeting-room?sessionId={session_id}",
        "mentor": f"/mentor-meeting-room?sessionId={session_id}",
    }


def mentee_snapshot_for_incident(session):
    mentee = session.mentee
    return {
        "id": mentee.id,
        "first_name": mentee.first_name,
        "last_name": mentee.last_name,
        "email": mentee.email,
        "grade": mentee.grade,
        "city_state": mentee.city_state,
        "timezone": mentee.timezone,
        "parent_mobile": mentee.parent_mobile,
    }


def parse_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def build_s3_object_url(*, bucket: str, key: str, region: str, custom_domain: str = "") -> str:
    normalized_bucket = str(bucket or "").strip()
    normalized_key = str(key or "").strip().lstrip("/")
    normalized_region = str(region or "").strip() or "us-east-1"
    domain = str(custom_domain or "").strip().rstrip("/")
    if not normalized_bucket or not normalized_key:
        return ""
    if domain:
        return f"https://{domain}/{normalized_key}"
    if normalized_region == "us-east-1":
        return f"https://{normalized_bucket}.s3.amazonaws.com/{normalized_key}"
    return f"https://{normalized_bucket}.s3.{normalized_region}.amazonaws.com/{normalized_key}"


def discover_s3_bucket_region(bucket: str, timeout_seconds: float = 3.0) -> str:
    normalized_bucket = str(bucket or "").strip()
    if not normalized_bucket:
        return ""
    probe_url = f"https://{normalized_bucket}.s3.amazonaws.com/"
    probe_request = urllib.request.Request(probe_url, method="HEAD")
    try:
        with urllib.request.urlopen(probe_request, timeout=timeout_seconds) as response:
            headers = response.headers
    except urllib.error.HTTPError as exc:
        headers = exc.headers
    except Exception:
        return ""
    return str(headers.get("x-amz-bucket-region", "") or "").strip()


SITE_SETTING_DONATE_LINK_ENABLED_KEY = "donate_link_enabled"


def get_site_setting_bool(key: str, default: bool = False) -> bool:
    value = SiteSetting.objects.filter(key=key).values_list("value", flat=True).first()
    if value is None:
        return bool(default)
    return parse_bool(value)


def set_site_setting_bool(key: str, enabled: bool) -> bool:
    normalized = "true" if bool(enabled) else "false"
    SiteSetting.objects.update_or_create(key=key, defaults={"value": normalized})
    return bool(enabled)


def warning_policy_config():
    warning_limit_before_disconnect = max(
        1, int(os.environ.get("SESSION_WARNING_LIMIT_BEFORE_DISCONNECT", "4"))
    )
    disconnect_on_warning = max(
        warning_limit_before_disconnect + 1,
        int(os.environ.get("SESSION_DISCONNECT_ON_WARNING", "5")),
    )
    return warning_limit_before_disconnect, disconnect_on_warning


def enforce_session_warning_policy(*, session, speaker_role, reason="", incident_id=None):
    role_value = str(speaker_role or "").strip().lower()
    if role_value not in {"mentor", "mentee"}:
        return {
            "warning_count": 0,
            "warning_limit_before_disconnect": warning_policy_config()[0],
            "disconnect_on_warning": warning_policy_config()[1],
            "auto_disconnected": False,
            "disconnect_signal_id": None,
        }

    warning_limit_before_disconnect, disconnect_on_warning = warning_policy_config()
    warning_count = SessionAbuseIncident.objects.filter(
        session=session,
        speaker_role=role_value,
    ).count()
    disconnect_signal_id = None

    admin_alert_threshold = max(
        1, int(os.environ.get("SESSION_ADMIN_ALERT_ON_WARNING", "1"))
    )
    if warning_count >= admin_alert_threshold:
        alert_guard_key = f"session:{session.id}:warning-alert:{role_value}:{warning_count}"
        if not cache.get(alert_guard_key):
            description = (
                f"Safety warnings reached {warning_count} for {role_value} "
                f"(alert threshold {admin_alert_threshold})."
            )
            SessionIssueReport.objects.update_or_create(
                session=session,
                defaults={
                    "mentor": session.mentor,
                    "category": "safety_concern",
                    "status": "open",
                    "description": description,
                },
            )
            send_admin_safety_alert_email(
                session=session,
                speaker_role=role_value,
                warning_count=warning_count,
                warning_limit_before_disconnect=warning_limit_before_disconnect,
                disconnect_on_warning=disconnect_on_warning,
                reason=reason,
            )
            cache.set(alert_guard_key, 1, timeout=24 * 60 * 60)

    return {
        "warning_count": warning_count,
        "warning_limit_before_disconnect": warning_limit_before_disconnect,
        "disconnect_on_warning": disconnect_on_warning,
        "auto_disconnected": False,
        "disconnect_signal_id": disconnect_signal_id,
    }


def transcribe_audio_chunk_with_openai(uploaded_file):
    api_key = str(getattr(settings, "OPENAI_API_KEY", "") or "").strip()
    if not api_key or not uploaded_file:
        return "", "missing_api_key_or_file"

    try:
        audio_bytes = uploaded_file.read()
    except Exception:
        return "", "unable_to_read_audio_chunk"
    if not audio_bytes:
        return "", "empty_audio_chunk"

    preferred_model = (
        os.environ.get("OPENAI_REALTIME_TRANSCRIPTION_MODEL")
        or os.environ.get("OPENAI_TRANSCRIPTION_MODEL")
        or "whisper-1"
    )
    model_candidates = []
    for item in [preferred_model, "gpt-4o-transcribe", "gpt-4o-mini-transcribe", "whisper-1"]:
        candidate = str(item or "").strip()
        if candidate and candidate not in model_candidates:
            model_candidates.append(candidate)
    filename = str(getattr(uploaded_file, "name", "") or "chunk.webm").strip() or "chunk.webm"
    content_type = str(getattr(uploaded_file, "content_type", "") or "").strip() or "audio/webm"
    boundary = f"----BondRoomBoundary{int(time.time() * 1000)}"

    def _field(name, value):
        return (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n"
        ).encode("utf-8")

    file_header = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode("utf-8")
    file_tail = b"\r\n"
    end_marker = f"--{boundary}--\r\n".encode("utf-8")

    last_error = "transcription_failed"
    for model in model_candidates:
        body = b"".join(
            [
                _field("model", model),
                _field("response_format", "json"),
                _field("language", "en"),
                file_header,
                audio_bytes,
                file_tail,
                end_marker,
            ]
        )
        request = urllib.request.Request(
            "https://api.openai.com/v1/audio/transcriptions",
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
            text = str(payload.get("text", "") if isinstance(payload, dict) else "").strip()
            if text:
                return text, ""
            last_error = f"empty_transcript:{model}"
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="ignore").strip()
            except Exception:
                detail = ""
            if len(detail) > 280:
                detail = detail[:280]
            last_error = f"{model}:HTTP{getattr(exc, 'code', 'ERR')}:{detail or exc.__class__.__name__}"
            continue
        except Exception as exc:
            last_error = f"{model}:{exc.__class__.__name__}:{str(exc)[:180]}"
            continue
    return "", last_error


def generate_meeting_summary_with_openai(session, transcript):
    api_key = settings.OPENAI_API_KEY
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured.")

    transcript_text = str(transcript or "").strip()
    if not transcript_text:
        raise RuntimeError("Transcript is required for summary generation.")

    model = (
        os.environ.get("OPENAI_MEETING_SUMMARY_MODEL")
        or os.environ.get("OPENAI_MODEL")
        or "gpt-4o-mini"
    )
    transcript_excerpt = transcript_text[:120000]
    prompt_payload = {
        "session": {
            "id": session.id,
            "scheduled_start": session.scheduled_start.isoformat()
            if session.scheduled_start
            else "",
            "scheduled_end": session.scheduled_end.isoformat() if session.scheduled_end else "",
            "mentor_id": session.mentor_id,
            "mentee_id": session.mentee_id,
        },
        "requirements": {
            "language": "English",
            "summary_length": "short",
            "include_action_items": True,
            "include_key_highlights": True,
        },
        "transcript": transcript_excerpt,
        "output_schema": {
            "summary": "string",
            "highlights": ["string"],
            "action_items": ["string"],
        },
    }
    body = {
        "model": model,
        "text": {"format": {"type": "json_object"}},
        "input": [
            {
                "role": "system",
                "content": (
                    "You summarize mentoring sessions. "
                    "Return strict JSON only with keys summary, highlights, and action_items. "
                    "Do not include markdown."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(prompt_payload),
            },
        ],
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))

    output_text = payload.get("output_text", "") or ""
    if not output_text:
        parts = []
        for item in payload.get("output", []):
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    parts.append(content.get("text", ""))
        output_text = "".join(parts).strip()

    parsed = {}
    if output_text:
        try:
            parsed = json.loads(output_text)
        except json.JSONDecodeError:
            parsed = {"summary": output_text}

    summary = str(parsed.get("summary", "")).strip()
    if not summary:
        raise RuntimeError("OpenAI did not return a valid summary.")
    highlights = parsed.get("highlights")
    action_items = parsed.get("action_items")
    return {
        "summary": summary,
        "highlights": highlights if isinstance(highlights, list) else [],
        "action_items": action_items if isinstance(action_items, list) else [],
        "model": model,
    }


def normalize_mobile(value):
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def normalize_email(value):
    return str(value or "").strip().lower()


def mentor_contact_cache_key(channel, value):
    normalized = normalize_email(value) if channel == "email" else normalize_mobile(value)
    return f"mentor-contact:{channel}:{normalized}"


def normalize_contact_for_channel(channel: str, value: str) -> str:
    return normalize_email(value) if channel == "email" else normalize_mobile(value)


def get_user_role_value(user):
    profile = UserProfile.objects.filter(user=user).first()
    if profile and profile.role:
        return profile.role
    if AdminAccount.objects.filter(user=user).exists():
        return "admin"
    return ""


def build_auth_token_payload(user):
    role = get_user_role_value(user)
    refresh = RefreshToken.for_user(user)
    refresh["role"] = role
    refresh["email"] = user.email
    access = refresh.access_token
    access["role"] = role
    access["email"] = user.email
    if api_settings.UPDATE_LAST_LOGIN:
        update_last_login(None, user)
    return {
        "refresh": str(refresh),
        "access": str(access),
    }


def find_mentor_by_mobile(mobile_value):
    normalized = normalize_mobile(mobile_value)
    if not normalized:
        return None
    for mentor in Mentor.objects.all().order_by("-id").only("id", "email", "mobile"):
        if normalize_mobile(mentor.mobile) == normalized:
            return mentor
    return None


def find_mentee_by_mobile(mobile_value):
    normalized = normalize_mobile(mobile_value)
    if not normalized:
        return None
    for mentee in Mentee.objects.all().order_by("-id").only("id", "email", "parent_mobile", "mobile"):
        parent_mobile = normalize_mobile(getattr(mentee, "parent_mobile", ""))
        mentee_mobile = normalize_mobile(getattr(mentee, "mobile", ""))
        if parent_mobile == normalized or mentee_mobile == normalized:
            return mentee
    return None


def build_module_video_payload(module):
    outline = module.lesson_outline if isinstance(module.lesson_outline, list) else []
    first_title = (
        str(outline[0]).strip()
        if len(outline) >= 1 and str(outline[0]).strip()
        else "Module walkthrough video 1"
    )
    second_title = (
        str(outline[1]).strip()
        if len(outline) >= 2 and str(outline[1]).strip()
        else "Module walkthrough video 2"
    )
    video_one_url = (module.video_url_1 or "").strip()
    video_two_url = (module.video_url_2 or "").strip()
    return [
        {"key": "video-1", "title": first_title, "url": video_one_url},
        {"key": "video-2", "title": second_title, "url": video_two_url},
    ]


def build_training_module_payload_for_mentor(modules, mentor_id):
    module_list = list(modules)
    progress_qs = MentorTrainingProgress.objects.filter(
        mentor_id=mentor_id, module_id__in=[item.id for item in module_list]
    )
    progress_map = {item.module_id: item for item in progress_qs}

    payload = []
    previous_modules_completed = True

    for module in module_list:
        progress = progress_map.get(module.id)
        is_completed = bool(
            progress and (progress.status == "completed" or progress.progress_percent >= 100)
        )
        if is_completed:
            training_status = "completed"
            progress_percent = 100
            completed_at = progress.completed_at if progress else None
        elif previous_modules_completed:
            training_status = "in_progress"
            progress_percent = 50 if progress and progress.progress_percent >= 50 else 0
            completed_at = None
        else:
            training_status = "locked"
            progress_percent = 0
            completed_at = None

        videos = build_module_video_payload(module)
        video_progress = [
            {
                **item,
                "watched": training_status == "completed"
                or (training_status == "in_progress" and item["key"] == "video-1" and progress_percent >= 50),
            }
            for item in videos
        ]

        module_data = TrainingModuleSerializer(module).data
        module_data.update(
            {
                "status": training_status,
                "training_status": training_status,
                "progress_percent": progress_percent,
                "completed_at": completed_at,
                "videos": videos,
                "video_progress": video_progress,
            }
        )
        payload.append(module_data)
        previous_modules_completed = previous_modules_completed and training_status == "completed"

    return payload


def sanitize_quiz_questions_for_client(questions):
    def clean(value):
        text = str(value or "").strip()
        text = re.sub(r"^\s*\[\s*q\s*\d+\s*\]\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"^\s*q\s*\d+\s*[:\.\-\)]\s*", "", text, flags=re.IGNORECASE)
        return text.strip()

    sanitized = []
    for item in questions or []:
        options = item.get("options")
        if not isinstance(options, list):
            continue
        sanitized.append(
            {
                "question": clean(item.get("question", "")),
                "options": options[:4],
                "module_title": item.get("module_title", ""),
            }
        )
    return sanitized


def quiz_questions_signature(questions):
    signature = []
    for item in questions or []:
        question_text = clean_question_text(item.get("question", "")).lower()
        options = item.get("options")
        options_key = ()
        if isinstance(options, list):
            options_key = tuple(str(option).strip().lower() for option in options[:4])
        module_title = str(item.get("module_title", "")).strip().lower()
        signature.append((question_text, options_key, module_title))
    return tuple(sorted(signature))


def build_training_quiz_summary(mentor):
    latest_attempt = (
        MentorTrainingQuizAttempt.objects.filter(mentor=mentor)
        .order_by("-started_at", "-id")
        .first()
    )
    passed = MentorTrainingQuizAttempt.objects.filter(mentor=mentor, status="passed").exists()
    return {
        "has_passed": passed,
        "latest_attempt": (
            {
                "id": latest_attempt.id,
                "status": latest_attempt.status,
                "score": latest_attempt.score,
                "pass_mark": TRAINING_QUIZ_PASS_MARK,
                "total_questions": latest_attempt.total_questions,
                "started_at": latest_attempt.started_at,
                "submitted_at": latest_attempt.submitted_at,
            }
            if latest_attempt
            else None
        ),
    }


def serialize_quiz_attempt_for_client(attempt, *, include_questions=False):
    if not attempt:
        return None

    payload = {
        "id": attempt.id,
        "status": attempt.status,
        "score": attempt.score,
        "pass_mark": TRAINING_QUIZ_PASS_MARK,
        "total_questions": attempt.total_questions,
        "selected_answers": attempt.selected_answers or [],
        "started_at": attempt.started_at,
        "submitted_at": attempt.submitted_at,
    }
    if include_questions:
        payload["questions"] = sanitize_quiz_questions_for_client(attempt.questions)
    return payload


class MenteeRegisterView(GenericAPIView):
    permission_classes = [AllowAny]
    serializer_class = MenteeRegisterSerializer
    parser_classes = [JSONParser, FormParser, MultiPartParser]

    def post(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        mentee = serializer.save()
        send_mentee_welcome_email(mentee)
        return Response(serializer.to_representation(mentee), status=status.HTTP_201_CREATED)


class MentorRegisterView(GenericAPIView):
    permission_classes = [AllowAny]
    serializer_class = MentorRegisterSerializer
    parser_classes = [JSONParser, FormParser, MultiPartParser]

    def post(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        is_new_registration = not serializer.validated_data.get("mentor_id")
        mentor = serializer.save()
        if is_new_registration:
            send_mentor_welcome_email(mentor)
        return Response(serializer.to_representation(mentor), status=status.HTTP_201_CREATED)


class AdminRegisterView(GenericAPIView):
    permission_classes = [AllowAny]
    serializer_class = AdminRegisterSerializer

    def post(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        admin_user = serializer.save()
        return Response(serializer.to_representation(admin_user), status=status.HTTP_201_CREATED)


class LocationStatesView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, _request):
        return Response({"states": get_states()})


class LocationCitiesView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        state_name = str(request.query_params.get("state", "")).strip()
        if not state_name:
            return Response(
                {"detail": "Query parameter 'state' is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        canonical_state, cities = get_cities_for_state(state_name)
        if not canonical_state:
            return Response({"detail": "State not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response({"state": canonical_state, "cities": cities})


class PublicDonateLinkSettingView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, _request):
        enabled = get_site_setting_bool(SITE_SETTING_DONATE_LINK_ENABLED_KEY, default=False)
        return Response({"key": SITE_SETTING_DONATE_LINK_ENABLED_KEY, "enabled": enabled})


class AdminDonateLinkSettingView(APIView):
    permission_classes = [IsAuthenticatedWithAppRole]

    def _update(self, request):
        require_role(request, {ROLE_ADMIN})
        if "enabled" not in request.data:
            raise ValidationError({"enabled": "This field is required."})
        enabled = parse_bool(request.data.get("enabled"))
        updated = set_site_setting_bool(SITE_SETTING_DONATE_LINK_ENABLED_KEY, enabled)
        return Response({"key": SITE_SETTING_DONATE_LINK_ENABLED_KEY, "enabled": updated})

    def patch(self, request):
        return self._update(request)

    def post(self, request):
        return self._update(request)


def razorpay_creds():
    key_id = str(os.environ.get("RAZORPAY_KEY_ID", "")).strip()
    key_secret = str(os.environ.get("RAZORPAY_KEY_SECRET", "")).strip()
    return key_id, key_secret


def razorpay_mock_mode():
    mode = str(os.environ.get("RAZORPAY_MOCK_MODE", "")).strip().lower()
    return mode in {"1", "true", "yes", "on"}


class RazorpayDonationOrderView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        amount_raw = request.data.get("amount")
        name = str(request.data.get("name", "")).strip()
        email = normalize_email(request.data.get("email"))
        phone = str(request.data.get("phone", "")).strip()
        message = str(request.data.get("message", "")).strip()

        if amount_raw in {None, ""}:
            raise ValidationError({"amount": "This field is required."})
        if not name:
            raise ValidationError({"name": "This field is required."})
        if not email:
            raise ValidationError({"email": "This field is required."})

        try:
            amount_value = Decimal(str(amount_raw))
        except Exception:
            raise ValidationError({"amount": "Enter a valid amount."})

        if amount_value <= 0:
            raise ValidationError({"amount": "Amount must be greater than zero."})
        if amount_value > Decimal("1000000"):
            raise ValidationError({"amount": "Amount exceeds allowed limit."})

        amount_paise = int((amount_value * 100).quantize(Decimal("1")))
        currency = "INR"

        if razorpay_mock_mode():
            fake_order_id = f"order_mock_{int(time.time())}"
            key_id = str(os.environ.get("RAZORPAY_KEY_ID", "")).strip() or "rzp_test_mock_key"
            return Response(
                {
                    "order_id": fake_order_id,
                    "amount": amount_paise,
                    "currency": currency,
                    "key_id": key_id,
                    "is_mock": True,
                }
            )

        key_id, key_secret = razorpay_creds()
        if not key_id or not key_secret:
            return Response(
                {"detail": "Razorpay credentials are not configured."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        receipt = f"bondroom_{int(time.time())}_{hashlib.md5(email.encode('utf-8')).hexdigest()[:8]}"
        payload = {
            "amount": amount_paise,
            "currency": currency,
            "receipt": receipt,
            "notes": {
                "name": name[:120],
                "email": email[:190],
                "phone": phone[:40],
                "message": message[:350],
                "source": "bond-room-donate-page",
            },
        }
        basic_token = base64.b64encode(f"{key_id}:{key_secret}".encode("utf-8")).decode("ascii")
        request_obj = urllib.request.Request(
            "https://api.razorpay.com/v1/orders",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Basic {basic_token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request_obj, timeout=30) as resp:
                order_payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="ignore")
            except Exception:
                detail = ""
            return Response(
                {"detail": "Razorpay order creation failed.", "error": detail[:320]},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        except Exception as exc:
            return Response(
                {"detail": f"Unable to create Razorpay order: {exc.__class__.__name__}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return Response(
            {
                "order_id": order_payload.get("id"),
                "amount": order_payload.get("amount", amount_paise),
                "currency": order_payload.get("currency", currency),
                "key_id": key_id,
                "is_mock": False,
            }
        )


class RazorpayDonationVerifyView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        order_id = str(request.data.get("razorpay_order_id", "")).strip()
        payment_id = str(request.data.get("razorpay_payment_id", "")).strip()
        signature = str(request.data.get("razorpay_signature", "")).strip()

        if not order_id or not payment_id or not signature:
            raise ValidationError(
                {
                    "detail": (
                        "razorpay_order_id, razorpay_payment_id, and "
                        "razorpay_signature are required."
                    )
                }
            )

        if razorpay_mock_mode():
            if signature != "mock_signature":
                return Response(
                    {"detail": "Invalid mock signature."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            return Response({"verified": True, "is_mock": True, "payment_id": payment_id, "order_id": order_id})

        _, key_secret = razorpay_creds()
        if not key_secret:
            return Response(
                {"detail": "Razorpay secret is not configured."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        payload = f"{order_id}|{payment_id}".encode("utf-8")
        expected_signature = hmac.new(
            key_secret.encode("utf-8"),
            payload,
            digestmod=hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected_signature, signature):
            return Response({"detail": "Invalid payment signature."}, status=status.HTTP_400_BAD_REQUEST)

        return Response({"verified": True, "is_mock": False, "payment_id": payment_id, "order_id": order_id})


class MobileLoginOtpVerifyView(GenericAPIView):
    permission_classes = [AllowAny]
    serializer_class = MobileLoginOtpVerifySerializer

    def post(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        otp = str(serializer.validated_data.get("otp", "")).strip()
        expected_otp = (getattr(settings, "MOCK_LOGIN_OTP", "") or "123456").strip()
        if otp != expected_otp:
            return Response({"detail": "Invalid OTP."}, status=status.HTTP_400_BAD_REQUEST)

        mobile = str(serializer.validated_data.get("mobile", "")).strip()
        role = serializer.validated_data.get("role")

        if role == "mentor":
            mentor = find_mentor_by_mobile(mobile)
            if not mentor:
                return Response({"detail": "No mentor account found for this mobile."}, status=status.HTTP_404_NOT_FOUND)
            user = User.objects.filter(email=mentor.email).first()
            if not user:
                return Response({"detail": "Linked user account not found."}, status=status.HTTP_404_NOT_FOUND)
            return Response(build_auth_token_payload(user))

        if role == "mentee":
            mentee = find_mentee_by_mobile(mobile)
            if not mentee:
                return Response({"detail": "No mentee account found for this mobile."}, status=status.HTTP_404_NOT_FOUND)
            user = User.objects.filter(email=mentee.email).first()
            if not user:
                return Response({"detail": "Linked user account not found."}, status=status.HTTP_404_NOT_FOUND)
            return Response(build_auth_token_payload(user))

        mentor = find_mentor_by_mobile(mobile)
        mentee = find_mentee_by_mobile(mobile)
        if mentor and mentee:
            return Response(
                {"detail": "This mobile is linked to multiple roles. Please select role."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if mentor:
            user = User.objects.filter(email=mentor.email).first()
            if not user:
                return Response({"detail": "Linked user account not found."}, status=status.HTTP_404_NOT_FOUND)
            return Response(build_auth_token_payload(user))
        if mentee:
            user = User.objects.filter(email=mentee.email).first()
            if not user:
                return Response({"detail": "Linked user account not found."}, status=status.HTTP_404_NOT_FOUND)
            return Response(build_auth_token_payload(user))
        return Response({"detail": "No account found for this mobile."}, status=status.HTTP_404_NOT_FOUND)


class ParentConsentSendOtpView(GenericAPIView):
    permission_classes = [AllowAny]
    serializer_class = ParentOtpSendSerializer

    def post(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        mentee = Mentee.objects.filter(id=serializer.validated_data["mentee_id"]).first()
        if not mentee:
            return Response({"detail": "Mentee not found."}, status=status.HTTP_404_NOT_FOUND)

        otp = generate_otp()
        verification, _ = ParentConsentVerification.objects.get_or_create(mentee=mentee)
        parent_mobile = serializer.validated_data.get("parent_mobile")
        if parent_mobile:
            verification.parent_mobile = parent_mobile
            mentee.parent_mobile = parent_mobile
            mentee.save(update_fields=["parent_mobile"])
        verification.otp_hash = hash_otp(otp)
        verification.otp_sent_at = timezone.now()
        verification.otp_expires_at = otp_expiry()
        verification.otp_attempts = 0
        verification.status = "pending"
        verification.save()
        return Response(
            {
                "message": "OTP sent successfully.",
                "otp": otp,
                "expires_at": verification.otp_expires_at,
                "verification_id": verification.id,
            }
        )


class ParentConsentVerifyOtpView(GenericAPIView):
    permission_classes = [AllowAny]
    serializer_class = ParentOtpVerifySerializer

    def post(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        mentee = Mentee.objects.filter(id=serializer.validated_data["mentee_id"]).first()
        if not mentee:
            return Response({"detail": "Mentee not found."}, status=status.HTTP_404_NOT_FOUND)
        verification = ParentConsentVerification.objects.filter(mentee=mentee).first()
        if not verification:
            return Response(
                {"detail": "OTP was not requested for this mentee."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if verification.otp_expires_at and verification.otp_expires_at < timezone.now():
            verification.status = "expired"
            verification.save(update_fields=["status"])
            return Response({"detail": "OTP expired."}, status=status.HTTP_400_BAD_REQUEST)

        otp_hash = hash_otp(serializer.validated_data["otp"])
        if otp_hash != verification.otp_hash:
            verification.otp_attempts += 1
            verification.status = "failed"
            verification.save(update_fields=["otp_attempts", "status"])
            return Response({"detail": "Invalid OTP."}, status=status.HTTP_400_BAD_REQUEST)

        verification.status = "verified"
        verification.verified_at = timezone.now()
        verification.save(update_fields=["status", "verified_at"])
        mentee.parent_guardian_consent = True
        mentee.save(update_fields=["parent_guardian_consent"])
        return Response({"message": "Parent consent verified."})


class MentorContactSendOtpView(GenericAPIView):
    permission_classes = [AllowAny]
    serializer_class = MentorContactOtpSendSerializer

    def post(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        mentor = None
        mentor_id = serializer.validated_data.get("mentor_id")
        if mentor_id:
            mentor = Mentor.objects.filter(id=mentor_id).first()
            if not mentor:
                return Response({"detail": "Mentor not found."}, status=status.HTTP_404_NOT_FOUND)

        otp = (settings.MENTOR_TEST_OTP or "").strip() or generate_otp()
        channel = serializer.validated_data["channel"]
        now = timezone.now()
        expiry = otp_expiry()
        otp_hash = hash_otp(otp)

        if mentor:
            verification, _ = MentorContactVerification.objects.get_or_create(mentor=mentor)
            if channel == "email":
                verification.email_otp_hash = otp_hash
                verification.email_otp_sent_at = now
                verification.email_otp_expires_at = expiry
                verification.email_otp_attempts = 0
            else:
                verification.phone_otp_hash = otp_hash
                verification.phone_otp_sent_at = now
                verification.phone_otp_expires_at = expiry
                verification.phone_otp_attempts = 0
            verification.save()
        else:
            # Production note: do not rely on Django cache here. In serverless / multi-worker
            # deployments the default cache is per-process and won't survive between OTP send
            # and verify calls, causing false "OTP not requested" errors.
            if channel == "email":
                email = serializer.validated_data["email"]
                if Mentor.objects.filter(email__iexact=email).exists() or User.objects.filter(email__iexact=email).exists():
                    return Response(
                        {"email": "This email is already registered."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                normalized_contact = normalize_contact_for_channel(channel, email)
            else:
                mobile = serializer.validated_data["mobile"]
                if find_mentor_by_mobile(mobile):
                    return Response(
                        {"mobile": "This mobile number is already registered."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                normalized_contact = normalize_contact_for_channel("phone", mobile)

            # Upsert so resends overwrite previous OTP state.
            ContactOtpRequest.objects.update_or_create(
                channel="email" if channel == "email" else "phone",
                normalized_contact=normalized_contact,
                defaults={
                    "otp_hash": otp_hash,
                    "expires_at": expiry,
                    "attempts": 0,
                },
            )
        return Response(
            {
                "message": f"{channel.title()} OTP sent successfully.",
                "otp": otp,
                "expires_at": expiry,
            }
        )


class MentorContactVerifyOtpView(GenericAPIView):
    permission_classes = [AllowAny]
    serializer_class = MentorContactOtpVerifySerializer

    def post(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        channel = serializer.validated_data["channel"]
        otp_hash = hash_otp(serializer.validated_data["otp"])
        now = timezone.now()
        mentor = None
        mentor_id = serializer.validated_data.get("mentor_id")

        if mentor_id:
            mentor = Mentor.objects.filter(id=mentor_id).first()
            if not mentor:
                return Response({"detail": "Mentor not found."}, status=status.HTTP_404_NOT_FOUND)
            verification = MentorContactVerification.objects.filter(mentor=mentor).first()
            if not verification:
                return Response(
                    {"detail": "OTP was not requested for this mentor."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if channel == "email":
                if verification.email_otp_expires_at and verification.email_otp_expires_at < now:
                    return Response({"detail": "OTP expired."}, status=status.HTTP_400_BAD_REQUEST)
                if otp_hash != verification.email_otp_hash:
                    verification.email_otp_attempts += 1
                    verification.save(update_fields=["email_otp_attempts"])
                    return Response({"detail": "Invalid OTP."}, status=status.HTTP_400_BAD_REQUEST)
                verification.email_verified = True
                verification.email_verified_at = now
                verification.save(update_fields=["email_verified", "email_verified_at"])
            else:
                if verification.phone_otp_expires_at and verification.phone_otp_expires_at < now:
                    return Response({"detail": "OTP expired."}, status=status.HTTP_400_BAD_REQUEST)
                if otp_hash != verification.phone_otp_hash:
                    verification.phone_otp_attempts += 1
                    verification.save(update_fields=["phone_otp_attempts"])
                    return Response({"detail": "Invalid OTP."}, status=status.HTTP_400_BAD_REQUEST)
                verification.phone_verified = True
                verification.phone_verified_at = now
                verification.save(update_fields=["phone_verified", "phone_verified_at"])
                onboarding, _ = MentorOnboardingStatus.objects.get_or_create(mentor=mentor)
                if onboarding.contact_status != "completed":
                    onboarding.contact_status = "completed"
                    onboarding.save(update_fields=["contact_status", "updated_at", "current_status"])
        else:
            contact_value = (
                serializer.validated_data.get("email")
                if channel == "email"
                else serializer.validated_data.get("mobile")
            )
            normalized_contact = normalize_contact_for_channel(
                "email" if channel == "email" else "phone",
                contact_value,
            )
            req = ContactOtpRequest.objects.filter(
                channel="email" if channel == "email" else "phone",
                normalized_contact=normalized_contact,
            ).first()
            if not req:
                return Response(
                    {"detail": "OTP was not requested for this contact."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if req.expires_at and req.expires_at < now:
                req.delete()
                return Response({"detail": "OTP expired."}, status=status.HTTP_400_BAD_REQUEST)
            if otp_hash != req.otp_hash:
                ContactOtpRequest.objects.filter(pk=req.pk).update(attempts=req.attempts + 1)
                return Response({"detail": "Invalid OTP."}, status=status.HTTP_400_BAD_REQUEST)
            req.delete()
        return Response({"message": f"{channel.title()} verified successfully."})


class LogoutView(APIView):
    permission_classes = [IsAuthenticatedWithAppRole]

    def post(self, request):
        return Response({"message": "Logout acknowledged on server."})

class MenteeViewSet(viewsets.ModelViewSet):
    queryset = Mentee.objects.all().order_by("-created_at")
    serializer_class = MenteeSerializer
    permission_classes = [IsMenteeOrAdminRole]

    def get_permissions(self):
        if self.action in {"create", "destroy"}:
            return [IsAdminRole()]
        return super().get_permissions()

    def create(self, request, *args, **kwargs):
        raise PermissionDenied("Use /api/auth/register/mentee/ for registration.")

    def destroy(self, request, *args, **kwargs):
        raise PermissionDenied("Delete is only allowed for admin users.")

    def get_queryset(self):
        queryset = super().get_queryset()
        role = user_role(self.request.user)
        if role == ROLE_ADMIN:
            email = self.request.query_params.get("email")
            if email:
                queryset = queryset.filter(email=email)
            return queryset
        if role == ROLE_MENTEE:
            my_id = current_mentee_id(self.request)
            return queryset.filter(id=my_id) if my_id else queryset.none()
        return queryset.none()

    @action(detail=False, methods=["get"], url_path="volunteer-count", permission_classes=[AllowAny])
    def volunteer_count(self, request):
        count = Mentee.objects.filter(volunteer_access=True).count()
        return Response({"count": count})

    @action(detail=True, methods=["get", "put", "patch"], url_path="preferences")
    def preferences(self, request, pk=None):
        mentee = self.get_object()
        prefs, _ = MenteePreferences.objects.get_or_create(mentee=mentee)
        if request.method in ["PUT", "PATCH"]:
            serializer = MenteePreferencesSerializer(
                prefs,
                data=request.data,
                partial=request.method == "PATCH",
            )
            serializer.is_valid(raise_exception=True)
            serializer.save(mentee=mentee)
            return Response(serializer.data)
        return Response(MenteePreferencesSerializer(prefs, context={"request": request}).data)

    @action(detail=True, methods=["get"], url_path="dashboard")
    def dashboard(self, request, pk=None):
        mentee = self.get_object()
        now = timezone.now()
        sessions = Session.objects.filter(mentee=mentee).select_related("mentor")
        upcoming = sessions.filter(
            status__in=["requested", "approved", "scheduled"],
            scheduled_start__gte=now,
        ).order_by("scheduled_start")[:10]
        recent = sessions.filter(
            Q(status__in=["completed", "canceled", "no_show"]) | Q(scheduled_start__lt=now)
        ).order_by("-scheduled_start")[:10]
        latest_request = MenteeRequest.objects.filter(mentee=mentee).order_by("-created_at").first()
        recommendations = []
        if latest_request:
            rec_qs = (
                MatchRecommendation.objects.filter(
                    mentee_request=latest_request,
                    mentor__onboarding_status__current_status="completed",
                )
                .select_related("mentor")
                .order_by("-score")[:10]
            )
            recommendations = MatchRecommendationSerializer(
                rec_qs,
                many=True,
                context={"request": request},
            ).data
        return Response(
            {
                "mentee": MenteeSerializer(mentee, context={"request": request}).data,
                "upcoming_sessions": SessionSerializer(
                    upcoming,
                    many=True,
                    context={"request": request},
                ).data,
                "recent_sessions": SessionSerializer(
                    recent,
                    many=True,
                    context={"request": request},
                ).data,
                "recommendations": recommendations,
                "stats": {
                    "total_sessions": sessions.count(),
                    "completed_sessions": sessions.filter(status="completed").count(),
                    "upcoming_count": sessions.filter(
                        status__in=["requested", "approved", "scheduled"],
                        scheduled_start__gte=now,
                    ).count(),
                },
            }
        )


class MentorViewSet(viewsets.ModelViewSet):
    queryset = Mentor.objects.all().select_related("profile").prefetch_related("availability_slots").order_by("-created_at")
    serializer_class = MentorSerializer
    permission_classes = [IsAuthenticatedWithAppRole]

    def get_permissions(self):
        if self.action in {"list", "retrieve"}:
            return [AllowAny()]
        if self.action in {"create", "destroy"}:
            return [IsAdminRole()]
        return super().get_permissions()

    def create(self, request, *args, **kwargs):
        raise PermissionDenied("Use /api/auth/register/mentor/ for registration.")

    def destroy(self, request, *args, **kwargs):
        raise PermissionDenied("Delete is only allowed for admin users.")

    def get_queryset(self):
        queryset = super().get_queryset()
        if not getattr(self.request.user, "is_authenticated", False):
            if self.action in {"list", "retrieve"}:
                return queryset
            return queryset.none()
        role = user_role(self.request.user)
        if role == ROLE_ADMIN:
            email = self.request.query_params.get("email")
            if email:
                queryset = queryset.filter(email=email)
            return queryset
        if role == ROLE_MENTEE:
            email = self.request.query_params.get("email")
            if email:
                queryset = queryset.filter(email=email)
            return queryset
        if role == ROLE_MENTOR:
            my_id = current_mentor_id(self.request)
            return queryset.filter(id=my_id) if my_id else queryset.none()
        return queryset.none()

    @action(detail=False, methods=["get"], url_path="recommended")
    def recommended(self, request):
        require_role(request, {ROLE_MENTEE, ROLE_ADMIN})
        role = user_role(request.user)
        mentee_request_id = request.query_params.get("mentee_request_id")
        mentee_id = request.query_params.get("mentee_id")
        my_mentee_id = None
        if role == ROLE_MENTEE:
            my_mentee_id = current_mentee_id(request)
            if not my_mentee_id:
                raise PermissionDenied("Mentee profile not found for this user.")
            mentee_id = my_mentee_id

        if not mentee_request_id and not mentee_id:
            return Response({"detail": "Provide mentee_request_id or mentee_id."}, status=400)

        req = None
        if mentee_request_id:
            req = MenteeRequest.objects.filter(id=mentee_request_id).first()
            if req and role == ROLE_MENTEE and req.mentee_id != my_mentee_id:
                raise PermissionDenied("You can only access your own recommendations.")
        elif mentee_id:
            req = MenteeRequest.objects.filter(mentee_id=mentee_id).order_by("-created_at").first()
        if not req:
            if mentee_request_id:
                return Response({"detail": "No request found."}, status=404)
            return Response([])

        should_refresh = role == ROLE_MENTEE or request.query_params.get("refresh") in {"1", "true", "True"}
        if should_refresh:
            generate_recommendations_for_request(req)

        recs = (
            MatchRecommendation.objects.filter(
                mentee_request=req,
                mentor__onboarding_status__current_status="completed",
            )
            .select_related("mentor")
            .order_by("-score")
        )
        return Response(
            MatchRecommendationSerializer(
                recs,
                many=True,
                context={"request": request},
            ).data
        )

    @action(detail=True, methods=["get", "put", "patch"], url_path="profile")
    def profile(self, request, pk=None):
        require_role(request, {ROLE_MENTOR, ROLE_ADMIN})
        mentor = self.get_object()
        profile, _ = MentorProfile.objects.get_or_create(
            mentor=mentor,
            defaults={"public_id": f"BR-{mentor.id:04d}"},
        )
        if request.method in ["PUT", "PATCH"]:
            serializer = MentorProfileSerializer(profile, data=request.data, partial=request.method == "PATCH")
            serializer.is_valid(raise_exception=True)
            updated_profile = serializer.save(mentor=mentor)

            return Response(MentorProfileSerializer(updated_profile, context={"request": request}).data)
        return Response(MentorProfileSerializer(profile, context={"request": request}).data)

    @action(detail=True, methods=["get"], url_path="reviews")
    def reviews(self, request, pk=None):
        require_role(request, {ROLE_MENTEE, ROLE_MENTOR, ROLE_ADMIN})
        mentor = self.get_object()
        role = user_role(request.user)
        if role == ROLE_MENTOR and mentor.id != current_mentor_id(request):
            raise PermissionDenied("You can only view reviews for your own profile.")

        feedback_qs = (
            SessionFeedback.objects.filter(session__mentor=mentor)
            .select_related("session")
            .order_by("-submitted_at", "-id")
        )
        summary = feedback_qs.aggregate(
            average_rating=Avg("rating"),
            total_reviews=Count("id"),
        )
        recent_feedback = [
            {
                "id": item.id,
                "session_id": item.session_id,
                "rating": item.rating,
                "comments": item.comments or "",
                "topics_discussed": item.topics_discussed or [],
                "submitted_at": item.submitted_at,
            }
            for item in feedback_qs[:25]
        ]
        return Response(
            {
                "mentor_id": mentor.id,
                "summary": {
                    "average_rating": round(summary["average_rating"] or 0, 2),
                    "total_reviews": int(summary["total_reviews"] or 0),
                },
                "recent_feedback": recent_feedback,
            }
        )

    @action(detail=True, methods=["get"], url_path="impact-dashboard")
    def impact_dashboard(self, request, pk=None):
        require_role(request, {ROLE_MENTOR, ROLE_ADMIN})
        mentor = self.get_object()
        sessions = Session.objects.filter(mentor=mentor)
        completed_sessions = sessions.filter(status="completed")
        avg_rating = SessionFeedback.objects.filter(session__mentor=mentor).aggregate(value=Avg("rating"))["value"]
        wallet, _ = MentorWallet.objects.get_or_create(mentor=mentor)
        topic_stats = (
            SessionFeedback.objects.filter(session__mentor=mentor)
            .values("topics_discussed")
            .annotate(count=Count("id"))
            .order_by("-count")[:10]
        )
        ledger = SessionDisposition.objects.filter(mentor=mentor).select_related("session").order_by("-decided_at")[:20]
        return Response(
            {
                "mentor": MentorSerializer(mentor, context={"request": request}).data,
                "summary": {
                    "total_sessions": sessions.count(),
                    "completed_sessions": completed_sessions.count(),
                    "hours_completed": round((completed_sessions.aggregate(v=Sum("duration_minutes"))["v"] or 0) / 60, 2),
                    "average_rating": round(avg_rating or 0, 2),
                    "pending_payout": wallet.pending_payout,
                    "total_claimed": wallet.total_claimed,
                    "total_donated": wallet.total_donated,
                },
                "topic_stats": list(topic_stats),
                "ledger": SessionDispositionSerializer(
                    ledger,
                    many=True,
                    context={"request": request},
                ).data,
            }
        )

    @action(detail=True, methods=["get"], url_path="onboarding")
    def onboarding(self, request, pk=None):
        require_role(request, {ROLE_MENTOR, ROLE_ADMIN})
        mentor = self.get_object()
        identity = MentorIdentityVerification.objects.filter(mentor=mentor).first()
        contact = MentorContactVerification.objects.filter(mentor=mentor).first()
        modules = TrainingModule.objects.filter(is_active=True).order_by("order", "id")
        module_payload = build_training_module_payload_for_mentor(modules, mentor.id)
        onboarding = sync_mentor_onboarding_training_status(mentor, module_payload)
        return Response(
            {
                "status": MentorOnboardingStatusSerializer(
                    onboarding,
                    context={"request": request},
                ).data,
                "identity_verification": (
                    MentorIdentityVerificationSerializer(
                        identity,
                        context={"request": request},
                    ).data
                    if identity
                    else None
                ),
                "contact_verification": (
                    MentorContactVerificationSerializer(
                        contact,
                        context={"request": request},
                    ).data
                    if contact
                    else None
                ),
                "training_modules": module_payload,
                "training_quiz": build_training_quiz_summary(mentor),
            }
        )

    @action(detail=True, methods=["post"], url_path="admin-decision")
    def admin_decision(self, request, pk=None):
        require_role(request, {ROLE_ADMIN})
        mentor = self.get_object()
        serializer = AdminOnboardingDecisionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        decision = serializer.validated_data

        onboarding, _ = MentorOnboardingStatus.objects.get_or_create(mentor=mentor)
        identity = MentorIdentityVerification.objects.filter(mentor=mentor).first()

        def to_onboarding_identity_status(identity_decision, fallback):
            if not identity_decision:
                return fallback
            if identity_decision == "verified":
                return "completed"
            if identity_decision == "rejected":
                return "rejected"
            if identity_decision == "in_review":
                return "in_review"
            return "pending"

        with transaction.atomic():
            identity_decision = decision.get("identity_decision")
            reviewer_notes = decision.get("reviewer_notes", "").strip()
            if identity and identity_decision:
                identity.status = identity_decision
                identity.reviewed_at = timezone.now()
                if reviewer_notes:
                    identity.reviewer_notes = reviewer_notes
                identity.save(update_fields=["status", "reviewed_at", "reviewer_notes", "updated_at"])

            next_identity_status = to_onboarding_identity_status(
                identity_decision,
                onboarding.identity_status,
            )
            modules = TrainingModule.objects.filter(is_active=True).order_by("order", "id")
            module_payload = build_training_module_payload_for_mentor(modules, mentor.id)
            next_training_status = decision.get("training_status", onboarding.training_status)
            if next_training_status == "completed":
                quiz_passed = MentorTrainingQuizAttempt.objects.filter(
                    mentor=mentor,
                    status="passed",
                ).exists()
                if not quiz_passed:
                    next_training_status = "in_review"
            next_final_rejection_reason = decision.get("final_rejection_reason", "").strip()

            next_current_status = MentorOnboardingStatus.derive_current_status(
                application_status=onboarding.application_status,
                identity_status=next_identity_status,
                contact_status=onboarding.contact_status,
                training_status=next_training_status,
            )

            onboarding.identity_status = next_identity_status
            onboarding.training_status = next_training_status
            onboarding.final_rejection_reason = next_final_rejection_reason
            onboarding.current_status = next_current_status
            onboarding.save(
                update_fields=[
                    "identity_status",
                    "training_status",
                    "final_rejection_reason",
                    "current_status",
                    "updated_at",
                ]
            )

        onboarding = sync_mentor_onboarding_training_status(mentor, module_payload)

        return Response(
            {
                "status": MentorOnboardingStatusSerializer(
                    onboarding,
                    context={"request": request},
                ).data,
                "identity_verification": (
                    MentorIdentityVerificationSerializer(
                        identity,
                        context={"request": request},
                    ).data
                    if identity
                    else None
                ),
                "training_modules": module_payload,
                "training_quiz": build_training_quiz_summary(mentor),
            }
        )

class MenteeRequestViewSet(viewsets.ModelViewSet):
    queryset = MenteeRequest.objects.all().order_by("-created_at")
    serializer_class = MenteeRequestSerializer
    permission_classes = [IsMenteeOrAdminRole]

    def get_queryset(self):
        queryset = super().get_queryset()
        role = user_role(self.request.user)
        if role == ROLE_ADMIN:
            mentee_id = self.request.query_params.get("mentee_id")
            if mentee_id:
                queryset = queryset.filter(mentee_id=mentee_id)
            return queryset
        if role == ROLE_MENTEE:
            my_id = current_mentee_id(self.request)
            queryset = queryset.filter(mentee_id=my_id) if my_id else queryset.none()
            mentee_id = self.request.query_params.get("mentee_id")
            if mentee_id:
                queryset = queryset.filter(mentee_id=mentee_id)
            return queryset
        return queryset.none()

    def perform_create(self, serializer):
        role = user_role(self.request.user)
        if role == ROLE_MENTEE:
            my_id = current_mentee_id(self.request)
            if not my_id:
                raise PermissionDenied("Mentee profile not found for this user.")
            serializer.save(mentee_id=my_id)
            return
        if not serializer.validated_data.get("mentee"):
            raise ValidationError({"mentee": ["This field is required."]})
        serializer.save()

    @action(detail=True, methods=["get"], url_path="recommendations")
    def recommendations(self, request, pk=None):
        req = self.get_object()
        recs = (
            MatchRecommendation.objects.filter(
                mentee_request=req,
                mentor__onboarding_status__current_status="completed",
            )
            .select_related("mentor")
            .order_by("-score")
        )
        return Response(
            MatchRecommendationSerializer(
                recs,
                many=True,
                context={"request": request},
            ).data
        )


class MatchRecommendationViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = MatchRecommendation.objects.all().select_related("mentor", "mentee_request").order_by("-created_at")
    serializer_class = MatchRecommendationSerializer
    permission_classes = [IsAuthenticatedWithAppRole]

    def get_queryset(self):
        queryset = super().get_queryset()
        role = user_role(self.request.user)
        if role == ROLE_MENTEE:
            my_id = current_mentee_id(self.request)
            queryset = queryset.filter(mentee_request__mentee_id=my_id) if my_id else queryset.none()
        elif role == ROLE_MENTOR:
            my_id = current_mentor_id(self.request)
            queryset = queryset.filter(mentor_id=my_id) if my_id else queryset.none()
        elif role != ROLE_ADMIN:
            queryset = queryset.none()
        mentee_request_id = self.request.query_params.get("mentee_request_id")
        mentor_id = self.request.query_params.get("mentor_id")
        if mentee_request_id:
            queryset = queryset.filter(mentee_request_id=mentee_request_id)
        if mentor_id:
            queryset = queryset.filter(mentor_id=mentor_id)
        return queryset


class VolunteerEventViewSet(viewsets.ModelViewSet):
    queryset = VolunteerEvent.objects.all()
    serializer_class = VolunteerEventSerializer
    permission_classes = [IsAuthenticatedWithAppRole]
    pagination_class = SixPerPagePagination

    def get_permissions(self):
        if self.action in {"list", "retrieve"}:
            return [AllowAny()]
        return super().get_permissions()

    def get_queryset(self):
        queryset = super().get_queryset()
        role = user_role(self.request.user)
        if role != ROLE_ADMIN:
            queryset = queryset.filter(is_active=True)
        status_param = str(self.request.query_params.get("status", "")).strip().lower()
        if status_param in {VolunteerEvent.STATUS_UPCOMING, VolunteerEvent.STATUS_COMPLETED}:
            queryset = queryset.filter(status=status_param)
        if status_param == VolunteerEvent.STATUS_COMPLETED:
            return queryset.order_by("-completed_on", "-id")
        if status_param == VolunteerEvent.STATUS_UPCOMING:
            return queryset.order_by("date", "id")
        return queryset.order_by("status", "date", "-completed_on", "id")

    def perform_create(self, serializer):
        require_role(self.request, {ROLE_ADMIN})
        serializer.save()

    def perform_update(self, serializer):
        require_role(self.request, {ROLE_ADMIN})
        serializer.save()

    def destroy(self, request, *args, **kwargs):
        require_role(request, {ROLE_ADMIN})
        return super().destroy(request, *args, **kwargs)


class VolunteerEventRegistrationViewSet(viewsets.ModelViewSet):
    queryset = VolunteerEventRegistration.objects.all().select_related("volunteer_event", "mentee")
    serializer_class = VolunteerEventRegistrationSerializer
    permission_classes = [IsAuthenticatedWithAppRole]
    pagination_class = SixPerPagePagination

    def get_queryset(self):
        queryset = super().get_queryset()
        role = user_role(self.request.user)
        if role == ROLE_ADMIN:
            mentee_id = self.request.query_params.get("mentee_id")
            event_id = self.request.query_params.get("volunteer_event_id")
            if mentee_id:
                queryset = queryset.filter(mentee_id=mentee_id)
            if event_id:
                queryset = queryset.filter(volunteer_event_id=event_id)
            return queryset
        if role == ROLE_MENTEE:
            my_id = current_mentee_id(self.request)
            queryset = queryset.filter(mentee_id=my_id) if my_id else queryset.none()
            event_id = self.request.query_params.get("volunteer_event_id")
            if event_id:
                queryset = queryset.filter(volunteer_event_id=event_id)
            status_value = str(self.request.query_params.get("status", "")).strip().lower()
            today = timezone.localdate()
            if status_value == VolunteerEvent.STATUS_COMPLETED:
                queryset = queryset.filter(volunteer_event__date__lt=today)
            elif status_value == VolunteerEvent.STATUS_UPCOMING:
                queryset = queryset.filter(volunteer_event__date__gte=today)

            search_value = str(self.request.query_params.get("search", "")).strip()
            if search_value:
                queryset = queryset.filter(
                    Q(volunteer_event__title__icontains=search_value)
                    | Q(team_name__icontains=search_value)
                    | Q(city__icontains=search_value)
                    | Q(state__icontains=search_value)
                    | Q(preferred_role__icontains=search_value)
                )

            return queryset
        return queryset.none()

    def perform_create(self, serializer):
        role = user_role(self.request.user)
        save_kwargs = {"mentee": None, "submitted_by_role": "guest"}
        volunteer_event = serializer.validated_data.get("volunteer_event")
        email_value = str(serializer.validated_data.get("email") or "").strip().lower()
        if role == ROLE_MENTEE:
            my_id = current_mentee_id(self.request)
            if not my_id:
                raise PermissionDenied("Mentee profile not found for this user.")
            if volunteer_event and VolunteerEventRegistration.objects.filter(
                volunteer_event_id=volunteer_event.id,
                mentee_id=my_id,
            ).exists():
                raise ValidationError({"detail": "You are already registered for this event."})
            save_kwargs = {"mentee_id": my_id, "submitted_by_role": ROLE_MENTEE}
        elif role in {ROLE_ADMIN, ROLE_MENTOR}:
            save_kwargs = {"mentee": None, "submitted_by_role": role}
        elif volunteer_event and email_value:
            if VolunteerEventRegistration.objects.filter(
                volunteer_event_id=volunteer_event.id,
                email__iexact=email_value,
            ).exists():
                raise ValidationError({"detail": "You are already registered for this event."})
        try:
            registration = serializer.save(**save_kwargs)
            send_volunteer_registration_confirmation_email(registration)
        except IntegrityError as exc:
            detail = str(exc).lower()
            if (
                "uniq_volunteer_event_registration_per_mentee" in detail
                or ("unique" in detail and "volunteer_event" in detail and "mentee" in detail)
            ):
                raise ValidationError({"detail": "You are already registered for this event."})
            if "not null" in detail and "mentee" in detail:
                raise ValidationError(
                    {"detail": "Guest registration setup is pending server migration. Please contact support."}
                )
            raise ValidationError({"detail": "Unable to submit registration right now. Please try again."})

    def update(self, request, *args, **kwargs):
        require_role(request, {ROLE_ADMIN})
        return super().update(request, *args, **kwargs)

    def partial_update(self, request, *args, **kwargs):
        require_role(request, {ROLE_ADMIN})
        return super().partial_update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        require_role(request, {ROLE_ADMIN})
        return super().destroy(request, *args, **kwargs)


class ParentConsentVerificationViewSet(viewsets.ModelViewSet):
    queryset = ParentConsentVerification.objects.all().order_by("-id")
    serializer_class = ParentConsentVerificationSerializer
    permission_classes = [IsMenteeOrAdminRole]

    def get_queryset(self):
        queryset = super().get_queryset()
        if user_role(self.request.user) == ROLE_ADMIN:
            return queryset
        my_id = current_mentee_id(self.request)
        return queryset.filter(mentee_id=my_id) if my_id else queryset.none()

    def perform_create(self, serializer):
        if user_role(self.request.user) == ROLE_MENTEE:
            my_id = current_mentee_id(self.request)
            serializer.save(mentee_id=my_id)
            return
        serializer.save()


class MenteePreferencesViewSet(viewsets.ModelViewSet):
    queryset = MenteePreferences.objects.all().order_by("-id")
    serializer_class = MenteePreferencesSerializer
    permission_classes = [IsMenteeOrAdminRole]

    def get_queryset(self):
        queryset = super().get_queryset()
        if user_role(self.request.user) == ROLE_ADMIN:
            mentee_id = self.request.query_params.get("mentee_id")
            if mentee_id:
                queryset = queryset.filter(mentee_id=mentee_id)
            return queryset
        my_id = current_mentee_id(self.request)
        queryset = queryset.filter(mentee_id=my_id) if my_id else queryset.none()
        mentee_id = self.request.query_params.get("mentee_id")
        if mentee_id:
            queryset = queryset.filter(mentee_id=mentee_id)
        return queryset

    def perform_create(self, serializer):
        if user_role(self.request.user) == ROLE_MENTEE:
            my_id = current_mentee_id(self.request)
            serializer.save(mentee_id=my_id)
            return
        serializer.save()


class MentorAvailabilitySlotViewSet(viewsets.ModelViewSet):
    queryset = MentorAvailabilitySlot.objects.all().order_by("start_time", "id")
    serializer_class = MentorAvailabilitySlotSerializer
    permission_classes = [IsAuthenticatedWithAppRole]

    def get_queryset(self):
        queryset = super().get_queryset()
        role = user_role(self.request.user)
        if role == ROLE_ADMIN:
            mentor_id = self.request.query_params.get("mentor_id")
            if mentor_id:
                queryset = queryset.filter(mentor_id=mentor_id)
        elif role == ROLE_MENTOR:
            my_id = current_mentor_id(self.request)
            queryset = queryset.filter(mentor_id=my_id) if my_id else queryset.none()
        elif role == ROLE_MENTEE:
            mentor_id = self.request.query_params.get("mentor_id")
            if mentor_id:
                queryset = queryset.filter(mentor_id=mentor_id)
        else:
            queryset = queryset.none()
        start_from = self.request.query_params.get("start_from")
        if start_from:
            queryset = queryset.filter(start_time__gte=start_from)
        return queryset

    def perform_create(self, serializer):
        role = user_role(self.request.user)
        if role == ROLE_MENTEE:
            raise PermissionDenied("Mentee users cannot create availability slots.")
        if role == ROLE_MENTOR:
            serializer.save(mentor_id=current_mentor_id(self.request))
            return
        serializer.save()

    def update(self, request, *args, **kwargs):
        deny_if_mentee_mutation(request)
        return super().update(request, *args, **kwargs)

    def partial_update(self, request, *args, **kwargs):
        deny_if_mentee_mutation(request)
        return super().partial_update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        deny_if_mentee_mutation(request)
        return super().destroy(request, *args, **kwargs)


class SessionViewSet(viewsets.ModelViewSet):
    queryset = Session.objects.all().select_related("mentor", "mentee").order_by("-scheduled_start", "-id")
    serializer_class = SessionSerializer
    permission_classes = [IsAuthenticatedWithAppRole]

    def get_queryset(self):
        queryset = super().get_queryset()
        role = user_role(self.request.user)
        if role == ROLE_ADMIN:
            pass
        elif role == ROLE_MENTEE:
            my_id = current_mentee_id(self.request)
            queryset = queryset.filter(mentee_id=my_id) if my_id else queryset.none()
        elif role == ROLE_MENTOR:
            my_id = current_mentor_id(self.request)
            queryset = queryset.filter(mentor_id=my_id) if my_id else queryset.none()
        else:
            queryset = queryset.none()
        mentee_id = self.request.query_params.get("mentee_id")
        mentor_id = self.request.query_params.get("mentor_id")
        status_value = self.request.query_params.get("status")
        search_value = str(self.request.query_params.get("search", "")).strip()
        if mentee_id:
            queryset = queryset.filter(mentee_id=mentee_id)
        if mentor_id:
            queryset = queryset.filter(mentor_id=mentor_id)
        if status_value:
            queryset = queryset.filter(status=status_value)
        if search_value:
            search_filters = (
                Q(mentor__first_name__icontains=search_value)
                | Q(mentor__last_name__icontains=search_value)
                | Q(mentee__first_name__icontains=search_value)
                | Q(mentee__last_name__icontains=search_value)
                | Q(topic_tags__icontains=search_value)
            )
            if search_value.isdigit():
                search_filters = search_filters | Q(id=int(search_value))
            queryset = queryset.filter(search_filters)
        return queryset

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        should_paginate = (
            request.query_params.get("page") is not None
            or request.query_params.get("page_size") is not None
        )
        if should_paginate:
            paginator = SessionRecordsPagination()
            page = paginator.paginate_queryset(queryset, request, view=self)
            serializer = self.get_serializer(page, many=True)
            return paginator.get_paginated_response(serializer.data)
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=["get"], url_path="request-stats")
    def request_stats(self, request):
        require_role(request, {ROLE_MENTOR, ROLE_ADMIN})
        role = user_role(request.user)
        mentor_id_param = request.query_params.get("mentor_id")

        if role == ROLE_MENTOR:
            mentor_id = current_mentor_id(request)
            if not mentor_id:
                raise PermissionDenied("Mentor profile not found for this user.")
            if mentor_id_param and str(mentor_id_param) != str(mentor_id):
                raise PermissionDenied("You can only access your own session stats.")
        else:
            mentor_id = mentor_id_param

        if not mentor_id:
            return Response({"approved_today": 0, "approved_this_week": 0})

        now = timezone.localtime()
        start_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_week = start_today - timedelta(days=start_today.weekday())

        approved_qs = Session.objects.filter(mentor_id=mentor_id, status="approved")
        approved_today = approved_qs.filter(updated_at__gte=start_today).count()
        approved_this_week = approved_qs.filter(updated_at__gte=start_week).count()

        return Response(
            {
                "approved_today": approved_today,
                "approved_this_week": approved_this_week,
            }
        )

    @action(detail=True, methods=["get"], url_path="mentee-profile")
    def mentee_profile(self, request, pk=None):
        require_role(request, {ROLE_MENTOR, ROLE_ADMIN})
        session = self.get_object()
        if user_role(request.user) == ROLE_MENTOR and session.mentor_id != current_mentor_id(request):
            raise PermissionDenied("You can only access mentee profiles for your own sessions.")
        mentee_data = MenteeSerializer(session.mentee, context={"request": request}).data

        latest_request = (
            MenteeRequest.objects.filter(mentee_id=session.mentee_id)
            .order_by("-created_at", "-id")
            .first()
        )
        preferences = MenteePreferences.objects.filter(mentee_id=session.mentee_id).first()

        mentee_data["latest_assessment"] = (
            {
                "id": latest_request.id,
                "feeling": latest_request.feeling,
                "feeling_cause": latest_request.feeling_cause,
                "support_type": latest_request.support_type,
                "comfort_level": latest_request.comfort_level,
                "topics": latest_request.topics or [],
                "preferred_times": latest_request.preferred_times or [],
                "preferred_format": latest_request.preferred_format,
                "language": latest_request.language,
                "timezone": latest_request.timezone,
                "session_mode": latest_request.session_mode,
                "access_needs": latest_request.access_needs,
                "safety_notes": latest_request.safety_notes,
                "free_text": latest_request.free_text,
                "created_at": latest_request.created_at,
            }
            if latest_request
            else None
        )
        mentee_data["assessment_preferences"] = (
            {
                "comfort_level": preferences.comfort_level,
                "preferred_session_minutes": preferences.preferred_session_minutes,
                "preferred_mentor_types": preferences.preferred_mentor_types or [],
                "updated_at": preferences.updated_at,
            }
            if preferences
            else None
        )

        return Response(mentee_data)

    def perform_create(self, serializer):
        role = user_role(self.request.user)
        if role == ROLE_MENTEE:
            mentee_id = current_mentee_id(self.request)
            if not mentee_id:
                raise PermissionDenied("Mentee profile not found for this user.")
            session = serializer.save(mentee_id=mentee_id)
        elif role == ROLE_ADMIN:
            if not serializer.validated_data.get("mentee"):
                raise ValidationError({"mentee": ["This field is required."]})
            session = serializer.save()
        else:
            raise PermissionDenied("Only mentee or admin can create sessions.")
        if session.availability_slot_id:
            MentorAvailabilitySlot.objects.filter(id=session.availability_slot_id).update(is_available=False)

    def perform_update(self, serializer):
        serializer.save()

    @action(detail=True, methods=["post"], url_path="join-link")
    def join_link(self, request, pk=None):
        session = self.get_object()
        participant_role = resolve_session_participant_role(request, session)
        if session.status not in {"approved", "scheduled"}:
            return Response({"detail": "Session not approved yet."}, status=status.HTTP_400_BAD_REQUEST)
        role_paths = build_role_meeting_paths(session.id)
        meeting_path = build_meeting_room_path(session.id, participant_role)
        update_fields = {
            "join_url": role_paths["mentee"],
            "host_join_url": role_paths["mentor"],
        }
        if participant_role == "mentor":
            update_fields["mentor_joined_at"] = timezone.now()
        elif participant_role == "mentee":
            update_fields["mentee_joined_at"] = timezone.now()
        Session.objects.filter(id=session.id).update(
            **update_fields,
        )
        return Response(
            {
                "provider": "bondroom",
                "meeting_url": meeting_path,
                "join_url": role_paths["mentee"],
                "host_join_url": role_paths["mentor"],
                "mentor_joined_at": update_fields.get("mentor_joined_at"),
                "mentee_joined_at": update_fields.get("mentee_joined_at"),
                "room_key": f"session-{session.id}",
            }
        )

    @action(detail=True, methods=["post"], url_path="terminate")
    def terminate(self, request, pk=None):
        require_role(request, {ROLE_ADMIN})
        session = self.get_object()
        status_value = str(session.status or "").strip().lower()
        if status_value in {"completed", "canceled", "no_show"}:
            return Response(
                {"detail": f"Session already marked as {status_value}."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        reason = str(request.data.get("reason", "")).strip()
        payload = {
            "reason": "admin_terminated",
            "note": reason or "Admin terminated the session.",
            "created_at": timezone.now().isoformat(),
        }
        signal = SessionMeetingSignal.objects.create(
            session=session,
            sender_role="admin",
            signal_type="bye",
            payload=payload,
        )
        Session.objects.filter(id=session.id).update(status="canceled")
        SessionIssueReport.objects.update_or_create(
            session=session,
            defaults={
                "mentor": session.mentor,
                "category": "safety_concern",
                "status": "open",
                "description": payload["note"],
            },
        )
        return Response(
            {
                "status": "canceled",
                "signal_id": signal.id,
                "reason": payload["note"],
            }
        )

    @action(detail=True, methods=["get", "post"], url_path="meeting-signals")
    def meeting_signals(self, request, pk=None):
        session = self.get_object()
        participant_role = resolve_session_participant_role(request, session)
        if request.method == "GET":
            try:
                after_id = int(request.query_params.get("after_id", 0) or 0)
            except (TypeError, ValueError):
                return Response({"detail": "after_id must be an integer."}, status=status.HTTP_400_BAD_REQUEST)
            queryset = SessionMeetingSignal.objects.filter(session=session, id__gt=after_id)
            if participant_role in {"mentee", "mentor"}:
                queryset = queryset.exclude(sender_role=participant_role)
            queryset = queryset.order_by("id")[:200]
            serializer = SessionMeetingSignalSerializer(
                queryset,
                many=True,
                context={"request": request},
            )
            return Response(serializer.data)

        signal_type = str(request.data.get("signal_type", "")).strip().lower()
        if signal_type not in {
            "offer",
            "answer",
            "ice",
            "bye",
            "media_state",
            "safety_alert",
            "transcript",
            "transcript_bundle",
            "mentor_transcript",
            "mentee_transcript",
            "mentor_bundle",
            "mentee_bundle",
        }:
            return Response(
                {
                    "detail": (
                        "signal_type must be one of: offer, answer, ice, bye, media_state, "
                        "safety_alert, transcript, transcript_bundle, mentor_transcript, "
                        "mentee_transcript, mentor_bundle, mentee_bundle."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        payload = request.data.get("payload") or {}
        if not isinstance(payload, dict):
            return Response({"detail": "payload must be an object."}, status=status.HTTP_400_BAD_REQUEST)
        if signal_type in {
            "transcript",
            "transcript_bundle",
            "mentor_transcript",
            "mentee_transcript",
            "mentor_bundle",
            "mentee_bundle",
        }:
            payload = dict(payload)
            payload["speaker_role"] = participant_role

        signal = SessionMeetingSignal.objects.create(
            session=session,
            sender_role=participant_role,
            signal_type=signal_type,
            payload=payload,
        )
        return Response(
            SessionMeetingSignalSerializer(signal, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"], url_path="mentor-monitoring-transcript")
    def mentor_monitoring_transcript(self, request, pk=None):
        session = self.get_object()
        participant_role = resolve_session_participant_role(request, session)
        if participant_role != "mentor":
            return Response({"detail": "Only mentor can post to this endpoint."}, status=status.HTTP_403_FORBIDDEN)

        payload = request.data.get("payload") or {}
        if not isinstance(payload, dict):
            return Response({"detail": "payload must be an object."}, status=status.HTTP_400_BAD_REQUEST)

        signal_type = str(request.data.get("signal_type", "mentor_transcript")).strip().lower()
        if signal_type not in {"mentor_transcript", "mentor_bundle"}:
            return Response(
                {"detail": "signal_type must be one of: mentor_transcript, mentor_bundle."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        payload = dict(payload)
        payload["speaker_role"] = "mentor"
        signal = SessionMeetingSignal.objects.create(
            session=session,
            sender_role="mentor",
            signal_type=signal_type,
            payload=payload,
        )
        return Response(
            SessionMeetingSignalSerializer(signal, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"], url_path="mentee-monitoring-transcript")
    def mentee_monitoring_transcript(self, request, pk=None):
        session = self.get_object()
        participant_role = resolve_session_participant_role(request, session)
        if participant_role != "mentee":
            return Response({"detail": "Only mentee can post to this endpoint."}, status=status.HTTP_403_FORBIDDEN)

        payload = request.data.get("payload") or {}
        if not isinstance(payload, dict):
            return Response({"detail": "payload must be an object."}, status=status.HTTP_400_BAD_REQUEST)

        signal_type = str(request.data.get("signal_type", "mentee_transcript")).strip().lower()
        if signal_type not in {"mentee_transcript", "mentee_bundle"}:
            return Response(
                {"detail": "signal_type must be one of: mentee_transcript, mentee_bundle."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        payload = dict(payload)
        payload["speaker_role"] = "mentee"
        signal = SessionMeetingSignal.objects.create(
            session=session,
            sender_role="mentee",
            signal_type=signal_type,
            payload=payload,
        )
        return Response(
            SessionMeetingSignalSerializer(signal, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"], url_path="realtime-transcript-chunk")
    def realtime_transcript_chunk(self, request, pk=None):
        session = self.get_object()
        participant_role = resolve_session_participant_role(request, session)
        if participant_role not in {"mentor", "mentee"}:
            return Response({"detail": "Only mentor or mentee can stream transcript chunks."}, status=status.HTTP_403_FORBIDDEN)

        audio_chunk = request.FILES.get("audio_chunk")
        if not audio_chunk:
            return Response({"detail": "audio_chunk file is required."}, status=status.HTTP_400_BAD_REQUEST)

        transcript, stt_error = transcribe_audio_chunk_with_openai(audio_chunk)
        transcript_excerpt = re.sub(r"\s+", " ", str(transcript or "").strip())
        if not transcript_excerpt:
            payload = {"transcript_excerpt": "", "signal": None}
            if settings.DEBUG:
                payload["transcription_error"] = stt_error or "unknown"
                payload["audio_size_bytes"] = int(getattr(audio_chunk, "size", 0) or 0)
                payload["audio_content_type"] = str(getattr(audio_chunk, "content_type", "") or "")
            return Response(payload, status=status.HTTP_200_OK)

        transcript_excerpt = transcript_excerpt[:1200]
        signal_type = "mentor_transcript" if participant_role == "mentor" else "mentee_transcript"
        payload = {
            "speaker_role": participant_role,
            "transcript_excerpt": transcript_excerpt,
            "created_at": str(request.data.get("created_at", "")).strip() or timezone.now().isoformat(),
        }
        signal = SessionMeetingSignal.objects.create(
            session=session,
            sender_role=participant_role,
            signal_type=signal_type,
            payload=payload,
        )
        return Response(
            {
                "transcript_excerpt": transcript_excerpt,
                "signal": SessionMeetingSignalSerializer(signal, context={"request": request}).data,
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["get", "post"], url_path="recording")
    def recording(self, request, pk=None):
        session = self.get_object()
        resolve_session_participant_role(request, session)
        recording, _ = SessionRecording.objects.get_or_create(session=session)
        if request.method == "GET":
            return Response(SessionRecordingSerializer(recording, context={"request": request}).data)

        require_role(request, {ROLE_MENTOR, ROLE_ADMIN})
        status_value = str(request.data.get("status", "")).strip().lower()
        if status_value not in {"not_started", "recording", "stopped", "uploaded", "failed"}:
            return Response(
                {"detail": "status must be one of: not_started, recording, stopped, uploaded, failed."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        recording.status = status_value
        recording.recording_url = str(request.data.get("recording_url", recording.recording_url or "")).strip()
        recording.storage_key = str(request.data.get("storage_key", recording.storage_key or "")).strip()
        file_size = request.data.get("file_size_bytes")
        uploaded_file = request.FILES.get("recording_file") or request.data.get("recording_file")
        running_on_vercel = bool(os.environ.get("VERCEL", "").strip())
        using_s3_media = bool(getattr(settings, "USE_S3_MEDIA", False))
        if uploaded_file and running_on_vercel and not using_s3_media:
            return Response(
                {
                    "detail": (
                        "recording_file upload is not supported on this deployment "
                        "without S3 media storage. Configure S3_MEDIA_BUCKET_NAME and AWS credentials, "
                        "or send recording_url/storage_key instead."
                    )
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        if uploaded_file:
            recording.recording_file = uploaded_file
            if not file_size and getattr(uploaded_file, "size", None) not in (None, ""):
                recording.file_size_bytes = int(uploaded_file.size)
        if file_size not in (None, ""):
            try:
                recording.file_size_bytes = int(file_size)
            except (TypeError, ValueError):
                return Response(
                    {"detail": "file_size_bytes must be an integer."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        metadata = request.data.get("metadata")
        if metadata is not None:
            if isinstance(metadata, str):
                metadata_raw = metadata.strip()
                if metadata_raw:
                    try:
                        metadata = json.loads(metadata_raw)
                    except json.JSONDecodeError:
                        return Response(
                            {"detail": "metadata must be a valid JSON object."},
                            status=status.HTTP_400_BAD_REQUEST,
                        )
                else:
                    metadata = {}
            if not isinstance(metadata, dict):
                return Response({"detail": "metadata must be an object."}, status=status.HTTP_400_BAD_REQUEST)
            merged_metadata = dict(recording.metadata or {})
            merged_metadata.update(metadata)
            recording.metadata = merged_metadata

        if status_value == "recording" and not recording.started_at:
            recording.started_at = timezone.now()
        if status_value in {"stopped", "uploaded", "failed"}:
            recording.ended_at = timezone.now()

        try:
            recording.save()
        except Exception as exc:
            return Response(
                {
                    "detail": "Unable to persist recording update.",
                    "error": str(exc) if settings.DEBUG else "Check server media storage configuration.",
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        if recording.recording_file:
            try:
                next_storage_key = str(recording.recording_file.name or "").strip()
                next_recording_url = build_absolute_media_url(recording.recording_file.url, request=request)
                needs_update = False
                update_fields = []
                if next_storage_key and recording.storage_key != next_storage_key:
                    recording.storage_key = next_storage_key
                    update_fields.append("storage_key")
                    needs_update = True
                if next_recording_url and recording.recording_url != next_recording_url:
                    recording.recording_url = next_recording_url
                    update_fields.append("recording_url")
                    needs_update = True
                if needs_update:
                    update_fields.append("updated_at")
                    recording.save(update_fields=update_fields)
            except Exception as exc:
                return Response(
                    {
                        "detail": "Recording metadata saved, but file URL resolution failed.",
                        "error": str(exc) if settings.DEBUG else "Check media storage URL configuration.",
                    },
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )
        return Response(SessionRecordingSerializer(recording, context={"request": request}).data)

    @action(detail=True, methods=["post"], url_path="recording-upload-signature")
    def recording_upload_signature(self, request, pk=None):
        session = self.get_object()
        resolve_session_participant_role(request, session)
        require_role(request, {ROLE_MENTOR, ROLE_ADMIN})

        s3_bucket = str(getattr(settings, "S3_MEDIA_BUCKET_NAME", "") or "").strip()
        configured_s3_region = str(getattr(settings, "AWS_S3_REGION_NAME", "") or "").strip()
        s3_region = configured_s3_region or "us-east-1"
        s3_endpoint_url = str(getattr(settings, "AWS_S3_ENDPOINT_URL", "") or "").strip()
        s3_custom_domain = str(getattr(settings, "S3_MEDIA_CUSTOM_DOMAIN", "") or "").strip()
        expires_seconds = max(60, int(getattr(settings, "S3_PRESIGNED_UPLOAD_EXPIRES_SECONDS", 900) or 900))
        recordings_prefix = str(
            getattr(settings, "S3_MEDIA_RECORDINGS_PREFIX", "session_recordings") or "session_recordings"
        ).strip().strip("/")

        if not s3_bucket or not boto3:
            return Response(
                {"detail": "S3 upload signing is not configured on backend."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        if not s3_endpoint_url and (not configured_s3_region or s3_region == "us-east-1"):
            discovered_bucket_region = discover_s3_bucket_region(s3_bucket)
            if discovered_bucket_region:
                s3_region = discovered_bucket_region

        file_name = str(request.data.get("file_name", "") or "").strip()
        content_type = str(request.data.get("content_type", "") or "").strip() or "video/webm"
        file_ext = os.path.splitext(file_name)[1].lower()
        if not file_ext or len(file_ext) > 10:
            file_ext = ".webm"
        if not file_ext.startswith("."):
            file_ext = f".{file_ext}"

        key_parts = [part for part in [recordings_prefix, f"session-{session.id}"] if part]
        key_prefix = "/".join(key_parts)
        object_key = f"{key_prefix}/{int(time.time())}-{uuid.uuid4().hex}{file_ext}"
        params = {
            "Bucket": s3_bucket,
            "Key": object_key,
            "ContentType": content_type,
        }
        client_kwargs = {"region_name": s3_region}
        if s3_endpoint_url:
            client_kwargs["endpoint_url"] = s3_endpoint_url
        if BotoConfig:
            client_kwargs["config"] = BotoConfig(signature_version="s3v4")
        aws_access_key_id = str(getattr(settings, "AWS_ACCESS_KEY_ID", "") or "").strip()
        aws_secret_access_key = str(getattr(settings, "AWS_SECRET_ACCESS_KEY", "") or "").strip()
        aws_session_token = str(getattr(settings, "AWS_SESSION_TOKEN", "") or "").strip()
        if aws_access_key_id and aws_secret_access_key:
            client_kwargs["aws_access_key_id"] = aws_access_key_id
            client_kwargs["aws_secret_access_key"] = aws_secret_access_key
        if aws_session_token:
            client_kwargs["aws_session_token"] = aws_session_token

        try:
            s3_client = boto3.client("s3", **client_kwargs)
            upload_url = s3_client.generate_presigned_url(
                "put_object",
                Params=params,
                ExpiresIn=expires_seconds,
                HttpMethod="PUT",
            )
        except Exception as exc:
            return Response(
                {
                    "detail": "Unable to generate S3 upload signature.",
                    "error": str(exc) if settings.DEBUG else "Check S3 configuration.",
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        recording_url = build_s3_object_url(
            bucket=s3_bucket,
            key=object_key,
            region=s3_region,
            custom_domain=s3_custom_domain,
        )

        return Response(
            {
                "provider": "s3",
                "upload_url": upload_url,
                "method": "PUT",
                "headers": {"Content-Type": content_type},
                "storage_key": object_key,
                "recording_url": recording_url,
                "expires_in": expires_seconds,
            }
        )

    @action(detail=True, methods=["post"], url_path="analyze-transcript")
    def analyze_transcript(self, request, pk=None):
        session = self.get_object()
        require_role(request, {ROLE_MENTEE, ROLE_MENTOR, ROLE_ADMIN})
        role = user_role(request.user)
        participant_role = resolve_session_participant_role(request, session)
        transcript = str(request.data.get("transcript", "")).strip()[:5000]
        generate_summary = parse_bool(request.data.get("generate_summary"))
        if generate_summary and role == ROLE_MENTEE:
            raise PermissionDenied("Mentee users cannot generate meeting summaries.")
        speaker_role = str(request.data.get("speaker_role", participant_role)).strip().lower() or participant_role
        if speaker_role not in {"mentee", "mentor", "admin", "system", "unknown"}:
            speaker_role = "unknown"
        if role == ROLE_MENTEE and speaker_role not in {"mentee", "system", "unknown"}:
            speaker_role = "mentee"
        if role == ROLE_MENTOR and speaker_role not in {"mentor", "mentee", "system", "unknown"}:
            speaker_role = "mentor"

        analysis = classify_abuse(transcript)
        incident = None
        warning_policy = {
            "warning_count": 0,
            "warning_limit_before_disconnect": warning_policy_config()[0],
            "disconnect_on_warning": warning_policy_config()[1],
            "auto_disconnected": False,
            "disconnect_signal_id": None,
        }
        snapshot = {}
        if analysis["flagged"]:
            if speaker_role == "mentee":
                snapshot = mentee_snapshot_for_incident(session)
            normalized_transcript = re.sub(r"\s+", " ", transcript.strip().lower())
            normalized_matches = sorted(
                {str(term or "").strip().lower() for term in analysis["matches"] if str(term or "").strip()}
            )
            duplicate_cutoff = timezone.now() - timedelta(seconds=20)
            recent_incidents = (
                SessionAbuseIncident.objects.filter(
                    session=session,
                    speaker_role=speaker_role,
                    created_at__gte=duplicate_cutoff,
                )
                .order_by("-created_at", "-id")[:10]
            )
            for row in recent_incidents:
                row_text = re.sub(r"\s+", " ", str(row.transcript_snippet or "").strip().lower())
                row_matches = sorted(
                    {
                        str(term or "").strip().lower()
                        for term in (row.matched_terms or [])
                        if str(term or "").strip()
                    }
                )
                if row_text == normalized_transcript and row_matches == normalized_matches:
                    incident = row
                    snapshot = dict(row.flagged_mentee_snapshot or snapshot)
                    break

            if incident is None:
                incident = SessionAbuseIncident.objects.create(
                    session=session,
                    incident_type="verbal_abuse",
                    detection_source="transcript",
                    speaker_role=speaker_role,
                    transcript_snippet=transcript,
                    matched_terms=analysis["matches"],
                    severity=analysis["severity"],
                    confidence_score=analysis["confidence_score"],
                    recommended_action=(
                        "terminate_session"
                        if analysis["severity"] == "high"
                        else ("escalate_review" if analysis["severity"] == "medium" else "warn")
                    ),
                    flagged_mentee_snapshot=snapshot,
                    detection_notes=str(request.data.get("notes", "")).strip(),
                )
                warning_policy = enforce_session_warning_policy(
                    session=session,
                    speaker_role=speaker_role,
                    reason="abusive transcript detected",
                    incident_id=incident.id,
                )

        summary_payload = None
        summary_error = ""
        if generate_summary:
            if not transcript:
                return Response(
                    {"detail": "transcript is required when generate_summary is true."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            try:
                summary_payload = generate_meeting_summary_with_openai(session, transcript)
                recording, _ = SessionRecording.objects.get_or_create(session=session)
                merged_metadata = dict(recording.metadata or {})
                merged_metadata.update(
                    {
                        "meeting_summary": summary_payload.get("summary", ""),
                        "meeting_highlights": summary_payload.get("highlights", []),
                        "meeting_action_items": summary_payload.get("action_items", []),
                        "summary_generated_at": timezone.now().isoformat(),
                        "summary_model": summary_payload.get("model", ""),
                    }
                )
                recording.metadata = merged_metadata
                recording.save(update_fields=["metadata", "updated_at"])
            except Exception as exc:
                summary_error = str(exc)

        return Response(
            {
                "flagged": analysis["flagged"],
                "speaker_role": speaker_role,
                "severity": analysis["severity"],
                "confidence_score": analysis["confidence_score"],
                "matched_terms": analysis["matches"],
                "incident_type": incident.incident_type if incident else "unknown",
                "recommended_action": incident.recommended_action if incident else "none",
                "incident_id": incident.id if incident else None,
                "flagged_mentee_info": snapshot if role in {ROLE_ADMIN, ROLE_MENTOR} else {},
                "summary_generated": bool(summary_payload),
                "summary": summary_payload.get("summary", "") if summary_payload else "",
                "highlights": summary_payload.get("highlights", []) if summary_payload else [],
                "action_items": summary_payload.get("action_items", []) if summary_payload else [],
                "summary_model": summary_payload.get("model", "") if summary_payload else "",
                "summary_error": summary_error,
                "warning_count": warning_policy["warning_count"],
                "warning_limit_before_disconnect": warning_policy["warning_limit_before_disconnect"],
                "disconnect_on_warning": warning_policy["disconnect_on_warning"],
                "auto_disconnected": warning_policy["auto_disconnected"],
                "disconnect_signal_id": warning_policy["disconnect_signal_id"],
            }
        )

    @action(detail=True, methods=["post"], url_path="report-behavior")
    def report_behavior(self, request, pk=None):
        session = self.get_object()
        role = user_role(request.user)
        participant_role = resolve_session_participant_role(request, session)

        speaker_role = str(request.data.get("speaker_role", "unknown")).strip().lower() or "unknown"
        if speaker_role not in {"mentee", "mentor", "admin", "system", "unknown"}:
            speaker_role = "unknown"
        if role in {ROLE_MENTEE, ROLE_MENTOR} and speaker_role not in {
            "mentee",
            "mentor",
            "system",
            "unknown",
        }:
            speaker_role = participant_role

        labels = request.data.get("labels") or []
        if isinstance(labels, str):
            labels = [labels]
        if not isinstance(labels, list):
            return Response({"detail": "labels must be a list of strings."}, status=status.HTTP_400_BAD_REQUEST)
        labels = [str(label).strip() for label in labels if str(label).strip()][:20]

        notes = str(request.data.get("notes", "")).strip()
        confidence_score = request.data.get("confidence_score", 0.0)
        classification = classify_behavior_signal(
            labels=labels,
            note=notes,
            confidence_score=confidence_score,
        )

        payload = request.data.get("payload") or {}
        if not isinstance(payload, dict):
            return Response({"detail": "payload must be an object."}, status=status.HTTP_400_BAD_REQUEST)

        event_timestamp = None
        raw_event_time = str(request.data.get("event_timestamp", "")).strip()
        if raw_event_time:
            event_timestamp = parse_datetime(raw_event_time)
            if event_timestamp is None:
                return Response(
                    {"detail": "event_timestamp must be a valid ISO datetime string."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        evidence_url = str(request.data.get("evidence_url", "")).strip()
        if evidence_url and not evidence_url.startswith(("http://", "https://")):
            return Response(
                {"detail": "evidence_url must start with http:// or https://"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        incident_type = str(request.data.get("incident_type", classification["incident_type"])).strip().lower() or "unknown"
        valid_incident_types = {choice[0] for choice in SessionAbuseIncident.INCIDENT_TYPE_CHOICES}
        if incident_type not in valid_incident_types:
            incident_type = classification["incident_type"] if classification["incident_type"] in valid_incident_types else "unknown"

        severity = str(request.data.get("severity", classification["severity"])).strip().lower() or classification["severity"]
        if severity not in {"low", "medium", "high"}:
            severity = classification["severity"]
        recommended_action = str(
            request.data.get("recommended_action", classification["recommended_action"])
        ).strip().lower() or classification["recommended_action"]
        if recommended_action not in {"none", "warn", "escalate_review", "terminate_session"}:
            recommended_action = classification["recommended_action"]

        if not classification["flagged"] and severity == "low" and not labels and not notes:
            return Response(
                {"detail": "Provide at least one behavior signal in labels or notes."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if speaker_role == "mentee":
            snapshot = mentee_snapshot_for_incident(session)
        else:
            snapshot = {}

        incident = SessionAbuseIncident.objects.create(
            session=session,
            incident_type=incident_type,
            detection_source="client_signal",
            speaker_role=speaker_role,
            transcript_snippet=notes,
            matched_terms=classification["matched_terms"] or labels,
            severity=severity,
            confidence_score=classification["confidence_score"],
            recommended_action=recommended_action,
            event_timestamp=event_timestamp,
            evidence_url=evidence_url,
            detection_payload={
                **payload,
                "labels": labels,
                "reported_by_role": participant_role,
            },
            flagged_mentee_snapshot=snapshot,
            detection_notes=notes,
        )
        warning_policy = enforce_session_warning_policy(
            session=session,
            speaker_role=speaker_role,
            reason=f"behavior alert ({incident_type})",
            incident_id=incident.id,
        )

        if severity in {"medium", "high"}:
            SessionIssueReport.objects.update_or_create(
                session=session,
                defaults={
                    "mentor": session.mentor,
                    "category": "safety_concern",
                    "status": "open",
                    "description": (
                        f"Behavior alert ({incident_type}) detected. "
                        f"severity={severity}, action={recommended_action}, "
                        f"speaker_role={speaker_role}, labels={', '.join(labels) or 'n/a'}."
                    ),
                },
            )

        return Response(
            {
                "incident_id": incident.id,
                "flagged": classification["flagged"] or severity in {"medium", "high"},
                "incident_type": incident.incident_type,
                "severity": incident.severity,
                "recommended_action": incident.recommended_action,
                "confidence_score": incident.confidence_score,
                "escalated_to_issue_report": severity in {"medium", "high"},
                "warning_count": warning_policy["warning_count"],
                "warning_limit_before_disconnect": warning_policy["warning_limit_before_disconnect"],
                "disconnect_on_warning": warning_policy["disconnect_on_warning"],
                "auto_disconnected": warning_policy["auto_disconnected"],
                "disconnect_signal_id": warning_policy["disconnect_signal_id"],
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"], url_path="analyze-video-frame")
    def analyze_video_frame(self, request, pk=None):
        session = self.get_object()
        role = user_role(request.user)
        participant_role = resolve_session_participant_role(request, session)
        frame_data_url = str(request.data.get("frame_data_url", "")).strip()
        if not frame_data_url.startswith("data:image/"):
            return Response(
                {"detail": "frame_data_url must be a valid data:image/... URL."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        speaker_role = str(request.data.get("speaker_role", "unknown")).strip().lower() or "unknown"
        if speaker_role not in {"mentee", "mentor", "admin", "system", "unknown"}:
            speaker_role = "unknown"
        if role in {ROLE_MENTEE, ROLE_MENTOR} and speaker_role not in {
            "mentee",
            "mentor",
            "system",
            "unknown",
        }:
            speaker_role = participant_role

        # Frame hash for short-term duplicate suppression after incident creation.
        frame_hash = hashlib.sha1(frame_data_url[:4000].encode("utf-8")).hexdigest()
        dedupe_key = f"session:{session.id}:vision:{speaker_role}:{frame_hash}"

        analysis = classify_video_behavior_frame(
            frame_data_url=frame_data_url,
            note=str(request.data.get("notes", "")).strip(),
        )
        if not analysis.get("flagged"):
            return Response(
                {
                    "flagged": False,
                    "incident_type": analysis.get("incident_type", "unknown"),
                    "severity": analysis.get("severity", "low"),
                    "recommended_action": analysis.get("recommended_action", "none"),
                    "confidence_score": analysis.get("confidence_score", 0.0),
                    "matched_terms": analysis.get("matched_terms", []),
                }
            )

        incident_type = str(analysis.get("incident_type", "unknown")).strip().lower() or "unknown"
        confidence_score = float(analysis.get("confidence_score") or 0.0)
        if incident_type == "unknown":
            return Response(
                {
                    "flagged": False,
                    "suppressed": True,
                    "reason": "unknown_incident_type",
                    "incident_type": incident_type,
                    "severity": analysis.get("severity", "low"),
                    "recommended_action": analysis.get("recommended_action", "none"),
                    "confidence_score": confidence_score,
                }
            )

        min_confidence_map = {
            "inappropriate_gesture": float(
                os.environ.get("VISION_GESTURE_MIN_CONFIDENCE", "0.55")
            ),
            "inappropriate_attire": float(
                os.environ.get("VISION_ATTIRE_MIN_CONFIDENCE", "0.65")
            ),
            "sexual_content": float(
                os.environ.get("VISION_SEXUAL_MIN_CONFIDENCE", "0.7")
            ),
            "harassment": float(
                os.environ.get("VISION_HARASSMENT_MIN_CONFIDENCE", "0.72")
            ),
            "unsafe_environment": float(
                os.environ.get("VISION_UNSAFE_ENV_MIN_CONFIDENCE", "0.78")
            ),
        }
        min_confidence = min_confidence_map.get(incident_type, 0.9)
        if confidence_score < min_confidence:
            return Response(
                {
                    "flagged": False,
                    "suppressed": True,
                    "reason": "low_confidence",
                    "incident_type": incident_type,
                    "severity": analysis.get("severity", "low"),
                    "recommended_action": analysis.get("recommended_action", "none"),
                    "confidence_score": confidence_score,
                    "required_confidence": min_confidence,
                }
            )

        consecutive_required_map = {
            "inappropriate_gesture": int(
                os.environ.get("VISION_GESTURE_CONSECUTIVE_REQUIRED", "1")
            ),
            "inappropriate_attire": int(
                os.environ.get("VISION_ATTIRE_CONSECUTIVE_REQUIRED", "1")
            ),
            "sexual_content": int(
                os.environ.get("VISION_SEXUAL_CONSECUTIVE_REQUIRED", "1")
            ),
            "harassment": int(
                os.environ.get("VISION_HARASSMENT_CONSECUTIVE_REQUIRED", "2")
            ),
            "unsafe_environment": int(
                os.environ.get("VISION_UNSAFE_ENV_CONSECUTIVE_REQUIRED", "3")
            ),
        }
        global_consecutive_required = int(os.environ.get("VISION_CONSECUTIVE_REQUIRED", "1"))
        consecutive_required = max(
            1, consecutive_required_map.get(incident_type, global_consecutive_required)
        )
        streak_key = f"session:{session.id}:vision-streak:{speaker_role}:{incident_type}"
        streak_count = int(cache.get(streak_key, 0) or 0) + 1
        cache.set(streak_key, streak_count, timeout=40)
        if streak_count < consecutive_required:
            return Response(
                {
                    "flagged": False,
                    "suppressed": True,
                    "reason": "requires_consecutive_detection",
                    "incident_type": incident_type,
                    "severity": analysis.get("severity", "low"),
                    "recommended_action": analysis.get("recommended_action", "none"),
                    "confidence_score": confidence_score,
                    "required_consecutive": consecutive_required,
                    "observed_consecutive": streak_count,
                }
            )
        cache.delete(streak_key)

        cooldown_seconds = max(15, int(os.environ.get("VISION_ALERT_COOLDOWN_SECONDS", "60")))
        cooldown_key = f"session:{session.id}:vision-cooldown:{speaker_role}:{incident_type}"
        if cache.get(cooldown_key):
            return Response(
                {
                    "flagged": False,
                    "suppressed": True,
                    "reason": "cooldown_active",
                    "incident_type": incident_type,
                    "severity": analysis.get("severity", "low"),
                    "recommended_action": analysis.get("recommended_action", "none"),
                    "confidence_score": confidence_score,
                }
            )
        duplicate_window_seconds = max(
            1, int(os.environ.get("VISION_FRAME_DEDUP_SECONDS", "3"))
        )
        if cache.get(dedupe_key):
            return Response(
                {
                    "flagged": False,
                    "suppressed": True,
                    "reason": "duplicate_frame",
                    "incident_type": incident_type,
                    "severity": analysis.get("severity", "low"),
                    "recommended_action": analysis.get("recommended_action", "none"),
                    "confidence_score": confidence_score,
                }
            )

        if speaker_role == "mentee":
            snapshot = mentee_snapshot_for_incident(session)
        else:
            snapshot = {}

        incident = SessionAbuseIncident.objects.create(
            session=session,
            incident_type=incident_type,
            detection_source="ai_vision",
            speaker_role=speaker_role,
            transcript_snippet=str(analysis.get("notes", "")).strip(),
            matched_terms=analysis.get("matched_terms", []),
            severity=str(analysis.get("severity", "low")).strip().lower() or "low",
            confidence_score=confidence_score,
            recommended_action=str(analysis.get("recommended_action", "warn")).strip().lower() or "warn",
            evidence_url=str(request.data.get("evidence_url", "")).strip(),
            detection_payload={
                "reported_by_role": participant_role,
                "model": str(os.environ.get("OPENAI_VISION_MODERATION_MODEL", "gpt-4.1-mini")).strip(),
                "source": "video_frame_auto",
            },
            flagged_mentee_snapshot=snapshot,
            detection_notes=str(request.data.get("notes", "")).strip(),
        )
        warning_policy = enforce_session_warning_policy(
            session=session,
            speaker_role=speaker_role,
            reason=f"ai vision incident ({incident.incident_type})",
            incident_id=incident.id,
        )

        if incident.severity in {"medium", "high"}:
            SessionIssueReport.objects.update_or_create(
                session=session,
                defaults={
                    "mentor": session.mentor,
                    "category": "safety_concern",
                    "status": "open",
                    "description": (
                        f"Video behavior alert ({incident.incident_type}) detected by AI vision. "
                        f"severity={incident.severity}, action={incident.recommended_action}, "
                        f"speaker_role={speaker_role}."
                    ),
                },
            )

        cache.set(cooldown_key, 1, timeout=cooldown_seconds)
        cache.set(dedupe_key, 1, timeout=duplicate_window_seconds)
        return Response(
            {
                "flagged": True,
                "incident_id": incident.id,
                "incident_type": incident.incident_type,
                "severity": incident.severity,
                "recommended_action": incident.recommended_action,
                "confidence_score": incident.confidence_score,
                "matched_terms": incident.matched_terms or [],
                "escalated_to_issue_report": incident.severity in {"medium", "high"},
                "warning_count": warning_policy["warning_count"],
                "warning_limit_before_disconnect": warning_policy["warning_limit_before_disconnect"],
                "disconnect_on_warning": warning_policy["disconnect_on_warning"],
                "auto_disconnected": warning_policy["auto_disconnected"],
                "disconnect_signal_id": warning_policy["disconnect_signal_id"],
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["get"], url_path="abuse-incidents")
    def abuse_incidents(self, request, pk=None):
        session = self.get_object()
        role = user_role(request.user)
        participant_role = resolve_session_participant_role(request, session)
        queryset = SessionAbuseIncident.objects.filter(session=session).order_by("-created_at", "-id")
        serializer = SessionAbuseIncidentSerializer(
            queryset,
            many=True,
            context={"request": request},
        )
        data = serializer.data
        if role == ROLE_MENTEE and participant_role == "mentee":
            for item in data:
                item["flagged_mentee_snapshot"] = {}
        return Response(data)

    @action(detail=True, methods=["get", "post"], url_path="feedback")
    def feedback(self, request, pk=None):
        session = self.get_object()
        feedback = SessionFeedback.objects.filter(session=session).first()
        role = user_role(request.user)
        if request.method == "GET":
            if not feedback:
                return Response({"detail": "Feedback not found."}, status=status.HTTP_404_NOT_FOUND)
            return Response(SessionFeedbackSerializer(feedback, context={"request": request}).data)
        require_role(request, {ROLE_MENTEE, ROLE_ADMIN})
        if role == ROLE_MENTEE and session.mentee_id != current_mentee_id(request):
            raise PermissionDenied("You can only submit feedback for your own sessions.")
        serializer = SessionFeedbackSerializer(
            feedback,
            data={**request.data, "session": session.id},
            partial=bool(feedback),
        )
        serializer.is_valid(raise_exception=True)
        serializer.save(session=session)

        # Keep mentor.average_rating in sync with actual submitted feedback.
        avg_rating = (
            SessionFeedback.objects
            .filter(session__mentor=session.mentor, rating__isnull=False)
            .aggregate(value=Avg("rating"))
            .get("value")
        )
        Mentor.objects.filter(id=session.mentor_id).update(
            average_rating=round(avg_rating, 2) if avg_rating is not None else None
        )

        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="disposition")
    def disposition(self, request, pk=None):
        require_role(request, {ROLE_MENTOR, ROLE_ADMIN})
        session = self.get_object()
        if user_role(request.user) == ROLE_MENTOR and session.mentor_id != current_mentor_id(request):
            raise PermissionDenied("You can only process your own sessions.")
        serializer = SessionDispositionActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        action_name = serializer.validated_data["action"]
        amount = serializer.validated_data.get("amount", Decimal("500.00"))
        note = serializer.validated_data.get("note", "")

        with transaction.atomic():
            disposition, _ = SessionDisposition.objects.update_or_create(
                session=session,
                defaults={
                    "mentor": session.mentor,
                    "action": action_name,
                    "status": "confirmed",
                    "amount": amount,
                    "note": note,
                },
            )
            wallet, _ = MentorWallet.objects.get_or_create(mentor=session.mentor)

            if action_name == "claim":
                wallet.pending_payout += amount
                wallet.total_claimed += amount
                wallet.save(update_fields=["pending_payout", "total_claimed", "updated_at"])
                PayoutTransaction.objects.create(
                    mentor=session.mentor,
                    session=session,
                    transaction_type="session_claim",
                    status="pending",
                    amount=amount,
                    note=note,
                )
            elif action_name == "donate":
                wallet.total_donated += amount
                wallet.save(update_fields=["total_donated", "updated_at"])
                DonationTransaction.objects.update_or_create(
                    session=session,
                    defaults={
                        "mentor": session.mentor,
                        "amount": amount,
                        "cause": "underprivileged_students",
                        "status": "completed",
                        "note": note,
                    },
                )
            else:
                SessionIssueReport.objects.update_or_create(
                    session=session,
                    defaults={
                        "mentor": session.mentor,
                        "category": serializer.validated_data.get("issue_category", "other"),
                        "description": serializer.validated_data.get(
                            "issue_description", note or "Issue reported by mentor."
                        ),
                        "status": "open",
                    },
                )

        return Response(SessionDispositionSerializer(disposition, context={"request": request}).data)

class SessionFeedbackViewSet(viewsets.ModelViewSet):
    queryset = SessionFeedback.objects.all().select_related("session").order_by("-submitted_at")
    serializer_class = SessionFeedbackSerializer
    permission_classes = [IsAuthenticatedWithAppRole]

    def get_queryset(self):
        queryset = super().get_queryset()
        role = user_role(self.request.user)
        if role == ROLE_ADMIN:
            pass
        elif role == ROLE_MENTEE:
            my_id = current_mentee_id(self.request)
            queryset = queryset.filter(session__mentee_id=my_id) if my_id else queryset.none()
        elif role == ROLE_MENTOR:
            my_id = current_mentor_id(self.request)
            queryset = queryset.filter(session__mentor_id=my_id) if my_id else queryset.none()
        else:
            queryset = queryset.none()
        session_id = self.request.query_params.get("session_id")
        mentor_id = self.request.query_params.get("mentor_id")
        if session_id:
            queryset = queryset.filter(session_id=session_id)
        if mentor_id:
            queryset = queryset.filter(session__mentor_id=mentor_id)
        return queryset

    def perform_create(self, serializer):
        role = user_role(self.request.user)
        if role not in {ROLE_MENTEE, ROLE_ADMIN}:
            raise PermissionDenied("Only mentee or admin can submit feedback.")
        session = serializer.validated_data["session"]
        if role == ROLE_MENTEE and session.mentee_id != current_mentee_id(self.request):
            raise PermissionDenied("You can only submit feedback for your own sessions.")
        serializer.save()

    def perform_update(self, serializer):
        role = user_role(self.request.user)
        if role not in {ROLE_MENTEE, ROLE_ADMIN}:
            raise PermissionDenied("Only mentee or admin can update feedback.")
        serializer.save()


class MentorIdentityVerificationViewSet(viewsets.ModelViewSet):
    queryset = MentorIdentityVerification.objects.all().order_by("-submitted_at")
    serializer_class = MentorIdentityVerificationSerializer
    permission_classes = [IsMentorOrAdminRole]
    REVIEW_KEYS = ("id_front", "id_back", "address_front", "address_back", "professional_certificate")
    REVIEW_DECISIONS = {"pending", "approved", "rejected"}
    REVIEW_CONTROL_FIELDS = {"document_review_status", "document_review_comments"}

    def get_queryset(self):
        queryset = super().get_queryset()
        if user_role(self.request.user) == ROLE_ADMIN:
            mentor_id = self.request.query_params.get("mentor_id")
            if mentor_id:
                queryset = queryset.filter(mentor_id=mentor_id)
            return queryset
        my_id = current_mentor_id(self.request)
        queryset = queryset.filter(mentor_id=my_id) if my_id else queryset.none()
        mentor_id = self.request.query_params.get("mentor_id")
        if mentor_id:
            queryset = queryset.filter(mentor_id=mentor_id)
        return queryset

    def perform_create(self, serializer):
        role = user_role(self.request.user)
        if role != ROLE_ADMIN and self._request_has_review_control_fields():
            raise PermissionDenied("Only admins can update document review decisions.")
        if role == ROLE_MENTOR:
            serializer.save(mentor_id=current_mentor_id(self.request))
            return
        serializer.save()

    def _request_has_review_control_fields(self):
        request_data = getattr(self.request, "data", {}) or {}
        keys = set()
        if hasattr(request_data, "keys"):
            keys = {str(item).strip() for item in request_data.keys()}
        return any(field in keys for field in self.REVIEW_CONTROL_FIELDS)

    @classmethod
    def _identity_status_from_document_review(cls, review_status):
        status_map = review_status if isinstance(review_status, dict) else {}
        values = [str(status_map.get(key, "pending")).strip().lower() for key in cls.REVIEW_KEYS]
        if any(item == "rejected" for item in values):
            return "rejected"
        if values and all(item == "approved" for item in values):
            return "verified"
        if any(item == "approved" for item in values):
            return "in_review"
        return "pending"

    @staticmethod
    def _onboarding_identity_status(identity_status):
        if identity_status == "verified":
            return "completed"
        if identity_status == "rejected":
            return "rejected"
        if identity_status == "in_review":
            return "in_review"
        return "pending"

    def _sync_identity_outcome(self, verification):
        next_identity_status = self._identity_status_from_document_review(
            verification.document_review_status
        )
        update_fields = []
        if verification.status != next_identity_status:
            verification.status = next_identity_status
            update_fields.append("status")
        if next_identity_status in {"verified", "rejected", "in_review"}:
            verification.reviewed_at = timezone.now()
            update_fields.append("reviewed_at")
        if update_fields:
            verification.save(update_fields=[*set(update_fields), "updated_at"])

        onboarding, _ = MentorOnboardingStatus.objects.get_or_create(mentor=verification.mentor)
        next_onboarding_status = self._onboarding_identity_status(next_identity_status)
        if onboarding.identity_status != next_onboarding_status:
            onboarding.identity_status = next_onboarding_status
            onboarding.save(update_fields=["identity_status", "updated_at", "current_status"])

    def _reset_review_for_mentor_resubmission(self, verification, previous_values=None):
        request_data = getattr(self.request, "data", {}) or {}
        if not hasattr(request_data, "keys"):
            return False

        previous_values = previous_values or {}
        touched_fields = {str(item).strip() for item in request_data.keys()}
        keys_to_reset = set()

        if "id_proof_document" in touched_fields:
            keys_to_reset.add("id_front")
        if "passport_or_license" in touched_fields:
            keys_to_reset.add("id_back")
        if "address_proof_document" in touched_fields or "aadhaar_front" in touched_fields:
            keys_to_reset.add("address_front")
        if "aadhaar_back" in touched_fields:
            keys_to_reset.add("address_back")
        if "professional_certificate_document" in touched_fields:
            keys_to_reset.add("professional_certificate")

        previous_id_type = str(previous_values.get("id_proof_type") or "").strip()
        current_id_type = str(verification.id_proof_type or "").strip()
        previous_id_number = str(previous_values.get("id_proof_number") or "").strip()
        current_id_number = str(verification.id_proof_number or "").strip()
        previous_address_type = str(previous_values.get("address_proof_type") or "").strip()
        current_address_type = str(verification.address_proof_type or "").strip()
        previous_address_number = str(previous_values.get("address_proof_number") or "").strip()
        current_address_number = str(verification.address_proof_number or "").strip()

        if (
            ("id_proof_type" in touched_fields and previous_id_type != current_id_type)
            or ("id_proof_number" in touched_fields and previous_id_number != current_id_number)
        ):
            keys_to_reset.update({"id_front", "id_back"})
        if (
            ("address_proof_type" in touched_fields and previous_address_type != current_address_type)
            or (
                "address_proof_number" in touched_fields
                and previous_address_number != current_address_number
            )
        ):
            keys_to_reset.update({"address_front", "address_back"})

        if not keys_to_reset:
            return False

        status_map = (
            dict(verification.document_review_status)
            if isinstance(verification.document_review_status, dict)
            else {}
        )
        comments_map = (
            dict(verification.document_review_comments)
            if isinstance(verification.document_review_comments, dict)
            else {}
        )
        changed = False
        for key in keys_to_reset:
            if status_map.get(key) != "pending":
                status_map[key] = "pending"
                changed = True
            if comments_map.get(key):
                comments_map[key] = ""
                changed = True

        if not changed:
            return False

        verification.document_review_status = status_map
        verification.document_review_comments = comments_map
        verification.save(update_fields=["document_review_status", "document_review_comments", "updated_at"])
        return True

    def perform_update(self, serializer):
        role = user_role(self.request.user)
        if role != ROLE_ADMIN and self._request_has_review_control_fields():
            raise PermissionDenied("Only admins can update document review decisions.")

        previous_values = {}
        if role == ROLE_MENTOR and serializer.instance is not None:
            previous_values = {
                "id_proof_type": serializer.instance.id_proof_type,
                "id_proof_number": serializer.instance.id_proof_number,
                "address_proof_type": serializer.instance.address_proof_type,
                "address_proof_number": serializer.instance.address_proof_number,
            }

        verification = serializer.save()
        if role == ROLE_MENTOR:
            self._reset_review_for_mentor_resubmission(
                verification,
                previous_values=previous_values,
            )
            self._sync_identity_outcome(verification)
            return
        if role == ROLE_ADMIN:
            self._sync_identity_outcome(verification)

    @action(detail=True, methods=["post"], url_path="document-decision", permission_classes=[IsAdminRole])
    def document_decision(self, request, pk=None):
        verification = self.get_object()
        document_key = str(request.data.get("document_key", "")).strip().lower()
        decision = str(request.data.get("decision", "")).strip().lower()
        comment = str(request.data.get("comment", "")).strip()

        if document_key not in self.REVIEW_KEYS:
            return Response({"detail": "Invalid document key."}, status=status.HTTP_400_BAD_REQUEST)
        if decision not in self.REVIEW_DECISIONS:
            return Response({"detail": "Invalid decision."}, status=status.HTTP_400_BAD_REQUEST)
        if decision == "rejected" and not comment:
            return Response(
                {"detail": "Comment is required when rejecting a document."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        status_map = verification.document_review_status if isinstance(verification.document_review_status, dict) else {}
        comments_map = (
            verification.document_review_comments
            if isinstance(verification.document_review_comments, dict)
            else {}
        )
        status_map = dict(status_map)
        comments_map = dict(comments_map)
        status_map[document_key] = decision
        comments_map[document_key] = comment if decision == "rejected" else ""

        verification.document_review_status = status_map
        verification.document_review_comments = comments_map
        verification.save(update_fields=["document_review_status", "document_review_comments", "updated_at"])
        self._sync_identity_outcome(verification)
        return Response(
            self.get_serializer(verification, context={"request": request}).data
        )


class MentorContactVerificationViewSet(viewsets.ModelViewSet):
    queryset = MentorContactVerification.objects.all().order_by("-id")
    serializer_class = MentorContactVerificationSerializer
    permission_classes = [IsMentorOrAdminRole]

    def get_queryset(self):
        queryset = super().get_queryset()
        if user_role(self.request.user) == ROLE_ADMIN:
            mentor_id = self.request.query_params.get("mentor_id")
            if mentor_id:
                queryset = queryset.filter(mentor_id=mentor_id)
            return queryset
        my_id = current_mentor_id(self.request)
        queryset = queryset.filter(mentor_id=my_id) if my_id else queryset.none()
        mentor_id = self.request.query_params.get("mentor_id")
        if mentor_id:
            queryset = queryset.filter(mentor_id=mentor_id)
        return queryset

    def perform_create(self, serializer):
        if user_role(self.request.user) == ROLE_MENTOR:
            serializer.save(mentor_id=current_mentor_id(self.request))
            return
        serializer.save()


class MentorOnboardingStatusViewSet(viewsets.ModelViewSet):
    queryset = MentorOnboardingStatus.objects.all().order_by("-id")
    serializer_class = MentorOnboardingStatusSerializer
    permission_classes = [IsMentorOrAdminRole]

    def get_queryset(self):
        queryset = super().get_queryset()
        if user_role(self.request.user) == ROLE_ADMIN:
            mentor_id = self.request.query_params.get("mentor_id")
            if mentor_id:
                queryset = queryset.filter(mentor_id=mentor_id)
            return queryset
        my_id = current_mentor_id(self.request)
        queryset = queryset.filter(mentor_id=my_id) if my_id else queryset.none()
        mentor_id = self.request.query_params.get("mentor_id")
        if mentor_id:
            queryset = queryset.filter(mentor_id=mentor_id)
        return queryset

    def perform_create(self, serializer):
        if user_role(self.request.user) == ROLE_MENTOR:
            serializer.save(mentor_id=current_mentor_id(self.request))
            return
        serializer.save()


class TrainingModuleViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = TrainingModule.objects.filter(is_active=True).order_by("order", "id")
    serializer_class = TrainingModuleSerializer
    permission_classes = [IsMentorOrAdminRole]

    def get_serializer_class(self):
        if self.action == "watch_video":
            return TrainingVideoWatchSerializer
        if self.action == "quiz_start":
            return TrainingQuizStartSerializer
        if self.action == "quiz_submit":
            return TrainingQuizSubmitSerializer
        if self.action == "quiz_abandon":
            return TrainingQuizAbandonSerializer
        return super().get_serializer_class()

    @staticmethod
    def _modules_fully_completed(module_payload):
        return bool(module_payload) and all(
            item.get("training_status") == "completed" for item in module_payload
        )

    def _resolve_mentor(self, request, mentor_id=None):
        role = user_role(request.user)
        if role == ROLE_MENTOR:
            mentor_id = current_mentor_id(request)
        elif role == ROLE_ADMIN:
            mentor_id = mentor_id or request.query_params.get("mentor_id")
        else:
            raise PermissionDenied("Only mentor or admin can access training modules.")

        if not mentor_id:
            return None
        mentor = Mentor.objects.filter(id=mentor_id).first()
        if not mentor:
            raise ValidationError({"mentor_id": "Mentor not found."})
        return mentor

    def list(self, request, *args, **kwargs):
        mentor = self._resolve_mentor(request)
        modules = self.get_queryset()
        if not mentor:
            return Response(
                TrainingModuleSerializer(
                    modules,
                    many=True,
                    context={"request": request},
                ).data
            )
        payload = build_training_module_payload_for_mentor(modules, mentor.id)
        sync_mentor_onboarding_training_status(mentor, payload)
        return Response(payload)

    @action(detail=False, methods=["get"], url_path="quiz")
    def quiz_status(self, request):
        mentor = self._resolve_mentor(request)
        if not mentor:
            raise ValidationError({"mentor_id": "Mentor is required for this action."})

        modules = list(self.get_queryset())
        module_payload = build_training_module_payload_for_mentor(modules, mentor.id)
        onboarding = sync_mentor_onboarding_training_status(mentor, module_payload)
        completed_modules = sum(
            1 for item in module_payload if item.get("training_status") == "completed"
        )

        latest_attempt = (
            MentorTrainingQuizAttempt.objects.filter(mentor=mentor)
            .order_by("-started_at", "-id")
            .first()
        )
        passed = MentorTrainingQuizAttempt.objects.filter(mentor=mentor, status="passed").exists()
        include_questions = bool(latest_attempt and latest_attempt.status == "pending")
        return Response(
            {
                "quiz_required": bool(module_payload),
                "modules_completed": self._modules_fully_completed(module_payload),
                "completed_modules": completed_modules,
                "total_modules": len(module_payload),
                "quiz_passed": passed,
                "latest_attempt": serialize_quiz_attempt_for_client(
                    latest_attempt, include_questions=include_questions
                ),
                "onboarding_training_status": onboarding.training_status,
            }
        )

    @action(detail=False, methods=["post"], url_path="quiz/start")
    def quiz_start(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        mentor = self._resolve_mentor(request, mentor_id=serializer.validated_data.get("mentor_id"))
        if not mentor:
            raise ValidationError({"mentor_id": "Mentor is required for this action."})

        modules = list(self.get_queryset())
        module_payload = build_training_module_payload_for_mentor(modules, mentor.id)
        if not self._modules_fully_completed(module_payload):
            return Response(
                {"detail": "Complete all training modules before starting the quiz."},
                status=status.HTTP_409_CONFLICT,
            )

        passed_attempt = (
            MentorTrainingQuizAttempt.objects.filter(mentor=mentor, status="passed")
            .order_by("-started_at", "-id")
            .first()
        )
        if passed_attempt:
            onboarding = sync_mentor_onboarding_training_status(mentor, module_payload)
            return Response(
                {
                    "detail": "Quiz already passed.",
                    "attempt": serialize_quiz_attempt_for_client(
                        passed_attempt, include_questions=False
                    ),
                    "quiz_passed": True,
                    "onboarding_training_status": onboarding.training_status,
                }
            )

        pending_attempt = (
            MentorTrainingQuizAttempt.objects.filter(mentor=mentor, status="pending")
            .order_by("-started_at", "-id")
            .first()
        )
        if pending_attempt:
            onboarding = sync_mentor_onboarding_training_status(mentor, module_payload)
            return Response(
                {
                    "attempt": serialize_quiz_attempt_for_client(
                        pending_attempt, include_questions=True
                    ),
                    "quiz_passed": False,
                    "onboarding_training_status": onboarding.training_status,
                }
            )

        total_questions = 15
        pass_mark = TRAINING_QUIZ_PASS_MARK
        latest_resolved_attempt = (
            MentorTrainingQuizAttempt.objects.filter(mentor=mentor)
            .exclude(status="pending")
            .order_by("-started_at", "-id")
            .first()
        )
        latest_signature = (
            quiz_questions_signature(latest_resolved_attempt.questions)
            if latest_resolved_attempt
            else None
        )
        generated_by = "openai"
        questions = []
        try:
            for _ in range(3):
                questions, generated_by = generate_training_quiz_questions(
                    modules, total_questions=total_questions
                )
                if not latest_signature or quiz_questions_signature(questions) != latest_signature:
                    break
        except Exception as exc:
            payload = {"detail": "Unable to generate quiz from OpenAI right now. Please try again."}
            if settings.DEBUG:
                payload["debug_error"] = str(exc)
                payload["debug_error_type"] = exc.__class__.__name__
            return Response(payload, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        if latest_signature and quiz_questions_signature(questions) == latest_signature:
            return Response(
                {"detail": "Could not generate a fresh quiz. Please try again."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        attempt = MentorTrainingQuizAttempt.objects.create(
            mentor=mentor,
            total_questions=total_questions,
            pass_mark=pass_mark,
            questions=questions,
            selected_answers=[],
            score=0,
            status="pending",
        )
        onboarding = sync_mentor_onboarding_training_status(mentor, module_payload)
        return Response(
            {
                "attempt": serialize_quiz_attempt_for_client(attempt, include_questions=True),
                "generated_by": generated_by,
                "quiz_passed": False,
                "onboarding_training_status": onboarding.training_status,
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=False, methods=["post"], url_path="quiz/submit")
    def quiz_submit(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        mentor = self._resolve_mentor(request, mentor_id=serializer.validated_data.get("mentor_id"))
        if not mentor:
            raise ValidationError({"mentor_id": "Mentor is required for this action."})

        attempt = MentorTrainingQuizAttempt.objects.filter(
            id=serializer.validated_data["attempt_id"],
            mentor=mentor,
        ).first()
        if not attempt:
            return Response({"detail": "Quiz attempt not found."}, status=status.HTTP_404_NOT_FOUND)

        if attempt.status != "pending":
            return Response(
                {
                    "detail": "This quiz attempt is already submitted.",
                    "attempt": serialize_quiz_attempt_for_client(attempt, include_questions=False),
                },
                status=status.HTTP_409_CONFLICT,
            )

        score, normalized_answers = evaluate_quiz_attempt(
            attempt.questions,
            serializer.validated_data["selected_answers"],
        )
        wrong_count = max(0, attempt.total_questions - score)
        if attempt.pass_mark != TRAINING_QUIZ_PASS_MARK:
            attempt.pass_mark = TRAINING_QUIZ_PASS_MARK
        passed = score >= TRAINING_QUIZ_PASS_MARK
        attempt.selected_answers = normalized_answers
        attempt.score = score
        attempt.status = "passed" if passed else "failed"
        attempt.submitted_at = timezone.now()
        attempt.save(
            update_fields=[
                "pass_mark",
                "selected_answers",
                "score",
                "status",
                "submitted_at",
                "updated_at",
            ]
        )

        modules = list(self.get_queryset())
        module_payload = build_training_module_payload_for_mentor(modules, mentor.id)
        onboarding = sync_mentor_onboarding_training_status(mentor, module_payload)
        return Response(
            {
                "passed": passed,
                "score": score,
                "wrong_count": wrong_count,
                "pass_mark": TRAINING_QUIZ_PASS_MARK,
                "total_questions": attempt.total_questions,
                "attempt": serialize_quiz_attempt_for_client(attempt, include_questions=False),
                "onboarding_training_status": onboarding.training_status,
            }
        )

    @action(detail=False, methods=["post"], url_path="quiz/abandon")
    def quiz_abandon(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        mentor = self._resolve_mentor(request, mentor_id=serializer.validated_data.get("mentor_id"))
        if not mentor:
            raise ValidationError({"mentor_id": "Mentor is required for this action."})

        attempt = MentorTrainingQuizAttempt.objects.filter(
            id=serializer.validated_data["attempt_id"],
            mentor=mentor,
        ).first()
        if not attempt:
            return Response({"detail": "Quiz attempt not found."}, status=status.HTTP_404_NOT_FOUND)

        if attempt.status == "pending":
            attempt.score = 0
            attempt.status = "failed"
            attempt.submitted_at = timezone.now()
            attempt.save(update_fields=["score", "status", "submitted_at", "updated_at"])

        modules = list(self.get_queryset())
        module_payload = build_training_module_payload_for_mentor(modules, mentor.id)
        onboarding = sync_mentor_onboarding_training_status(mentor, module_payload)
        return Response(
            {
                "detail": "Quiz attempt marked as failed.",
                "attempt": serialize_quiz_attempt_for_client(attempt, include_questions=False),
                "onboarding_training_status": onboarding.training_status,
            }
        )

    @action(detail=True, methods=["post"], url_path="watch-video")
    def watch_video(self, request, pk=None):
        module = self.get_object()
        serializer = TrainingVideoWatchSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        mentor = self._resolve_mentor(request, mentor_id=serializer.validated_data.get("mentor_id"))
        if not mentor:
            raise ValidationError({"mentor_id": "Mentor is required for this action."})

        modules = list(self.get_queryset())
        module_payload = build_training_module_payload_for_mentor(modules, mentor.id)
        module_state = next((item for item in module_payload if item["id"] == module.id), None)
        if not module_state:
            return Response({"detail": "Module not found."}, status=status.HTTP_404_NOT_FOUND)
        if module_state["training_status"] == "locked":
            return Response(
                {"detail": "Complete previous modules first."},
                status=status.HTTP_409_CONFLICT,
            )

        progress, _ = MentorTrainingProgress.objects.get_or_create(
            mentor=mentor,
            module=module,
            defaults={"status": "in_progress", "progress_percent": 0},
        )
        current_percent = 100 if progress.progress_percent >= 100 else 50 if progress.progress_percent >= 50 else 0
        video_index = serializer.validated_data["video_index"]

        if video_index == 1:
            next_percent = max(current_percent, 50)
        else:
            if current_percent < 50:
                return Response(
                    {"detail": "Complete video 1 before marking video 2."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            next_percent = 100

        progress.status = "completed" if next_percent >= 100 else "in_progress"
        progress.progress_percent = next_percent
        progress.last_activity_at = timezone.now()
        progress.completed_at = timezone.now() if next_percent >= 100 else None
        progress.save(
            update_fields=[
                "status",
                "progress_percent",
                "last_activity_at",
                "completed_at",
                "updated_at",
            ]
        )

        module_payload = build_training_module_payload_for_mentor(modules, mentor.id)
        sync_mentor_onboarding_training_status(mentor, module_payload)
        updated_module_state = next(item for item in module_payload if item["id"] == module.id)
        return Response({"module": updated_module_state, "modules": module_payload})


class MentorTrainingProgressViewSet(viewsets.ModelViewSet):
    queryset = MentorTrainingProgress.objects.all().select_related("mentor", "module").order_by("module__order")
    serializer_class = MentorTrainingProgressSerializer
    permission_classes = [IsMentorOrAdminRole]

    def get_queryset(self):
        queryset = super().get_queryset()
        if user_role(self.request.user) == ROLE_ADMIN:
            mentor_id = self.request.query_params.get("mentor_id")
            if mentor_id:
                queryset = queryset.filter(mentor_id=mentor_id)
            return queryset
        my_id = current_mentor_id(self.request)
        queryset = queryset.filter(mentor_id=my_id) if my_id else queryset.none()
        mentor_id = self.request.query_params.get("mentor_id")
        if mentor_id:
            queryset = queryset.filter(mentor_id=mentor_id)
        return queryset

    def perform_create(self, serializer):
        if user_role(self.request.user) == ROLE_MENTOR:
            serializer.save(mentor_id=current_mentor_id(self.request))
            return
        serializer.save()


class MentorProfileViewSet(viewsets.ModelViewSet):
    queryset = MentorProfile.objects.all().select_related("mentor").order_by("-id")
    serializer_class = MentorProfileSerializer
    permission_classes = [IsMentorOrAdminRole]

    def get_queryset(self):
        queryset = super().get_queryset()
        if user_role(self.request.user) == ROLE_ADMIN:
            mentor_id = self.request.query_params.get("mentor_id")
            if mentor_id:
                queryset = queryset.filter(mentor_id=mentor_id)
            return queryset
        my_id = current_mentor_id(self.request)
        queryset = queryset.filter(mentor_id=my_id) if my_id else queryset.none()
        mentor_id = self.request.query_params.get("mentor_id")
        if mentor_id:
            queryset = queryset.filter(mentor_id=mentor_id)
        return queryset

    def perform_create(self, serializer):
        if user_role(self.request.user) == ROLE_MENTOR:
            serializer.save(mentor_id=current_mentor_id(self.request))
            return
        serializer.save()


class SessionDispositionViewSet(viewsets.ModelViewSet):
    queryset = SessionDisposition.objects.all().select_related("session", "mentor").order_by("-decided_at")
    serializer_class = SessionDispositionSerializer
    permission_classes = [IsMentorOrAdminRole]

    def get_permissions(self):
        if self.action in {"create", "update", "partial_update", "destroy"}:
            return [IsAdminRole()]
        return super().get_permissions()

    def get_queryset(self):
        queryset = super().get_queryset()
        if user_role(self.request.user) == ROLE_ADMIN:
            pass
        else:
            my_id = current_mentor_id(self.request)
            queryset = queryset.filter(mentor_id=my_id) if my_id else queryset.none()
        mentor_id = self.request.query_params.get("mentor_id")
        action_name = self.request.query_params.get("action")
        if mentor_id:
            queryset = queryset.filter(mentor_id=mentor_id)
        if action_name:
            queryset = queryset.filter(action=action_name)
        return queryset


class MentorWalletViewSet(
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    mixins.ListModelMixin,
    viewsets.GenericViewSet,
):
    queryset = MentorWallet.objects.all().select_related("mentor").order_by("-id")
    serializer_class = MentorWalletSerializer
    permission_classes = [IsMentorOrAdminRole]

    def get_permissions(self):
        if self.action in {"update", "partial_update"}:
            return [IsAdminRole()]
        return super().get_permissions()

    def get_queryset(self):
        queryset = super().get_queryset()
        if user_role(self.request.user) == ROLE_ADMIN:
            mentor_id = self.request.query_params.get("mentor_id")
            if mentor_id:
                queryset = queryset.filter(mentor_id=mentor_id)
            return queryset
        my_id = current_mentor_id(self.request)
        queryset = queryset.filter(mentor_id=my_id) if my_id else queryset.none()
        mentor_id = self.request.query_params.get("mentor_id")
        if mentor_id:
            queryset = queryset.filter(mentor_id=mentor_id)
        return queryset


class PayoutTransactionViewSet(viewsets.ModelViewSet):
    queryset = PayoutTransaction.objects.all().select_related("mentor", "session").order_by("-created_at")
    serializer_class = PayoutTransactionSerializer
    permission_classes = [IsMentorOrAdminRole]

    def get_permissions(self):
        if self.action in {"create", "update", "partial_update", "destroy"}:
            return [IsAdminRole()]
        if self.action == "mark_paid":
            return [IsMentorOrAdminRole()]
        return super().get_permissions()

    def get_queryset(self):
        queryset = super().get_queryset()
        if user_role(self.request.user) == ROLE_ADMIN:
            pass
        else:
            my_id = current_mentor_id(self.request)
            queryset = queryset.filter(mentor_id=my_id) if my_id else queryset.none()
        mentor_id = self.request.query_params.get("mentor_id")
        status_value = self.request.query_params.get("status")
        if mentor_id:
            queryset = queryset.filter(mentor_id=mentor_id)
        if status_value:
            queryset = queryset.filter(status=status_value)
        return queryset

    def _sync_wallet_pending_payout(self, payout_tx, previous_status, next_status):
        if previous_status == next_status:
            return
        wallet, _ = MentorWallet.objects.get_or_create(mentor=payout_tx.mentor)
        amount = payout_tx.amount or Decimal("0.00")

        if previous_status != "paid" and next_status == "paid":
            if wallet.pending_payout < amount:
                raise ValidationError("Pending payout is lower than the payout transaction amount.")
            wallet.pending_payout -= amount
            wallet.save(update_fields=["pending_payout", "updated_at"])
            return

        if previous_status == "paid" and next_status != "paid":
            wallet.pending_payout += amount
            wallet.save(update_fields=["pending_payout", "updated_at"])

    def perform_update(self, serializer):
        existing = self.get_object()
        previous_status = existing.status

        with transaction.atomic():
            payout_tx = serializer.save()
            self._sync_wallet_pending_payout(payout_tx, previous_status, payout_tx.status)
            if previous_status != "paid" and payout_tx.status == "paid" and not payout_tx.processed_at:
                payout_tx.processed_at = timezone.now()
                payout_tx.save(update_fields=["processed_at", "updated_at"])

    @action(detail=True, methods=["post"], url_path="mark-paid")
    def mark_paid(self, request, pk=None):
        require_role(request, {ROLE_MENTOR, ROLE_ADMIN})
        payout_tx = self.get_object()
        if user_role(request.user) == ROLE_MENTOR and payout_tx.mentor_id != current_mentor_id(request):
            raise PermissionDenied("You can only process your own payouts.")
        if payout_tx.status == "paid":
            return Response(self.get_serializer(payout_tx).data)

        reference_id = str(request.data.get("reference_id") or "").strip()
        note = request.data.get("note")

        with transaction.atomic():
            self._sync_wallet_pending_payout(payout_tx, payout_tx.status, "paid")
            payout_tx.status = "paid"
            payout_tx.processed_at = timezone.now()
            update_fields = ["status", "processed_at", "updated_at"]
            if reference_id:
                payout_tx.reference_id = reference_id
                update_fields.append("reference_id")
            if note is not None:
                payout_tx.note = str(note)
                update_fields.append("note")
            payout_tx.save(update_fields=update_fields)

        return Response(self.get_serializer(payout_tx).data)


class DonationTransactionViewSet(viewsets.ModelViewSet):
    queryset = DonationTransaction.objects.all().select_related("mentor", "session").order_by("-created_at")
    serializer_class = DonationTransactionSerializer
    permission_classes = [IsMentorOrAdminRole]

    def get_permissions(self):
        if self.action in {"create", "update", "partial_update", "destroy"}:
            return [IsAdminRole()]
        return super().get_permissions()

    def get_queryset(self):
        queryset = super().get_queryset()
        if user_role(self.request.user) == ROLE_ADMIN:
            pass
        else:
            my_id = current_mentor_id(self.request)
            queryset = queryset.filter(mentor_id=my_id) if my_id else queryset.none()
        mentor_id = self.request.query_params.get("mentor_id")
        status_value = self.request.query_params.get("status")
        if mentor_id:
            queryset = queryset.filter(mentor_id=mentor_id)
        if status_value:
            queryset = queryset.filter(status=status_value)
        return queryset


class SessionIssueReportViewSet(viewsets.ModelViewSet):
    queryset = SessionIssueReport.objects.all().select_related("mentor", "session").order_by("-created_at")
    serializer_class = SessionIssueReportSerializer
    permission_classes = [IsMentorOrAdminRole]

    def get_permissions(self):
        if self.action in {"create", "update", "partial_update", "destroy"}:
            return [IsAdminRole()]
        return super().get_permissions()

    def get_queryset(self):
        queryset = super().get_queryset()
        if user_role(self.request.user) == ROLE_ADMIN:
            pass
        else:
            my_id = current_mentor_id(self.request)
            queryset = queryset.filter(mentor_id=my_id) if my_id else queryset.none()
        mentor_id = self.request.query_params.get("mentor_id")
        status_value = self.request.query_params.get("status")
        if mentor_id:
            queryset = queryset.filter(mentor_id=mentor_id)
        if status_value:
            queryset = queryset.filter(status=status_value)
        return queryset

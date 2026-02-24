from datetime import timedelta
from decimal import Decimal
import re

from django.contrib.auth import get_user_model
from django.conf import settings
from django.db import transaction
from django.db.models import Avg, Count, Q, Sum
from django.contrib.auth.models import update_last_login
from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.generics import GenericAPIView
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.settings import api_settings
from rest_framework_simplejwt.tokens import RefreshToken

from .location_catalog import get_cities_for_state, get_states
from .models import (
    AdminAccount,
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
    TrainingModule,
    UserProfile,
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
    generate_otp,
    hash_otp,
    otp_expiry,
)
from .quiz import (
    clean_question_text,
    evaluate_quiz_attempt,
    generate_training_quiz_questions,
)
from .abuse_monitoring import classify_abuse
from .signals import generate_recommendations_for_request

TRAINING_QUIZ_PASS_MARK = 7
User = get_user_model()


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


def normalize_mobile(value):
    return "".join(ch for ch in str(value or "") if ch.isdigit())


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
    for mentee in Mentee.objects.all().order_by("-id").only("id", "email", "parent_mobile"):
        if normalize_mobile(mentee.parent_mobile) == normalized:
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

    def post(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        mentee = serializer.save()
        return Response(serializer.to_representation(mentee), status=status.HTTP_201_CREATED)


class MentorRegisterView(GenericAPIView):
    permission_classes = [AllowAny]
    serializer_class = MentorRegisterSerializer

    def post(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        mentor = serializer.save()
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
        mentor = Mentor.objects.filter(id=serializer.validated_data["mentor_id"]).first()
        if not mentor:
            return Response({"detail": "Mentor not found."}, status=status.HTTP_404_NOT_FOUND)

        otp = (settings.MENTOR_TEST_OTP or "").strip() or generate_otp()
        channel = serializer.validated_data["channel"]
        verification, _ = MentorContactVerification.objects.get_or_create(mentor=mentor)
        now = timezone.now()
        expiry = otp_expiry()
        otp_hash = hash_otp(otp)
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
        mentor = Mentor.objects.filter(id=serializer.validated_data["mentor_id"]).first()
        if not mentor:
            return Response({"detail": "Mentor not found."}, status=status.HTTP_404_NOT_FOUND)
        verification = MentorContactVerification.objects.filter(mentor=mentor).first()
        if not verification:
            return Response(
                {"detail": "OTP was not requested for this mentor."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        channel = serializer.validated_data["channel"]
        otp_hash = hash_otp(serializer.validated_data["otp"])
        now = timezone.now()
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
        return Response(MenteePreferencesSerializer(prefs).data)

    @action(detail=True, methods=["get"], url_path="dashboard")
    def dashboard(self, request, pk=None):
        mentee = self.get_object()
        now = timezone.now()
        sessions = Session.objects.filter(mentee=mentee).select_related("mentor")
        upcoming = sessions.filter(
            status__in=["requested", "approved", "scheduled"],
            scheduled_start__gte=now,
        ).order_by("scheduled_start")[:5]
        recent = sessions.filter(
            Q(status__in=["completed", "canceled", "no_show"]) | Q(scheduled_start__lt=now)
        ).order_by("-scheduled_start")[:5]
        latest_request = MenteeRequest.objects.filter(mentee=mentee).order_by("-created_at").first()
        recommendations = []
        if latest_request:
            rec_qs = MatchRecommendation.objects.filter(mentee_request=latest_request).select_related(
                "mentor"
            ).order_by("-score")[:5]
            recommendations = MatchRecommendationSerializer(rec_qs, many=True).data
        return Response(
            {
                "mentee": MenteeSerializer(mentee).data,
                "upcoming_sessions": SessionSerializer(upcoming, many=True).data,
                "recent_sessions": SessionSerializer(recent, many=True).data,
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
    queryset = Mentor.objects.all().prefetch_related("availability_slots").order_by("-created_at")
    serializer_class = MentorSerializer
    permission_classes = [IsAuthenticatedWithAppRole]

    def get_permissions(self):
        if self.action in {"create", "destroy"}:
            return [IsAdminRole()]
        return super().get_permissions()

    def create(self, request, *args, **kwargs):
        raise PermissionDenied("Use /api/auth/register/mentor/ for registration.")

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
            return Response({"detail": "No request found."}, status=404)

        should_refresh = role == ROLE_MENTEE or request.query_params.get("refresh") in {"1", "true", "True"}
        if should_refresh:
            generate_recommendations_for_request(req)

        recs = MatchRecommendation.objects.filter(mentee_request=req).select_related("mentor").order_by("-score")
        return Response(MatchRecommendationSerializer(recs, many=True).data)

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
            serializer.save(mentor=mentor)
            return Response(serializer.data)
        return Response(MentorProfileSerializer(profile).data)

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
                "mentor": MentorSerializer(mentor).data,
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
                "ledger": SessionDispositionSerializer(ledger, many=True).data,
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
                "status": MentorOnboardingStatusSerializer(onboarding).data,
                "identity_verification": MentorIdentityVerificationSerializer(identity).data if identity else None,
                "contact_verification": MentorContactVerificationSerializer(contact).data if contact else None,
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
                "status": MentorOnboardingStatusSerializer(onboarding).data,
                "identity_verification": MentorIdentityVerificationSerializer(identity).data if identity else None,
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
        recs = MatchRecommendation.objects.filter(mentee_request=req).select_related("mentor").order_by("-score")
        return Response(MatchRecommendationSerializer(recs, many=True).data)


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
        if mentee_id:
            queryset = queryset.filter(mentee_id=mentee_id)
        if mentor_id:
            queryset = queryset.filter(mentor_id=mentor_id)
        if status_value:
            queryset = queryset.filter(status=status_value)
        return queryset

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
        return Response(MenteeSerializer(session.mentee).data)

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
        Session.objects.filter(id=session.id).update(
            join_url=role_paths["mentee"],
            host_join_url=role_paths["mentor"],
        )
        return Response(
            {
                "provider": "bondroom",
                "meeting_url": meeting_path,
                "join_url": role_paths["mentee"],
                "host_join_url": role_paths["mentor"],
                "room_key": f"session-{session.id}",
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
            serializer = SessionMeetingSignalSerializer(queryset, many=True)
            return Response(serializer.data)

        signal_type = str(request.data.get("signal_type", "")).strip().lower()
        if signal_type not in {"offer", "answer", "ice", "bye"}:
            return Response(
                {"detail": "signal_type must be one of: offer, answer, ice, bye."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        payload = request.data.get("payload") or {}
        if not isinstance(payload, dict):
            return Response({"detail": "payload must be an object."}, status=status.HTTP_400_BAD_REQUEST)

        signal = SessionMeetingSignal.objects.create(
            session=session,
            sender_role=participant_role,
            signal_type=signal_type,
            payload=payload,
        )
        return Response(SessionMeetingSignalSerializer(signal).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="recording")
    def recording(self, request, pk=None):
        session = self.get_object()
        require_role(request, {ROLE_MENTOR, ROLE_ADMIN})
        resolve_session_participant_role(request, session)
        status_value = str(request.data.get("status", "")).strip().lower()
        if status_value not in {"not_started", "recording", "stopped", "uploaded", "failed"}:
            return Response(
                {"detail": "status must be one of: not_started, recording, stopped, uploaded, failed."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        recording, _ = SessionRecording.objects.get_or_create(session=session)
        recording.status = status_value
        recording.recording_url = str(request.data.get("recording_url", recording.recording_url or "")).strip()
        recording.storage_key = str(request.data.get("storage_key", recording.storage_key or "")).strip()
        file_size = request.data.get("file_size_bytes")
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
            if not isinstance(metadata, dict):
                return Response({"detail": "metadata must be an object."}, status=status.HTTP_400_BAD_REQUEST)
            recording.metadata = metadata

        if status_value == "recording" and not recording.started_at:
            recording.started_at = timezone.now()
        if status_value in {"stopped", "uploaded", "failed"}:
            recording.ended_at = timezone.now()

        recording.save()
        return Response(SessionRecordingSerializer(recording).data)

    @action(detail=True, methods=["post"], url_path="analyze-transcript")
    def analyze_transcript(self, request, pk=None):
        session = self.get_object()
        require_role(request, {ROLE_MENTOR, ROLE_ADMIN})
        participant_role = resolve_session_participant_role(request, session)
        transcript = str(request.data.get("transcript", "")).strip()
        speaker_role = str(request.data.get("speaker_role", participant_role)).strip().lower() or participant_role
        if speaker_role not in {"mentee", "mentor", "admin", "system", "unknown"}:
            speaker_role = "unknown"

        analysis = classify_abuse(transcript)
        incident = None
        snapshot = {}
        if analysis["flagged"]:
            if speaker_role == "mentee":
                snapshot = mentee_snapshot_for_incident(session)
            incident = SessionAbuseIncident.objects.create(
                session=session,
                speaker_role=speaker_role,
                transcript_snippet=transcript[:5000],
                matched_terms=analysis["matches"],
                severity=analysis["severity"],
                confidence_score=analysis["confidence_score"],
                flagged_mentee_snapshot=snapshot,
                detection_notes=str(request.data.get("notes", "")).strip(),
            )

        role = user_role(request.user)
        return Response(
            {
                "flagged": analysis["flagged"],
                "severity": analysis["severity"],
                "confidence_score": analysis["confidence_score"],
                "matched_terms": analysis["matches"],
                "incident_id": incident.id if incident else None,
                "flagged_mentee_info": snapshot if role in {ROLE_ADMIN, ROLE_MENTOR} else {},
            }
        )

    @action(detail=True, methods=["get"], url_path="abuse-incidents")
    def abuse_incidents(self, request, pk=None):
        session = self.get_object()
        role = user_role(request.user)
        participant_role = resolve_session_participant_role(request, session)
        queryset = SessionAbuseIncident.objects.filter(session=session).order_by("-created_at", "-id")
        serializer = SessionAbuseIncidentSerializer(queryset, many=True)
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
            return Response(SessionFeedbackSerializer(feedback).data)
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

        return Response(SessionDispositionSerializer(disposition).data)

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
            return Response(TrainingModuleSerializer(modules, many=True).data)
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

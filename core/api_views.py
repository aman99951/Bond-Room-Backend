from decimal import Decimal

from django.db import transaction
from django.db.models import Avg, Count, Q, Sum
from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.generics import GenericAPIView
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import (
    DonationTransaction,
    MatchRecommendation,
    Mentor,
    MentorAvailabilitySlot,
    MentorContactVerification,
    MentorIdentityVerification,
    MentorOnboardingStatus,
    MentorProfile,
    MentorTrainingProgress,
    MentorWallet,
    Mentee,
    MenteePreferences,
    MenteeRequest,
    ParentConsentVerification,
    PayoutTransaction,
    Session,
    SessionDisposition,
    SessionFeedback,
    SessionIssueReport,
    TrainingModule,
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
    SessionFeedbackSerializer,
    SessionIssueReportSerializer,
    SessionSerializer,
    TrainingModuleSerializer,
    generate_otp,
    hash_otp,
    otp_expiry,
)
from .zoom import maybe_attach_zoom_links, zoom_is_configured
from .signals import generate_recommendations_for_request


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

        otp = generate_otp()
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
        onboarding, _ = MentorOnboardingStatus.objects.get_or_create(mentor=mentor)
        identity = MentorIdentityVerification.objects.filter(mentor=mentor).first()
        contact = MentorContactVerification.objects.filter(mentor=mentor).first()
        modules = TrainingModule.objects.filter(is_active=True).order_by("order", "id")
        progress = MentorTrainingProgress.objects.filter(
            mentor=mentor, module_id__in=modules.values_list("id", flat=True)
        )
        progress_map = {item.module_id: item for item in progress}
        module_payload = []
        for module in modules:
            item = progress_map.get(module.id)
            module_payload.append(
                {
                    **TrainingModuleSerializer(module).data,
                    "status": item.status if item else "locked",
                    "progress_percent": item.progress_percent if item else 0,
                }
            )
        return Response(
            {
                "status": MentorOnboardingStatusSerializer(onboarding).data,
                "identity_verification": MentorIdentityVerificationSerializer(identity).data if identity else None,
                "contact_verification": MentorContactVerificationSerializer(contact).data if contact else None,
                "training_modules": module_payload,
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
            next_training_status = decision.get("training_status", onboarding.training_status)
            next_final_status = decision.get("final_approval_status", onboarding.final_approval_status)
            next_final_rejection_reason = decision.get(
                "final_rejection_reason",
                onboarding.final_rejection_reason,
            ).strip()
            if next_final_status == "rejected" and not next_final_rejection_reason:
                raise ValidationError(
                    {"final_rejection_reason": "Reject reason is required for final approval rejection."}
                )
            if next_final_status != "rejected":
                next_final_rejection_reason = ""

            next_current_status = MentorOnboardingStatus.derive_current_status(
                application_status=onboarding.application_status,
                identity_status=next_identity_status,
                contact_status=onboarding.contact_status,
                training_status=next_training_status,
                final_approval_status=next_final_status,
            )

            onboarding.identity_status = next_identity_status
            onboarding.training_status = next_training_status
            onboarding.final_approval_status = next_final_status
            onboarding.final_rejection_reason = next_final_rejection_reason
            onboarding.current_status = next_current_status
            onboarding.save(
                update_fields=[
                    "identity_status",
                    "training_status",
                    "final_approval_status",
                    "final_rejection_reason",
                    "current_status",
                    "updated_at",
                ]
            )

        modules = TrainingModule.objects.filter(is_active=True).order_by("order", "id")
        progress = MentorTrainingProgress.objects.filter(
            mentor=mentor, module_id__in=modules.values_list("id", flat=True)
        )
        progress_map = {item.module_id: item for item in progress}
        module_payload = []
        for module in modules:
            item = progress_map.get(module.id)
            module_payload.append(
                {
                    **TrainingModuleSerializer(module).data,
                    "status": item.status if item else "locked",
                    "progress_percent": item.progress_percent if item else 0,
                }
            )

        return Response(
            {
                "status": MentorOnboardingStatusSerializer(onboarding).data,
                "identity_verification": MentorIdentityVerificationSerializer(identity).data if identity else None,
                "training_modules": module_payload,
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
        session = serializer.save()
        meeting_links = maybe_attach_zoom_links(session)
        if meeting_links:
            Session.objects.filter(id=session.id).update(**meeting_links)

    @action(detail=True, methods=["post"], url_path="join-link")
    def join_link(self, request, pk=None):
        session = self.get_object()
        role = user_role(request.user)
        if role == ROLE_MENTOR and session.mentor_id != current_mentor_id(request):
            raise PermissionDenied("You can only access your own sessions.")
        if role == ROLE_MENTEE and session.mentee_id != current_mentee_id(request):
            raise PermissionDenied("You can only access your own sessions.")
        if session.status not in {"approved", "scheduled"}:
            return Response({"detail": "Session not approved yet."}, status=status.HTTP_400_BAD_REQUEST)
        if not zoom_is_configured():
            return Response({"detail": "Zoom credentials are not configured."}, status=status.HTTP_400_BAD_REQUEST)

        meeting_links = maybe_attach_zoom_links(session)
        if meeting_links:
            if meeting_links.get("error"):
                return Response(
                    {"detail": meeting_links.get("error")},
                    status=status.HTTP_502_BAD_GATEWAY,
                )
            Session.objects.filter(id=session.id).update(**meeting_links)
            session.refresh_from_db(fields=["join_url", "host_join_url"])

        if not session.join_url and not session.host_join_url:
            return Response({"detail": "Unable to create Zoom meeting."}, status=status.HTTP_502_BAD_GATEWAY)

        return Response(
            {
                "join_url": session.join_url,
                "host_join_url": session.host_join_url,
            }
        )

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

    def list(self, request, *args, **kwargs):
        mentor_id = request.query_params.get("mentor_id")
        role = user_role(request.user)
        if role == ROLE_MENTOR:
            mentor_id = current_mentor_id(request)
        modules = self.get_queryset()
        if not mentor_id:
            return Response(TrainingModuleSerializer(modules, many=True).data)
        progress_qs = MentorTrainingProgress.objects.filter(
            mentor_id=mentor_id, module_id__in=modules.values_list("id", flat=True)
        )
        progress_map = {item.module_id: item for item in progress_qs}
        payload = []
        for module in modules:
            progress = progress_map.get(module.id)
            module_data = TrainingModuleSerializer(module).data
            module_data.update(
                {
                    "training_status": progress.status if progress else "locked",
                    "progress_percent": progress.progress_percent if progress else 0,
                    "completed_at": progress.completed_at if progress else None,
                }
            )
            payload.append(module_data)
        return Response(payload)


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

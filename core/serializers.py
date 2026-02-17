from datetime import timedelta
from hashlib import sha256
from random import randint

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import serializers

from .models import (
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
    SessionDisposition,
    SessionFeedback,
    SessionIssueReport,
    TrainingModule,
    UserProfile,
)


User = get_user_model()


def generate_otp() -> str:
    return f"{randint(0, 999999):06d}"


def hash_otp(otp: str) -> str:
    return sha256(otp.encode("utf-8")).hexdigest()


def otp_expiry(minutes: int = 5):
    return timezone.now() + timedelta(minutes=minutes)


def ensure_username(base: str) -> str:
    base = (base or "user").strip().lower().replace(" ", "_")
    candidate = base
    index = 1
    while User.objects.filter(username=candidate).exists():
        index += 1
        candidate = f"{base}_{index}"
    return candidate


class MenteeSerializer(serializers.ModelSerializer):
    class Meta:
        model = Mentee
        fields = "__all__"


class MentorSerializer(serializers.ModelSerializer):
    class Meta:
        model = Mentor
        fields = "__all__"

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data["weekly_availability"] = data.get("availability") or []
        data["availability"] = MentorAvailabilitySlotSerializer(
            instance.availability_slots.all().order_by("start_time", "id"),
            many=True,
        ).data
        return data


class MenteePreferencesSerializer(serializers.ModelSerializer):
    class Meta:
        model = MenteePreferences
        fields = "__all__"


class ParentConsentVerificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = ParentConsentVerification
        fields = "__all__"


class MentorAvailabilitySlotSerializer(serializers.ModelSerializer):
    class Meta:
        model = MentorAvailabilitySlot
        fields = "__all__"


class SessionFeedbackSerializer(serializers.ModelSerializer):
    class Meta:
        model = SessionFeedback
        fields = "__all__"

    def validate_rating(self, value):
        if value is not None and (value < 1 or value > 5):
            raise serializers.ValidationError("Rating must be between 1 and 5.")
        return value


class SessionSerializer(serializers.ModelSerializer):
    feedback = SessionFeedbackSerializer(read_only=True)
    mentee_name = serializers.SerializerMethodField()
    mentee_first_name = serializers.CharField(source="mentee.first_name", read_only=True)
    mentee_last_name = serializers.CharField(source="mentee.last_name", read_only=True)

    class Meta:
        model = Session
        fields = "__all__"
        extra_kwargs = {
            "mentee": {"required": False},
        }

    def get_mentee_name(self, obj):
        first_name = (obj.mentee.first_name or "").strip() if obj.mentee_id else ""
        last_name = (obj.mentee.last_name or "").strip() if obj.mentee_id else ""
        full_name = " ".join(part for part in [first_name, last_name] if part).strip()
        if full_name:
            return full_name
        if obj.mentee_id:
            return f"Mentee #{obj.mentee_id}"
        return "Mentee"

    def validate(self, attrs):
        start = attrs.get("scheduled_start")
        end = attrs.get("scheduled_end")
        if start and end and end <= start:
            raise serializers.ValidationError(
                {"scheduled_end": "scheduled_end must be after scheduled_start."}
            )
        return attrs


class MentorIdentityVerificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = MentorIdentityVerification
        fields = "__all__"


class MentorContactVerificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = MentorContactVerification
        fields = "__all__"


class MentorOnboardingStatusSerializer(serializers.ModelSerializer):
    class Meta:
        model = MentorOnboardingStatus
        fields = "__all__"


class TrainingModuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = TrainingModule
        fields = "__all__"


class TrainingVideoWatchSerializer(serializers.Serializer):
    mentor_id = serializers.IntegerField(required=False)
    video_index = serializers.IntegerField(min_value=1, max_value=2)


class MentorTrainingProgressSerializer(serializers.ModelSerializer):
    class Meta:
        model = MentorTrainingProgress
        fields = "__all__"


class MentorTrainingQuizAttemptSerializer(serializers.ModelSerializer):
    class Meta:
        model = MentorTrainingQuizAttempt
        fields = "__all__"


class TrainingQuizStartSerializer(serializers.Serializer):
    mentor_id = serializers.IntegerField(required=False)


class TrainingQuizSubmitSerializer(serializers.Serializer):
    mentor_id = serializers.IntegerField(required=False)
    attempt_id = serializers.IntegerField(required=True)
    selected_answers = serializers.ListField(
        child=serializers.IntegerField(min_value=0, max_value=3),
        allow_empty=False,
        required=True,
    )


class TrainingQuizAbandonSerializer(serializers.Serializer):
    mentor_id = serializers.IntegerField(required=False)
    attempt_id = serializers.IntegerField(required=True)


class MentorProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = MentorProfile
        fields = "__all__"


class SessionDispositionSerializer(serializers.ModelSerializer):
    class Meta:
        model = SessionDisposition
        fields = "__all__"


class MentorWalletSerializer(serializers.ModelSerializer):
    class Meta:
        model = MentorWallet
        fields = "__all__"


class PayoutTransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = PayoutTransaction
        fields = "__all__"


class DonationTransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = DonationTransaction
        fields = "__all__"


class SessionIssueReportSerializer(serializers.ModelSerializer):
    class Meta:
        model = SessionIssueReport
        fields = "__all__"


class MatchRecommendationSerializer(serializers.ModelSerializer):
    mentor = MentorSerializer(read_only=True)

    class Meta:
        model = MatchRecommendation
        fields = "__all__"


class MenteeRequestSerializer(serializers.ModelSerializer):
    recommendations = MatchRecommendationSerializer(many=True, read_only=True)

    class Meta:
        model = MenteeRequest
        fields = "__all__"
        extra_kwargs = {
            "mentee": {"required": False},
        }


class MenteeRegisterSerializer(serializers.Serializer):
    first_name = serializers.CharField(max_length=100)
    last_name = serializers.CharField(max_length=100)
    grade = serializers.ChoiceField(choices=Mentee.GRADE_CHOICES)
    email = serializers.EmailField()
    dob = serializers.DateField()
    gender = serializers.ChoiceField(choices=Mentee.GENDER_CHOICES)
    city_state = serializers.CharField(max_length=150, required=False, allow_blank=True)
    timezone = serializers.CharField(max_length=50, required=False, allow_blank=True)
    parent_guardian_consent = serializers.BooleanField(required=False, default=False)
    parent_mobile = serializers.CharField(max_length=20, required=False, allow_blank=True)
    record_consent = serializers.BooleanField(required=False, default=False)
    password = serializers.CharField(required=False, allow_blank=True, write_only=True)

    def create(self, validated_data):
        password = validated_data.pop("password", "")
        email = validated_data["email"]
        username = ensure_username(email.split("@")[0])
        user, created = User.objects.get_or_create(
            email=email,
            defaults={"username": username},
        )
        if created and password:
            user.set_password(password)
            user.save(update_fields=["password"])
        if not created and password:
            user.set_password(password)
            user.save(update_fields=["password"])
        UserProfile.objects.get_or_create(user=user, defaults={"role": "mentee"})
        mentee, _ = Mentee.objects.update_or_create(email=email, defaults=validated_data)
        return mentee

    def to_representation(self, instance):
        return MenteeSerializer(instance).data


class MentorRegisterSerializer(serializers.Serializer):
    first_name = serializers.CharField(max_length=100)
    last_name = serializers.CharField(max_length=100)
    email = serializers.EmailField()
    mobile = serializers.CharField(max_length=20)
    dob = serializers.DateField()
    gender = serializers.ChoiceField(choices=Mentor.GENDER_CHOICES)
    city_state = serializers.CharField(max_length=150)
    languages = serializers.ListField(child=serializers.CharField(), required=False)
    care_areas = serializers.ListField(child=serializers.CharField(), required=False)
    preferred_formats = serializers.ListField(child=serializers.CharField(), required=False)
    availability = serializers.ListField(child=serializers.JSONField(), required=False)
    timezone = serializers.CharField(max_length=50, required=False, allow_blank=True)
    qualification = serializers.CharField(max_length=150, required=False, allow_blank=True)
    bio = serializers.CharField(required=False, allow_blank=True)
    avatar = serializers.URLField(required=False, allow_blank=True)
    consent = serializers.BooleanField(required=False, default=False)
    password = serializers.CharField(required=False, allow_blank=True, write_only=True)

    def create(self, validated_data):
        password = validated_data.pop("password", "")
        email = validated_data["email"]
        username = ensure_username(email.split("@")[0])
        user, created = User.objects.get_or_create(
            email=email,
            defaults={"username": username},
        )
        if created and password:
            user.set_password(password)
            user.save(update_fields=["password"])
        if not created and password:
            user.set_password(password)
            user.save(update_fields=["password"])
        UserProfile.objects.get_or_create(user=user, defaults={"role": "mentor"})
        mentor, _ = Mentor.objects.update_or_create(email=email, defaults=validated_data)
        return mentor

    def to_representation(self, instance):
        return MentorSerializer(instance).data


class AdminRegisterSerializer(serializers.Serializer):
    first_name = serializers.CharField(max_length=100)
    last_name = serializers.CharField(max_length=100)
    email = serializers.EmailField()
    mobile = serializers.CharField(max_length=20)
    password = serializers.CharField(write_only=True, min_length=6)

    def create(self, validated_data):
        email = validated_data["email"].strip().lower()
        password = validated_data["password"]
        first_name = validated_data["first_name"].strip()
        last_name = validated_data["last_name"].strip()
        username = ensure_username(email.split("@")[0])
        user, created = User.objects.get_or_create(
            email=email,
            defaults={
                "username": username,
                "first_name": first_name,
                "last_name": last_name,
            },
        )

        profile = UserProfile.objects.filter(user=user).first()
        if profile and profile.role != "admin":
            raise serializers.ValidationError(
                {"email": "This email is already registered for a non-admin account."}
            )

        user.first_name = first_name
        user.last_name = last_name
        user.is_active = True
        user.set_password(password)
        user.save(update_fields=["first_name", "last_name", "is_active", "password"])

        UserProfile.objects.update_or_create(user=user, defaults={"role": "admin"})
        return user

    def to_representation(self, instance):
        return {
            "id": instance.id,
            "first_name": instance.first_name,
            "last_name": instance.last_name,
            "email": instance.email,
            "role": "admin",
        }


class AdminOnboardingDecisionSerializer(serializers.Serializer):
    identity_decision = serializers.ChoiceField(
        choices=MentorIdentityVerification.STATUS_CHOICES, required=False
    )
    training_status = serializers.ChoiceField(
        choices=MentorOnboardingStatus.STATUS_CHOICES, required=False
    )
    final_approval_status = serializers.ChoiceField(
        choices=MentorOnboardingStatus.STATUS_CHOICES, required=False
    )
    reviewer_notes = serializers.CharField(required=False, allow_blank=True)
    final_rejection_reason = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError("Provide at least one decision field.")
        if attrs.get("final_approval_status") == "rejected" and not attrs.get(
            "final_rejection_reason", ""
        ).strip():
            raise serializers.ValidationError(
                {"final_rejection_reason": "Reject reason is required for final approval rejection."}
            )
        return attrs


class ParentOtpSendSerializer(serializers.Serializer):
    mentee_id = serializers.IntegerField()
    parent_mobile = serializers.CharField(max_length=20, required=False, allow_blank=True)


class ParentOtpVerifySerializer(serializers.Serializer):
    mentee_id = serializers.IntegerField()
    otp = serializers.CharField(max_length=6)


class MentorContactOtpSendSerializer(serializers.Serializer):
    mentor_id = serializers.IntegerField()
    channel = serializers.ChoiceField(choices=[("email", "email"), ("phone", "phone")])


class MentorContactOtpVerifySerializer(serializers.Serializer):
    mentor_id = serializers.IntegerField()
    channel = serializers.ChoiceField(choices=[("email", "email"), ("phone", "phone")])
    otp = serializers.CharField(max_length=6)


class SessionDispositionActionSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=SessionDisposition.ACTION_CHOICES)
    amount = serializers.DecimalField(max_digits=10, decimal_places=2, required=False)
    note = serializers.CharField(required=False, allow_blank=True)
    issue_category = serializers.ChoiceField(
        choices=SessionIssueReport.CATEGORY_CHOICES,
        required=False,
    )
    issue_description = serializers.CharField(required=False, allow_blank=True)

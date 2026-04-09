from datetime import date, timedelta
from hashlib import sha256
from random import randint
import json
import re

from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import serializers

from .models import (
    DonationTransaction,
    MatchRecommendation,
    AdminAccount,
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
    VolunteerEvent,
    VolunteerEventRegistration,
)


User = get_user_model()


def generate_otp() -> str:
    return f"{randint(0, 999999):06d}"


def hash_otp(otp: str) -> str:
    return sha256(otp.encode("utf-8")).hexdigest()


def otp_expiry(minutes: int = 5):
    return timezone.now() + timedelta(minutes=minutes)


def normalize_mobile(value: str) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def find_existing_mentor_by_mobile(mobile_value: str, *, exclude_id=None):
    normalized = normalize_mobile(mobile_value)
    if not normalized:
        return None
    for mentor in Mentor.objects.all().order_by("-id").only("id", "mobile"):
        if exclude_id and mentor.id == exclude_id:
            continue
        if normalize_mobile(mentor.mobile) == normalized:
            return mentor
    return None


def find_existing_mentee_by_parent_mobile(mobile_value: str, *, exclude_id=None):
    normalized = normalize_mobile(mobile_value)
    if not normalized:
        return None
    for mentee in Mentee.objects.all().order_by("-id").only("id", "parent_mobile"):
        if exclude_id and mentee.id == exclude_id:
            continue
        if normalize_mobile(mentee.parent_mobile) == normalized:
            return mentee
    return None


def find_existing_mentee_by_mobile(mobile_value: str, *, exclude_id=None):
    normalized = normalize_mobile(mobile_value)
    if not normalized:
        return None
    for mentee in Mentee.objects.all().order_by("-id").only("id", "mobile"):
        if exclude_id and mentee.id == exclude_id:
            continue
        if normalize_mobile(getattr(mentee, "mobile", "")) == normalized:
            return mentee
    return None


def sync_mentor_contact_verification(mentor, *, email_verified=False, phone_verified=False):
    verification, _ = MentorContactVerification.objects.get_or_create(mentor=mentor)
    now = timezone.now()
    update_fields = []

    if email_verified and not verification.email_verified:
        verification.email_verified = True
        verification.email_verified_at = now
        update_fields.extend(["email_verified", "email_verified_at"])

    if phone_verified and not verification.phone_verified:
        verification.phone_verified = True
        verification.phone_verified_at = now
        update_fields.extend(["phone_verified", "phone_verified_at"])

    if update_fields:
        verification.save(update_fields=update_fields)

    if phone_verified:
        onboarding, _ = MentorOnboardingStatus.objects.get_or_create(mentor=mentor)
        if onboarding.contact_status != "completed":
            onboarding.contact_status = "completed"
            onboarding.save(update_fields=["contact_status", "updated_at", "current_status"])

    return verification


def _age_in_years(dob: date, today=None) -> int:
    reference = today or timezone.localdate()
    age = reference.year - dob.year
    if (reference.month, reference.day) < (dob.month, dob.day):
        age -= 1
    return age


def _validate_age_range(dob: date, *, min_age: int, max_age: int, role_label: str) -> date:
    age = _age_in_years(dob)
    if age < min_age or age > max_age:
        raise serializers.ValidationError(
            f"{role_label} age must be between {min_age} and {max_age} years."
        )
    return dob


def ensure_username(base: str) -> str:
    base = (base or "user").strip().lower().replace(" ", "_")
    candidate = base
    index = 1
    while User.objects.filter(username=candidate).exists():
        index += 1
        candidate = f"{base}_{index}"
    return candidate


def build_absolute_media_url(raw_url: str, request=None) -> str:
    value = str(raw_url or "").strip()
    if not value:
        return ""
    if value.startswith("http://") or value.startswith("https://"):
        return value
    if request is not None:
        try:
            return request.build_absolute_uri(value)
        except Exception:
            pass
    public_base = str(getattr(settings, "PUBLIC_BASE_URL", "") or "").strip()
    if public_base:
        return f"{public_base.rstrip('/')}/{value.lstrip('/')}"
    return value


IDENTITY_PROOF_NUMBER_RULES = {
    "ration_card": {
        "pattern": re.compile(r"^[A-Z0-9]{6,12}$"),
        "message": "Ration card number must be 6-12 alphanumeric characters without spaces or special characters.",
    },
    "aadhaar": {
        "pattern": re.compile(r"^[0-9]{12}$"),
        "message": "Aadhaar number must be exactly 12 digits.",
    },
    "passport": {
        "pattern": re.compile(r"^[A-Z][0-9]{7}$"),
        "message": "Passport number must follow format: 1 uppercase letter, then 7 digits.",
    },
    "pan_card": {
        "pattern": re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$"),
        "message": "PAN number must follow format ABCDE1234F.",
    },
    "driving_license": {
        "pattern": re.compile(r"^[A-Z0-9]{14,18}$"),
        "message": "Driving license number must be 14-18 alphanumeric characters without spaces or special characters.",
    },
}
IDENTITY_REVIEW_DOCUMENT_KEYS = (
    "id_front",
    "id_back",
    "address_front",
    "address_back",
    "professional_certificate",
)
IDENTITY_REVIEW_DECISIONS = {"pending", "approved", "rejected"}


def normalize_proof_type(value: str) -> str:
    return str(value or "").strip().lower()


def normalize_proof_number(value: str) -> str:
    return str(value or "").strip().upper()


def validate_proof_number(proof_type: str, proof_number: str):
    rule = IDENTITY_PROOF_NUMBER_RULES.get(proof_type)
    if not rule:
        raise serializers.ValidationError("Select a valid proof type.")
    if not rule["pattern"].fullmatch(proof_number):
        raise serializers.ValidationError(rule["message"])


def normalize_document_review_status(value):
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise serializers.ValidationError("document_review_status must be an object.")
    normalized = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key or "").strip().lower()
        if key not in IDENTITY_REVIEW_DOCUMENT_KEYS:
            raise serializers.ValidationError(
                f"Unsupported document key '{raw_key}'."
            )
        decision = str(raw_value or "").strip().lower()
        if decision not in IDENTITY_REVIEW_DECISIONS:
            raise serializers.ValidationError(
                f"Invalid decision '{raw_value}' for {key}."
            )
        normalized[key] = decision
    return normalized


def normalize_document_review_comments(value):
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise serializers.ValidationError("document_review_comments must be an object.")
    normalized = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key or "").strip().lower()
        if key not in IDENTITY_REVIEW_DOCUMENT_KEYS:
            raise serializers.ValidationError(
                f"Unsupported document key '{raw_key}'."
            )
        comment = str(raw_value or "").strip()
        if len(comment) > 500:
            raise serializers.ValidationError(
                f"Comment for {key} must be 500 characters or fewer."
            )
        normalized[key] = comment
    return normalized


class MenteeSerializer(serializers.ModelSerializer):
    class Meta:
        model = Mentee
        fields = "__all__"

    def to_representation(self, instance):
        data = super().to_representation(instance)
        request = self.context.get("request")
        data["avatar"] = build_absolute_media_url(data.get("avatar", ""), request=request)
        return data


class MentorSerializer(serializers.ModelSerializer):
    class Meta:
        model = Mentor
        fields = "__all__"

    def to_representation(self, instance):
        data = super().to_representation(instance)
        request = self.context.get("request")

        profile_photo_url = ""
        profile = getattr(instance, "profile", None)
        if profile and getattr(profile, "profile_photo", None):
            try:
                profile_photo_url = profile.profile_photo.url or ""
            except Exception:
                profile_photo_url = ""

        profile_photo_url = build_absolute_media_url(profile_photo_url, request=request)
        if profile_photo_url:
            data["profile_photo"] = profile_photo_url
            data["avatar"] = profile_photo_url
        else:
            data["profile_photo"] = ""
            data["avatar"] = build_absolute_media_url(data.get("avatar", ""), request=request)

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


class SessionRecordingSerializer(serializers.ModelSerializer):
    class Meta:
        model = SessionRecording
        fields = "__all__"


class SessionMeetingSignalSerializer(serializers.ModelSerializer):
    class Meta:
        model = SessionMeetingSignal
        fields = "__all__"


class SessionAbuseIncidentSerializer(serializers.ModelSerializer):
    class Meta:
        model = SessionAbuseIncident
        fields = "__all__"


class SessionSerializer(serializers.ModelSerializer):
    feedback = SessionFeedbackSerializer(read_only=True)
    mentee_name = serializers.SerializerMethodField()
    mentee_avatar = serializers.SerializerMethodField()
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

    def get_mentee_avatar(self, obj):
        if not obj.mentee_id or not getattr(obj.mentee, "avatar", None):
            return ""
        try:
            avatar_url = obj.mentee.avatar.url or ""
            if not avatar_url:
                return ""
            request = self.context.get("request")
            return build_absolute_media_url(avatar_url, request=request)
        except ValueError:
            return ""

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

    def validate(self, attrs):
        attrs = super().validate(attrs)

        instance = getattr(self, "instance", None)

        id_proof_type = normalize_proof_type(
            attrs.get("id_proof_type", getattr(instance, "id_proof_type", ""))
        )
        id_proof_number = normalize_proof_number(
            attrs.get("id_proof_number", getattr(instance, "id_proof_number", ""))
        )
        id_proof_document = attrs.get("id_proof_document", getattr(instance, "id_proof_document", None))

        address_proof_type = normalize_proof_type(
            attrs.get("address_proof_type", getattr(instance, "address_proof_type", ""))
        )
        address_proof_number = normalize_proof_number(
            attrs.get("address_proof_number", getattr(instance, "address_proof_number", ""))
        )
        address_proof_document = attrs.get(
            "address_proof_document",
            getattr(instance, "address_proof_document", None),
        )
        professional_certificate_document = attrs.get(
            "professional_certificate_document",
            getattr(instance, "professional_certificate_document", None),
        )
        review_status = normalize_document_review_status(
            attrs.get(
                "document_review_status",
                getattr(instance, "document_review_status", {}),
            )
        )
        review_comments = normalize_document_review_comments(
            attrs.get(
                "document_review_comments",
                getattr(instance, "document_review_comments", {}),
            )
        )

        errors = {}
        if instance is None:
            if not id_proof_type:
                errors["id_proof_type"] = "ID proof type is required."
            if not id_proof_number:
                errors["id_proof_number"] = "ID proof number is required."
            if not id_proof_document:
                errors["id_proof_document"] = "ID proof document is required."
            if not address_proof_type:
                errors["address_proof_type"] = "Address proof type is required."
            if not address_proof_number:
                errors["address_proof_number"] = "Address proof number is required."
            if not address_proof_document:
                errors["address_proof_document"] = "Address proof document is required."
            if not professional_certificate_document:
                errors["professional_certificate_document"] = "Professional certificate document is required."

        if id_proof_type and id_proof_number:
            try:
                validate_proof_number(id_proof_type, id_proof_number)
            except serializers.ValidationError as exc:
                errors["id_proof_number"] = exc.detail

        if address_proof_type and address_proof_number:
            try:
                validate_proof_number(address_proof_type, address_proof_number)
            except serializers.ValidationError as exc:
                errors["address_proof_number"] = exc.detail

        if address_proof_type == "pan_card":
            errors["address_proof_type"] = "PAN Card cannot be used as Address Proof."

        if id_proof_type and address_proof_type and id_proof_type == address_proof_type:
            errors["non_field_errors"] = ["ID proof and Address proof must be different document types."]

        if errors:
            raise serializers.ValidationError(errors)

        if id_proof_type:
            attrs["id_proof_type"] = id_proof_type
        if id_proof_number:
            attrs["id_proof_number"] = id_proof_number
        if address_proof_type:
            attrs["address_proof_type"] = address_proof_type
        if address_proof_number:
            attrs["address_proof_number"] = address_proof_number
        attrs["document_review_status"] = review_status
        attrs["document_review_comments"] = review_comments

        return attrs


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

    def to_representation(self, instance):
        data = super().to_representation(instance)
        request = self.context.get("request")
        data["profile_photo"] = build_absolute_media_url(data.get("profile_photo", ""), request=request)
        return data


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


class VolunteerEventSerializer(serializers.ModelSerializer):
    available_roles = serializers.ListField(
        child=serializers.CharField(max_length=120),
        required=False,
        allow_empty=True,
    )
    gallery_images = serializers.ListField(
        child=serializers.URLField(max_length=700),
        required=False,
        allow_empty=True,
    )

    class Meta:
        model = VolunteerEvent
        fields = "__all__"

    def validate_available_roles(self, value):
        if value in (None, ""):
            return []
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise serializers.ValidationError("Invalid roles format.") from exc
        if not isinstance(value, list):
            raise serializers.ValidationError("Roles must be a list.")
        cleaned = []
        seen = set()
        for item in value:
            role = str(item or "").strip()
            if not role or role in seen:
                continue
            seen.add(role)
            cleaned.append(role)
        return cleaned

    def validate_gallery_images(self, value):
        if value in (None, ""):
            return []
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise serializers.ValidationError("Invalid gallery images format.") from exc
        if not isinstance(value, list):
            raise serializers.ValidationError("Gallery images must be a list.")
        cleaned = []
        seen = set()
        for item in value:
            url = str(item or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            cleaned.append(url)
        return cleaned

    def to_representation(self, instance):
        data = super().to_representation(instance)
        request = self.context.get("request")
        uploaded_file_url = data.get("image_file", "")
        if uploaded_file_url:
            data["image"] = build_absolute_media_url(uploaded_file_url, request=request)
        else:
            data["image"] = build_absolute_media_url(data.get("image", ""), request=request)
        return data


class VolunteerEventRegistrationSerializer(serializers.ModelSerializer):
    volunteer_event_title = serializers.CharField(source="volunteer_event.title", read_only=True)
    volunteer_event_date = serializers.DateField(source="volunteer_event.date", read_only=True)
    volunteer_event_time = serializers.CharField(source="volunteer_event.time", read_only=True)

    class Meta:
        model = VolunteerEventRegistration
        fields = "__all__"
        read_only_fields = ("mentee", "submitted_by_role")

    def validate(self, attrs):
        attrs = super().validate(attrs)
        event = attrs.get("volunteer_event")
        preferred_role = str(attrs.get("preferred_role") or "").strip()
        if event and event.status != VolunteerEvent.STATUS_UPCOMING:
            raise serializers.ValidationError({"volunteer_event": "Registration is allowed only for upcoming events."})
        if event and preferred_role:
            available_roles = event.available_roles if isinstance(event.available_roles, list) else []
            if available_roles and preferred_role not in available_roles:
                raise serializers.ValidationError({"preferred_role": "Selected role is not available for this event."})
        if attrs.get("consent") is not True:
            raise serializers.ValidationError({"consent": "Consent is required."})
        return attrs


class MenteeRegisterSerializer(serializers.Serializer):
    first_name = serializers.CharField(max_length=100)
    last_name = serializers.CharField(max_length=100)
    grade = serializers.ChoiceField(choices=Mentee.GRADE_CHOICES)
    email = serializers.EmailField()
    dob = serializers.DateField()
    gender = serializers.ChoiceField(choices=Mentee.GENDER_CHOICES)
    school_or_college = serializers.CharField(max_length=200, required=False, allow_blank=True)
    country = serializers.CharField(max_length=80, required=False, allow_blank=True)
    state = serializers.CharField(max_length=80, required=False, allow_blank=True)
    city = serializers.CharField(max_length=80, required=False, allow_blank=True)
    postal_code = serializers.CharField(max_length=20, required=False, allow_blank=True)
    city_state = serializers.CharField(max_length=150, required=False, allow_blank=True)
    timezone = serializers.CharField(max_length=50, required=False, allow_blank=True)
    mobile = serializers.CharField(max_length=20, required=False, allow_blank=True)
    parent_guardian_consent = serializers.BooleanField(required=False, default=False)
    parent_mobile = serializers.CharField(max_length=20, required=False, allow_blank=True)
    record_consent = serializers.BooleanField(required=False, default=False)
    volunteer_access = serializers.BooleanField(required=False, default=False)
    signup_source = serializers.ChoiceField(choices=Mentee.SIGNUP_SOURCE_CHOICES, required=False, default=Mentee.SIGNUP_SOURCE_REGULAR)
    mentee_program_enabled = serializers.BooleanField(required=False, default=True)
    avatar = serializers.ImageField(required=False, allow_null=True)
    password = serializers.CharField(required=False, allow_blank=True, write_only=True)

    def validate_dob(self, value):
        return _validate_age_range(value, min_age=13, max_age=18, role_label="Student")

    def validate(self, attrs):
        attrs = super().validate(attrs)
        email = str(attrs.get("email", "")).strip().lower()
        mobile = str(attrs.get("mobile", "")).strip()
        parent_mobile = str(attrs.get("parent_mobile", "")).strip()
        attrs["email"] = email
        attrs["mobile"] = mobile
        attrs["parent_mobile"] = parent_mobile

        errors = {}
        if Mentee.objects.filter(email__iexact=email).exists():
            errors["email"] = "This email is already registered."
        elif Mentor.objects.filter(email__iexact=email).exists():
            errors["email"] = "This email is already registered."
        elif User.objects.filter(email__iexact=email).exists():
            errors["email"] = "This email is already registered."

        if mobile:
            mentee_mobile_conflict = find_existing_mentee_by_mobile(mobile)
            if mentee_mobile_conflict or find_existing_mentor_by_mobile(mobile):
                errors["mobile"] = "This mobile number is already registered."

        if not parent_mobile:
            if errors:
                raise serializers.ValidationError(errors)
            return attrs

        mentee_conflict = find_existing_mentee_by_parent_mobile(parent_mobile)
        mentee_mobile_conflict = find_existing_mentee_by_mobile(parent_mobile)
        if mentee_conflict or mentee_mobile_conflict or find_existing_mentor_by_mobile(parent_mobile):
            errors["parent_mobile"] = "This mobile number is already registered."

        if errors:
            raise serializers.ValidationError(errors)

        return attrs

    def create(self, validated_data):
        password = validated_data.pop("password", "")
        email = validated_data["email"]
        username = ensure_username(email.split("@")[0])
        user, created = User.objects.get_or_create(
            email=email,
            defaults={"username": username},
        )
        if password:
            user.set_password(password)
            user.save(update_fields=["password"])
        elif created and user.has_usable_password():
            user.set_unusable_password()
            user.save(update_fields=["password"])
        UserProfile.objects.get_or_create(user=user, defaults={"role": "mentee"})
        mentee, _ = Mentee.objects.update_or_create(email=email, defaults=validated_data)
        return mentee

    def to_representation(self, instance):
        return MenteeSerializer(instance, context=self.context).data


class MentorRegisterSerializer(serializers.Serializer):
    mentor_id = serializers.IntegerField(required=False)
    first_name = serializers.CharField(max_length=100)
    last_name = serializers.CharField(max_length=100)
    email = serializers.EmailField()
    mobile = serializers.CharField(max_length=20)
    country_code = serializers.CharField(max_length=8, required=False, allow_blank=True)
    dob = serializers.DateField()
    gender = serializers.ChoiceField(choices=Mentor.GENDER_CHOICES)
    country = serializers.CharField(max_length=80, required=False, allow_blank=True)
    state = serializers.CharField(max_length=80, required=False, allow_blank=True)
    city = serializers.CharField(max_length=80, required=False, allow_blank=True)
    postal_code = serializers.CharField(max_length=20, required=False, allow_blank=True)
    city_state = serializers.CharField(max_length=150)
    languages = serializers.ListField(child=serializers.CharField(), required=False)
    care_areas = serializers.ListField(child=serializers.CharField(), required=False)
    preferred_formats = serializers.ListField(child=serializers.CharField(), required=False)
    availability = serializers.ListField(child=serializers.JSONField(), required=False)
    timezone = serializers.CharField(max_length=50, required=False, allow_blank=True)
    qualification = serializers.CharField(max_length=150, required=False, allow_blank=True)
    bio = serializers.CharField(required=False, allow_blank=True)
    avatar = serializers.URLField(required=False, allow_blank=True)
    profile_image = serializers.ImageField(required=False, allow_null=True, write_only=True)
    consent = serializers.BooleanField(required=False, default=False)
    email_verified = serializers.BooleanField(required=False, default=False)
    phone_verified = serializers.BooleanField(required=False, default=False)
    password = serializers.CharField(required=False, allow_blank=True, write_only=True)

    def validate_dob(self, value):
        return _validate_age_range(value, min_age=25, max_age=65, role_label="Mentor")

    def validate(self, attrs):
        attrs = super().validate(attrs)
        mentor_id = attrs.get("mentor_id")
        mentor = Mentor.objects.filter(id=mentor_id).first() if mentor_id else None

        if mentor_id and not mentor:
            raise serializers.ValidationError({"mentor_id": "Mentor not found."})

        email = str(attrs.get("email", "")).strip().lower()
        mobile = str(attrs.get("mobile", "")).strip()
        country_code = str(attrs.get("country_code", "")).strip()
        country = str(attrs.get("country", "")).strip()
        state = str(attrs.get("state", "")).strip()
        city = str(attrs.get("city", "")).strip()
        postal_code = str(attrs.get("postal_code", "")).strip()
        city_state = str(attrs.get("city_state", "")).strip()
        attrs["email"] = email
        attrs["mobile"] = mobile
        attrs["country_code"] = country_code
        attrs["country"] = country
        attrs["state"] = state
        attrs["city"] = city
        attrs["postal_code"] = postal_code
        attrs["city_state"] = city_state
        bio = str(attrs.get("bio", "")).strip()
        bio_word_count = len([chunk for chunk in bio.split() if chunk])
        if bio_word_count > 1000:
            raise serializers.ValidationError({"bio": "Brief bio must be 1000 words or fewer."})
        raw_care_areas = attrs.get("care_areas", [])
        if raw_care_areas in (None, ""):
            raw_care_areas = []
        if not isinstance(raw_care_areas, list):
            raise serializers.ValidationError({"care_areas": "Care areas must be a list."})

        cleaned_care_areas = []
        seen_care_areas = set()
        for item in raw_care_areas:
            value = str(item or "").strip()
            if not value:
                continue
            key = value.lower()
            if key in seen_care_areas:
                continue
            seen_care_areas.add(key)
            cleaned_care_areas.append(value)

        if not cleaned_care_areas:
            raise serializers.ValidationError({"care_areas": "Select at least one care area for mentor matching."})
        attrs["care_areas"] = cleaned_care_areas

        errors = {}

        email_conflict = Mentor.objects.filter(email__iexact=email)
        if mentor:
            email_conflict = email_conflict.exclude(id=mentor.id)
        if email_conflict.exists():
            errors["email"] = "This email is already registered."

        mobile_conflict = find_existing_mentor_by_mobile(mobile, exclude_id=mentor.id if mentor else None)
        if mobile_conflict:
            errors["mobile"] = "This mobile number is already registered."

        current_user = User.objects.filter(email__iexact=mentor.email).first() if mentor else None
        existing_user = User.objects.filter(email__iexact=email).first()
        if existing_user and (not current_user or existing_user.id != current_user.id):
            errors["email"] = "This email is already registered."

        if errors:
            raise serializers.ValidationError(errors)

        return attrs

    def create(self, validated_data):
        mentor_id = validated_data.pop("mentor_id", None)
        password = validated_data.pop("password", "")
        email_verified = validated_data.pop("email_verified", False)
        phone_verified = validated_data.pop("phone_verified", False)
        profile_image = validated_data.pop("profile_image", None)
        email = validated_data["email"]
        first_name = validated_data.get("first_name", "").strip()
        last_name = validated_data.get("last_name", "").strip()
        mentor = Mentor.objects.filter(id=mentor_id).first() if mentor_id else None

        def save_profile_image(target_mentor, image_file):
            if not image_file:
                return
            profile, _ = MentorProfile.objects.get_or_create(
                mentor=target_mentor,
                defaults={"public_id": f"BR-{target_mentor.id:04d}"},
            )
            profile.profile_photo = image_file
            profile.save(update_fields=["profile_photo", "updated_at"])

        if mentor:
            user = User.objects.filter(email__iexact=mentor.email).first()
            if not user:
                user = User(
                    username=ensure_username(email.split("@")[0]),
                    email=email,
                )
            user.email = email
            user.first_name = first_name
            user.last_name = last_name
            user.is_active = True
            if password:
                user.set_password(password)
            elif not user.pk:
                user.set_unusable_password()
            user.save()
            UserProfile.objects.update_or_create(user=user, defaults={"role": "mentor"})

            for field, value in validated_data.items():
                setattr(mentor, field, value)
            mentor.save()
            save_profile_image(mentor, profile_image)
            sync_mentor_contact_verification(
                mentor,
                email_verified=email_verified,
                phone_verified=phone_verified,
            )
            return mentor

        user = User(
            username=ensure_username(email.split("@")[0]),
            email=email,
            first_name=first_name,
            last_name=last_name,
            is_active=True,
        )
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save()
        UserProfile.objects.update_or_create(user=user, defaults={"role": "mentor"})
        mentor = Mentor.objects.create(**validated_data)
        save_profile_image(mentor, profile_image)
        sync_mentor_contact_verification(
            mentor,
            email_verified=email_verified,
            phone_verified=phone_verified,
        )
        return mentor

    def to_representation(self, instance):
        return MentorSerializer(instance, context=self.context).data


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
        AdminAccount.objects.update_or_create(
            user=user,
            defaults={"mobile": validated_data.get("mobile", "").strip()},
        )
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
    # Deprecated input kept for backward compatibility with existing admin UI payloads.
    final_approval_status = serializers.ChoiceField(
        choices=MentorOnboardingStatus.STATUS_CHOICES, required=False
    )
    reviewer_notes = serializers.CharField(required=False, allow_blank=True)
    final_rejection_reason = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError("Provide at least one decision field.")
        return attrs


class ParentOtpSendSerializer(serializers.Serializer):
    mentee_id = serializers.IntegerField()
    parent_mobile = serializers.CharField(max_length=20, required=False, allow_blank=True)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        parent_mobile = str(attrs.get("parent_mobile", "")).strip()
        attrs["parent_mobile"] = parent_mobile
        if not parent_mobile:
            return attrs

        mentee_id = attrs.get("mentee_id")
        mentee_conflict = find_existing_mentee_by_parent_mobile(
            parent_mobile,
            exclude_id=mentee_id,
        )
        mentee_mobile_conflict = find_existing_mentee_by_mobile(
            parent_mobile,
            exclude_id=mentee_id,
        )
        if mentee_conflict or mentee_mobile_conflict or find_existing_mentor_by_mobile(parent_mobile):
            raise serializers.ValidationError(
                {"parent_mobile": "This mobile number is already registered."}
            )

        return attrs


class ParentOtpVerifySerializer(serializers.Serializer):
    mentee_id = serializers.IntegerField()
    otp = serializers.CharField(max_length=6)


class MentorContactOtpSendSerializer(serializers.Serializer):
    mentor_id = serializers.IntegerField(required=False)
    channel = serializers.ChoiceField(choices=[("email", "email"), ("phone", "phone")])
    email = serializers.EmailField(required=False, allow_blank=True)
    mobile = serializers.CharField(max_length=20, required=False, allow_blank=True)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        if attrs.get("mentor_id"):
            return attrs

        channel = attrs.get("channel")
        if channel == "email":
            email = str(attrs.get("email", "")).strip().lower()
            if not email:
                raise serializers.ValidationError({"email": "Email is required."})
            attrs["email"] = email
            return attrs

        mobile = str(attrs.get("mobile", "")).strip()
        if not mobile:
            raise serializers.ValidationError({"mobile": "Mobile number is required."})
        attrs["mobile"] = mobile
        return attrs


class MentorContactOtpVerifySerializer(serializers.Serializer):
    mentor_id = serializers.IntegerField(required=False)
    channel = serializers.ChoiceField(choices=[("email", "email"), ("phone", "phone")])
    email = serializers.EmailField(required=False, allow_blank=True)
    mobile = serializers.CharField(max_length=20, required=False, allow_blank=True)
    otp = serializers.CharField(max_length=6)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        if attrs.get("mentor_id"):
            return attrs

        channel = attrs.get("channel")
        if channel == "email":
            email = str(attrs.get("email", "")).strip().lower()
            if not email:
                raise serializers.ValidationError({"email": "Email is required."})
            attrs["email"] = email
            return attrs

        mobile = str(attrs.get("mobile", "")).strip()
        if not mobile:
            raise serializers.ValidationError({"mobile": "Mobile number is required."})
        attrs["mobile"] = mobile
        return attrs


class MobileLoginOtpVerifySerializer(serializers.Serializer):
    mobile = serializers.CharField(max_length=20)
    role = serializers.ChoiceField(
        choices=[("mentee", "mentee"), ("mentor", "mentor")],
        required=False,
        allow_null=True,
    )
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

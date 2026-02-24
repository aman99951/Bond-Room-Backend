import hashlib
import json
import os
import urllib.error
import urllib.request

from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .matching_logic import filter_mentors, score_mentors
from .models import (
    AdminAccount,
    DonationTransaction,
    MatchRecommendation,
    MentorAvailabilitySlot,
    MentorProfile,
    MentorWallet,
    Mentee,
    MenteePreferences,
    MenteeRequest,
    Mentor,
    MentorContactVerification,
    MentorIdentityVerification,
    MentorOnboardingStatus,
    MentorTrainingQuizAttempt,
    MentorTrainingProgress,
    PayoutTransaction,
    ParentConsentVerification,
    Session,
    SessionDisposition,
    SessionFeedback,
    SessionIssueReport,
    SessionAbuseIncident,
    SessionMeetingSignal,
    SessionRecording,
    TrainingModule,
    UserProfile,
)

User = get_user_model()


def ensure_username(base: str) -> str:
    base = (base or "admin").strip().lower().replace(" ", "_")
    candidate = base
    index = 1
    while User.objects.filter(username=candidate).exists():
        index += 1
        candidate = f"{base}_{index}"
    return candidate


class UserProfileInline(admin.StackedInline):
    model = UserProfile
    can_delete = False
    extra = 0
    max_num = 1


class AppUserAdmin(DjangoUserAdmin):
    inlines = (UserProfileInline,)
    list_display = DjangoUserAdmin.list_display + ('profile_role',)
    actions = ('mark_as_admin_role',)

    @admin.display(description='Role')
    def profile_role(self, obj):
        return getattr(getattr(obj, 'userprofile', None), 'role', '-')

    @admin.action(description='Set selected users role as admin')
    def mark_as_admin_role(self, request, queryset):
        updated_count = 0
        for user in queryset:
            UserProfile.objects.update_or_create(
                user=user,
                defaults={'role': 'admin'},
            )
            updated_count += 1
        self.message_user(
            request,
            f'{updated_count} user(s) updated with admin role.',
            level=messages.SUCCESS,
        )


try:
    admin.site.unregister(User)
except admin.sites.NotRegistered:
    pass
admin.site.register(User, AppUserAdmin)


class AdminAccountAdminForm(forms.ModelForm):
    email = forms.EmailField(label="Email")
    first_name = forms.CharField(max_length=150, label="First name")
    last_name = forms.CharField(max_length=150, required=False, label="Last name")
    password = forms.CharField(
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text="Required for new admin accounts. Leave blank to keep existing password.",
    )

    class Meta:
        model = AdminAccount
        fields = ("email", "first_name", "last_name", "mobile", "password")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk and self.instance.user_id:
            self.fields["email"].initial = self.instance.user.email
            self.fields["first_name"].initial = self.instance.user.first_name
            self.fields["last_name"].initial = self.instance.user.last_name

    def clean_email(self):
        email = self.cleaned_data.get("email", "").strip().lower()
        existing = User.objects.filter(email__iexact=email)
        if self.instance and self.instance.pk and self.instance.user_id:
            existing = existing.exclude(pk=self.instance.user_id)
        if existing.exists():
            raise forms.ValidationError("A user with this email already exists.")
        return email

    def clean_password(self):
        password = self.cleaned_data.get("password", "")
        if not self.instance.pk and not password:
            raise forms.ValidationError("Password is required for a new admin account.")
        return password

    def save(self, commit=True):
        instance = super().save(commit=False)
        email = self.cleaned_data["email"]
        first_name = self.cleaned_data["first_name"].strip()
        last_name = self.cleaned_data.get("last_name", "").strip()
        password = self.cleaned_data.get("password", "")

        if instance.pk and instance.user_id:
            user = instance.user
            user.email = email
            user.first_name = first_name
            user.last_name = last_name
            user.is_active = True
        else:
            user = User(
                username=ensure_username(email.split("@")[0]),
                email=email,
                first_name=first_name,
                last_name=last_name,
                is_active=True,
            )

        if password:
            user.set_password(password)
        user.save()

        instance.user = user
        if commit:
            instance.save()
            UserProfile.objects.update_or_create(user=user, defaults={"role": "admin"})
        return instance


@admin.register(AdminAccount)
class AdminAccountAdmin(admin.ModelAdmin):
    form = AdminAccountAdminForm
    list_display = ("id", "email_display", "name_display", "mobile", "created_at")
    search_fields = ("user__email", "user__first_name", "user__last_name", "mobile")
    readonly_fields = ("created_at", "updated_at")
    fields = (
        "email",
        "first_name",
        "last_name",
        "mobile",
        "password",
        "created_at",
        "updated_at",
    )

    @admin.display(description="Email")
    def email_display(self, obj):
        return obj.user.email

    @admin.display(description="Name")
    def name_display(self, obj):
        return f"{obj.user.first_name} {obj.user.last_name}".strip() or "-"


@admin.register(Mentee)
class MenteeAdmin(admin.ModelAdmin):
    list_display = (
        'first_name',
        'last_name',
        'email',
        'grade',
        'gender',
        'parent_guardian_consent',
        'record_consent',
        'created_at',
    )
    list_filter = ('grade', 'gender', 'parent_guardian_consent', 'record_consent')
    search_fields = ('first_name', 'last_name', 'email')


@admin.register(Mentor)
class MentorAdmin(admin.ModelAdmin):
    list_display = (
        'first_name',
        'last_name',
        'email',
        'mobile',
        'gender',
        'city_state',
        'consent',
        'created_at',
    )
    list_filter = ('gender', 'consent')
    search_fields = ('first_name', 'last_name', 'email', 'mobile')


@admin.register(MentorProfile)
class MentorProfileAdmin(admin.ModelAdmin):
    list_display = (
        'mentor',
        'public_id',
        'specialization',
        'years_experience',
        'is_active',
        'sessions_completed',
        'updated_at',
    )
    list_filter = ('is_active',)
    search_fields = ('public_id', 'mentor__first_name', 'mentor__last_name', 'mentor__email')


@admin.register(SessionDisposition)
class SessionDispositionAdmin(admin.ModelAdmin):
    list_display = ('session', 'mentor', 'action', 'status', 'amount', 'decided_at')
    list_filter = ('action', 'status')
    search_fields = ('mentor__first_name', 'mentor__last_name', 'mentor__email')


@admin.register(MentorWallet)
class MentorWalletAdmin(admin.ModelAdmin):
    list_display = (
        'mentor',
        'current_balance',
        'pending_payout',
        'total_claimed',
        'total_donated',
        'updated_at',
    )
    search_fields = ('mentor__first_name', 'mentor__last_name', 'mentor__email')


@admin.register(PayoutTransaction)
class PayoutTransactionAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'mentor',
        'session',
        'transaction_type',
        'status',
        'amount',
        'created_at',
    )
    list_filter = ('transaction_type', 'status')
    search_fields = ('mentor__first_name', 'mentor__last_name', 'mentor__email', 'reference_id')


@admin.register(DonationTransaction)
class DonationTransactionAdmin(admin.ModelAdmin):
    list_display = ('id', 'mentor', 'session', 'amount', 'cause', 'status', 'created_at')
    list_filter = ('status',)
    search_fields = ('mentor__first_name', 'mentor__last_name', 'mentor__email', 'cause')


@admin.register(SessionIssueReport)
class SessionIssueReportAdmin(admin.ModelAdmin):
    list_display = ('session', 'mentor', 'category', 'status', 'created_at', 'resolved_at')
    list_filter = ('category', 'status')
    search_fields = ('mentor__first_name', 'mentor__last_name', 'mentor__email')


@admin.register(SessionRecording)
class SessionRecordingAdmin(admin.ModelAdmin):
    list_display = ('session', 'status', 'recording_url', 'started_at', 'ended_at', 'updated_at')
    list_filter = ('status',)
    search_fields = ('session__id', 'session__mentee__email', 'session__mentor__email')


@admin.register(SessionMeetingSignal)
class SessionMeetingSignalAdmin(admin.ModelAdmin):
    list_display = ('id', 'session', 'sender_role', 'signal_type', 'created_at')
    list_filter = ('sender_role', 'signal_type')
    search_fields = ('session__id',)


@admin.register(SessionAbuseIncident)
class SessionAbuseIncidentAdmin(admin.ModelAdmin):
    list_display = ('session', 'speaker_role', 'severity', 'confidence_score', 'created_at')
    list_filter = ('speaker_role', 'severity')
    search_fields = ('session__id', 'transcript_snippet')


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'role', 'created_at')
    list_filter = ('role',)
    search_fields = ('user__username', 'user__email')
    actions = ['test_openai_connection']

    def test_openai_connection(self, request, queryset):
        api_key = settings.OPENAI_API_KEY
        if not api_key:
            self.message_user(
                request,
                "OPENAI_API_KEY is missing. Add it to .env and restart the server.",
                level=messages.ERROR,
            )
            return

        req = urllib.request.Request(
            "https://api.openai.com/v1/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            model_count = len(payload.get("data", []))
            self.message_user(
                request,
                f"OpenAI OK. {model_count} models returned.",
                level=messages.SUCCESS,
            )
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="ignore")
            self.message_user(
                request,
                f"OpenAI HTTP error {exc.code}: {body}",
                level=messages.ERROR,
            )
        except Exception as exc:  # pragma: no cover - unexpected runtime failures
            self.message_user(
                request,
                f"OpenAI request failed: {exc}",
                level=messages.ERROR,
            )

    test_openai_connection.short_description = "Test OpenAI connection"


@admin.register(MentorIdentityVerification)
class MentorIdentityVerificationAdmin(admin.ModelAdmin):
    list_display = ('mentor', 'status', 'submitted_at', 'reviewed_at')
    list_filter = ('status',)
    search_fields = ('mentor__first_name', 'mentor__last_name', 'mentor__email')


@admin.register(MentorContactVerification)
class MentorContactVerificationAdmin(admin.ModelAdmin):
    list_display = ('mentor', 'email_verified', 'phone_verified', 'updated_at')
    list_filter = ('email_verified', 'phone_verified')
    search_fields = ('mentor__first_name', 'mentor__last_name', 'mentor__email')


@admin.register(MentorOnboardingStatus)
class MentorOnboardingStatusAdmin(admin.ModelAdmin):
    list_display = (
        'mentor',
        'application_status',
        'identity_status',
        'contact_status',
        'training_status',
        'current_status',
        'updated_at',
    )
    list_filter = (
        'application_status',
        'identity_status',
        'contact_status',
        'training_status',
        'current_status',
    )
    search_fields = ('mentor__first_name', 'mentor__last_name', 'mentor__email')


@admin.register(TrainingModule)
class TrainingModuleAdmin(admin.ModelAdmin):
    list_display = ('title', 'order', 'video_url_1', 'video_url_2', 'is_active', 'estimated_minutes')
    list_filter = ('is_active',)
    search_fields = ('title',)
    fields = (
        'title',
        'description',
        'order',
        'lesson_outline',
        'video_url_1',
        'video_url_2',
        'estimated_minutes',
        'is_active',
    )


@admin.register(MentorTrainingProgress)
class MentorTrainingProgressAdmin(admin.ModelAdmin):
    list_display = ('mentor', 'module', 'status', 'progress_percent', 'completed_at')
    list_filter = ('status',)
    search_fields = ('mentor__first_name', 'mentor__last_name', 'mentor__email', 'module__title')


@admin.register(MentorTrainingQuizAttempt)
class MentorTrainingQuizAttemptAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "mentor",
        "status",
        "score",
        "pass_mark",
        "total_questions",
        "started_at",
        "submitted_at",
    )
    list_filter = ("status",)
    search_fields = ("mentor__first_name", "mentor__last_name", "mentor__email")


@admin.register(ParentConsentVerification)
class ParentConsentVerificationAdmin(admin.ModelAdmin):
    list_display = ('mentee', 'parent_mobile', 'status', 'otp_sent_at', 'verified_at')
    list_filter = ('status',)
    search_fields = ('mentee__first_name', 'mentee__last_name', 'mentee__email')


@admin.register(MenteePreferences)
class MenteePreferencesAdmin(admin.ModelAdmin):
    list_display = ('mentee', 'comfort_level', 'preferred_session_minutes', 'updated_at')
    search_fields = ('mentee__first_name', 'mentee__last_name', 'mentee__email')


@admin.register(MentorAvailabilitySlot)
class MentorAvailabilitySlotAdmin(admin.ModelAdmin):
    list_display = ('mentor', 'start_time', 'end_time', 'timezone', 'is_available')
    list_filter = ('is_available', 'timezone')
    search_fields = ('mentor__first_name', 'mentor__last_name', 'mentor__email')


@admin.register(Session)
class SessionAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'mentee',
        'mentor',
        'scheduled_start',
        'scheduled_end',
        'duration_minutes',
        'mode',
        'status',
    )
    list_filter = ('status', 'mode')
    search_fields = ('mentee__first_name', 'mentee__last_name', 'mentor__first_name', 'mentor__last_name')


@admin.register(SessionFeedback)
class SessionFeedbackAdmin(admin.ModelAdmin):
    list_display = ('session', 'rating', 'submitted_at')
    list_filter = ('rating',)


@admin.register(MenteeRequest)
class MenteeRequestAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'mentee',
        'feeling',
        'feeling_cause',
        'support_type',
        'comfort_level',
        'preferred_format',
        'language',
        'timezone',
        'session_mode',
        'allow_auto_match',
        'safety_flag',
        'created_at',
    )
    list_filter = ('preferred_format', 'language', 'timezone')
    search_fields = ('mentee__first_name', 'mentee__last_name', 'mentee__email')
    actions = ['generate_ai_recommendations']

    def generate_ai_recommendations(self, request, queryset):
        api_key = settings.OPENAI_API_KEY
        if not api_key:
            self.message_user(
                request,
                "OPENAI_API_KEY is missing. Add it to .env and restart the server.",
                level=messages.ERROR,
            )
            return

        for req in queryset:
            if not req.allow_auto_match or req.safety_flag:
                self.message_user(
                    request,
                    f"Request {req.id} skipped (auto-match disabled or safety flagged).",
                    level=messages.WARNING,
                )
                continue

            max_mentors = os.environ.get("OPENAI_MAX_MENTORS", "")
            try:
                max_mentors = int(max_mentors) if max_mentors else 0
            except ValueError:
                max_mentors = 0

            mentor_qs = Mentor.objects.all()
            mentors = list(mentor_qs[:max_mentors]) if max_mentors > 0 else list(mentor_qs)
            mentors = filter_mentors(req, mentors)
            if not mentors:
                self.message_user(
                    request,
                    "No mentors available to recommend.",
                    level=messages.ERROR,
                )
                continue

            mentor_payload = [
                {
                    "id": m.id,
                    "name": f"{m.first_name} {m.last_name}".strip(),
                    "care_areas": m.care_areas,
                    "languages": m.languages,
                    "preferred_formats": m.preferred_formats,
                    "availability": m.availability,
                    "timezone": m.timezone,
                    "average_rating": float(m.average_rating) if m.average_rating is not None else None,
                    "response_time_minutes": m.response_time_minutes,
                    "city_state": m.city_state,
                }
                for m in mentors
            ]

            payload = {
                "model": os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
                "text": {"format": {"type": "json_object"}},
                "input": [
                    {
                        "role": "system",
                        "content": (
                            "You are a matching engine. "
                            "Use the rules: filter by availability+timezone, score by topic overlap, "
                            "boost strong ratings and quick responses, prefer local for in-person. "
                            "Return strict JSON only with: "
                            "{'recommendations': [{'mentor_id': int, 'score': number, 'explanation': str}]}"
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "mentee_request": {
                                    "topics": req.topics,
                                    "preferred_times": req.preferred_times,
                                    "preferred_format": req.preferred_format,
                                    "language": req.language,
                                    "timezone": req.timezone,
                                    "access_needs": req.access_needs,
                                    "safety_notes": req.safety_notes,
                                    "session_mode": req.session_mode,
                                    "mentee_city": req.mentee.city_state,
                                },
                                "mentors": mentor_payload,
                            }
                        ),
                    },
                ],
            }

            prompt_hash = hashlib.sha256(
                json.dumps(payload, sort_keys=True).encode("utf-8")
            ).hexdigest()

            req_data = json.dumps(payload).encode("utf-8")
            req_headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            req_obj = urllib.request.Request(
                "https://api.openai.com/v1/responses",
                data=req_data,
                headers=req_headers,
            )
            try:
                with urllib.request.urlopen(req_obj, timeout=20) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                response_id = body.get("id", "")
                output_text = ""
                if "output_text" in body:
                    output_text = body.get("output_text") or ""
                else:
                    for item in body.get("output", []):
                        for content in item.get("content", []):
                            if content.get("type") == "output_text":
                                output_text += content.get("text", "")

                output_text = output_text.strip()
                try:
                    result = json.loads(output_text) if output_text else {}
                except json.JSONDecodeError:
                    self.message_user(
                        request,
                        f"OpenAI returned non-JSON for request {req.id}: {output_text[:400]}",
                        level=messages.ERROR,
                    )
                    continue
                recs = result.get("recommendations", [])
                if not recs:
                    self.message_user(
                        request,
                        f"No recommendations returned for request {req.id}.",
                        level=messages.WARNING,
                    )
                    continue

                MatchRecommendation.objects.filter(
                    mentee_request=req, source="openai"
                ).delete()

                max_recs = os.environ.get("OPENAI_MAX_RECOMMENDATIONS", "")
                try:
                    max_recs = int(max_recs) if max_recs else 3
                except ValueError:
                    max_recs = 3

                for rec in recs[:max_recs]:
                    mentor = Mentor.objects.filter(id=rec.get("mentor_id")).first()
                    if not mentor:
                        continue
                    MatchRecommendation.objects.create(
                        mentee_request=req,
                        mentor=mentor,
                        score=rec.get("score", 0) or 0,
                        explanation=rec.get("explanation", ""),
                        matched_topics=list(set(req.topics).intersection(set(mentor.care_areas))),
                        availability_overlap=req.preferred_times,
                        rating_score=mentor.average_rating or None,
                        response_time_score=(
                            round(50 - (mentor.response_time_minutes or 90) / 3, 2)
                            if mentor.response_time_minutes is not None
                            else None
                        ),
                        status="suggested",
                        source="openai",
                        model=payload["model"],
                        response_id=response_id,
                        prompt_hash=prompt_hash,
                    )

                self.message_user(
                    request,
                    f"AI recommendations created for request {req.id}.",
                    level=messages.SUCCESS,
                )
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="ignore")
                self.message_user(
                    request,
                    f"OpenAI HTTP error {exc.code} for request {req.id}: {body}",
                    level=messages.ERROR,
                )
            except Exception as exc:  # pragma: no cover - unexpected runtime failures
                self.message_user(
                    request,
                    f"OpenAI request failed for request {req.id}: {exc}",
                    level=messages.ERROR,
                )

    generate_ai_recommendations.short_description = "Generate AI recommendations"


@admin.register(MatchRecommendation)
class MatchRecommendationAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'mentee_request',
        'mentor',
        'score',
        'status',
        'source',
        'model',
        'created_at',
    )
    list_filter = ('status', 'source')
    search_fields = ('mentor__first_name', 'mentor__last_name', 'mentor__email')

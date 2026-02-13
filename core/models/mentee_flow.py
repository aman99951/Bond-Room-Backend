from django.db import models

from .mentee import Mentee
from .mentor import Mentor


class ParentConsentVerification(models.Model):
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("verified", "Verified"),
        ("expired", "Expired"),
        ("failed", "Failed"),
    ]

    mentee = models.OneToOneField(
        Mentee, on_delete=models.CASCADE, related_name="parent_consent_verification"
    )
    parent_mobile = models.CharField(max_length=20, blank=True)
    otp_hash = models.CharField(max_length=128, blank=True)
    otp_sent_at = models.DateTimeField(null=True, blank=True)
    otp_expires_at = models.DateTimeField(null=True, blank=True)
    otp_attempts = models.PositiveSmallIntegerField(default=0)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    verified_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True, null=True, blank=True)

    def __str__(self) -> str:
        return f"Parent consent for mentee {self.mentee_id}"


class MenteePreferences(models.Model):
    mentor_type_choices = [
        ("listener", "Listener"),
        ("advisor", "Advisor"),
        ("problem_solver", "Problem-Solver"),
        ("career_guide", "Career Guide"),
        ("friendly", "Friendly"),
    ]

    mentee = models.OneToOneField(
        Mentee, on_delete=models.CASCADE, related_name="preferences"
    )
    comfort_level = models.CharField(max_length=30, blank=True)
    preferred_session_minutes = models.PositiveSmallIntegerField(null=True, blank=True)
    preferred_mentor_types = models.JSONField(default=list, blank=True)
    updated_at = models.DateTimeField(auto_now=True, null=True, blank=True)

    def __str__(self) -> str:
        return f"Preferences for mentee {self.mentee_id}"


class MentorAvailabilitySlot(models.Model):
    mentor = models.ForeignKey(
        Mentor, on_delete=models.CASCADE, related_name="availability_slots"
    )
    start_time = models.DateTimeField()
    end_time = models.DateTimeField()
    timezone = models.CharField(max_length=50, blank=True)
    is_available = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True, null=True, blank=True)

    class Meta:
        ordering = ["start_time", "id"]

    def __str__(self) -> str:
        return f"Slot {self.start_time} - {self.end_time} (mentor {self.mentor_id})"


class Session(models.Model):
    STATUS_CHOICES = [
        ("requested", "Requested"),
        ("approved", "Approved"),
        ("scheduled", "Scheduled"),
        ("completed", "Completed"),
        ("canceled", "Canceled"),
        ("no_show", "No Show"),
    ]
    MODE_CHOICES = [
        ("online", "Online"),
        ("in_person", "In Person"),
    ]

    mentee = models.ForeignKey(
        Mentee, on_delete=models.CASCADE, related_name="sessions"
    )
    mentor = models.ForeignKey(
        Mentor, on_delete=models.CASCADE, related_name="sessions"
    )
    availability_slot = models.ForeignKey(
        MentorAvailabilitySlot,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sessions",
    )
    scheduled_start = models.DateTimeField()
    scheduled_end = models.DateTimeField()
    duration_minutes = models.PositiveSmallIntegerField(default=45)
    timezone = models.CharField(max_length=50, blank=True)
    mode = models.CharField(max_length=20, choices=MODE_CHOICES, default="online")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="requested")
    topic_tags = models.JSONField(default=list, blank=True)
    mentee_notes = models.TextField(blank=True)
    mentor_notes = models.TextField(blank=True)
    join_url = models.URLField(blank=True)
    host_join_url = models.URLField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True, null=True, blank=True)

    class Meta:
        ordering = ["-scheduled_start", "-id"]

    def __str__(self) -> str:
        return f"Session {self.id} ({self.mentee_id} -> {self.mentor_id})"


class SessionFeedback(models.Model):
    session = models.OneToOneField(
        Session, on_delete=models.CASCADE, related_name="feedback"
    )
    rating = models.PositiveSmallIntegerField(null=True, blank=True)
    topics_discussed = models.JSONField(default=list, blank=True)
    comments = models.TextField(blank=True)
    submitted_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True, null=True, blank=True)

    def __str__(self) -> str:
        return f"Feedback for session {self.session_id}"

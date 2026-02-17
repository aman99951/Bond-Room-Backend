from django.db import models

from .mentor import Mentor


class MentorIdentityVerification(models.Model):
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("in_review", "In Review"),
        ("verified", "Verified"),
        ("rejected", "Rejected"),
    ]

    mentor = models.OneToOneField(
        Mentor, on_delete=models.CASCADE, related_name="identity_verification"
    )
    aadhaar_front = models.FileField(
        upload_to="mentor_verification/aadhaar/front/",
        null=True,
        blank=True,
    )
    aadhaar_back = models.FileField(
        upload_to="mentor_verification/aadhaar/back/",
        null=True,
        blank=True,
    )
    passport_or_license = models.FileField(
        upload_to="mentor_verification/id/",
        null=True,
        blank=True,
    )
    additional_notes = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    submitted_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewer_notes = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True, null=True, blank=True)

    def __str__(self) -> str:
        return f"Identity verification for mentor {self.mentor_id}"


class MentorContactVerification(models.Model):
    mentor = models.OneToOneField(
        Mentor, on_delete=models.CASCADE, related_name="contact_verification"
    )
    email_verified = models.BooleanField(default=False)
    email_verified_at = models.DateTimeField(null=True, blank=True)
    email_otp_hash = models.CharField(max_length=128, blank=True)
    email_otp_sent_at = models.DateTimeField(null=True, blank=True)
    email_otp_expires_at = models.DateTimeField(null=True, blank=True)
    email_otp_attempts = models.PositiveSmallIntegerField(default=0)
    phone_verified = models.BooleanField(default=False)
    phone_verified_at = models.DateTimeField(null=True, blank=True)
    phone_otp_hash = models.CharField(max_length=128, blank=True)
    phone_otp_sent_at = models.DateTimeField(null=True, blank=True)
    phone_otp_expires_at = models.DateTimeField(null=True, blank=True)
    phone_otp_attempts = models.PositiveSmallIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True, null=True, blank=True)

    def __str__(self) -> str:
        return f"Contact verification for mentor {self.mentor_id}"


class MentorOnboardingStatus(models.Model):
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("in_review", "In Review"),
        ("completed", "Completed"),
        ("rejected", "Rejected"),
    ]

    mentor = models.OneToOneField(
        Mentor, on_delete=models.CASCADE, related_name="onboarding_status"
    )
    application_status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default="completed"
    )
    identity_status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default="pending"
    )
    contact_status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default="pending"
    )
    training_status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default="pending"
    )
    final_approval_status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default="pending"
    )
    final_rejection_reason = models.TextField(blank=True)
    current_status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default="in_review"
    )
    updated_at = models.DateTimeField(auto_now=True, null=True, blank=True)

    def __str__(self) -> str:
        return f"Onboarding status for mentor {self.mentor_id}"

    @classmethod
    def derive_current_status(
        cls,
        *,
        application_status: str,
        identity_status: str,
        contact_status: str,
        training_status: str,
        final_approval_status: str,
    ) -> str:
        stages = [
            application_status,
            identity_status,
            contact_status,
            training_status,
            final_approval_status,
        ]

        if any(item == "rejected" for item in stages):
            return "rejected"

        # Product rule: once these three stages are complete, onboarding is complete.
        if (
            identity_status == "completed"
            and training_status == "completed"
            and final_approval_status == "completed"
        ):
            return "completed"

        if all(item == "pending" for item in stages):
            return "pending"
        if all(item == "completed" for item in stages):
            return "completed"
        return "in_review"

    def sync_current_status(self) -> str:
        self.current_status = self.derive_current_status(
            application_status=self.application_status,
            identity_status=self.identity_status,
            contact_status=self.contact_status,
            training_status=self.training_status,
            final_approval_status=self.final_approval_status,
        )
        return self.current_status

    def save(self, *args, **kwargs):
        self.sync_current_status()
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and "current_status" not in update_fields:
            kwargs["update_fields"] = [*update_fields, "current_status"]
        return super().save(*args, **kwargs)


class TrainingModule(models.Model):
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    order = models.PositiveSmallIntegerField(default=1)
    lesson_outline = models.JSONField(default=list, blank=True)
    video_url_1 = models.URLField(blank=True, default="")
    video_url_2 = models.URLField(blank=True, default="")
    is_active = models.BooleanField(default=True)
    estimated_minutes = models.PositiveSmallIntegerField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True, null=True, blank=True)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self) -> str:
        return self.title


class MentorTrainingProgress(models.Model):
    STATUS_CHOICES = [
        ("locked", "Locked"),
        ("in_progress", "In Progress"),
        ("completed", "Completed"),
    ]

    mentor = models.ForeignKey(
        Mentor, on_delete=models.CASCADE, related_name="training_progress"
    )
    module = models.ForeignKey(
        TrainingModule, on_delete=models.CASCADE, related_name="mentor_progress"
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="locked")
    progress_percent = models.PositiveSmallIntegerField(default=0)
    completed_at = models.DateTimeField(null=True, blank=True)
    last_activity_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True, null=True, blank=True)

    class Meta:
        unique_together = ("mentor", "module")

    def __str__(self) -> str:
        return f"{self.mentor_id} - {self.module.title}"


class MentorTrainingQuizAttempt(models.Model):
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("passed", "Passed"),
        ("failed", "Failed"),
    ]

    mentor = models.ForeignKey(
        Mentor, on_delete=models.CASCADE, related_name="training_quiz_attempts"
    )
    total_questions = models.PositiveSmallIntegerField(default=15)
    pass_mark = models.PositiveSmallIntegerField(default=7)
    questions = models.JSONField(default=list, blank=True)
    selected_answers = models.JSONField(default=list, blank=True)
    score = models.PositiveSmallIntegerField(default=0)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    started_at = models.DateTimeField(auto_now_add=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True, null=True, blank=True)

    class Meta:
        ordering = ["-started_at", "-id"]

    def __str__(self) -> str:
        return f"Mentor {self.mentor_id} quiz attempt #{self.id} ({self.status})"

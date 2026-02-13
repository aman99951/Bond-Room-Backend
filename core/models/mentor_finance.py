from decimal import Decimal

from django.db import models

from .mentee_flow import Session
from .mentor import Mentor


class MentorProfile(models.Model):
    mentor = models.OneToOneField(
        Mentor, on_delete=models.CASCADE, related_name="profile"
    )
    public_id = models.CharField(max_length=40, unique=True)
    specialization = models.CharField(max_length=150, blank=True)
    years_experience = models.PositiveSmallIntegerField(null=True, blank=True)
    profile_photo = models.FileField(
        upload_to="mentor_profiles/photos/",
        null=True,
        blank=True,
    )
    is_active = models.BooleanField(default=True)
    sessions_completed = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True, null=True, blank=True)

    def __str__(self) -> str:
        return f"Profile for mentor {self.mentor_id}"


class SessionDisposition(models.Model):
    ACTION_CHOICES = [
        ("claim", "Claim Payment"),
        ("donate", "Donate Session"),
        ("report", "Report Issue"),
    ]
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("confirmed", "Confirmed"),
        ("rejected", "Rejected"),
    ]

    session = models.OneToOneField(
        Session, on_delete=models.CASCADE, related_name="disposition"
    )
    mentor = models.ForeignKey(
        Mentor, on_delete=models.CASCADE, related_name="session_dispositions"
    )
    action = models.CharField(max_length=20, choices=ACTION_CHOICES)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    amount = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal("0.00")
    )
    note = models.TextField(blank=True)
    decided_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True, null=True, blank=True)

    def __str__(self) -> str:
        return f"Disposition for session {self.session_id}"


class MentorWallet(models.Model):
    mentor = models.OneToOneField(
        Mentor, on_delete=models.CASCADE, related_name="wallet"
    )
    current_balance = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal("0.00")
    )
    pending_payout = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal("0.00")
    )
    total_claimed = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal("0.00")
    )
    total_donated = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal("0.00")
    )
    updated_at = models.DateTimeField(auto_now=True, null=True, blank=True)

    def __str__(self) -> str:
        return f"Wallet for mentor {self.mentor_id}"


class PayoutTransaction(models.Model):
    TYPE_CHOICES = [
        ("session_claim", "Session Claim"),
        ("bank_payout", "Bank Payout"),
        ("adjustment", "Adjustment"),
    ]
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("processing", "Processing"),
        ("paid", "Paid"),
        ("failed", "Failed"),
    ]

    mentor = models.ForeignKey(
        Mentor, on_delete=models.CASCADE, related_name="payout_transactions"
    )
    session = models.ForeignKey(
        Session,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payout_transactions",
    )
    transaction_type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    reference_id = models.CharField(max_length=100, blank=True)
    note = models.TextField(blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True, null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"Payout tx {self.id} for mentor {self.mentor_id}"


class DonationTransaction(models.Model):
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("completed", "Completed"),
        ("failed", "Failed"),
    ]

    mentor = models.ForeignKey(
        Mentor, on_delete=models.CASCADE, related_name="donation_transactions"
    )
    session = models.OneToOneField(
        Session,
        on_delete=models.CASCADE,
        related_name="donation_transaction",
    )
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    cause = models.CharField(max_length=150, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="completed")
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True, null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"Donation tx {self.id} for mentor {self.mentor_id}"


class SessionIssueReport(models.Model):
    CATEGORY_CHOICES = [
        ("technical_issue", "Technical Issue"),
        ("mentee_no_show", "Mentee No-show"),
        ("safety_concern", "Safety Concern"),
        ("other", "Other"),
    ]
    STATUS_CHOICES = [
        ("open", "Open"),
        ("in_review", "In Review"),
        ("resolved", "Resolved"),
        ("dismissed", "Dismissed"),
    ]

    session = models.OneToOneField(
        Session, on_delete=models.CASCADE, related_name="issue_report"
    )
    mentor = models.ForeignKey(
        Mentor, on_delete=models.CASCADE, related_name="issue_reports"
    )
    category = models.CharField(max_length=30, choices=CATEGORY_CHOICES, default="other")
    description = models.TextField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="open")
    resolution_notes = models.TextField(blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True, null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"Issue report for session {self.session_id}"

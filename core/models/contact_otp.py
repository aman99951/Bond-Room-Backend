from django.db import models


class ContactOtpRequest(models.Model):
    """
    DB-backed OTP request state for contact verification flows that do not yet have
    a Mentor record (e.g. signup). This avoids relying on per-process cache, which
    breaks in serverless / multi-worker production deployments.
    """

    CHANNEL_CHOICES = [
        ("email", "Email"),
        ("phone", "Phone"),
    ]

    channel = models.CharField(max_length=16, choices=CHANNEL_CHOICES)
    # Normalized: email lowercased/trimmed; phone digits only.
    normalized_contact = models.CharField(max_length=320, db_index=True)
    otp_hash = models.CharField(max_length=128)
    expires_at = models.DateTimeField()
    attempts = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["channel", "normalized_contact"],
                name="uniq_contact_otp_request_channel_contact",
            )
        ]

    def __str__(self) -> str:
        return f"ContactOtpRequest(channel={self.channel}, contact={self.normalized_contact})"


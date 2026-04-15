from django.db import models

from .mentee import Mentee


class VolunteerEvent(models.Model):
    STATUS_UPCOMING = "upcoming"
    STATUS_COMPLETED = "completed"
    STATUS_CHOICES = [
        (STATUS_UPCOMING, "Upcoming"),
        (STATUS_COMPLETED, "Completed"),
    ]

    title = models.CharField(max_length=220)
    stream = models.CharField(max_length=120, blank=True)
    image = models.URLField(max_length=700, blank=True)
    image_file = models.FileField(upload_to="volunteer/events/", null=True, blank=True)
    description = models.TextField(blank=True)
    summary = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_UPCOMING)
    date = models.DateField(null=True, blank=True)
    time = models.CharField(max_length=120, blank=True)
    completed_on = models.DateField(null=True, blank=True)
    location = models.CharField(max_length=220, blank=True)
    organizer = models.CharField(max_length=220, blank=True)
    seats = models.PositiveIntegerField(default=0)
    budget_spent = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    impact = models.CharField(max_length=220, blank=True)
    joined_count = models.PositiveIntegerField(default=0)
    completion_brief = models.TextField(blank=True)
    gallery_images = models.JSONField(default=list, blank=True)
    available_roles = models.JSONField(default=list, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True, null=True, blank=True)

    class Meta:
        ordering = ["status", "date", "-completed_on", "id"]

    def __str__(self) -> str:
        return self.title


class VolunteerEventRegistration(models.Model):
    ROLE_CHOICES = [
        ("mentee", "Mentee"),
        ("mentor", "Mentor"),
        ("admin", "Admin"),
        ("guest", "Guest"),
    ]

    volunteer_event = models.ForeignKey(
        VolunteerEvent,
        on_delete=models.CASCADE,
        related_name="registrations",
    )
    mentee = models.ForeignKey(
        Mentee,
        on_delete=models.CASCADE,
        related_name="volunteer_registrations",
        null=True,
        blank=True,
    )
    submitted_by_role = models.CharField(max_length=20, choices=ROLE_CHOICES, default="mentee")
    full_name = models.CharField(max_length=120)
    email = models.EmailField()
    phone = models.CharField(max_length=20)
    team_name = models.CharField(max_length=150, blank=True)
    school_or_college = models.CharField(max_length=200)
    country = models.CharField(max_length=80)
    state = models.CharField(max_length=80)
    city = models.CharField(max_length=80)
    postal_code = models.CharField(max_length=20)
    preferred_role = models.CharField(max_length=120, blank=True)
    emergency_contact = models.CharField(max_length=20)
    notes = models.TextField(blank=True)
    consent = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True, null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["volunteer_event", "mentee"],
                name="uniq_volunteer_event_registration_per_mentee",
            )
        ]

    def __str__(self) -> str:
        return f"Registration #{self.id} for event {self.volunteer_event_id}"

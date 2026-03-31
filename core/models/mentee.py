from django.db import models


class Mentee(models.Model):
    SIGNUP_SOURCE_REGULAR = "regular"
    SIGNUP_SOURCE_EVENT_FLOW = "event_flow"
    SIGNUP_SOURCE_CHOICES = [
        (SIGNUP_SOURCE_REGULAR, "Regular"),
        (SIGNUP_SOURCE_EVENT_FLOW, "Event Flow"),
    ]

    GRADE_CHOICES = [
        ('10th Grade', '10th Grade'),
        ('11th Grade', '11th Grade'),
        ('12th Grade', '12th Grade'),
    ]
    GENDER_CHOICES = [
        ('Female', 'Female'),
        ('Male', 'Male'),
    ]

    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    grade = models.CharField(max_length=20, choices=GRADE_CHOICES)
    email = models.EmailField(unique=True)
    dob = models.DateField()
    gender = models.CharField(max_length=32, choices=GENDER_CHOICES)
    school_or_college = models.CharField(max_length=200, blank=True)
    country = models.CharField(max_length=80, blank=True)
    state = models.CharField(max_length=80, blank=True)
    city = models.CharField(max_length=80, blank=True)
    postal_code = models.CharField(max_length=20, blank=True)
    city_state = models.CharField(max_length=150, blank=True)
    timezone = models.CharField(max_length=50, blank=True)
    mobile = models.CharField(max_length=20, blank=True)
    parent_guardian_consent = models.BooleanField(default=False)
    parent_mobile = models.CharField(max_length=20, blank=True)
    record_consent = models.BooleanField(default=False)
    volunteer_access = models.BooleanField(default=False)
    signup_source = models.CharField(max_length=20, choices=SIGNUP_SOURCE_CHOICES, default=SIGNUP_SOURCE_REGULAR)
    mentee_program_enabled = models.BooleanField(default=True)
    avatar = models.FileField(upload_to='mentee/avatar/', null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True, null=True, blank=True)

    def __str__(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

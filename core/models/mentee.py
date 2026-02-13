from django.db import models


class Mentee(models.Model):
    GRADE_CHOICES = [
        ('9th Grade', '9th Grade'),
        ('10th Grade', '10th Grade'),
        ('11th Grade', '11th Grade'),
        ('12th Grade', '12th Grade'),
    ]
    GENDER_CHOICES = [
        ('Female', 'Female'),
        ('Male', 'Male'),
        ('Non-binary', 'Non-binary'),
        ('Prefer not to say', 'Prefer not to say'),
    ]

    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    grade = models.CharField(max_length=20, choices=GRADE_CHOICES)
    email = models.EmailField(unique=True)
    dob = models.DateField()
    gender = models.CharField(max_length=32, choices=GENDER_CHOICES)
    city_state = models.CharField(max_length=150, blank=True)
    timezone = models.CharField(max_length=50, blank=True)
    parent_guardian_consent = models.BooleanField(default=False)
    parent_mobile = models.CharField(max_length=20, blank=True)
    record_consent = models.BooleanField(default=False)
    avatar = models.FileField(upload_to='mentee/avatar/', null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True, null=True, blank=True)

    def __str__(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

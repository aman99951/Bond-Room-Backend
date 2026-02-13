from django.db import models


class Mentor(models.Model):
    GENDER_CHOICES = [
        ('Female', 'Female'),
        ('Male', 'Male'),
        ('Non-binary', 'Non-binary'),
        ('Prefer not to say', 'Prefer not to say'),
    ]
    LANGUAGE_CHOICES = [
        ('Tamil', 'Tamil'),
        ('English', 'English'),
        ('Telugu', 'Telugu'),
        ('Kannada', 'Kannada'),
    ]
    CARE_AREA_CHOICES = [
        ('Anxiety', 'Anxiety'),
        ('Relationships', 'Relationships'),
        ('Academic Stress', 'Academic Stress'),
    ]
    FORMAT_CHOICES = [
        ('1:1', '1:1'),
        ('Group', 'Group'),
        ('Drop-in', 'Drop-in'),
        ('Workshop', 'Workshop'),
    ]

    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    email = models.EmailField(unique=True)
    mobile = models.CharField(max_length=20)
    dob = models.DateField()
    gender = models.CharField(max_length=32, choices=GENDER_CHOICES)
    city_state = models.CharField(max_length=150)
    languages = models.JSONField(default=list, blank=True)
    care_areas = models.JSONField(default=list, blank=True)
    preferred_formats = models.JSONField(default=list, blank=True)
    availability = models.JSONField(default=list, blank=True)
    timezone = models.CharField(max_length=50, blank=True)
    qualification = models.CharField(max_length=150, blank=True)
    bio = models.TextField(blank=True)
    avatar = models.URLField(blank=True)
    average_rating = models.DecimalField(max_digits=4, decimal_places=2, null=True, blank=True)
    response_time_minutes = models.IntegerField(null=True, blank=True)
    consent = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True, null=True, blank=True)

    def __str__(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

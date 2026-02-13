from django.db import models

from .mentee import Mentee
from .mentor import Mentor


class MenteeRequest(models.Model):
    TOPIC_CHOICES = [
        ('Anxiety', 'Anxiety'),
        ('Study Skills', 'Study Skills'),
        ('Math', 'Math'),
        ('Career Chat', 'Career Chat'),
        ('Academic Stress', 'Academic Stress'),
    ]
    FORMAT_CHOICES = [
        ('1:1', '1:1'),
        ('Group', 'Group'),
        ('Drop-in', 'Drop-in'),
        ('Workshop', 'Workshop'),
    ]
    FEELING_CHOICES = [
        ('Burnt Out', 'Burnt Out'),
        ('Anxious', 'Anxious'),
        ('Confused', 'Confused'),
        ('Lonely', 'Lonely'),
        ('Hopeful', 'Hopeful'),
        ('Other', 'Other'),
    ]
    CAUSE_CHOICES = [
        ('Exam Pressure', 'Exam Pressure'),
        ('Parent Expectations', 'Parent Expectations'),
        ('Friend Issues', 'Friend Issues'),
        ('Future Anxiety (Career/College)', 'Future Anxiety (Career/College)'),
        ('Concentration Struggles', 'Concentration Struggles'),
        ('Study Struggles', 'Study Struggles'),
        ('Others', 'Others'),
    ]
    SUPPORT_CHOICES = [
        ('Someone to Listen', 'Someone to Listen'),
        ('Study Guidance / Tips', 'Study Guidance / Tips'),
        ('Motivation', 'Motivation'),
        ('Stress Relief Strategies', 'Stress Relief Strategies'),
        ('Life Advice / Perspective', 'Life Advice / Perspective'),
        ("I'm Not Sure", "I'm Not Sure"),
    ]
    COMFORT_CHOICES = [
        ('Very Uncomfortable', 'Very Uncomfortable'),
        ('Somewhat Uncomfortable', 'Somewhat Uncomfortable'),
        ('Neutral', 'Neutral'),
        ('Comfortable', 'Comfortable'),
        ('Very Comfortable', 'Very Comfortable'),
    ]
    SESSION_MODE_CHOICES = [
        ('online', 'Online'),
        ('in_person', 'In Person'),
    ]

    mentee = models.ForeignKey(Mentee, on_delete=models.CASCADE, related_name='requests')
    feeling = models.CharField(max_length=50, choices=FEELING_CHOICES, blank=True)
    feeling_cause = models.CharField(max_length=80, choices=CAUSE_CHOICES, blank=True)
    support_type = models.CharField(max_length=80, choices=SUPPORT_CHOICES, blank=True)
    comfort_level = models.CharField(max_length=30, choices=COMFORT_CHOICES, blank=True)
    topics = models.JSONField(default=list, blank=True)
    free_text = models.TextField(blank=True)
    preferred_times = models.JSONField(default=list, blank=True)
    preferred_format = models.CharField(max_length=20, choices=FORMAT_CHOICES, blank=True)
    language = models.CharField(max_length=50, blank=True)
    timezone = models.CharField(max_length=50, blank=True)
    access_needs = models.TextField(blank=True)
    safety_notes = models.TextField(blank=True)
    session_mode = models.CharField(max_length=20, choices=SESSION_MODE_CHOICES, default='online')
    allow_auto_match = models.BooleanField(default=True)
    safety_flag = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True, null=True, blank=True)

    def __str__(self) -> str:
        return f"Request #{self.id} for {self.mentee_id}"


class MatchRecommendation(models.Model):
    STATUS_CHOICES = [
        ('suggested', 'Suggested'),
        ('accepted', 'Accepted'),
        ('rejected', 'Rejected'),
        ('expired', 'Expired'),
    ]
    SOURCE_CHOICES = [
        ('seed', 'Seed'),
        ('rules', 'Rules'),
        ('openai', 'OpenAI'),
        ('manual', 'Manual'),
    ]

    mentee_request = models.ForeignKey(
        MenteeRequest, on_delete=models.CASCADE, related_name='recommendations'
    )
    mentor = models.ForeignKey(Mentor, on_delete=models.CASCADE, related_name='recommendations')
    score = models.DecimalField(max_digits=5, decimal_places=2)
    explanation = models.TextField(blank=True)
    matched_topics = models.JSONField(default=list, blank=True)
    availability_overlap = models.JSONField(default=list, blank=True)
    response_time_score = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    rating_score = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='suggested')
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default='seed')
    model = models.CharField(max_length=100, blank=True)
    response_id = models.CharField(max_length=100, blank=True)
    prompt_hash = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True, null=True, blank=True)

    def __str__(self) -> str:
        return f"Rec #{self.id} (req {self.mentee_request_id} → mentor {self.mentor_id})"

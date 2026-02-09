import random
from datetime import date, timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import MatchRecommendation, Mentee, MenteeRequest, Mentor, UserProfile
from django.contrib.auth import get_user_model


class Command(BaseCommand):
    help = "Seed sample mentees, mentors, requests, and match recommendations."

    def add_arguments(self, parser):
        parser.add_argument(
            "--count",
            type=int,
            default=10,
            help="Number of mentees and mentors to create (default: 10).",
        )

    def handle(self, *args, **options):
        count = options["count"]
        User = get_user_model()
        random.seed(42)

        test_domain = "bondroom.local"
        UserProfile.objects.filter(user__email__endswith=f"@{test_domain}").delete()
        MatchRecommendation.objects.filter(
            mentee_request__mentee__email__endswith=f"@{test_domain}"
        ).delete()
        MenteeRequest.objects.filter(mentee__email__endswith=f"@{test_domain}").delete()
        Mentee.objects.filter(email__endswith=f"@{test_domain}").delete()
        Mentor.objects.filter(email__endswith=f"@{test_domain}").delete()
        User.objects.filter(email__endswith=f"@{test_domain}").delete()

        first_names = ["Priya", "Rahul", "Ananya", "Karthik", "Meera", "Arjun", "Nila", "Vikram"]
        last_names = ["Sharma", "Iyer", "Patel", "Rao", "Menon", "Gupta", "Nair", "Singh"]
        grades = ["9th Grade", "10th Grade", "11th Grade", "12th Grade"]
        genders = ["Female", "Male", "Non-binary", "Prefer not to say"]
        languages = ["Tamil", "English", "Telugu", "Kannada"]
        care_areas = ["Anxiety", "Relationships", "Academic Stress"]
        topics = ["Anxiety", "Study Skills", "Math", "Career Chat", "Academic Stress"]
        feelings = ["Burnt Out", "Anxious", "Confused", "Lonely", "Hopeful", "Other"]
        causes = [
            "Exam Pressure",
            "Parent Expectations",
            "Friend Issues",
            "Future Anxiety (Career/College)",
            "Concentration Struggles",
            "Study Struggles",
            "Others",
        ]
        supports = [
            "Someone to Listen",
            "Study Guidance / Tips",
            "Motivation",
            "Stress Relief Strategies",
            "Life Advice / Perspective",
            "I'm Not Sure",
        ]
        comforts = [
            "Very Uncomfortable",
            "Somewhat Uncomfortable",
            "Neutral",
            "Comfortable",
            "Very Comfortable",
        ]
        formats = ["1:1", "Group", "Drop-in", "Workshop"]
        cities = [
            "Chennai, Tamil Nadu",
            "Bengaluru, Karnataka",
            "Hyderabad, Telangana",
            "Mumbai, Maharashtra",
            "Delhi, India",
        ]
        timezones = ["Asia/Kolkata", "Asia/Dubai", "Asia/Singapore"]
        access_needs = [
            "",
            "Needs larger text and clear audio.",
            "Prefers short sessions with breaks.",
        ]
        safety_notes = [
            "",
            "Student gets anxious before exams; gentle pacing preferred.",
            "Parent wants weekly progress updates.",
        ]

        availability_pool = [
            {"day": "Monday", "start": "17:00", "end": "19:00"},
            {"day": "Wednesday", "start": "16:00", "end": "18:00"},
            {"day": "Friday", "start": "18:00", "end": "20:00"},
            {"day": "Saturday", "start": "10:00", "end": "12:00"},
            {"day": "Sunday", "start": "15:00", "end": "17:00"},
        ]

        mentees = []
        mentors = []

        for i in range(count):
            fn = random.choice(first_names)
            ln = random.choice(last_names)
            email = f"mentee{i+1}@bondroom.local"
            user = User.objects.create_user(username=email, email=email, password="password123")
            UserProfile.objects.create(user=user, role="mentee")

            city = random.choice(cities)
            tz = random.choice(timezones)
            mentee = Mentee.objects.create(
                first_name=fn,
                last_name=ln,
                grade=random.choice(grades),
                email=email,
                dob=date.today() - timedelta(days=365 * random.randint(13, 17)),
                gender=random.choice(genders),
                city_state=city,
                timezone=tz,
                parent_guardian_consent=True,
                parent_mobile=f"98{random.randint(10000000, 99999999)}",
                record_consent=True,
            )
            mentees.append(mentee)

        for i in range(count):
            fn = random.choice(first_names)
            ln = random.choice(last_names)
            email = f"mentor{i+1}@bondroom.local"
            user = User.objects.create_user(username=email, email=email, password="password123")
            UserProfile.objects.create(user=user, role="mentor")

            mentor_city = random.choice(cities)
            mentor = Mentor.objects.create(
                first_name=fn,
                last_name=ln,
                email=email,
                mobile="+91 90000 00000",
                dob=date.today() - timedelta(days=365 * random.randint(60, 75)),
                gender=random.choice(genders),
                city_state=mentor_city,
                languages=random.sample(languages, k=random.randint(1, 2)),
                care_areas=random.sample(care_areas, k=random.randint(1, 2)),
                preferred_formats=random.sample(formats, k=random.randint(1, 2)),
                availability=random.sample(availability_pool, k=2),
                timezone=random.choice(timezones),
                qualification="Retired Teacher",
                bio="I love helping students build confidence and study habits.",
                average_rating=round(random.uniform(4.0, 5.0), 2),
                response_time_minutes=random.randint(30, 180),
                consent=True,
            )
            mentors.append(mentor)

        def overlap_slots(a, b):
            a_set = {(s["day"], s["start"], s["end"]) for s in a}
            b_set = {(s["day"], s["start"], s["end"]) for s in b}
            return list(a_set.intersection(b_set))

        for mentee in mentees:
            preferred_times = random.sample(availability_pool, k=2)
            req = MenteeRequest.objects.create(
                mentee=mentee,
                feeling=random.choice(feelings),
                feeling_cause=random.choice(causes),
                support_type=random.choice(supports),
                comfort_level=random.choice(comforts),
                topics=random.sample(topics, k=random.randint(1, 3)),
                free_text="Looking for guidance and support.",
                preferred_times=preferred_times,
                preferred_format=random.choice(formats),
                language=random.choice(languages),
                timezone=mentee.timezone or random.choice(timezones),
                access_needs=random.choice(access_needs),
                safety_notes=random.choice(safety_notes),
                session_mode=random.choice(["online", "in_person"]),
                allow_auto_match=True,
                safety_flag=random.choice([False, False, False, True]),
            )

            ranked = random.sample(mentors, k=min(3, len(mentors)))
            for idx, mentor in enumerate(ranked, start=1):
                matched_topics = list(set(req.topics).intersection(set(mentor.care_areas)))
                availability_overlap = overlap_slots(req.preferred_times, mentor.availability)
                topic_score = min(len(matched_topics) * 15, 30)
                rating_score = float(mentor.average_rating or 4.5) * 10
                response_score = max(0, 50 - (mentor.response_time_minutes or 90) / 3)
                overlap_score = 10 if availability_overlap else 0
                raw_score = 40 + topic_score + overlap_score + rating_score * 0.3 + response_score * 0.2
                MatchRecommendation.objects.create(
                    mentee_request=req,
                    mentor=mentor,
                    score=round(raw_score - idx * 3, 2),
                    explanation=(
                        "Good fit: topic overlap and availability match."
                        if matched_topics and availability_overlap
                        else "Potential fit based on mentor strengths."
                    ),
                    matched_topics=matched_topics,
                    availability_overlap=availability_overlap,
                    response_time_score=round(response_score / 10, 2),
                    rating_score=mentor.average_rating or 4.5,
                    status="suggested",
                    source="seed",
                    created_at=timezone.now(),
                )

            # Add a sample "openai" recommendation so the admin shows AI data without calling the API.
            if ranked:
                mentor = ranked[0]
                matched_topics = list(set(req.topics).intersection(set(mentor.care_areas)))
                availability_overlap = overlap_slots(req.preferred_times, mentor.availability)
                MatchRecommendation.objects.create(
                    mentee_request=req,
                    mentor=mentor,
                    score=round(raw_score + 1, 2),
                    explanation="AI match: strong topic alignment and time overlap.",
                    matched_topics=matched_topics,
                    availability_overlap=availability_overlap,
                    response_time_score=round(response_score / 10, 2),
                    rating_score=mentor.average_rating or 4.5,
                    status="suggested",
                    source="openai",
                    model="gpt-4o-mini",
                    response_id=f"seeded-{req.id}",
                    prompt_hash="seeded",
                    created_at=timezone.now(),
                )

        self.stdout.write(self.style.SUCCESS("Seed data created successfully."))

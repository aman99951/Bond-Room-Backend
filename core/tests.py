from datetime import date

from django.test import TestCase

from core.models import Mentor, MentorOnboardingStatus


class MentorOnboardingStatusTests(TestCase):
    def test_current_status_auto_completes_when_key_three_steps_complete(self):
        mentor = Mentor.objects.create(
            first_name="Test",
            last_name="Mentor",
            email="mentor-status@test.com",
            mobile="+10000000000",
            dob=date(1990, 1, 1),
            gender="Male",
            city_state="Chennai",
        )
        onboarding = MentorOnboardingStatus.objects.create(
            mentor=mentor,
            application_status="completed",
            identity_status="completed",
            contact_status="pending",
            training_status="completed",
            final_approval_status="completed",
            current_status="in_review",
        )

        onboarding.refresh_from_db()
        self.assertEqual(onboarding.current_status, "completed")

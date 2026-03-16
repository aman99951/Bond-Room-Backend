from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from core.abuse_monitoring import classify_behavior_signal, detect_abusive_terms
from core.models import (
    AdminAccount,
    MatchRecommendation,
    MenteeRequest,
    Mentor,
    MentorWallet,
    MentorOnboardingStatus,
    Mentee,
    PayoutTransaction,
    SessionAbuseIncident,
    Session,
    SessionFeedback,
    SessionIssueReport,
    TrainingModule,
    UserProfile,
)
from core.quiz import generate_training_quiz_questions
from core.signals import generate_recommendations_for_request
from django.contrib.auth import get_user_model


class MentorOnboardingStatusTests(TestCase):
    def test_current_status_auto_completes_when_application_and_identity_complete(self):
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
            training_status="pending",
            current_status="in_review",
        )

        onboarding.refresh_from_db()
        self.assertEqual(onboarding.current_status, "completed")


class RecommendationEligibilityTests(TestCase):
    def setUp(self):
        self.mentee = Mentee.objects.create(
            first_name="Reco",
            last_name="Mentee",
            grade="10th Grade",
            email="reco.mentee@test.com",
            dob=date(2008, 1, 1),
            gender="Female",
            city_state="Chennai",
            timezone="Asia/Kolkata",
        )
        self.request = MenteeRequest.objects.create(
            mentee=self.mentee,
            feeling="Anxious",
            feeling_cause="Exam Pressure",
            support_type="Someone to Listen",
            comfort_level="Comfortable",
            topics=["Anxiety"],
            preferred_times=[{"day": "Monday", "start": "10:00", "end": "12:00"}],
            preferred_format="1:1",
            language="English",
            timezone="Asia/Kolkata",
            session_mode="online",
            allow_auto_match=True,
        )

    def _create_mentor(self, *, suffix: str, completed_onboarding: bool):
        mentor = Mentor.objects.create(
            first_name="Reco",
            last_name=f"Mentor{suffix}",
            email=f"reco.mentor.{suffix}@test.com",
            mobile=f"+9112345678{suffix}",
            dob=date(1980, 1, 1),
            gender="Male",
            city_state="Chennai",
            languages=["English"],
            care_areas=["Anxiety"],
            preferred_formats=["1:1"],
            availability=[{"day": "Monday", "start": "10:00", "end": "12:00"}],
            timezone="Asia/Kolkata",
            consent=True,
        )
        if completed_onboarding:
            MentorOnboardingStatus.objects.create(
                mentor=mentor,
                application_status="completed",
                identity_status="completed",
                contact_status="pending",
                training_status="pending",
            )
        else:
            MentorOnboardingStatus.objects.create(
                mentor=mentor,
                application_status="pending",
                identity_status="pending",
                contact_status="pending",
                training_status="pending",
            )
        return mentor

    @patch("core.signals._call_openai")
    def test_generate_recommendations_only_uses_completed_onboarding_mentors(self, mock_call_openai):
        completed_mentor = self._create_mentor(suffix="1", completed_onboarding=True)
        pending_mentor = self._create_mentor(suffix="2", completed_onboarding=False)

        mock_call_openai.return_value = (
            {
                "recs": [
                    {"mentor_id": pending_mentor.id, "score": 99, "explanation": "pending mentor"},
                    {"mentor_id": completed_mentor.id, "score": 92, "explanation": "completed mentor"},
                ],
                "model": "gpt-test",
                "response_id": "resp-test",
                "prompt_hash": "a" * 64,
            },
            None,
        )

        generate_recommendations_for_request(self.request)

        rec_ids = list(
            MatchRecommendation.objects.filter(mentee_request=self.request).values_list("mentor_id", flat=True)
        )
        self.assertEqual(rec_ids, [completed_mentor.id])

    def test_generate_recommendations_clears_existing_when_no_completed_mentors(self):
        pending_mentor = self._create_mentor(suffix="3", completed_onboarding=False)
        MatchRecommendation.objects.create(
            mentee_request=self.request,
            mentor=pending_mentor,
            score=50,
            explanation="old recommendation",
            status="suggested",
            source="rules",
        )

        generate_recommendations_for_request(self.request, replace_existing=True)

        self.assertFalse(MatchRecommendation.objects.filter(mentee_request=self.request).exists())


class OpenAIQuizGenerationTests(TestCase):
    @patch("core.quiz._generate_questions_with_openai")
    def test_generate_quiz_retries_and_collects_enough_valid_questions(self, mock_openai_generate):
        module_one = TrainingModule.objects.create(
            title="Active Listening",
            description="First module",
            order=1,
            lesson_outline=["Observe", "Reflect"],
            is_active=True,
        )
        module_two = TrainingModule.objects.create(
            title="Boundaries",
            description="Second module",
            order=2,
            lesson_outline=["Limits", "Referral"],
            is_active=True,
        )

        def q(text, module_title):
            return {
                "question": text,
                "options": ["A", "B", "C", "D"],
                "correct_option_index": 0,
                "module_title": module_title,
            }

        mock_openai_generate.side_effect = [
            [q(f"Q{idx} active", "Active Listening") for idx in range(1, 7)],
            [q(f"Q{idx} boundary", "Boundaries") for idx in range(1, 7)],
        ]

        questions, generated_by = generate_training_quiz_questions(
            [module_one, module_two],
            total_questions=10,
        )

        self.assertEqual(generated_by, "openai")
        self.assertEqual(len(questions), 10)
        module_titles = {item["module_title"] for item in questions}
        self.assertIn("Active Listening", module_titles)
        self.assertIn("Boundaries", module_titles)
        self.assertGreaterEqual(mock_openai_generate.call_count, 2)


class RoleBasedLoginRouteTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.admin_password = "AdminPass123!"
        cls.mentee_password = "MenteePass123!"

        cls.admin_user = User.objects.create_user(
            username="admin_login_route_test",
            email="admin.login.route@test.com",
            password=cls.admin_password,
        )
        cls.mentee_user = User.objects.create_user(
            username="mentee_login_route_test",
            email="mentee.login.route@test.com",
            password=cls.mentee_password,
        )
        UserProfile.objects.create(user=cls.admin_user, role="admin")
        UserProfile.objects.create(user=cls.mentee_user, role="mentee")

    def test_standard_login_rejects_admin_role(self):
        response = self.client.post(
            "/api/login/",
            {"email": self.admin_user.email, "password": self.admin_password},
            format="json",
        )
        self.assertEqual(response.status_code, 401)

    def test_admin_login_accepts_admin_role(self):
        response = self.client.post(
            "/api/admin/login/",
            {"email": self.admin_user.email, "password": self.admin_password},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("access", response.data)
        self.assertIn("refresh", response.data)

    def test_admin_login_rejects_non_admin_role(self):
        response = self.client.post(
            "/api/admin/login/",
            {"email": self.mentee_user.email, "password": self.mentee_password},
            format="json",
        )
        self.assertEqual(response.status_code, 401)

    def test_admin_account_without_user_profile_can_login_on_admin_route(self):
        User = get_user_model()
        password = "AdminPass123!"
        user = User.objects.create_user(
            username="admin_account_only_user",
            email="admin.account.only@test.com",
            password=password,
        )
        AdminAccount.objects.create(user=user, mobile="+10000000098")

        response = self.client.post(
            "/api/admin/login/",
            {"email": user.email, "password": password},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("access", response.data)

    def test_admin_account_without_user_profile_can_access_protected_route(self):
        User = get_user_model()
        password = "AdminPass123!"
        user = User.objects.create_user(
            username="admin_account_only_user_protected",
            email="admin.account.only.protected@test.com",
            password=password,
        )
        AdminAccount.objects.create(user=user, mobile="+10000000096")

        login_response = self.client.post(
            "/api/admin/login/",
            {"email": user.email, "password": password},
            format="json",
        )
        self.assertEqual(login_response.status_code, 200)

        access = login_response.data.get("access")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        protected_response = self.client.post("/api/auth/logout/", {}, format="json")
        self.assertEqual(protected_response.status_code, 200)

    def test_admin_account_without_user_profile_is_blocked_on_standard_login(self):
        User = get_user_model()
        password = "AdminPass123!"
        user = User.objects.create_user(
            username="admin_account_only_user_blocked",
            email="admin.account.only.blocked@test.com",
            password=password,
        )
        AdminAccount.objects.create(user=user, mobile="+10000000097")

        response = self.client.post(
            "/api/login/",
            {"email": user.email, "password": password},
            format="json",
        )
        self.assertEqual(response.status_code, 401)

    def test_admin_register_sets_admin_role(self):
        payload = {
            "first_name": "New",
            "last_name": "Admin",
            "email": "new.admin.route@test.com",
            "mobile": "+10000000099",
            "password": "AdminPass123!",
        }
        response = self.client.post("/api/auth/register/admin/", payload, format="json")
        self.assertEqual(response.status_code, 201)
        User = get_user_model()
        created_user = User.objects.get(email=payload["email"])
        profile = UserProfile.objects.get(user=created_user)
        self.assertEqual(profile.role, "admin")
        self.assertTrue(AdminAccount.objects.filter(user=created_user).exists())


class MobileOtpLoginTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.mentor_user = User.objects.create_user(
            username="mobile_mentor_user",
            email="mobile.mentor@test.com",
            password="MentorPass123!",
        )
        cls.mentee_user = User.objects.create_user(
            username="mobile_mentee_user",
            email="mobile.mentee@test.com",
            password="MenteePass123!",
        )
        UserProfile.objects.create(user=cls.mentor_user, role="mentor")
        UserProfile.objects.create(user=cls.mentee_user, role="mentee")
        cls.mentor = Mentor.objects.create(
            first_name="Mobile",
            last_name="Mentor",
            email=cls.mentor_user.email,
            mobile="+911111111111",
            dob=date(1990, 1, 1),
            gender="Male",
            city_state="Chennai",
        )
        cls.mentee = Mentee.objects.create(
            first_name="Mobile",
            last_name="Mentee",
            grade="10th Grade",
            email=cls.mentee_user.email,
            dob=date(2008, 1, 1),
            gender="Female",
            city_state="Chennai",
            parent_mobile="+922222222222",
        )

    def test_mobile_login_returns_tokens_for_mentor(self):
        response = self.client.post(
            "/api/auth/mobile-login/verify-otp/",
            {"mobile": self.mentor.mobile, "role": "mentor", "otp": "123456"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn("access", response.data)
        self.assertIn("refresh", response.data)

    def test_mobile_login_rejects_invalid_otp(self):
        response = self.client.post(
            "/api/auth/mobile-login/verify-otp/",
            {"mobile": self.mentor.mobile, "role": "mentor", "otp": "000000"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_mobile_login_returns_tokens_for_mentee(self):
        response = self.client.post(
            "/api/auth/mobile-login/verify-otp/",
            {"mobile": self.mentee.parent_mobile, "role": "mentee", "otp": "123456"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn("access", response.data)
        self.assertIn("refresh", response.data)


class MentorReviewsEndpointTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.mentee_user = User.objects.create_user(
            username="reviews_mentee_user",
            email="reviews.mentee@test.com",
            password="MenteePass123!",
        )
        cls.mentor_user = User.objects.create_user(
            username="reviews_mentor_user",
            email="reviews.mentor@test.com",
            password="MentorPass123!",
        )
        UserProfile.objects.create(user=cls.mentee_user, role="mentee")
        UserProfile.objects.create(user=cls.mentor_user, role="mentor")

        cls.mentee = Mentee.objects.create(
            first_name="Review",
            last_name="Mentee",
            grade="11th Grade",
            email=cls.mentee_user.email,
            dob=date(2008, 2, 1),
            gender="Female",
            city_state="Chennai",
            parent_guardian_consent=True,
        )
        cls.mentor = Mentor.objects.create(
            first_name="Review",
            last_name="Mentor",
            email=cls.mentor_user.email,
            mobile="+911234500001",
            dob=date(1990, 1, 1),
            gender="Male",
            city_state="Chennai",
        )
        now = timezone.now()
        session = Session.objects.create(
            mentee=cls.mentee,
            mentor=cls.mentor,
            scheduled_start=now - timedelta(days=1),
            scheduled_end=now - timedelta(days=1) + timedelta(hours=1),
            duration_minutes=60,
            timezone="Asia/Kolkata",
            mode="online",
            status="completed",
        )
        SessionFeedback.objects.create(
            session=session,
            rating=5,
            comments="Very supportive mentor.",
            topics_discussed=["Anxiety"],
        )

    def test_mentee_can_view_mentor_reviews(self):
        self.client.force_authenticate(user=self.mentee_user)
        response = self.client.get(f"/api/mentors/{self.mentor.id}/reviews/", format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["mentor_id"], self.mentor.id)
        self.assertEqual(response.data["summary"]["total_reviews"], 1)
        self.assertTrue(len(response.data["recent_feedback"]) >= 1)


class TrainingModuleVideoWorkflowTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.user = User.objects.create_user(
            username="mentor_video_test",
            email="mentor.video@test.com",
            password="StrongPass123!",
        )
        UserProfile.objects.create(user=cls.user, role="mentor")
        cls.mentor = Mentor.objects.create(
            first_name="Video",
            last_name="Mentor",
            email=cls.user.email,
            mobile="+10000000001",
            dob=date(1991, 1, 1),
            gender="Male",
            city_state="Chennai",
        )
        cls.module_one = TrainingModule.objects.create(
            title="Module One",
            description="First module",
            order=1,
            lesson_outline=["Video 1", "Video 2"],
            is_active=True,
        )
        cls.module_two = TrainingModule.objects.create(
            title="Module Two",
            description="Second module",
            order=2,
            lesson_outline=["Video 1", "Video 2"],
            is_active=True,
        )

    def setUp(self):
        self.client.force_authenticate(user=self.user)

    def _complete_module(self, module_id):
        first_video = self.client.post(
            f"/api/training-modules/{module_id}/watch-video/",
            {"video_index": 1},
            format="json",
        )
        self.assertEqual(first_video.status_code, 200)
        second_video = self.client.post(
            f"/api/training-modules/{module_id}/watch-video/",
            {"video_index": 2},
            format="json",
        )
        self.assertEqual(second_video.status_code, 200)

    def _complete_all_modules(self):
        for module in [self.module_one, self.module_two]:
            self._complete_module(module.id)

    def test_training_modules_unlock_in_sequence(self):
        response = self.client.get("/api/training-modules/", format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data[0]["training_status"], "in_progress")
        self.assertEqual(response.data[1]["training_status"], "locked")

    def test_module_completes_only_after_two_videos_and_unlocks_next(self):
        first_video = self.client.post(
            f"/api/training-modules/{self.module_one.id}/watch-video/",
            {"video_index": 1},
            format="json",
        )
        self.assertEqual(first_video.status_code, 200)
        self.assertEqual(first_video.data["module"]["progress_percent"], 50)
        self.assertEqual(first_video.data["module"]["training_status"], "in_progress")

        second_video = self.client.post(
            f"/api/training-modules/{self.module_one.id}/watch-video/",
            {"video_index": 2},
            format="json",
        )
        self.assertEqual(second_video.status_code, 200)
        self.assertEqual(second_video.data["module"]["progress_percent"], 100)
        self.assertEqual(second_video.data["module"]["training_status"], "completed")

        modules = self.client.get("/api/training-modules/", format="json")
        self.assertEqual(modules.status_code, 200)
        self.assertEqual(modules.data[0]["training_status"], "completed")
        self.assertEqual(modules.data[1]["training_status"], "in_progress")

    def test_second_video_requires_first_video_completion(self):
        response = self.client.post(
            f"/api/training-modules/{self.module_one.id}/watch-video/",
            {"video_index": 2},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_quiz_start_requires_all_modules_completed(self):
        response = self.client.post("/api/training-modules/quiz/start/", {}, format="json")
        self.assertEqual(response.status_code, 409)

    @patch("core.api_views.generate_training_quiz_questions")
    def test_onboarding_training_status_completes_only_after_quiz_pass(self, mock_generate_quiz):
        self._complete_all_modules()

        onboarding = MentorOnboardingStatus.objects.get(mentor=self.mentor)
        self.assertEqual(onboarding.training_status, "in_review")

        mock_questions = [
            {
                "question": f"Question {index + 1}",
                "options": ["A", "B", "C", "D"],
                "correct_option_index": 1,
                "module_title": "Module",
            }
            for index in range(15)
        ]
        mock_generate_quiz.return_value = (mock_questions, "openai")

        start = self.client.post("/api/training-modules/quiz/start/", {}, format="json")
        self.assertEqual(start.status_code, 201)
        self.assertEqual(len(start.data["attempt"]["questions"]), 15)
        attempt_id = start.data["attempt"]["id"]

        submit = self.client.post(
            "/api/training-modules/quiz/submit/",
            {"attempt_id": attempt_id, "selected_answers": [1] * 15},
            format="json",
        )
        self.assertEqual(submit.status_code, 200)
        self.assertTrue(submit.data["passed"])

        onboarding.refresh_from_db()
        self.assertEqual(onboarding.training_status, "completed")

    @patch("core.api_views.generate_training_quiz_questions")
    def test_abandoning_pending_quiz_marks_attempt_failed(self, mock_generate_quiz):
        self._complete_all_modules()
        mock_generate_quiz.return_value = (
            [
                {
                    "question": f"Question {index + 1}",
                    "options": ["A", "B", "C", "D"],
                    "correct_option_index": 1,
                    "module_title": "Module",
                }
                for index in range(15)
            ],
            "openai",
        )

        start = self.client.post("/api/training-modules/quiz/start/", {}, format="json")
        self.assertEqual(start.status_code, 201)
        attempt_id = start.data["attempt"]["id"]

        abandon = self.client.post(
            "/api/training-modules/quiz/abandon/",
            {"attempt_id": attempt_id},
            format="json",
        )
        self.assertEqual(abandon.status_code, 200)
        self.assertEqual(abandon.data["attempt"]["status"], "failed")

    @patch("core.api_views.generate_training_quiz_questions")
    def test_score_of_seven_passes_quiz(self, mock_generate_quiz):
        self._complete_all_modules()
        mock_generate_quiz.return_value = (
            [
                {
                    "question": f"Question {index + 1}",
                    "options": ["A", "B", "C", "D"],
                    "correct_option_index": 0,
                    "module_title": "Module",
                }
                for index in range(15)
            ],
            "openai",
        )

        start = self.client.post("/api/training-modules/quiz/start/", {}, format="json")
        self.assertEqual(start.status_code, 201)
        attempt_id = start.data["attempt"]["id"]

        selected_answers = [0] * 7 + [1] * 8
        submit = self.client.post(
            "/api/training-modules/quiz/submit/",
            {"attempt_id": attempt_id, "selected_answers": selected_answers},
            format="json",
        )
        self.assertEqual(submit.status_code, 200)
        self.assertTrue(submit.data["passed"])
        self.assertEqual(submit.data["score"], 7)

    @patch("core.api_views.generate_training_quiz_questions", side_effect=RuntimeError("OpenAI unavailable"))
    def test_quiz_start_returns_service_unavailable_when_openai_generation_fails(self, _mock_generate_quiz):
        self._complete_all_modules()
        response = self.client.post("/api/training-modules/quiz/start/", {}, format="json")
        self.assertEqual(response.status_code, 503)
        self.assertIn("Unable to generate quiz from OpenAI", response.data["detail"])


class LocationCatalogPublicApiTests(APITestCase):
    def test_states_endpoint_returns_non_empty_state_list(self):
        response = self.client.get("/api/locations/states/", format="json")
        self.assertEqual(response.status_code, 200)
        self.assertIn("states", response.data)
        self.assertTrue(len(response.data["states"]) > 0)

    def test_cities_endpoint_returns_cities_for_selected_state(self):
        response = self.client.get("/api/locations/cities/?state=Tamil%20Nadu", format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["state"], "Tamil Nadu")
        self.assertIn("cities", response.data)
        self.assertTrue(len(response.data["cities"]) > 0)

    def test_cities_endpoint_requires_state_query(self):
        response = self.client.get("/api/locations/cities/", format="json")
        self.assertEqual(response.status_code, 400)


class MentorContactVerificationStatusTests(APITestCase):
    def setUp(self):
        self.mentor = Mentor.objects.create(
            first_name="Contact",
            last_name="Mentor",
            email="mentor.contact@test.com",
            mobile="+10000000022",
            dob=date(1990, 5, 1),
            gender="Male",
            city_state="Chennai",
        )

    def test_phone_verification_marks_onboarding_contact_status_completed(self):
        send = self.client.post(
            "/api/auth/mentor-contact/send-otp/",
            {"mentor_id": self.mentor.id, "channel": "phone"},
            format="json",
        )
        self.assertEqual(send.status_code, 200)
        otp = send.data.get("otp")
        self.assertTrue(otp)

        verify = self.client.post(
            "/api/auth/mentor-contact/verify-otp/",
            {"mentor_id": self.mentor.id, "channel": "phone", "otp": otp},
            format="json",
        )
        self.assertEqual(verify.status_code, 200)

        onboarding = MentorOnboardingStatus.objects.get(mentor=self.mentor)
        self.assertEqual(onboarding.contact_status, "completed")

    def test_email_otp_can_be_verified_before_mentor_registration(self):
        send = self.client.post(
            "/api/auth/mentor-contact/send-otp/",
            {"channel": "email", "email": "preverify.mentor@test.com"},
            format="json",
        )
        self.assertEqual(send.status_code, 200, send.data)
        otp = send.data.get("otp")
        self.assertTrue(otp)

        verify = self.client.post(
            "/api/auth/mentor-contact/verify-otp/",
            {"channel": "email", "email": "preverify.mentor@test.com", "otp": otp},
            format="json",
        )
        self.assertEqual(verify.status_code, 200, verify.data)

    def test_phone_otp_can_be_verified_before_mentor_registration(self):
        send = self.client.post(
            "/api/auth/mentor-contact/send-otp/",
            {"channel": "phone", "mobile": "+911234567000"},
            format="json",
        )
        self.assertEqual(send.status_code, 200, send.data)
        otp = send.data.get("otp")
        self.assertTrue(otp)

        verify = self.client.post(
            "/api/auth/mentor-contact/verify-otp/",
            {"channel": "phone", "mobile": "+911234567000", "otp": otp},
            format="json",
        )
        self.assertEqual(verify.status_code, 200, verify.data)

    def test_email_otp_send_rejects_existing_mentor_email(self):
        response = self.client.post(
            "/api/auth/mentor-contact/send-otp/",
            {"channel": "email", "email": self.mentor.email},
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("email", response.data)

    def test_phone_otp_send_rejects_existing_mentor_mobile(self):
        response = self.client.post(
            "/api/auth/mentor-contact/send-otp/",
            {"channel": "phone", "mobile": self.mentor.mobile},
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("mobile", response.data)


class RegistrationAgeValidationTests(APITestCase):
    @staticmethod
    def years_ago(years):
        today = timezone.localdate()
        try:
            return today.replace(year=today.year - years)
        except ValueError:
            # Handle leap-day edge cases in a deterministic way.
            return today.replace(year=today.year - years, day=28)

    def test_mentee_registration_rejects_age_outside_13_to_18(self):
        payload = {
            "first_name": "Young",
            "last_name": "Student",
            "grade": "10th Grade",
            "email": "young.student@test.com",
            "dob": self.years_ago(12).isoformat(),
            "gender": "Female",
        }
        response = self.client.post("/api/auth/register/mentee/", payload, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("dob", response.data)

    def test_mentor_registration_rejects_age_outside_45_to_60(self):
        payload = {
            "first_name": "Early",
            "last_name": "Mentor",
            "email": "early.mentor@test.com",
            "mobile": "+911234500999",
            "dob": self.years_ago(30).isoformat(),
            "gender": "Male",
            "city_state": "Chennai",
        }
        response = self.client.post("/api/auth/register/mentor/", payload, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("dob", response.data)

    @override_settings(
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        DEFAULT_FROM_EMAIL="Bond Room <noreply@bondroom.test>",
        PUBLIC_BASE_URL="https://bond-room.vercel.app",
    )
    def test_mentee_registration_sends_welcome_email(self):
        payload = {
            "first_name": "Student",
            "last_name": "Welcome",
            "grade": "10th Grade",
            "email": "mentee.welcome@test.com",
            "dob": self.years_ago(14).isoformat(),
            "gender": "Female",
            "parent_guardian_consent": True,
            "parent_mobile": "+911234501130",
        }
        response = self.client.post("/api/auth/register/mentee/", payload, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["mentee.welcome@test.com"])
        self.assertIn("Welcome to Bond Room", mail.outbox[0].subject)
        self.assertIn("Open Dashboard", mail.outbox[0].alternatives[0][0])

    def test_mentee_registration_accepts_boundary_age_13(self):
        payload = {
            "first_name": "Boundary",
            "last_name": "Teen",
            "grade": "11th Grade",
            "email": "boundary.teen@test.com",
            "dob": self.years_ago(13).isoformat(),
            "gender": "Female",
        }
        response = self.client.post("/api/auth/register/mentee/", payload, format="json")
        self.assertEqual(response.status_code, 201, response.data)

    def test_mentor_registration_accepts_boundary_age_45(self):
        payload = {
            "first_name": "Boundary",
            "last_name": "Guide",
            "email": "boundary.guide@test.com",
            "mobile": "+911234501000",
            "dob": self.years_ago(45).isoformat(),
            "gender": "Male",
            "city_state": "Chennai",
        }
        response = self.client.post("/api/auth/register/mentor/", payload, format="json")
        self.assertEqual(response.status_code, 201, response.data)

    def test_mentor_registration_rejects_duplicate_email(self):
        Mentor.objects.create(
            first_name="Existing",
            last_name="Mentor",
            email="duplicate.mentor@test.com",
            mobile="+911234501001",
            dob=self.years_ago(45),
            gender="Male",
            city_state="Chennai",
        )
        payload = {
            "first_name": "New",
            "last_name": "Mentor",
            "email": "duplicate.mentor@test.com",
            "mobile": "+911234501002",
            "dob": self.years_ago(46).isoformat(),
            "gender": "Male",
            "city_state": "Chennai",
        }
        response = self.client.post("/api/auth/register/mentor/", payload, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("email", response.data)

    def test_mentee_registration_rejects_duplicate_email(self):
        Mentee.objects.create(
            first_name="Existing",
            last_name="Mentee",
            grade="10th Grade",
            email="existing.mentee.email@test.com",
            dob=self.years_ago(15),
            gender="Female",
            parent_guardian_consent=True,
            parent_mobile="+911234501010",
        )
        payload = {
            "first_name": "New",
            "last_name": "Mentee",
            "grade": "11th Grade",
            "email": "existing.mentee.email@test.com",
            "dob": self.years_ago(14).isoformat(),
            "gender": "Female",
            "parent_guardian_consent": True,
            "parent_mobile": "+911234501011",
        }
        response = self.client.post("/api/auth/register/mentee/", payload, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("email", response.data)

    def test_mentee_registration_rejects_email_used_by_mentor(self):
        Mentor.objects.create(
            first_name="Existing",
            last_name="Mentor",
            email="shared.email.conflict@test.com",
            mobile="+911234501012",
            dob=self.years_ago(45),
            gender="Male",
            city_state="Chennai",
        )
        payload = {
            "first_name": "New",
            "last_name": "Mentee",
            "grade": "12th Grade",
            "email": "shared.email.conflict@test.com",
            "dob": self.years_ago(16).isoformat(),
            "gender": "Female",
            "parent_guardian_consent": True,
            "parent_mobile": "+911234501013",
        }
        response = self.client.post("/api/auth/register/mentee/", payload, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("email", response.data)

    def test_mentor_registration_rejects_duplicate_mobile(self):
        Mentor.objects.create(
            first_name="Existing",
            last_name="Mentor",
            email="existing.mobile@test.com",
            mobile="+91 12345 01003",
            dob=self.years_ago(45),
            gender="Male",
            city_state="Chennai",
        )
        payload = {
            "first_name": "New",
            "last_name": "Mentor",
            "email": "new.mobile@test.com",
            "mobile": "+911234501003",
            "dob": self.years_ago(46).isoformat(),
            "gender": "Male",
            "city_state": "Chennai",
        }
        response = self.client.post("/api/auth/register/mentor/", payload, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("mobile", response.data)

    def test_mentee_registration_rejects_duplicate_parent_mobile(self):
        Mentee.objects.create(
            first_name="Existing",
            last_name="Mentee",
            grade="10th Grade",
            email="existing.parent.mobile@test.com",
            dob=self.years_ago(15),
            gender="Female",
            parent_mobile="+91 12345 01006",
            parent_guardian_consent=True,
        )
        payload = {
            "first_name": "New",
            "last_name": "Mentee",
            "grade": "11th Grade",
            "email": "new.parent.mobile@test.com",
            "dob": self.years_ago(14).isoformat(),
            "gender": "Female",
            "parent_guardian_consent": True,
            "parent_mobile": "+911234501006",
        }
        response = self.client.post("/api/auth/register/mentee/", payload, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("parent_mobile", response.data)

    def test_mentee_registration_rejects_parent_mobile_used_by_mentor(self):
        Mentor.objects.create(
            first_name="Existing",
            last_name="Mentor",
            email="mentor.mobile.conflict@test.com",
            mobile="+91 12345 01007",
            dob=self.years_ago(45),
            gender="Male",
            city_state="Chennai",
        )
        payload = {
            "first_name": "New",
            "last_name": "Mentee",
            "grade": "12th Grade",
            "email": "new.mentee.mobile.conflict@test.com",
            "dob": self.years_ago(16).isoformat(),
            "gender": "Female",
            "parent_guardian_consent": True,
            "parent_mobile": "+911234501007",
        }
        response = self.client.post("/api/auth/register/mentee/", payload, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("parent_mobile", response.data)

    def test_mentor_registration_allows_resubmitting_same_pending_mentor(self):
        create_payload = {
            "first_name": "Pending",
            "last_name": "Mentor",
            "email": "pending.mentor@test.com",
            "mobile": "+911234501004",
            "dob": self.years_ago(45).isoformat(),
            "gender": "Male",
            "city_state": "Chennai",
        }
        create_response = self.client.post("/api/auth/register/mentor/", create_payload, format="json")
        self.assertEqual(create_response.status_code, 201, create_response.data)

        update_payload = {
            **create_payload,
            "mentor_id": create_response.data["id"],
            "mobile": "+911234501005",
            "qualification": "Counsellor",
        }
        update_response = self.client.post("/api/auth/register/mentor/", update_payload, format="json")
        self.assertEqual(update_response.status_code, 201, update_response.data)
        self.assertEqual(update_response.data["id"], create_response.data["id"])
        self.assertEqual(update_response.data["mobile"], "+911234501005")

    @override_settings(
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        DEFAULT_FROM_EMAIL="Bond Room <noreply@bondroom.test>",
        PUBLIC_BASE_URL="https://bond-room.vercel.app",
    )
    def test_mentor_registration_sends_welcome_email(self):
        payload = {
            "first_name": "Email",
            "last_name": "Mentor",
            "email": "mentor.welcome@test.com",
            "mobile": "+911234501111",
            "dob": self.years_ago(45).isoformat(),
            "gender": "Male",
            "city_state": "Chennai",
        }
        response = self.client.post("/api/auth/register/mentor/", payload, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["mentor.welcome@test.com"])
        self.assertIn("Welcome to Bond Room", mail.outbox[0].subject)
        self.assertIn("Open Mentor Dashboard", mail.outbox[0].alternatives[0][0])

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_mentor_registration_update_does_not_send_welcome_email(self):
        create_payload = {
            "first_name": "NoSpam",
            "last_name": "Mentor",
            "email": "mentor.nospam@test.com",
            "mobile": "+911234501121",
            "dob": self.years_ago(45).isoformat(),
            "gender": "Male",
            "city_state": "Chennai",
        }
        create_response = self.client.post("/api/auth/register/mentor/", create_payload, format="json")
        self.assertEqual(create_response.status_code, 201, create_response.data)
        self.assertEqual(len(mail.outbox), 1)

        update_payload = {
            **create_payload,
            "mentor_id": create_response.data["id"],
            "qualification": "Counsellor",
            "mobile": "+911234501122",
        }
        update_response = self.client.post("/api/auth/register/mentor/", update_payload, format="json")
        self.assertEqual(update_response.status_code, 201, update_response.data)
        self.assertEqual(len(mail.outbox), 1)


class PayoutSettlementTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.admin_user = User.objects.create_user(
            username="payout_admin_user",
            email="payout.admin@test.com",
            password="AdminPass123!",
        )
        UserProfile.objects.create(user=cls.admin_user, role="admin")
        cls.mentor_user = User.objects.create_user(
            username="payout_mentor_user",
            email="payout.mentor@test.com",
            password="MentorPass123!",
        )
        UserProfile.objects.create(user=cls.mentor_user, role="mentor")

        cls.mentor = Mentor.objects.create(
            first_name="Payout",
            last_name="Mentor",
            email=cls.mentor_user.email,
            mobile="+911111119999",
            dob=date(1990, 3, 1),
            gender="Male",
            city_state="Chennai",
        )
        cls.mentee = Mentee.objects.create(
            first_name="Payout",
            last_name="Mentee",
            grade="11th Grade",
            email="payout.mentee@test.com",
            dob=date(2008, 3, 1),
            gender="Female",
            city_state="Chennai",
            parent_guardian_consent=True,
        )
        now = timezone.now()
        cls.session = Session.objects.create(
            mentee=cls.mentee,
            mentor=cls.mentor,
            scheduled_start=now - timedelta(days=1),
            scheduled_end=now - timedelta(days=1) + timedelta(hours=1),
            duration_minutes=60,
            timezone="Asia/Kolkata",
            mode="online",
            status="completed",
        )
        cls.wallet = MentorWallet.objects.create(
            mentor=cls.mentor,
            current_balance=Decimal("0.00"),
            pending_payout=Decimal("200.00"),
            total_claimed=Decimal("200.00"),
            total_donated=Decimal("0.00"),
        )
        cls.payout_tx = PayoutTransaction.objects.create(
            mentor=cls.mentor,
            session=cls.session,
            transaction_type="session_claim",
            status="pending",
            amount=Decimal("200.00"),
        )

    def setUp(self):
        self.client.force_authenticate(user=self.admin_user)

    def test_mark_paid_reduces_pending_payout(self):
        response = self.client.post(
            f"/api/payout-transactions/{self.payout_tx.id}/mark-paid/",
            {"reference_id": "BANK-REF-1"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)

        self.wallet.refresh_from_db()
        self.payout_tx.refresh_from_db()
        self.assertEqual(self.wallet.pending_payout, Decimal("0.00"))
        self.assertEqual(self.payout_tx.status, "paid")
        self.assertIsNotNone(self.payout_tx.processed_at)
        self.assertEqual(self.payout_tx.reference_id, "BANK-REF-1")

    def test_patch_status_to_paid_reduces_pending_payout(self):
        response = self.client.patch(
            f"/api/payout-transactions/{self.payout_tx.id}/",
            {"status": "paid"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)

        self.wallet.refresh_from_db()
        self.payout_tx.refresh_from_db()
        self.assertEqual(self.wallet.pending_payout, Decimal("0.00"))
        self.assertEqual(self.payout_tx.status, "paid")
        self.assertIsNotNone(self.payout_tx.processed_at)

    def test_mark_paid_fails_when_pending_payout_is_lower_than_amount(self):
        self.wallet.pending_payout = Decimal("50.00")
        self.wallet.save(update_fields=["pending_payout", "updated_at"])

        response = self.client.post(
            f"/api/payout-transactions/{self.payout_tx.id}/mark-paid/",
            {},
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("Pending payout is lower", str(response.data))

    def test_mentor_can_mark_own_payout_paid(self):
        self.client.force_authenticate(user=self.mentor_user)
        response = self.client.post(
            f"/api/payout-transactions/{self.payout_tx.id}/mark-paid/",
            {},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)

        self.wallet.refresh_from_db()
        self.payout_tx.refresh_from_db()
        self.assertEqual(self.wallet.pending_payout, Decimal("0.00"))
        self.assertEqual(self.payout_tx.status, "paid")


class SessionBehaviorMonitoringTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.mentor_user = User.objects.create_user(
            username="behavior_mentor_user",
            email="behavior.mentor@test.com",
            password="MentorPass123!",
        )
        cls.mentee_user = User.objects.create_user(
            username="behavior_mentee_user",
            email="behavior.mentee@test.com",
            password="MenteePass123!",
        )
        UserProfile.objects.create(user=cls.mentor_user, role="mentor")
        UserProfile.objects.create(user=cls.mentee_user, role="mentee")

        cls.mentor = Mentor.objects.create(
            first_name="Behavior",
            last_name="Mentor",
            email=cls.mentor_user.email,
            mobile="+911111110101",
            dob=date(1975, 3, 1),
            gender="Male",
            city_state="Chennai",
        )
        cls.mentee = Mentee.objects.create(
            first_name="Behavior",
            last_name="Mentee",
            grade="10th Grade",
            email=cls.mentee_user.email,
            dob=date(2010, 3, 1),
            gender="Female",
            city_state="Chennai",
            parent_guardian_consent=True,
        )
        now = timezone.now()
        cls.session = Session.objects.create(
            mentee=cls.mentee,
            mentor=cls.mentor,
            scheduled_start=now - timedelta(minutes=30),
            scheduled_end=now + timedelta(minutes=30),
            duration_minutes=60,
            timezone="Asia/Kolkata",
            mode="online",
            status="scheduled",
        )

    def test_mentor_can_report_high_risk_video_behavior(self):
        self.client.force_authenticate(user=self.mentor_user)
        response = self.client.post(
            f"/api/sessions/{self.session.id}/report-behavior/",
            {
                "speaker_role": "mentee",
                "labels": ["inappropriate dress", "partial nudity"],
                "confidence_score": 0.93,
                "notes": "Detected at minute 12.",
                "evidence_url": "https://example.com/snapshots/frame-12.jpg",
                "payload": {"frame_timestamp_ms": 720000},
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["incident_type"], "inappropriate_attire")
        self.assertEqual(response.data["severity"], "high")
        self.assertEqual(response.data["recommended_action"], "terminate_session")
        self.assertTrue(response.data["escalated_to_issue_report"])

        incident = SessionAbuseIncident.objects.get(id=response.data["incident_id"])
        self.assertEqual(incident.detection_source, "client_signal")
        self.assertEqual(incident.incident_type, "inappropriate_attire")
        self.assertEqual(incident.speaker_role, "mentee")
        self.assertEqual(incident.recommended_action, "terminate_session")
        self.assertEqual(incident.detection_payload.get("frame_timestamp_ms"), 720000)
        self.assertEqual(incident.detection_payload.get("reported_by_role"), "mentor")

        issue = SessionIssueReport.objects.get(session=self.session)
        self.assertEqual(issue.category, "safety_concern")
        self.assertEqual(issue.status, "open")

    def test_invalid_evidence_url_is_rejected(self):
        self.client.force_authenticate(user=self.mentee_user)
        response = self.client.post(
            f"/api/sessions/{self.session.id}/report-behavior/",
            {
                "labels": ["inappropriate hand signal"],
                "evidence_url": "frame://bad-link",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("evidence_url", str(response.data))

    @patch("core.api_views.classify_video_behavior_frame")
    @patch.dict("os.environ", {"VISION_CONSECUTIVE_REQUIRED": "1"})
    def test_analyze_video_frame_creates_incident(self, mock_classify):
        mock_classify.return_value = {
            "flagged": True,
            "incident_type": "inappropriate_gesture",
            "severity": "high",
            "recommended_action": "terminate_session",
            "confidence_score": 0.94,
            "matched_terms": ["middle finger"],
            "notes": "Detected obscene hand signal.",
        }
        self.client.force_authenticate(user=self.mentor_user)
        response = self.client.post(
            f"/api/sessions/{self.session.id}/analyze-video-frame/",
            {
                "speaker_role": "mentee",
                "frame_data_url": "data:image/jpeg;base64,ZmFrZTI=",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(response.data["flagged"])
        self.assertEqual(response.data["incident_type"], "inappropriate_gesture")
        self.assertEqual(response.data["recommended_action"], "terminate_session")

        incident = SessionAbuseIncident.objects.get(id=response.data["incident_id"])
        self.assertEqual(incident.detection_source, "ai_vision")
        self.assertEqual(incident.incident_type, "inappropriate_gesture")
        self.assertEqual(incident.speaker_role, "mentee")
        self.assertEqual(incident.severity, "high")

    def test_analyze_video_frame_rejects_invalid_payload(self):
        self.client.force_authenticate(user=self.mentor_user)
        response = self.client.post(
            f"/api/sessions/{self.session.id}/analyze-video-frame/",
            {
                "speaker_role": "mentee",
                "frame_data_url": "not-a-data-url",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("frame_data_url", str(response.data))

    @patch("core.api_views.classify_video_behavior_frame")
    def test_analyze_video_frame_suppresses_low_confidence_unsafe_environment(self, mock_classify):
        mock_classify.return_value = {
            "flagged": True,
            "incident_type": "unsafe_environment",
            "severity": "high",
            "recommended_action": "escalate_review",
            "confidence_score": 0.41,
            "matched_terms": ["unsafe environment"],
            "notes": "Possible unsafe background.",
        }
        self.client.force_authenticate(user=self.mentor_user)
        response = self.client.post(
            f"/api/sessions/{self.session.id}/analyze-video-frame/",
            {
                "speaker_role": "mentee",
                "frame_data_url": "data:image/jpeg;base64,ZmFrZQ==",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(response.data["flagged"])
        self.assertTrue(response.data["suppressed"])
        self.assertEqual(response.data["reason"], "low_confidence")


class AbuseMonitoringClassificationTests(TestCase):
    def test_detect_abusive_terms_matches_obfuscated_wording(self):
        matches = detect_abusive_terms("You are f u c k i n g rude.")
        self.assertIn("fuck", matches)

    def test_classify_behavior_signal_ignores_negated_safety_phrase(self):
        result = classify_behavior_signal(
            labels=[],
            note="No unsafe environment detected in this frame.",
            confidence_score=0.12,
        )
        self.assertFalse(result["flagged"])
        self.assertEqual(result["incident_type"], "unknown")

    def test_classify_behavior_signal_flags_explicit_gesture_phrase(self):
        result = classify_behavior_signal(
            labels=["middle finger"],
            note="",
            confidence_score=0.9,
        )
        self.assertTrue(result["flagged"])
        self.assertEqual(result["incident_type"], "inappropriate_gesture")

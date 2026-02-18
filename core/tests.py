from datetime import date
from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APITestCase

from core.models import AdminAccount, Mentor, MentorOnboardingStatus, TrainingModule, UserProfile
from django.contrib.auth import get_user_model


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

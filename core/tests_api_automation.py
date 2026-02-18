from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4
import warnings
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.schemas.openapi import SchemaGenerator
from rest_framework.test import APITestCase

from core.models import (
    DonationTransaction,
    MatchRecommendation,
    Mentor,
    MentorAvailabilitySlot,
    MentorContactVerification,
    MentorIdentityVerification,
    MentorOnboardingStatus,
    MentorProfile,
    MentorTrainingQuizAttempt,
    MentorTrainingProgress,
    MentorWallet,
    Mentee,
    MenteePreferences,
    MenteeRequest,
    ParentConsentVerification,
    PayoutTransaction,
    Session,
    SessionDisposition,
    SessionFeedback,
    SessionIssueReport,
    TrainingModule,
    UserProfile,
)
from core.schema import PUBLIC_PATHS


POST_ONLY_PUBLIC_PATHS = {
    "/api/admin/login/",
    "/api/auth/mentor-contact/send-otp/",
    "/api/auth/mentor-contact/verify-otp/",
    "/api/auth/parent-consent/send-otp/",
    "/api/auth/parent-consent/verify-otp/",
    "/api/auth/register/admin/",
    "/api/auth/register/mentee/",
    "/api/auth/register/mentor/",
    "/api/login/",
    "/api/token/refresh/",
}

POST_ONLY_AUTHENTICATED_PATHS = {
    "/api/auth/logout/",
    "/api/mentors/{id}/admin-decision/",
    "/api/sessions/{id}/disposition/",
    "/api/sessions/{id}/join-link/",
    "/api/training-modules/{id}/watch-video/",
    "/api/training-modules/quiz/start/",
    "/api/training-modules/quiz/submit/",
    "/api/training-modules/quiz/abandon/",
}

REGISTER_PATHS = {
    "/api/auth/register/admin/",
    "/api/auth/register/mentee/",
    "/api/auth/register/mentor/",
}

CREATED_PATHS = {
    "/api/training-modules/quiz/start/",
}


class ApiAutomationCoverageTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()

        cls.admin_password = "AdminPass123!"
        cls.mentee_password = "MenteePass123!"
        cls.mentor_password = "MentorPass123!"

        cls.admin_user = User.objects.create_user(
            username="admin_automation",
            email="admin.automation@example.com",
            password=cls.admin_password,
            first_name="Admin",
            last_name="Automation",
            is_staff=True,
            is_superuser=True,
        )
        cls.mentee_user = User.objects.create_user(
            username="mentee_automation",
            email="mentee.automation@example.com",
            password=cls.mentee_password,
            first_name="Mentee",
            last_name="Automation",
        )
        cls.mentor_user = User.objects.create_user(
            username="mentor_automation",
            email="mentor.automation@example.com",
            password=cls.mentor_password,
            first_name="Mentor",
            last_name="Automation",
        )

        UserProfile.objects.create(user=cls.admin_user, role="admin")
        UserProfile.objects.create(user=cls.mentee_user, role="mentee")
        UserProfile.objects.create(user=cls.mentor_user, role="mentor")

        cls.mentee = Mentee.objects.create(
            first_name="Mentee",
            last_name="Primary",
            grade="11th Grade",
            email=cls.mentee_user.email,
            dob=date(2008, 1, 1),
            gender="Female",
            city_state="Chennai",
            timezone="Asia/Kolkata",
            parent_guardian_consent=True,
            parent_mobile="+911234567890",
        )
        cls.mentor = Mentor.objects.create(
            first_name="Mentor",
            last_name="Primary",
            email=cls.mentor_user.email,
            mobile="+911234567891",
            dob=date(1992, 5, 5),
            gender="Male",
            city_state="Chennai",
            languages=["English", "Tamil"],
            care_areas=["Anxiety", "Academic Stress"],
            preferred_formats=["1:1"],
            availability=[{"day": "Monday", "from": "10:00", "to": "12:00"}],
            timezone="Asia/Kolkata",
            qualification="MA Psychology",
            bio="Automation mentor",
            consent=True,
        )

        cls.parent_consent = ParentConsentVerification.objects.create(
            mentee=cls.mentee,
            parent_mobile=cls.mentee.parent_mobile,
            otp_hash="seed",
            status="verified",
        )
        cls.mentee_preferences = MenteePreferences.objects.create(
            mentee=cls.mentee,
            comfort_level="Neutral",
            preferred_session_minutes=45,
            preferred_mentor_types=["listener"],
        )

        now = timezone.now()
        cls.slot = MentorAvailabilitySlot.objects.create(
            mentor=cls.mentor,
            start_time=now + timedelta(days=1),
            end_time=now + timedelta(days=1, hours=1),
            timezone="Asia/Kolkata",
            is_available=True,
        )

        cls.session = Session.objects.create(
            mentee=cls.mentee,
            mentor=cls.mentor,
            availability_slot=cls.slot,
            scheduled_start=now + timedelta(days=1),
            scheduled_end=now + timedelta(days=1, hours=1),
            duration_minutes=60,
            timezone="Asia/Kolkata",
            mode="online",
            status="approved",
            topic_tags=["Anxiety"],
            mentee_notes="Need support",
        )
        cls.session_feedback = SessionFeedback.objects.create(
            session=cls.session,
            rating=4,
            topics_discussed=["Anxiety"],
            comments="Helpful session",
        )

        cls.mentee_request = MenteeRequest.objects.create(
            mentee=cls.mentee,
            feeling="Anxious",
            feeling_cause="Exam Pressure",
            support_type="Someone to Listen",
            comfort_level="Comfortable",
            topics=["Anxiety"],
            preferred_times=[{"day": "Monday", "from": "10:00", "to": "12:00"}],
            preferred_format="1:1",
            language="English",
            timezone="Asia/Kolkata",
            session_mode="online",
            allow_auto_match=False,
        )
        cls.match_recommendation = MatchRecommendation.objects.create(
            mentee_request=cls.mentee_request,
            mentor=cls.mentor,
            score=Decimal("91.20"),
            explanation="Good fit for anxiety support.",
            matched_topics=["Anxiety"],
            availability_overlap=[{"day": "Monday"}],
            status="suggested",
            source="manual",
        )

        cls.identity = MentorIdentityVerification.objects.create(
            mentor=cls.mentor,
            status="in_review",
            additional_notes="Seed identity",
        )
        cls.contact = MentorContactVerification.objects.create(
            mentor=cls.mentor,
            email_verified=True,
            phone_verified=False,
            email_otp_hash="seed",
        )
        cls.onboarding = MentorOnboardingStatus.objects.create(
            mentor=cls.mentor,
            application_status="completed",
            identity_status="in_review",
            contact_status="pending",
            training_status="pending",
            final_approval_status="pending",
        )

        cls.training_module = TrainingModule.objects.create(
            title="Boundaries 101",
            description="Mentor training module",
            order=1,
            lesson_outline=["lesson-a", "lesson-b"],
            is_active=True,
            estimated_minutes=20,
        )
        cls.training_progress = MentorTrainingProgress.objects.create(
            mentor=cls.mentor,
            module=cls.training_module,
            status="completed",
            progress_percent=100,
        )
        cls.mentor_profile = MentorProfile.objects.create(
            mentor=cls.mentor,
            public_id="BR-0001",
            specialization="Anxiety",
            years_experience=4,
            is_active=True,
            sessions_completed=12,
        )
        cls.wallet = MentorWallet.objects.create(
            mentor=cls.mentor,
            current_balance=Decimal("500.00"),
            pending_payout=Decimal("100.00"),
            total_claimed=Decimal("300.00"),
            total_donated=Decimal("50.00"),
        )
        cls.disposition = SessionDisposition.objects.create(
            session=cls.session,
            mentor=cls.mentor,
            action="claim",
            status="confirmed",
            amount=Decimal("100.00"),
            note="Seed disposition",
        )
        cls.payout = PayoutTransaction.objects.create(
            mentor=cls.mentor,
            session=cls.session,
            transaction_type="session_claim",
            status="pending",
            amount=Decimal("100.00"),
            reference_id="PAYOUT-SEED-1",
        )
        cls.donation = DonationTransaction.objects.create(
            mentor=cls.mentor,
            session=cls.session,
            amount=Decimal("50.00"),
            cause="underprivileged_students",
            status="completed",
        )
        cls.issue_report = SessionIssueReport.objects.create(
            session=cls.session,
            mentor=cls.mentor,
            category="technical_issue",
            description="Seed technical issue report",
            status="open",
        )

    def setUp(self):
        self.client.defaults["HTTP_HOST"] = "testserver"
        self._refresh_token = None

    def _schema_paths(self):
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="You have a duplicated operationId in your OpenAPI schema*")
            schema = SchemaGenerator(title="Bond Room API").get_schema(public=True)
        return schema.get("paths", {}) if schema else {}

    def _authenticate_as_admin(self):
        self.client.force_authenticate(user=self.admin_user)

    def _clear_authentication(self):
        self.client.force_authenticate(user=None)

    def _endpoint_id_map(self):
        return {
            "/api/donation-transactions/{id}/": self.donation.id,
            "/api/match-recommendations/{id}/": self.match_recommendation.id,
            "/api/mentee-preferences/{id}/": self.mentee_preferences.id,
            "/api/mentee-requests/{id}/": self.mentee_request.id,
            "/api/mentee-requests/{id}/recommendations/": self.mentee_request.id,
            "/api/mentees/{id}/": self.mentee.id,
            "/api/mentees/{id}/dashboard/": self.mentee.id,
            "/api/mentees/{id}/preferences/": self.mentee.id,
            "/api/mentor-availability-slots/{id}/": self.slot.id,
            "/api/mentor-contact-verifications/{id}/": self.contact.id,
            "/api/mentor-identity-verifications/{id}/": self.identity.id,
            "/api/mentor-onboarding-statuses/{id}/": self.onboarding.id,
            "/api/mentor-profiles/{id}/": self.mentor_profile.id,
            "/api/mentor-training-progress/{id}/": self.training_progress.id,
            "/api/mentor-wallets/{id}/": self.wallet.id,
            "/api/mentors/{id}/": self.mentor.id,
            "/api/mentors/{id}/admin-decision/": self.mentor.id,
            "/api/mentors/{id}/impact-dashboard/": self.mentor.id,
            "/api/mentors/{id}/onboarding/": self.mentor.id,
            "/api/mentors/{id}/profile/": self.mentor.id,
            "/api/parent-consent-verifications/{id}/": self.parent_consent.id,
            "/api/payout-transactions/{id}/": self.payout.id,
            "/api/session-dispositions/{id}/": self.disposition.id,
            "/api/session-feedback/{id}/": self.session_feedback.id,
            "/api/session-issue-reports/{id}/": self.issue_report.id,
            "/api/sessions/{id}/": self.session.id,
            "/api/sessions/{id}/disposition/": self.session.id,
            "/api/sessions/{id}/feedback/": self.session.id,
            "/api/sessions/{id}/join-link/": self.session.id,
            "/api/sessions/{id}/mentee-profile/": self.session.id,
            "/api/training-modules/{id}/": self.training_module.id,
            "/api/training-modules/{id}/watch-video/": self.training_module.id,
        }

    def _resolve_path(self, schema_path):
        if schema_path == "/api/mentors/recommended/":
            return f"/api/mentors/recommended/?mentee_request_id={self.mentee_request.id}"
        if schema_path == "/api/training-modules/quiz/":
            return f"/api/training-modules/quiz/?mentor_id={self.mentor.id}"
        endpoint_id = self._endpoint_id_map().get(schema_path)
        if endpoint_id is not None:
            return schema_path.replace("{id}", str(endpoint_id))
        return schema_path

    def _pick_positive_method(self, schema_path, methods):
        if schema_path in POST_ONLY_PUBLIC_PATHS or schema_path in POST_ONLY_AUTHENTICATED_PATHS:
            return "POST"
        if "GET" in methods:
            return "GET"
        if "POST" in methods:
            return "POST"
        if "PATCH" in methods:
            return "PATCH"
        if "PUT" in methods:
            return "PUT"
        return "DELETE"

    def _post_login_and_get_refresh(self):
        if self._refresh_token:
            return self._refresh_token
        self._clear_authentication()
        response = self.client.post(
            "/api/admin/login/",
            {"email": self.admin_user.email, "password": self.admin_password},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self._refresh_token = response.data["refresh"]
        return self._refresh_token

    def _positive_payload(self, schema_path):
        unique_token = uuid4().hex[:8]

        if schema_path == "/api/auth/register/admin/":
            return {
                "first_name": "Auto",
                "last_name": "Admin",
                "email": f"auto-admin-{unique_token}@example.com",
                "mobile": "+910000000001",
                "password": "StrongPass123",
            }
        if schema_path == "/api/auth/register/mentee/":
            return {
                "first_name": "Auto",
                "last_name": "Mentee",
                "grade": "10th Grade",
                "email": f"auto-mentee-{unique_token}@example.com",
                "dob": "2008-02-01",
                "gender": "Female",
                "city_state": "Chennai",
                "timezone": "Asia/Kolkata",
                "password": "StrongPass123",
            }
        if schema_path == "/api/auth/register/mentor/":
            return {
                "first_name": "Auto",
                "last_name": "Mentor",
                "email": f"auto-mentor-{unique_token}@example.com",
                "mobile": "+910000000002",
                "dob": "1995-03-01",
                "gender": "Male",
                "city_state": "Chennai",
                "languages": ["English"],
                "care_areas": ["Anxiety"],
                "preferred_formats": ["1:1"],
                "consent": True,
                "password": "StrongPass123",
            }
        if schema_path == "/api/auth/parent-consent/send-otp/":
            return {"mentee_id": self.mentee.id, "parent_mobile": self.mentee.parent_mobile}
        if schema_path == "/api/auth/parent-consent/verify-otp/":
            send = self.client.post(
                "/api/auth/parent-consent/send-otp/",
                {"mentee_id": self.mentee.id, "parent_mobile": self.mentee.parent_mobile},
                format="json",
            )
            self.assertEqual(send.status_code, 200, send.data)
            return {"mentee_id": self.mentee.id, "otp": send.data["otp"]}
        if schema_path == "/api/auth/mentor-contact/send-otp/":
            return {"mentor_id": self.mentor.id, "channel": "email"}
        if schema_path == "/api/auth/mentor-contact/verify-otp/":
            send = self.client.post(
                "/api/auth/mentor-contact/send-otp/",
                {"mentor_id": self.mentor.id, "channel": "email"},
                format="json",
            )
            self.assertEqual(send.status_code, 200, send.data)
            return {"mentor_id": self.mentor.id, "channel": "email", "otp": send.data["otp"]}
        if schema_path == "/api/login/":
            return {"email": self.mentee_user.email, "password": self.mentee_password}
        if schema_path == "/api/admin/login/":
            return {"email": self.admin_user.email, "password": self.admin_password}
        if schema_path == "/api/token/refresh/":
            return {"refresh": self._post_login_and_get_refresh()}
        if schema_path == "/api/auth/logout/":
            return {}
        if schema_path == "/api/mentors/{id}/admin-decision/":
            return {"training_status": "completed", "final_approval_status": "completed"}
        if schema_path == "/api/sessions/{id}/disposition/":
            return {"action": "claim", "amount": "120.00", "note": "Automation claim"}
        if schema_path == "/api/sessions/{id}/join-link/":
            return {}
        if schema_path == "/api/training-modules/{id}/watch-video/":
            return {"mentor_id": self.mentor.id, "video_index": 1}
        if schema_path == "/api/training-modules/quiz/start/":
            return {"mentor_id": self.mentor.id}
        if schema_path == "/api/training-modules/quiz/submit/":
            attempt = MentorTrainingQuizAttempt.objects.filter(
                mentor=self.mentor,
                status="pending",
            ).first()
            if not attempt:
                attempt = MentorTrainingQuizAttempt.objects.create(
                    mentor=self.mentor,
                    total_questions=15,
                    pass_mark=11,
                    questions=[
                        {
                            "question": f"Q{idx + 1}",
                            "options": ["A", "B", "C", "D"],
                            "correct_option_index": 0,
                            "module_title": self.training_module.title,
                        }
                        for idx in range(15)
                    ],
                    status="pending",
                )
            if not attempt:
                raise AssertionError("Unable to create pending training quiz attempt for submit endpoint.")
            return {
                "mentor_id": self.mentor.id,
                "attempt_id": attempt.id,
                "selected_answers": [0 for _ in range(15)],
            }
        if schema_path == "/api/training-modules/quiz/abandon/":
            attempt = MentorTrainingQuizAttempt.objects.filter(
                mentor=self.mentor,
                status="pending",
            ).first()
            if not attempt:
                attempt = MentorTrainingQuizAttempt.objects.create(
                    mentor=self.mentor,
                    total_questions=15,
                    pass_mark=7,
                    questions=[
                        {
                            "question": f"Q{idx + 1}",
                            "options": ["A", "B", "C", "D"],
                            "correct_option_index": 0,
                            "module_title": self.training_module.title,
                        }
                        for idx in range(15)
                    ],
                    status="pending",
                )
            return {
                "mentor_id": self.mentor.id,
                "attempt_id": attempt.id,
            }
        return None

    def _negative_public_case(self, schema_path):
        if schema_path == "/api/schema/":
            return "POST", "/api/schema/", {}, {405}
        if schema_path == "/api/login/":
            return "POST", "/api/login/", {"email": self.admin_user.email, "password": self.admin_password}, {401}
        if schema_path == "/api/admin/login/":
            return "POST", "/api/admin/login/", {"email": self.admin_user.email, "password": "wrong-pass"}, {401}
        if schema_path == "/api/token/refresh/":
            return "POST", "/api/token/refresh/", {"refresh": "invalid-token"}, {401}
        if schema_path == "/api/auth/register/admin/":
            return "POST", "/api/auth/register/admin/", {}, {400}
        if schema_path == "/api/auth/register/mentee/":
            return "POST", "/api/auth/register/mentee/", {}, {400}
        if schema_path == "/api/auth/register/mentor/":
            return "POST", "/api/auth/register/mentor/", {}, {400}
        if schema_path == "/api/auth/parent-consent/send-otp/":
            return "POST", "/api/auth/parent-consent/send-otp/", {"mentee_id": 999999}, {404}
        if schema_path == "/api/auth/parent-consent/verify-otp/":
            return "POST", "/api/auth/parent-consent/verify-otp/", {"mentee_id": 999999, "otp": "000000"}, {404}
        if schema_path == "/api/auth/mentor-contact/send-otp/":
            return "POST", "/api/auth/mentor-contact/send-otp/", {"mentor_id": 999999, "channel": "email"}, {404}
        if schema_path == "/api/auth/mentor-contact/verify-otp/":
            return (
                "POST",
                "/api/auth/mentor-contact/verify-otp/",
                {"mentor_id": 999999, "channel": "email", "otp": "000000"},
                {404},
            )
        raise AssertionError(f"Unhandled negative public case for {schema_path}")

    def _request(self, method, path, payload, schema_path):
        client_method = getattr(self.client, method.lower())
        if schema_path == "/api/sessions/{id}/join-link/" and method == "POST":
            with patch("core.api_views.zoom_is_configured", return_value=True), patch(
                "core.api_views.maybe_attach_zoom_links",
                return_value={"join_url": "https://zoom.test/join", "host_join_url": "https://zoom.test/host"},
            ):
                return client_method(path, payload or {}, format="json")
        if method in {"POST", "PUT", "PATCH"}:
            return client_method(path, payload or {}, format="json")
        return client_method(path, format="json")

    def _expected_positive_status(self, schema_path):
        if schema_path in REGISTER_PATHS or schema_path in CREATED_PATHS:
            return {201}
        return {200}

    def test_api_surface_size_is_large_enough_for_regression_suite(self):
        paths = self._schema_paths()
        operation_count = sum(
            len([method for method in operations.keys() if method in {"get", "post", "put", "patch", "delete"}])
            for operations in paths.values()
        )

        self.assertGreaterEqual(len(paths), 50)
        self.assertGreaterEqual(operation_count, 120)

    def test_positive_path_coverage_for_all_api_paths(self):
        paths = self._schema_paths()
        covered_paths = set()

        for schema_path, operations in sorted(paths.items()):
            methods = {method.upper() for method in operations.keys() if method in {"get", "post", "put", "patch", "delete"}}
            method = self._pick_positive_method(schema_path, methods)
            path = self._resolve_path(schema_path)
            payload = self._positive_payload(schema_path)

            if schema_path in PUBLIC_PATHS:
                self._clear_authentication()
            else:
                self._authenticate_as_admin()

            response = self._request(method, path, payload, schema_path)
            self.assertIn(
                response.status_code,
                self._expected_positive_status(schema_path),
                f"Positive coverage failed for {schema_path} ({method}) with {response.status_code}: {getattr(response, 'data', None)}",
            )
            covered_paths.add(schema_path)

        self.assertEqual(set(paths.keys()), covered_paths)

    def test_negative_path_coverage_for_all_api_paths(self):
        paths = self._schema_paths()
        covered_paths = set()

        for schema_path, operations in sorted(paths.items()):
            methods = {method.upper() for method in operations.keys() if method in {"get", "post", "put", "patch", "delete"}}
            method = self._pick_positive_method(schema_path, methods)

            if schema_path in PUBLIC_PATHS:
                self._clear_authentication()
                neg_method, neg_path, payload, expected_statuses = self._negative_public_case(schema_path)
                response = self._request(neg_method, neg_path, payload, schema_path)
                self.assertIn(
                    response.status_code,
                    expected_statuses,
                    f"Negative public case failed for {schema_path}: {response.status_code}, {getattr(response, 'data', None)}",
                )
            else:
                self._clear_authentication()
                path = self._resolve_path(schema_path)
                payload = self._positive_payload(schema_path)
                response = self._request(method, path, payload, schema_path)
                self.assertIn(
                    response.status_code,
                    {401, 403},
                    f"Protected endpoint should reject unauthenticated access: {schema_path} ({method}) -> {response.status_code}",
                )
            covered_paths.add(schema_path)

        self.assertEqual(set(paths.keys()), covered_paths)

    def test_all_protected_operations_reject_unauthenticated_requests(self):
        paths = self._schema_paths()
        for schema_path, operations in sorted(paths.items()):
            if schema_path in PUBLIC_PATHS:
                continue

            for method in sorted([m.upper() for m in operations.keys() if m in {"get", "post", "put", "patch", "delete"}]):
                self._clear_authentication()
                path = self._resolve_path(schema_path)
                payload = self._positive_payload(schema_path) if method in {"POST", "PUT", "PATCH"} else None
                response = self._request(method, path, payload, schema_path)
                self.assertIn(
                    response.status_code,
                    {401, 403},
                    f"Expected unauth rejection for {schema_path} {method}, got {response.status_code}",
                )

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .api_views import (
    AdminRegisterView,
    DonationTransactionViewSet,
    LocationCitiesView,
    LocationStatesView,
    LogoutView,
    MatchRecommendationViewSet,
    MentorAvailabilitySlotViewSet,
    MentorContactSendOtpView,
    MentorContactVerifyOtpView,
    MentorContactVerificationViewSet,
    MentorIdentityVerificationViewSet,
    MentorOnboardingStatusViewSet,
    MentorProfileViewSet,
    MentorRegisterView,
    MentorTrainingProgressViewSet,
    MentorViewSet,
    MentorWalletViewSet,
    MenteePreferencesViewSet,
    MenteeRegisterView,
    MenteeRequestViewSet,
    MenteeViewSet,
    MobileLoginOtpVerifyView,
    ParentConsentSendOtpView,
    ParentConsentVerificationViewSet,
    ParentConsentVerifyOtpView,
    PayoutTransactionViewSet,
    SessionDispositionViewSet,
    SessionFeedbackViewSet,
    SessionIssueReportViewSet,
    SessionViewSet,
    TrainingModuleViewSet,
)

router = DefaultRouter()
router.register(r"mentees", MenteeViewSet, basename="mentee")
router.register(r"mentors", MentorViewSet, basename="mentor")
router.register(r"mentee-requests", MenteeRequestViewSet, basename="mentee-request")
router.register(r"match-recommendations", MatchRecommendationViewSet, basename="match-recommendation")
router.register(r"parent-consent-verifications", ParentConsentVerificationViewSet, basename="parent-consent")
router.register(r"mentee-preferences", MenteePreferencesViewSet, basename="mentee-preferences")
router.register(r"mentor-availability-slots", MentorAvailabilitySlotViewSet, basename="mentor-availability")
router.register(r"sessions", SessionViewSet, basename="session")
router.register(r"session-feedback", SessionFeedbackViewSet, basename="session-feedback")
router.register(r"mentor-identity-verifications", MentorIdentityVerificationViewSet, basename="mentor-identity")
router.register(r"mentor-contact-verifications", MentorContactVerificationViewSet, basename="mentor-contact")
router.register(r"mentor-onboarding-statuses", MentorOnboardingStatusViewSet, basename="mentor-onboarding")
router.register(r"training-modules", TrainingModuleViewSet, basename="training-module")
router.register(r"mentor-training-progress", MentorTrainingProgressViewSet, basename="mentor-training-progress")
router.register(r"mentor-profiles", MentorProfileViewSet, basename="mentor-profile")
router.register(r"session-dispositions", SessionDispositionViewSet, basename="session-disposition")
router.register(r"mentor-wallets", MentorWalletViewSet, basename="mentor-wallet")
router.register(r"payout-transactions", PayoutTransactionViewSet, basename="payout-transaction")
router.register(r"donation-transactions", DonationTransactionViewSet, basename="donation-transaction")
router.register(r"session-issue-reports", SessionIssueReportViewSet, basename="session-issue-report")


urlpatterns = [
    path("locations/states/", LocationStatesView.as_view(), name="location-states"),
    path("locations/cities/", LocationCitiesView.as_view(), name="location-cities"),
    path("auth/mobile-login/verify-otp/", MobileLoginOtpVerifyView.as_view(), name="mobile-login-verify-otp"),
    path("auth/register/admin/", AdminRegisterView.as_view(), name="register-admin"),
    path("auth/register/mentee/", MenteeRegisterView.as_view(), name="register-mentee"),
    path("auth/register/mentor/", MentorRegisterView.as_view(), name="register-mentor"),
    path("auth/parent-consent/send-otp/", ParentConsentSendOtpView.as_view(), name="parent-send-otp"),
    path("auth/parent-consent/verify-otp/", ParentConsentVerifyOtpView.as_view(), name="parent-verify-otp"),
    path("auth/mentor-contact/send-otp/", MentorContactSendOtpView.as_view(), name="mentor-send-otp"),
    path("auth/mentor-contact/verify-otp/", MentorContactVerifyOtpView.as_view(), name="mentor-verify-otp"),
    path("auth/logout/", LogoutView.as_view(), name="auth-logout"),
    path("", include(router.urls)),
]

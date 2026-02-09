from rest_framework.permissions import BasePermission


ROLE_ADMIN = "admin"
ROLE_MENTEE = "mentee"
ROLE_MENTOR = "mentor"
APP_ROLES = {ROLE_ADMIN, ROLE_MENTEE, ROLE_MENTOR}


def user_role(user):
    if not user or not user.is_authenticated:
        return None
    if user.is_superuser:
        return ROLE_ADMIN
    try:
        return user.userprofile.role
    except Exception:
        return None


class IsAuthenticatedWithAppRole(BasePermission):
    def has_permission(self, request, view):
        role = user_role(request.user)
        return bool(role in APP_ROLES)


class IsAdminRole(BasePermission):
    def has_permission(self, request, view):
        return user_role(request.user) == ROLE_ADMIN


class IsMenteeOrAdminRole(BasePermission):
    def has_permission(self, request, view):
        role = user_role(request.user)
        return bool(role in {ROLE_MENTEE, ROLE_ADMIN})


class IsMentorOrAdminRole(BasePermission):
    def has_permission(self, request, view):
        role = user_role(request.user)
        return bool(role in {ROLE_MENTOR, ROLE_ADMIN})

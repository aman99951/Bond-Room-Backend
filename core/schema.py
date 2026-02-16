from rest_framework.permissions import AllowAny
from rest_framework.renderers import JSONOpenAPIRenderer
from rest_framework.response import Response
from rest_framework.schemas.openapi import AutoSchema
from rest_framework.schemas.openapi import SchemaGenerator
from rest_framework.views import APIView


class BondRoomAutoSchema(AutoSchema):
    def get_operation_id(self, path, method):
        base = super().get_operation_id(path, method)
        return f"{base}{method.capitalize()}"


PUBLIC_PATHS = {
    "/api/login/",
    "/api/token/refresh/",
    "/api/auth/register/admin/",
    "/api/auth/register/mentee/",
    "/api/auth/register/mentor/",
    "/api/auth/parent-consent/send-otp/",
    "/api/auth/parent-consent/verify-otp/",
    "/api/auth/mentor-contact/send-otp/",
    "/api/auth/mentor-contact/verify-otp/",
    "/api/schema/",
    "/api/docs/",
}

TAG_ORDER = {
    "Auth": 0,
    "Mentee Role": 1,
    "Mentor Role": 2,
    "Shared Role": 3,
    "General": 4,
}


def tag_for_path(path: str) -> str:
    if path.startswith("/api/login/") or path.startswith("/api/token/") or path.startswith("/api/auth/"):
        return "Auth"
    if path.startswith("/api/mentees/"):
        return "Mentee Role"
    if path.startswith("/api/mentee-"):
        return "Mentee Role"
    if path.startswith("/api/parent-consent-verifications/"):
        return "Mentee Role"
    if path.startswith("/api/match-recommendations/"):
        return "Mentee Role"
    if path.startswith("/api/mentors/"):
        return "Mentor Role"
    if path.startswith("/api/mentor-"):
        return "Mentor Role"
    if path.startswith("/api/training-modules/"):
        return "Mentor Role"
    if path.startswith("/api/session-dispositions/"):
        return "Mentor Role"
    if path.startswith("/api/payout-transactions/"):
        return "Mentor Role"
    if path.startswith("/api/donation-transactions/"):
        return "Mentor Role"
    if path.startswith("/api/session-issue-reports/"):
        return "Mentor Role"
    if path.startswith("/api/sessions/") or path.startswith("/api/session-feedback/"):
        return "Shared Role"
    return "General"


class BondRoomSchemaView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    renderer_classes = [JSONOpenAPIRenderer]

    def get(self, request, *args, **kwargs):
        generator = SchemaGenerator(
            title="Bond Room API",
            description="Backend APIs for mentee and mentor workflows.",
            version="1.0.0",
        )
        schema = generator.get_schema(request=request, public=True)
        if not schema:
            return Response({})

        components = schema.setdefault("components", {})
        security_schemes = components.setdefault("securitySchemes", {})
        security_schemes["HTTPBearer"] = {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
        }

        schema["tags"] = [
            {"name": "Auth", "description": "Authentication and onboarding endpoints."},
            {"name": "Mentee Role", "description": "Endpoints for mentee users and mentee-owned data."},
            {"name": "Mentor Role", "description": "Endpoints for mentor users and mentor-owned data."},
            {"name": "Shared Role", "description": "Endpoints used by both mentee and mentor roles."},
            {"name": "General", "description": "Other endpoints."},
        ]

        path_tags = {}
        for path in schema.get("paths", {}):
            path_tags[path] = tag_for_path(path)

        for path, operations in schema.get("paths", {}).items():
            for method, operation in operations.items():
                if method.lower() not in {"get", "post", "put", "patch", "delete"}:
                    continue
                operation["tags"] = [path_tags[path]]
                if path in PUBLIC_PATHS:
                    operation.pop("security", None)
                else:
                    operation["security"] = [{"HTTPBearer": []}]

        sorted_paths = {}
        for path in sorted(
            schema.get("paths", {}).keys(),
            key=lambda item: (TAG_ORDER.get(path_tags[item], 99), item),
        ):
            sorted_paths[path] = schema["paths"][path]
        schema["paths"] = sorted_paths

        return Response(schema)

"""
URL configuration for bondroom_backend project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from pathlib import Path, PurePosixPath

from django.contrib import admin
from django.conf import settings
from django.http import Http404, JsonResponse
from django.urls import include, path, re_path
from django.views.static import serve
from django.views.generic import TemplateView
from rest_framework_simplejwt.views import TokenRefreshView

from core.auth import BondRoomAdminTokenObtainPairView, BondRoomTokenObtainPairView
from core.schema import BondRoomSchemaView


def api_root(_request):
    return JsonResponse({
        'status': 'ok',
        'message': 'Bond Room backend is running',
        'docs': '/api/docs/',
    })


def _normalize_media_path(path: str) -> str:
    normalized = str(PurePosixPath("/") / str(path or ""))
    return str(PurePosixPath(normalized).relative_to("/"))


def serve_media(request, path):
    media_roots = [Path(settings.MEDIA_ROOT)]
    relative_path = _normalize_media_path(path)
    for root in media_roots:
        resolved_root = root.resolve()
        candidate = (resolved_root / relative_path).resolve()
        try:
            candidate.relative_to(resolved_root)
        except ValueError:
            continue
        if candidate.is_file():
            return serve(request, relative_path, document_root=str(resolved_root))

    raise Http404("Media file not found.")


urlpatterns = [
    path('', api_root, name='api_root'),
    path('admin/', admin.site.urls),
    path('api/login/', BondRoomTokenObtainPairView.as_view(), name='api_login'),
    path('api/admin/login/', BondRoomAdminTokenObtainPairView.as_view(), name='api_admin_login'),
    path('api/token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    path('api/schema/', BondRoomSchemaView.as_view(), name='api-schema'),
    path('api/docs/', TemplateView.as_view(template_name='swagger-ui.html'), name='api-docs'),
    path('api/', include('core.urls')),
]

urlpatterns.append(re_path(r'^media/(?P<path>.*)$', serve_media, name='media'))

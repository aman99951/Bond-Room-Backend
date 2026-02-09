"""
Root-level Vercel entrypoint for Django backend deployments.
This allows deployments that use repository root as Vercel root directory.
"""

import os
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = REPO_ROOT / "backend"

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "bondroom_backend.settings")

from django.core.wsgi import get_wsgi_application  # noqa: E402


app = get_wsgi_application()

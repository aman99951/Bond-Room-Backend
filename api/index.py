"""
Root-level Vercel entrypoint for Django backend deployments.
This allows deployments that use repository root as Vercel root directory.
"""

import os
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = REPO_ROOT / "backend"

for candidate in (REPO_ROOT, BACKEND_DIR):
    candidate_str = str(candidate)
    if candidate.exists() and candidate_str not in sys.path:
        sys.path.insert(0, candidate_str)

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "bondroom_backend.settings")

from django.core.wsgi import get_wsgi_application  # noqa: E402


app = get_wsgi_application()

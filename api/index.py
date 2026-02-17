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

_STARTUP_MIGRATIONS_RAN = False


def _should_run_startup_migrations() -> bool:
    if os.environ.get("VERCEL", "").strip() == "":
        return False
    disabled = os.environ.get("DISABLE_STARTUP_MIGRATIONS", "").strip().lower()
    return disabled not in {"1", "true", "yes"}


def _run_startup_migrations() -> None:
    global _STARTUP_MIGRATIONS_RAN
    if _STARTUP_MIGRATIONS_RAN or not _should_run_startup_migrations():
        return

    import django
    from django.core.management import call_command

    django.setup()
    call_command("migrate", interactive=False, run_syncdb=True, verbosity=0)
    _STARTUP_MIGRATIONS_RAN = True


_run_startup_migrations()

from django.core.wsgi import get_wsgi_application  # noqa: E402


app = get_wsgi_application()

import os
from pathlib import Path

os.environ.setdefault("USE_SQLITE_FOR_TESTS", "1")
os.environ.setdefault("OPENAI_API_KEY", "")

from .settings import *  # noqa: F401,F403


PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.MD5PasswordHasher",
]

ALLOWED_HOSTS = [*ALLOWED_HOSTS, "testserver"]

STATIC_ROOT = BASE_DIR / "staticfiles_test"
Path(STATIC_ROOT).mkdir(parents=True, exist_ok=True)

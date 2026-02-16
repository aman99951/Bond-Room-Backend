#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""
import os
import sys


def _is_test_command(argv):
    return len(argv) > 1 and argv[1] == "test"


def main():
    """Run administrative tasks."""
    if _is_test_command(sys.argv):
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "bondroom_backend.settings_test")
        os.environ.setdefault("USE_SQLITE_FOR_TESTS", "1")
    else:
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "bondroom_backend.settings")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == '__main__':
    main()

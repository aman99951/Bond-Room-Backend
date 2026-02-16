#!/usr/bin/env python
import json
import os
import sys
from pathlib import Path


def deep_sort(obj):
    if isinstance(obj, dict):
        return {k: deep_sort(v) for k, v in sorted(obj.items())}
    if isinstance(obj, list):
        return [deep_sort(item) for item in obj]
    return obj


def main():
    if len(sys.argv) != 2:
        print("Usage: export_runtime_schema.py <output_path>")
        sys.exit(2)

    project_root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(project_root))

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "bondroom_backend.settings_test")
    os.environ.setdefault("USE_SQLITE_FOR_TESTS", "1")

    import django  # pylint: disable=import-outside-toplevel

    django.setup()

    from rest_framework.schemas.openapi import SchemaGenerator  # pylint: disable=import-outside-toplevel

    schema = SchemaGenerator(
        title="Bond Room API",
        description="Backend APIs for mentee and mentor workflows.",
        version="1.0.0",
    ).get_schema(public=True)

    if not schema:
        print("Runtime schema generation returned empty output.")
        sys.exit(1)

    output_path = sys.argv[1]
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as handle:
        json.dump(deep_sort(schema), handle, indent=2, ensure_ascii=False)
        handle.write("\n")


if __name__ == "__main__":
    main()

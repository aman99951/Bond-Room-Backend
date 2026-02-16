# Backend CI Notes

## API Schema Diff Gate

The backend CI has a `contract` stage that compares:

1. **Committed schema**: `api/schema/openapi.json`
2. **Runtime schema** generated from Django (`SchemaGenerator`)

If they differ, CI fails in the contract stage.

## Intentional API Changes

When API changes are intentional, regenerate and commit the schema:

```bash
python scripts/ci/export_runtime_schema.py "api/schema/openapi.json"
```

Then commit the updated schema file in the same PR.

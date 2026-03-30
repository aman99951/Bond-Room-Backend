$ErrorActionPreference = "Stop"

function Invoke-Step {
    param(
        [string]$Name,
        [scriptblock]$Action
    )

    Write-Host ""
    Write-Host "==> $Name"
    & $Action
    if ($LASTEXITCODE -ne 0) {
        throw "$Name failed with exit code $LASTEXITCODE"
    }
    Write-Host "<== $Name passed"
}

function Assert-SchemaSnapshotMatches {
    param(
        [string]$CommittedSchema = "api/schema/openapi.json",
        [string]$RuntimeSchema = "artifacts/contract/runtime-openapi.json"
    )

    if (!(Test-Path $CommittedSchema)) {
        throw "Committed schema file not found: $CommittedSchema"
    }

    python scripts/ci/export_runtime_schema.py $RuntimeSchema
    if ($LASTEXITCODE -ne 0) {
        throw "Runtime schema export failed with exit code $LASTEXITCODE"
    }

    $committedHash = (Get-FileHash $CommittedSchema -Algorithm SHA256).Hash
    $runtimeHash = (Get-FileHash $RuntimeSchema -Algorithm SHA256).Hash
    if ($committedHash -ne $runtimeHash) {
        throw "Schema diff gate failed: $CommittedSchema is out of date. Regenerate and commit latest schema."
    }
}

$env:DJANGO_SETTINGS_MODULE = "bondroom_backend.settings_test"
$env:USE_SQLITE_FOR_TESTS = "1"

Invoke-Step -Name "Django system check" -Action {
    python manage.py check
}

Invoke-Step -Name "Backend API automation coverage suite" -Action {
    python manage.py test --noinput core.tests_api_automation
}

Invoke-Step -Name "Backend full test suite" -Action {
    python manage.py test --noinput
}

Invoke-Step -Name "OpenAPI schema snapshot gate" -Action {
    Assert-SchemaSnapshotMatches
}

Write-Host ""
Write-Host "Backend pre-push checks passed."

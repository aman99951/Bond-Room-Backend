import json
import os
import re
import urllib.request

from django.conf import settings


DEFAULT_ABUSE_TERMS = (
    "idiot",
    "stupid",
    "shut up",
    "loser",
    "hate you",
    "kill yourself",
    "moron",
    "useless",
    "dumb",
    "fool",
)
OPENAI_MODERATION_MODEL = str(os.environ.get("OPENAI_MODERATION_MODEL", "omni-moderation-latest")).strip()
SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3}


def _normalized_text(value):
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _max_severity(left, right):
    left_value = str(left or "low").strip().lower()
    right_value = str(right or "low").strip().lower()
    return left_value if SEVERITY_RANK.get(left_value, 1) >= SEVERITY_RANK.get(right_value, 1) else right_value


def _severity_from_match_count(match_count):
    count = int(match_count or 0)
    if count >= 3:
        return "high"
    if count == 2:
        return "medium"
    return "low"


def _severity_from_score(score):
    value = float(score or 0.0)
    if value >= 0.8:
        return "high"
    if value >= 0.45:
        return "medium"
    return "low"


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def detect_abusive_terms(text, terms=None):
    source = _normalized_text(text)
    if not source:
        return []
    checks = terms or DEFAULT_ABUSE_TERMS
    matches = []
    for term in checks:
        normalized = _normalized_text(term)
        if not normalized:
            continue
        pattern = r"\b" + re.escape(normalized).replace(r"\ ", r"\s+") + r"\b"
        if re.search(pattern, source):
            matches.append(normalized)
    return matches


def _classify_abuse_with_openai(text):
    source = _normalized_text(text)
    if not source:
        return None
    api_key = str(getattr(settings, "OPENAI_API_KEY", "") or "").strip()
    if not api_key:
        return None

    body = {
        "model": OPENAI_MODERATION_MODEL or "omni-moderation-latest",
        "input": source[:8000],
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/moderations",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None

    result_rows = payload.get("results") if isinstance(payload, dict) else []
    if not isinstance(result_rows, list) or not result_rows:
        return None
    result = result_rows[0] if isinstance(result_rows[0], dict) else {}
    categories = result.get("categories")
    category_scores = result.get("category_scores")
    categories = categories if isinstance(categories, dict) else {}
    category_scores = category_scores if isinstance(category_scores, dict) else {}

    matched_categories = []
    for key, value in categories.items():
        if bool(value):
            normalized_key = str(key or "").strip().lower().replace("/", " ").replace("-", " ").replace("_", " ")
            if normalized_key:
                matched_categories.append(normalized_key)
    matched_categories = sorted(set(matched_categories))

    confidence = 0.0
    if matched_categories:
        for raw_key, raw_score in category_scores.items():
            normalized_key = str(raw_key or "").strip().lower().replace("/", " ").replace("-", " ").replace("_", " ")
            if normalized_key in matched_categories:
                confidence = max(confidence, _to_float(raw_score))
    flagged = bool(result.get("flagged")) or bool(matched_categories)
    if flagged and confidence <= 0:
        confidence = 0.51

    return {
        "flagged": flagged,
        "severity": _severity_from_score(confidence) if flagged else "low",
        "confidence_score": round(confidence, 2),
        "matches": matched_categories,
    }


def classify_abuse(text, terms=None):
    lexical_matches = detect_abusive_terms(text, terms=terms)
    lexical_result = {
        "flagged": bool(lexical_matches),
        "severity": _severity_from_match_count(len(lexical_matches)),
        "confidence_score": round(min(0.98, 0.58 + (len(lexical_matches) * 0.14)), 2) if lexical_matches else 0.0,
        "matches": lexical_matches,
    }

    if lexical_result["flagged"]:
        return lexical_result

    openai_result = _classify_abuse_with_openai(text)
    if not openai_result:
        return lexical_result

    combined_flagged = bool(lexical_result["flagged"] or openai_result["flagged"])
    if not combined_flagged:
        return {
            "flagged": False,
            "severity": "low",
            "confidence_score": max(
                _to_float(lexical_result.get("confidence_score")),
                _to_float(openai_result.get("confidence_score")),
            ),
            "matches": [],
        }

    combined_matches = sorted(
        {
            str(item or "").strip().lower()
            for item in [*lexical_result["matches"], *openai_result.get("matches", [])]
            if str(item or "").strip()
        }
    )
    combined_severity = _max_severity(
        lexical_result.get("severity", "low"),
        openai_result.get("severity", "low"),
    )
    combined_confidence = round(
        max(
            _to_float(lexical_result.get("confidence_score")),
            _to_float(openai_result.get("confidence_score")),
        ),
        2,
    )

    return {
        "flagged": True,
        "severity": combined_severity,
        "confidence_score": combined_confidence if combined_confidence > 0 else 0.51,
        "matches": combined_matches,
    }

import json
import os
import re
import urllib.request
import urllib.error

from django.conf import settings


DEFAULT_ABUSE_TERMS = (
    "idiot",
    "stupid",
    "stupid idiot",
    "shut up",
    "loser",
    "hate you",
    "kill yourself",
    "moron",
    "useless",
    "dumb",
    "fool",
    "bastard",
    "asshole",
    "f***",
    "fuck",
    "fuck off",
    "fucker",
    "fucking",
    "shit",
    "bitch",
    "motherfucker",
    "bloody idiot",
    "madarchod",
    "madar chod",
    "behenchod",
    "behen chod",
    "benchod",
    "bsdk",
    "bkl",
    "gandu",
    "kameena",
    "haramzada",
    "chutiya",
    "harami",
)
OPENAI_MODERATION_MODEL = str(os.environ.get("OPENAI_MODERATION_MODEL", "omni-moderation-latest")).strip()
OPENAI_LANGUAGE_MODERATION_MODEL = str(
    os.environ.get("OPENAI_LANGUAGE_MODERATION_MODEL", "gpt-4.1-mini")
).strip()
OPENAI_VISION_MODERATION_MODEL = str(
    os.environ.get("OPENAI_VISION_MODERATION_MODEL", "gpt-4.1-mini")
).strip()
SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3}
BEHAVIOR_RULES = (
    {
        "keywords": {
            "middle finger",
            "middle-finger",
            "obscene gesture",
            "obscene hand gesture",
            "inappropriate hand signal",
            "inappropriate hand gesture",
            "flipping off",
            "fuck you sign",
        },
        "incident_type": "inappropriate_gesture",
        "severity": "high",
        "recommended_action": "terminate_session",
    },
    {
        "keywords": {
            "nudity",
            "partial nudity",
            "underwear visible",
            "inappropriate dress",
            "inappropriate attire",
            "revealing clothing",
            "revealing clothes",
            "shirtless",
        },
        "incident_type": "inappropriate_attire",
        "severity": "high",
        "recommended_action": "terminate_session",
    },
    {
        "keywords": {
            "sexual content",
            "sexual gesture",
            "explicit behavior",
            "sexually explicit",
            "lewd behavior",
        },
        "incident_type": "sexual_content",
        "severity": "high",
        "recommended_action": "terminate_session",
    },
    {
        "keywords": {
            "harassment",
            "threatening gesture",
            "threatening pose",
            "intimidation",
            "bullying behavior",
        },
        "incident_type": "harassment",
        "severity": "medium",
        "recommended_action": "escalate_review",
    },
    {
        "keywords": {
            "unsafe environment",
            "weapon visible",
            "knife visible",
            "gun visible",
            "violence risk",
        },
        "incident_type": "unsafe_environment",
        "severity": "high",
        "recommended_action": "escalate_review",
    },
)
INCIDENT_TYPE_DEFAULTS = {
    "inappropriate_gesture": {"severity": "high", "recommended_action": "terminate_session"},
    "inappropriate_attire": {"severity": "high", "recommended_action": "terminate_session"},
    "sexual_content": {"severity": "high", "recommended_action": "terminate_session"},
    "harassment": {"severity": "medium", "recommended_action": "escalate_review"},
    "unsafe_environment": {"severity": "high", "recommended_action": "escalate_review"},
}
INCIDENT_TYPE_ALIASES = {
    "obscene_gesture": "inappropriate_gesture",
    "obscene_hand_gesture": "inappropriate_gesture",
    "inappropriate_hand_signal": "inappropriate_gesture",
    "inappropriate_hand_gesture": "inappropriate_gesture",
    "inappropriate_clothing": "inappropriate_attire",
    "inappropriate_dress": "inappropriate_attire",
    "nudity": "inappropriate_attire",
    "sexual": "sexual_content",
    "sexual_behavior": "sexual_content",
    "unsafe_env": "unsafe_environment",
}
COMPACT_MATCH_ALLOWLIST = {
    "asshole",
    "bastard",
    "behenchod",
    "chutiya",
    "fuck",
    "fucking",
    "fucker",
    "harami",
    "kill yourself",
    "madarchod",
    "motherfucker",
    "bitch",
    "shit",
    "shut up",
}
LEET_TRANSLATION_TABLE = str.maketrans(
    {
        "@": "a",
        "$": "s",
        "!": "i",
        "0": "o",
        "1": "i",
        "3": "e",
        "4": "a",
        "5": "s",
        "7": "t",
    }
)
NEGATION_TAIL_PATTERN = re.compile(r"(?:\bno\b|\bnot\b|\bnever\b|\bwithout\b|\bnone\b)\s*$", flags=re.IGNORECASE)


def _normalized_text(value):
    text = str(value or "").strip().lower()
    text = re.sub(r"[-_/]+", " ", text)
    text = re.sub(r"[^\w\s*]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _normalize_incident_type(value):
    normalized = _normalized_text(value).replace(" ", "_")
    if normalized in INCIDENT_TYPE_DEFAULTS:
        return normalized
    return INCIDENT_TYPE_ALIASES.get(normalized, "unknown")


def _compact_alnum_text(value):
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    raw = raw.translate(LEET_TRANSLATION_TABLE)
    return re.sub(r"[^a-z0-9]+", "", raw)


def _contains_non_negated_phrase(text, phrase):
    haystack = _normalized_text(text)
    needle = _normalized_text(phrase)
    if not haystack or not needle:
        return False
    pattern = r"\b" + re.escape(needle).replace(r"\ ", r"\s+") + r"\b"
    for match in re.finditer(pattern, haystack):
        prefix = haystack[max(0, match.start() - 32) : match.start()]
        if NEGATION_TAIL_PATTERN.search(prefix):
            continue
        return True
    return False


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
    source_compact = _compact_alnum_text(text)
    checks = terms or DEFAULT_ABUSE_TERMS
    matches = []
    for term in checks:
        normalized = _normalized_text(term)
        if not normalized:
            continue
        pattern = r"\b" + re.escape(normalized).replace(r"\ ", r"\s+") + r"\b"
        if re.search(pattern, source):
            matches.append(normalized)
            continue
        if not source_compact:
            continue
        compact_term = _compact_alnum_text(normalized.replace("*", ""))
        should_try_compact = (
            normalized in COMPACT_MATCH_ALLOWLIST
            or "*" in str(term or "")
            or " " in normalized
        )
        if should_try_compact and len(compact_term) >= 4 and compact_term in source_compact:
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


def _classify_bad_language_with_openai(text):
    source = _normalized_text(text)
    if not source:
        return None
    api_key = str(getattr(settings, "OPENAI_API_KEY", "") or "").strip()
    if not api_key:
        return None

    instruction = (
        "You are a real-time language safety classifier for a mentor-mentee meeting transcript. "
        "Detect abusive language including profanity, insults, harassment, threats, humiliating language, and slurs. "
        "If text is neutral or non-abusive, return flagged=false. "
        "Return strict JSON with keys only: "
        "flagged(boolean), confidence_score(number 0..1), matched_terms(array of short strings), notes(string)."
    )
    body = {
        "model": OPENAI_LANGUAGE_MODERATION_MODEL or "gpt-4.1-mini",
        "text": {"format": {"type": "json_object"}},
        "input": [
            {
                "role": "system",
                "content": [{"type": "input_text", "text": instruction}],
            },
            {
                "role": "user",
                "content": [{"type": "input_text", "text": source[:1800]}],
            },
        ],
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None

    raw_text = str(payload.get("output_text", "")).strip() if isinstance(payload, dict) else ""
    if not raw_text:
        raw_text = _extract_response_text(payload)

    parsed = {}
    if raw_text:
        try:
            parsed = json.loads(raw_text)
        except ValueError:
            parsed = {}

    flagged = bool(parsed.get("flagged")) if isinstance(parsed, dict) else False
    confidence_score = _to_float(parsed.get("confidence_score")) if isinstance(parsed, dict) else 0.0
    confidence_score = round(min(0.99, max(0.0, confidence_score)), 2)

    matched_terms = parsed.get("matched_terms") if isinstance(parsed, dict) else []
    if isinstance(matched_terms, str):
        matched_terms = [matched_terms]
    if not isinstance(matched_terms, list):
        matched_terms = []
    matched_terms = sorted({_normalized_text(item) for item in matched_terms if _normalized_text(item)})

    if not flagged and matched_terms and confidence_score >= 0.55:
        flagged = True
    if flagged and confidence_score <= 0:
        confidence_score = 0.61

    return {
        "flagged": flagged,
        "severity": _severity_from_score(confidence_score) if flagged else "low",
        "confidence_score": confidence_score,
        "matches": matched_terms,
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

    language_result = _classify_bad_language_with_openai(text)
    if language_result and language_result.get("flagged"):
        language_matches = language_result.get("matches", []) if isinstance(language_result, dict) else []
        return {
            "flagged": True,
            "severity": str(language_result.get("severity", "low")).strip().lower() or "low",
            "confidence_score": round(max(_to_float(language_result.get("confidence_score")), 0.51), 2),
            "matches": sorted({str(item or "").strip().lower() for item in language_matches if str(item or "").strip()}),
        }

    openai_result = _classify_abuse_with_openai(text)
    if not openai_result and not language_result:
        return lexical_result

    combined_flagged = bool(
        lexical_result["flagged"]
        or (language_result or {}).get("flagged")
        or (openai_result or {}).get("flagged")
    )
    if not combined_flagged:
        return {
            "flagged": False,
            "severity": "low",
            "confidence_score": max(
                _to_float(lexical_result.get("confidence_score")),
                _to_float((language_result or {}).get("confidence_score")),
                _to_float(openai_result.get("confidence_score")),
            ),
            "matches": [],
        }

    combined_matches = sorted(
        {
            str(item or "").strip().lower()
            for item in [
                *lexical_result["matches"],
                *(language_result or {}).get("matches", []),
                *openai_result.get("matches", []),
            ]
            if str(item or "").strip()
        }
    )
    combined_severity = _max_severity(
        _max_severity(
            lexical_result.get("severity", "low"),
            (language_result or {}).get("severity", "low"),
        ),
        openai_result.get("severity", "low"),
    )
    combined_confidence = round(
        max(
            _to_float(lexical_result.get("confidence_score")),
            _to_float((language_result or {}).get("confidence_score")),
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


def classify_behavior_signal(*, labels=None, note="", confidence_score=0.0):
    normalized_labels = [_normalized_text(label) for label in (labels or []) if _normalized_text(label)]
    normalized_note = _normalized_text(note)

    matched_rule = None
    for rule in BEHAVIOR_RULES:
        for keyword in rule["keywords"]:
            normalized_keyword = _normalized_text(keyword)
            if not normalized_keyword:
                continue
            label_hit = any(_contains_non_negated_phrase(label, normalized_keyword) for label in normalized_labels)
            note_hit = _contains_non_negated_phrase(normalized_note, normalized_keyword)
            if label_hit or note_hit:
                matched_rule = rule
                break
        if matched_rule:
            break

    parsed_confidence = _to_float(confidence_score)
    if not matched_rule:
        return {
            "flagged": False,
            "incident_type": "unknown",
            "severity": "low",
            "recommended_action": "none",
            "confidence_score": round(max(0.0, parsed_confidence), 2),
            "matched_terms": [],
        }

    if parsed_confidence <= 0:
        parsed_confidence = 0.75 if matched_rule["severity"] == "high" else 0.55

    return {
        "flagged": True,
        "incident_type": matched_rule["incident_type"],
        "severity": matched_rule["severity"],
        "recommended_action": matched_rule["recommended_action"],
        "confidence_score": round(min(0.99, parsed_confidence), 2),
        "matched_terms": normalized_labels,
    }


def _extract_response_text(payload):
    if not isinstance(payload, dict):
        return ""
    output_rows = payload.get("output")
    if not isinstance(output_rows, list):
        return ""
    for row in output_rows:
        content_rows = row.get("content") if isinstance(row, dict) else None
        if not isinstance(content_rows, list):
            continue
        for item in content_rows:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "output_text":
                text = str(item.get("text", "")).strip()
                if text:
                    return text
    return ""


def classify_video_behavior_frame(*, frame_data_url, note=""):
    frame_value = str(frame_data_url or "").strip()
    if not frame_value.startswith("data:image/"):
        return {
            "flagged": False,
            "incident_type": "unknown",
            "severity": "low",
            "recommended_action": "none",
            "confidence_score": 0.0,
            "matched_terms": [],
            "reason": "invalid_image_data",
        }
    api_key = str(getattr(settings, "OPENAI_API_KEY", "") or "").strip()
    if not api_key:
        return {
            "flagged": False,
            "incident_type": "unknown",
            "severity": "low",
            "recommended_action": "none",
            "confidence_score": 0.0,
            "matched_terms": [],
            "reason": "missing_api_key",
        }

    instruction = (
        "You are a realtime meeting safety classifier for a student mentoring call. "
        "Analyze the image and determine if it shows prohibited visual behavior such as "
        "inappropriate hand signal/gesture, inappropriate attire, sexual content, harassment, "
        "or unsafe environment. Be conservative: if the evidence is not clear, return flagged=false. "
        "Do not guess from ambiguous pose, low light, blur, or normal clothing. "
        "Return strict JSON only with keys: "
        "flagged(boolean), incident_type(string), labels(array of short strings), confidence_score(number 0-1), notes(string). "
        "incident_type must be one of: inappropriate_gesture, inappropriate_attire, sexual_content, harassment, unsafe_environment, unknown."
    )
    body = {
        "model": OPENAI_VISION_MODERATION_MODEL or "gpt-4.1-mini",
        "text": {"format": {"type": "json_object"}},
        "input": [
            {
                "role": "system",
                "content": [{"type": "input_text", "text": instruction}],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": str(note or "Frame from ongoing video meeting."),
                    },
                    {
                        "type": "input_image",
                        "image_url": frame_value,
                    },
                ],
            },
        ],
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError):
        return {
            "flagged": False,
            "incident_type": "unknown",
            "severity": "low",
            "recommended_action": "none",
            "confidence_score": 0.0,
            "matched_terms": [],
            "reason": "vision_request_failed",
        }

    raw_text = _extract_response_text(payload)
    parsed = {}
    if raw_text:
        try:
            parsed = json.loads(raw_text)
        except ValueError:
            parsed = {}

    labels = parsed.get("labels") if isinstance(parsed, dict) else []
    if isinstance(labels, str):
        labels = [labels]
    if not isinstance(labels, list):
        labels = []
    labels = [str(item).strip() for item in labels if str(item).strip()][:8]
    confidence = parsed.get("confidence_score") if isinstance(parsed, dict) else 0.0
    notes_value = str(parsed.get("notes", "")).strip() if isinstance(parsed, dict) else ""
    incident_type = _normalize_incident_type(parsed.get("incident_type", "") if isinstance(parsed, dict) else "")
    model_flagged = bool(parsed.get("flagged")) if isinstance(parsed, dict) else False
    confidence_value = round(min(0.99, max(0.0, _to_float(confidence))), 2)
    has_positive_signals = bool(model_flagged or labels)

    if not has_positive_signals and incident_type == "unknown":
        return {
            "flagged": False,
            "incident_type": "unknown",
            "severity": "low",
            "recommended_action": "none",
            "confidence_score": confidence_value,
            "matched_terms": [],
            "notes": notes_value,
        }

    if incident_type in INCIDENT_TYPE_DEFAULTS and (has_positive_signals or confidence_value >= 0.45):
        defaults = INCIDENT_TYPE_DEFAULTS[incident_type]
        if confidence_value <= 0:
            confidence_value = 0.78 if defaults["severity"] == "high" else 0.58
        return {
            "flagged": True,
            "incident_type": incident_type,
            "severity": defaults["severity"],
            "recommended_action": defaults["recommended_action"],
            "confidence_score": confidence_value,
            "matched_terms": [_normalized_text(item) for item in labels if _normalized_text(item)],
            "notes": notes_value,
        }

    behavior_result = classify_behavior_signal(
        labels=labels,
        note=f"{notes_value} {incident_type.replace('_', ' ')}".strip(),
        confidence_score=confidence,
    )
    behavior_result["notes"] = notes_value
    return behavior_result

import re


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


def _normalized_text(value):
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


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


def classify_abuse(text, terms=None):
    matches = detect_abusive_terms(text, terms=terms)
    if not matches:
        return {
            "flagged": False,
            "severity": "low",
            "confidence_score": 0.0,
            "matches": [],
        }

    if len(matches) >= 3:
        severity = "high"
    elif len(matches) == 2:
        severity = "medium"
    else:
        severity = "low"

    confidence = min(0.98, round(0.58 + (len(matches) * 0.14), 2))
    return {
        "flagged": True,
        "severity": severity,
        "confidence_score": confidence,
        "matches": matches,
    }

import hashlib
import json
import os
import urllib.error
import urllib.request

from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver

from .matching_logic import filter_mentors, score_mentors
from .models import MatchRecommendation, MenteeRequest, Mentor


def _get_max_int(env_key: str, default: int) -> int:
    raw = os.environ.get(env_key, "")
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _call_openai(req: MenteeRequest, mentors):
    api_key = settings.OPENAI_API_KEY
    if not api_key:
        return None, "missing_api_key"

    mentor_payload = [
        {
            "id": m.id,
            "name": f"{m.first_name} {m.last_name}".strip(),
            "care_areas": m.care_areas,
            "languages": m.languages,
            "preferred_formats": m.preferred_formats,
            "availability": m.availability,
            "timezone": m.timezone,
            "average_rating": float(m.average_rating) if m.average_rating is not None else None,
            "response_time_minutes": m.response_time_minutes,
            "city_state": m.city_state,
        }
        for m in mentors
    ]

    payload = {
        "model": os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
        "text": {"format": {"type": "json_object"}},
        "input": [
            {
                "role": "system",
                "content": (
                    "You are a matching engine. "
                    "Use the rules: filter by availability+timezone, score by topic overlap, "
                    "boost strong ratings and quick responses, prefer local for in-person. "
                    "Return strict JSON only with: "
                    "{'recommendations': [{'mentor_id': int, 'score': number, 'explanation': str}]}"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "mentee_request": {
                            "topics": req.topics,
                            "preferred_times": req.preferred_times,
                            "preferred_format": req.preferred_format,
                            "language": req.language,
                            "timezone": req.timezone,
                            "access_needs": req.access_needs,
                            "safety_notes": req.safety_notes,
                            "session_mode": req.session_mode,
                            "mentee_city": req.mentee.city_state,
                        },
                        "mentors": mentor_payload,
                    }
                ),
            },
        ],
    }

    prompt_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()

    req_data = json.dumps(payload).encode("utf-8")
    req_headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    req_obj = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=req_data,
        headers=req_headers,
    )
    try:
        with urllib.request.urlopen(req_obj, timeout=20) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        response_id = body.get("id", "")
        output_text = ""
        if "output_text" in body:
            output_text = body.get("output_text") or ""
        else:
            for item in body.get("output", []):
                for content in item.get("content", []):
                    if content.get("type") == "output_text":
                        output_text += content.get("text", "")

        output_text = output_text.strip()
        result = json.loads(output_text) if output_text else {}
        recs = result.get("recommendations", [])
        return {
            "recs": recs,
            "model": payload["model"],
            "response_id": response_id,
            "prompt_hash": prompt_hash,
        }, None
    except Exception as exc:  # pragma: no cover - runtime/network failures
        return None, str(exc)


def generate_recommendations_for_request(
    instance: MenteeRequest, *, replace_existing: bool = True
):
    if not instance.allow_auto_match or instance.safety_flag:
        return

    max_mentors = _get_max_int("OPENAI_MAX_MENTORS", 0)
    mentor_qs = Mentor.objects.all()
    mentor_pool = list(mentor_qs[:max_mentors]) if max_mentors > 0 else list(mentor_qs[:25])
    if not mentor_pool:
        return
    mentors = filter_mentors(instance, mentor_pool)
    if not mentors:
        mentors = mentor_pool

    if replace_existing:
        MatchRecommendation.objects.filter(
            mentee_request=instance, source__in=["openai", "rules"]
        ).delete()

    result, _error = _call_openai(instance, mentors)
    if result and result.get("recs"):
        max_recs = _get_max_int("OPENAI_MAX_RECOMMENDATIONS", 3)
        for rec in result["recs"][:max_recs]:
            mentor = Mentor.objects.filter(id=rec.get("mentor_id")).first()
            if not mentor:
                continue
            MatchRecommendation.objects.create(
                mentee_request=instance,
                mentor=mentor,
                score=rec.get("score", 0) or 0,
                explanation=rec.get("explanation", ""),
                matched_topics=list(set(instance.topics).intersection(set(mentor.care_areas))),
                availability_overlap=instance.preferred_times,
                rating_score=mentor.average_rating or None,
                response_time_score=(
                    round(50 - (mentor.response_time_minutes or 90) / 3, 2)
                    if mentor.response_time_minutes is not None
                    else None
                ),
                status="suggested",
                source="openai",
                model=result["model"],
                response_id=result["response_id"],
                prompt_hash=result["prompt_hash"],
            )
        return

    scored = score_mentors(instance, mentors)
    max_recs = _get_max_int("OPENAI_MAX_RECOMMENDATIONS", 3)
    for rec in scored[:max_recs]:
        MatchRecommendation.objects.create(
            mentee_request=instance,
            mentor=rec.mentor,
            score=rec.score,
            explanation=rec.explanation,
            matched_topics=rec.matched_topics,
            availability_overlap=rec.availability_overlap,
            rating_score=rec.rating_score,
            response_time_score=rec.response_time_score,
            status="suggested",
            source="rules",
        )


@receiver(post_save, sender=MenteeRequest)
def auto_recommend_on_request(sender, instance: MenteeRequest, created: bool, **kwargs):
    if not created:
        return
    generate_recommendations_for_request(instance)

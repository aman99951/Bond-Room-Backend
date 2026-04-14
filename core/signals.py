import hashlib
import json
import os
import urllib.error
import urllib.request

from django.conf import settings
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .matching_logic import filter_mentors
from .models import (
    MatchRecommendation,
    MenteeRequest,
    Mentor,
    MentorTrainingProgress,
    MentorTrainingQuizAttempt,
)
from .onboarding import sync_mentor_onboarding_training_status


def _get_max_int(env_key: str, default: int) -> int:
    raw = os.environ.get(env_key, "")
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _env_flag(env_key: str, default: bool) -> bool:
    raw = os.environ.get(env_key, "")
    if not raw:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _recommendation_provider() -> str:
    # OPENAI=false switches recommendation generation to OpenRouter.
    return "openai" if _env_flag("OPENAI", True) else "openrouter"


def _extract_json_text(raw_text: str) -> str:
    text = str(raw_text or "").strip()
    if not text:
        return ""
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3 and lines[-1].strip().startswith("```"):
            text = "\n".join(lines[1:-1]).strip()
    if text.startswith("{") and text.endswith("}"):
        return text
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1].strip()
    return text


def _openrouter_message_text(message) -> str:
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                chunk = item.get("text")
                if isinstance(chunk, str) and chunk.strip():
                    parts.append(chunk)
        return "\n".join(parts).strip()
    return ""


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


def _call_openrouter(req: MenteeRequest, mentors):
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
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

    system_prompt = (
        "You are a matching engine. "
        "Use the rules: filter by availability+timezone, score by topic overlap, "
        "boost strong ratings and quick responses, prefer local for in-person. "
        "Return strict JSON only with: "
        "{'recommendations': [{'mentor_id': int, 'score': number, 'explanation': str}]}"
    )

    payload = {
        "model": os.environ.get("OPENROUTER_MODEL", "meta-llama/llama-3.2-3b-instruct"),
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_prompt},
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

    req_headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    def _request_openrouter(request_payload):
        req_data = json.dumps(request_payload).encode("utf-8")
        req_obj = urllib.request.Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=req_data,
            headers=req_headers,
        )
        with urllib.request.urlopen(req_obj, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))

    try:
        body = _request_openrouter(payload)
    except urllib.error.HTTPError as exc:
        body_text = ""
        try:
            body_text = exc.read().decode("utf-8", errors="ignore")
        except Exception:
            body_text = ""
        # Some models reject response_format=json_object. Retry once without it.
        if exc.code == 400 and "response_format" in body_text:
            retry_payload = dict(payload)
            retry_payload.pop("response_format", None)
            try:
                body = _request_openrouter(retry_payload)
            except Exception as retry_exc:  # pragma: no cover - runtime/network failures
                return None, str(retry_exc)
        else:
            return None, f"HTTP{exc.code}:{body_text[:240]}".strip()
    except Exception as exc:  # pragma: no cover - runtime/network failures
        return None, str(exc)

    try:
        response_id = body.get("id", "")
        choices = body.get("choices") or []
        message = (choices[0] or {}).get("message", {}) if choices else {}
        output_text = _openrouter_message_text(message)
        json_text = _extract_json_text(output_text)
        if not json_text:
            return None, "empty_response_text"
        try:
            result = json.loads(json_text)
        except json.JSONDecodeError:
            snippet = json_text[:240].replace("\n", " ").strip()
            return None, f"non_json_response:{snippet}"
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
        return {
            "generated": False,
            "count": 0,
            "reason_code": "auto_match_disabled_or_safety_flagged",
            "detail": "Auto-match is disabled or the request is safety-flagged.",
        }

    if replace_existing:
        MatchRecommendation.objects.filter(
            mentee_request=instance, source__in=["openai", "openrouter", "rules"]
        ).delete()

    max_mentors = _get_max_int("OPENAI_MAX_MENTORS", 0)
    mentor_qs = Mentor.objects.filter(onboarding_status__current_status="completed")
    mentor_pool = list(mentor_qs[:max_mentors]) if max_mentors > 0 else list(mentor_qs[:25])
    if not mentor_pool:
        return {
            "generated": False,
            "count": 0,
            "reason_code": "no_completed_mentors",
            "detail": "No completed mentors are currently available for recommendations.",
        }
    mentors = filter_mentors(instance, mentor_pool)
    if not mentors:
        mentors = mentor_pool

    eligible_mentors_by_id = {mentor.id: mentor for mentor in mentors}

    provider = _recommendation_provider()
    call_fn = _call_openai if provider == "openai" else _call_openrouter
    result, _error = call_fn(instance, mentors)
    created_count = 0
    if result and result.get("recs"):
        max_recs_env = "OPENAI_MAX_RECOMMENDATIONS" if provider == "openai" else "OPENROUTER_MAX_RECOMMENDATIONS"
        max_recs = _get_max_int(max_recs_env, 3)
        for rec in result["recs"][:max_recs]:
            mentor = eligible_mentors_by_id.get(rec.get("mentor_id"))
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
                source=provider,
                model=result["model"],
                response_id=result["response_id"],
                prompt_hash=result["prompt_hash"],
            )
            created_count += 1
        if created_count > 0:
            return {
                "generated": True,
                "count": created_count,
                "reason_code": f"{provider}_success",
                "detail": "",
                "source": provider,
            }
        return {
            "generated": False,
            "count": 0,
            "reason_code": f"{provider}_no_valid_mentor_match",
            "detail": f"{provider.title()} returned recommendations, but none matched eligible mentors.",
            "source": provider,
        }

    # If provider fails or returns no valid rows, keep recommendations empty.
    error_text = str(_error or "").strip()
    reason_code = f"{provider}_empty_response"
    provider_name = "OpenAI" if provider == "openai" else "OpenRouter"
    api_key_env = "OPENAI_API_KEY" if provider == "openai" else "OPENROUTER_API_KEY"
    detail = f"{provider_name} returned an empty recommendation response."
    if not error_text:
        pass
    elif "429" in error_text:
        reason_code = f"{provider}_rate_limited"
        detail = f"{provider_name} rate limit or quota exceeded (HTTP 429)."
    elif "401" in error_text or "403" in error_text:
        reason_code = f"{provider}_auth_failed"
        detail = f"{provider_name} authentication failed. Check API key/project permissions."
    elif "missing_api_key" in error_text:
        reason_code = f"{provider}_api_key_missing"
        detail = f"{api_key_env} is missing on backend."
    else:
        reason_code = f"{provider}_request_failed"
        detail = error_text

    return {
        "generated": False,
        "count": 0,
        "reason_code": reason_code,
        "detail": detail,
        "source": provider,
    }


@receiver(post_save, sender=MenteeRequest)
def auto_recommend_on_request(sender, instance: MenteeRequest, created: bool, **kwargs):
    if not created:
        return
    generate_recommendations_for_request(instance)


@receiver(post_save, sender=MentorTrainingProgress)
def auto_sync_training_status_on_progress_save(
    sender, instance: MentorTrainingProgress, **kwargs
):
    if kwargs.get("raw"):
        return
    sync_mentor_onboarding_training_status(instance.mentor_id)


@receiver(post_delete, sender=MentorTrainingProgress)
def auto_sync_training_status_on_progress_delete(
    sender, instance: MentorTrainingProgress, **kwargs
):
    sync_mentor_onboarding_training_status(instance.mentor_id)


@receiver(post_save, sender=MentorTrainingQuizAttempt)
def auto_sync_training_status_on_quiz_save(
    sender, instance: MentorTrainingQuizAttempt, **kwargs
):
    if kwargs.get("raw"):
        return
    sync_mentor_onboarding_training_status(instance.mentor_id)


@receiver(post_delete, sender=MentorTrainingQuizAttempt)
def auto_sync_training_status_on_quiz_delete(
    sender, instance: MentorTrainingQuizAttempt, **kwargs
):
    sync_mentor_onboarding_training_status(instance.mentor_id)

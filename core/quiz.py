import json
import os
import random
import re
import urllib.request

from django.conf import settings


def clean_question_text(value):
    text = str(value or "").strip()
    if not text:
        return ""
    text = re.sub(r"^\s*\[\s*q\s*\d+\s*\]\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^\s*q\s*\d+\s*[:\.\-\)]\s*", "", text, flags=re.IGNORECASE)
    return text.strip()


def _module_summary_payload(modules):
    summary = []
    for module in modules:
        outline = module.lesson_outline if isinstance(module.lesson_outline, list) else []
        summary.append(
            {
                "id": module.id,
                "title": module.title,
                "description": module.description or "",
                "outline": [str(item).strip() for item in outline if str(item).strip()],
            }
        )
    return summary


def _fallback_questions(modules, total_questions):
    module_summaries = _module_summary_payload(modules)
    if not module_summaries:
        module_summaries = [
            {"id": 0, "title": "Mentor Training", "description": "General mentoring fundamentals", "outline": []}
        ]

    questions = []
    index = 1
    while len(questions) < total_questions:
        module = module_summaries[(index - 1) % len(module_summaries)]
        outline = module.get("outline") or []
        focus = outline[(index - 1) % len(outline)] if outline else module["title"]
        question = {
            "question": f"In {module['title']}, which statement best reflects {focus}?",
            "options": [
                "Respond with empathy, boundaries, and active listening.",
                "Promise outcomes you cannot guarantee.",
                "Ignore signs of distress to avoid discomfort.",
                "Share private mentee details with others.",
            ],
            "correct_option_index": 0,
            "module_title": module["title"],
        }
        questions.append(question)
        index += 1
    return questions


def _normalize_generated_questions(raw_questions, modules, total_questions):
    normalized = []
    seen = set()
    for item in raw_questions or []:
        question_text = clean_question_text(item.get("question", ""))
        options = item.get("options")
        if not question_text or not isinstance(options, list):
            continue
        options = [str(option).strip() for option in options if str(option).strip()]
        if len(options) < 4:
            continue
        options = options[:4]
        try:
            correct_idx = int(item.get("correct_option_index", 0))
        except (TypeError, ValueError):
            correct_idx = 0
        if correct_idx < 0 or correct_idx >= len(options):
            correct_idx = 0
        dedupe_key = question_text.lower()
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        normalized.append(
            {
                "question": question_text,
                "options": options,
                "correct_option_index": correct_idx,
                "module_title": str(item.get("module_title", "")).strip(),
            }
        )
        if len(normalized) >= total_questions:
            break

    if len(normalized) < total_questions:
        existing_questions = {item["question"].lower() for item in normalized}
        for fallback in _fallback_questions(modules, total_questions * 2):
            if fallback["question"].lower() in existing_questions:
                continue
            normalized.append(fallback)
            if len(normalized) >= total_questions:
                break

    return normalized[:total_questions]


def _generate_questions_with_openai(modules, total_questions):
    api_key = settings.OPENAI_API_KEY
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured.")

    module_payload = _module_summary_payload(modules)
    if not module_payload:
        raise RuntimeError("No training modules available to generate quiz.")

    prompt_payload = {
        "total_questions": total_questions,
        "rules": {
            "question_type": "multiple_choice",
            "options_per_question": 4,
            "difficulty_mix": "easy_medium_hard",
            "language": "English",
            "avoid_trick_questions": True,
        },
        "modules": module_payload,
        "output_schema": {
            "questions": [
                {
                    "question": "string",
                    "options": ["string", "string", "string", "string"],
                    "correct_option_index": 0,
                    "module_title": "string",
                }
            ]
        },
    }

    model = os.environ.get("OPENAI_QUIZ_MODEL") or os.environ.get("OPENAI_MODEL") or "gpt-4o-mini"
    body = {
        "model": model,
        "text": {"format": {"type": "json_object"}},
        "input": [
            {
                "role": "system",
                "content": (
                    "You create mentor training quizzes. Return strict JSON only. "
                    "Create practical, scenario-based MCQs from provided modules."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(prompt_payload),
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

    with urllib.request.urlopen(request, timeout=25) as response:
        payload = json.loads(response.read().decode("utf-8"))

    output_text = payload.get("output_text", "") or ""
    if not output_text:
        parts = []
        for item in payload.get("output", []):
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    parts.append(content.get("text", ""))
        output_text = "".join(parts).strip()

    parsed = json.loads(output_text) if output_text else {}
    return parsed.get("questions", [])


def generate_training_quiz_questions(modules, total_questions=15):
    modules = list(modules)
    try:
        generated = _generate_questions_with_openai(modules, total_questions)
        questions = _normalize_generated_questions(generated, modules, total_questions)
        random.shuffle(questions)
        return questions, "openai"
    except Exception:
        questions = _normalize_generated_questions([], modules, total_questions)
        random.shuffle(questions)
        return questions, "fallback"


def evaluate_quiz_attempt(questions, selected_answers):
    if not isinstance(selected_answers, list):
        raise ValueError("selected_answers must be a list.")

    answers = [None for _ in range(len(questions))]
    for index, value in enumerate(selected_answers[: len(questions)]):
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = None
        answers[index] = parsed if parsed is not None and parsed >= 0 else None

    score = 0
    for idx, question in enumerate(questions):
        correct = question.get("correct_option_index")
        if answers[idx] is not None and answers[idx] == correct:
            score += 1
    return score, answers

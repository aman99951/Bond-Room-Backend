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


def _canonicalize_module_title(raw_title, module_titles, module_index):
    if not module_titles:
        return str(raw_title or "").strip()

    raw = str(raw_title or "").strip()
    if not raw:
        return module_titles[module_index % len(module_titles)]

    raw_key = raw.lower()
    for title in module_titles:
        title_key = title.lower()
        if raw_key == title_key:
            return title
        if raw_key in title_key or title_key in raw_key:
            return title

    return module_titles[module_index % len(module_titles)]


def _normalize_generated_questions(raw_questions, modules, limit=None):
    module_titles = [module["title"] for module in _module_summary_payload(modules)]
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
        module_title = _canonicalize_module_title(
            item.get("module_title", ""),
            module_titles,
            len(normalized),
        )
        normalized.append(
            {
                "question": question_text,
                "options": options,
                "correct_option_index": correct_idx,
                "module_title": module_title,
            }
        )
        if limit and len(normalized) >= limit:
            break
    return normalized


def _select_questions_for_quiz(candidates, modules, total_questions):
    module_titles = [module["title"] for module in _module_summary_payload(modules)]
    selected = []
    used_questions = set()

    # Ensure at least one question per module when quiz length permits.
    if module_titles and total_questions >= len(module_titles):
        for module_title in module_titles:
            module_question = next(
                (
                    item
                    for item in candidates
                    if item.get("module_title") == module_title
                    and item.get("question", "").lower() not in used_questions
                ),
                None,
            )
            if not module_question:
                raise RuntimeError("OpenAI output did not include questions for all modules.")
            selected.append(module_question)
            used_questions.add(module_question.get("question", "").lower())

    for item in candidates:
        question_key = item.get("question", "").lower()
        if question_key in used_questions:
            continue
        selected.append(item)
        used_questions.add(question_key)
        if len(selected) >= total_questions:
            break

    if len(selected) < total_questions:
        raise RuntimeError("OpenAI did not return enough valid quiz questions.")
    return selected[:total_questions]


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
            "exact_question_count": total_questions,
            "difficulty_mix": "easy_medium_hard",
            "language": "English",
            "avoid_trick_questions": True,
            "module_title_must_match_one_of": [item["title"] for item in module_payload],
            "cover_all_modules_when_possible": total_questions >= len(module_payload),
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
                    "Create practical, scenario-based MCQs from provided modules. "
                    "Return exactly the requested number of questions with exactly four options each. "
                    "Use module_title exactly from provided module titles."
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
    if total_questions <= 0:
        raise RuntimeError("total_questions must be greater than zero.")

    all_candidates = []
    seen_questions = set()
    for _ in range(5):
        represented_modules = {item.get("module_title") for item in all_candidates}
        all_module_titles = [module.title for module in modules]
        missing_modules = [title for title in all_module_titles if title not in represented_modules]
        request_modules = [module for module in modules if module.title in missing_modules] or modules
        request_count = max(6, total_questions - len(all_candidates), len(request_modules))

        generated = _generate_questions_with_openai(request_modules, request_count)
        normalized = _normalize_generated_questions(generated, modules)
        for item in normalized:
            question_key = item.get("question", "").lower()
            if question_key in seen_questions:
                continue
            seen_questions.add(question_key)
            all_candidates.append(item)

        try:
            questions = _select_questions_for_quiz(all_candidates, modules, total_questions)
            random.shuffle(questions)
            return questions, "openai"
        except RuntimeError:
            continue

    raise RuntimeError(
        f"OpenAI returned {len(all_candidates)} valid unique questions, required {total_questions}."
    )


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

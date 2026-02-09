from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Tuple

from .models import MenteeRequest, Mentor


@dataclass
class ScoredMentor:
    mentor: Mentor
    score: float
    matched_topics: List[str]
    availability_overlap: List[dict]
    rating_score: float | None
    response_time_score: float | None
    explanation: str


def _to_minutes(hhmm: str) -> int:
    hour, minute = hhmm.split(":")
    return int(hour) * 60 + int(minute)


def _overlap(a_start: str, a_end: str, b_start: str, b_end: str) -> bool:
    return _to_minutes(a_start) < _to_minutes(b_end) and _to_minutes(b_start) < _to_minutes(a_end)


def availability_overlap(a: Iterable[dict], b: Iterable[dict]) -> List[dict]:
    overlaps = []
    for slot_a in a or []:
        for slot_b in b or []:
            if slot_a.get("day") != slot_b.get("day"):
                continue
            if _overlap(slot_a.get("start", "00:00"), slot_a.get("end", "00:00"),
                        slot_b.get("start", "00:00"), slot_b.get("end", "00:00")):
                overlaps.append(slot_a)
                break
    return overlaps


def filter_mentors(req: MenteeRequest, mentors: Iterable[Mentor]) -> List[Mentor]:
    filtered = []
    for mentor in mentors:
        if req.language and req.language not in (mentor.languages or []):
            continue
        if req.preferred_format and req.preferred_format not in (mentor.preferred_formats or []):
            continue
        if req.timezone and mentor.timezone and req.timezone != mentor.timezone:
            continue
        if not availability_overlap(req.preferred_times, mentor.availability):
            continue
        filtered.append(mentor)
    return filtered


def score_mentors(req: MenteeRequest, mentors: Iterable[Mentor]) -> List[ScoredMentor]:
    results: List[ScoredMentor] = []
    for mentor in mentors:
        matched_topics = list(set(req.topics).intersection(set(mentor.care_areas or [])))
        overlap_slots = availability_overlap(req.preferred_times, mentor.availability)

        topic_score = len(matched_topics) * 20
        rating_score = float(mentor.average_rating) if mentor.average_rating is not None else None
        rating_boost = (rating_score - 3.5) * 8 if rating_score else 0
        response_boost = 0
        response_time_score = None
        if mentor.response_time_minutes is not None:
            response_time_score = max(0.0, 50 - (mentor.response_time_minutes / 3.0))
            response_boost = response_time_score * 0.2

        availability_boost = 10 if overlap_slots else 0

        local_boost = 0
        if req.session_mode == "in_person":
            mentee_city = (req.mentee.city_state or "").strip().lower()
            mentor_city = (mentor.city_state or "").strip().lower()
            if mentee_city and mentor_city:
                local_boost = 10 if mentee_city == mentor_city else -10

        score = 40 + topic_score + rating_boost + response_boost + availability_boost + local_boost

        explanation_bits = []
        if matched_topics:
            explanation_bits.append("topic overlap")
        if overlap_slots:
            explanation_bits.append("availability match")
        if rating_score and rating_score >= 4.5:
            explanation_bits.append("strong ratings")
        if mentor.response_time_minutes is not None and mentor.response_time_minutes <= 60:
            explanation_bits.append("quick response")
        if req.session_mode == "in_person" and local_boost > 0:
            explanation_bits.append("local match")

        explanation = "Good fit: " + ", ".join(explanation_bits) if explanation_bits else "Potential fit."

        results.append(
            ScoredMentor(
                mentor=mentor,
                score=round(score, 2),
                matched_topics=matched_topics,
                availability_overlap=overlap_slots,
                rating_score=rating_score,
                response_time_score=round(response_time_score, 2) if response_time_score is not None else None,
                explanation=explanation,
            )
        )

    results.sort(key=lambda r: r.score, reverse=True)
    return results

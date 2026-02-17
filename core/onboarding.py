from django.db.models import Q

from .models import (
    MentorOnboardingStatus,
    MentorTrainingProgress,
    MentorTrainingQuizAttempt,
    TrainingModule,
)


def derive_training_status_from_module_payload(module_payload):
    if not module_payload:
        return "pending"
    if all(item.get("training_status") == "completed" for item in module_payload):
        return "completed"
    if any((item.get("progress_percent") or 0) > 0 for item in module_payload):
        return "in_review"
    return "pending"


def _has_passed_training_quiz(mentor_id):
    return MentorTrainingQuizAttempt.objects.filter(mentor_id=mentor_id, status="passed").exists()


def _derive_training_status_from_db(mentor_id):
    active_module_ids = list(
        TrainingModule.objects.filter(is_active=True).values_list("id", flat=True)
    )
    if not active_module_ids:
        return "pending"

    progress_qs = MentorTrainingProgress.objects.filter(
        mentor_id=mentor_id,
        module_id__in=active_module_ids,
    )
    completed_ids = set(
        progress_qs.filter(Q(status="completed") | Q(progress_percent__gte=100)).values_list(
            "module_id", flat=True
        )
    )
    all_modules_completed = len(completed_ids) == len(active_module_ids)
    if all_modules_completed:
        return "completed" if _has_passed_training_quiz(mentor_id) else "in_review"

    has_started = progress_qs.filter(
        Q(progress_percent__gt=0) | Q(status__in=["in_progress", "completed"])
    ).exists()
    return "in_review" if has_started else "pending"


def sync_mentor_onboarding_training_status(mentor_or_id, module_payload=None):
    mentor_id = getattr(mentor_or_id, "id", mentor_or_id)
    onboarding, _ = MentorOnboardingStatus.objects.get_or_create(mentor_id=mentor_id)
    if module_payload is None:
        next_status = _derive_training_status_from_db(mentor_id)
    else:
        module_status = derive_training_status_from_module_payload(module_payload)
        if module_status == "completed" and not _has_passed_training_quiz(mentor_id):
            next_status = "in_review"
        else:
            next_status = module_status

    if onboarding.training_status != next_status:
        onboarding.training_status = next_status
        onboarding.save(update_fields=["training_status", "updated_at", "current_status"])
    return onboarding

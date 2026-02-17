from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0016_alter_gender_choices"),
    ]

    operations = [
        migrations.CreateModel(
            name="MentorTrainingQuizAttempt",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("total_questions", models.PositiveSmallIntegerField(default=15)),
                ("pass_mark", models.PositiveSmallIntegerField(default=11)),
                ("questions", models.JSONField(blank=True, default=list)),
                ("selected_answers", models.JSONField(blank=True, default=list)),
                ("score", models.PositiveSmallIntegerField(default=0)),
                (
                    "status",
                    models.CharField(
                        choices=[("pending", "Pending"), ("passed", "Passed"), ("failed", "Failed")],
                        default="pending",
                        max_length=20,
                    ),
                ),
                ("started_at", models.DateTimeField(auto_now_add=True)),
                ("submitted_at", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True, null=True)),
                (
                    "mentor",
                    models.ForeignKey(
                        on_delete=models.deletion.CASCADE,
                        related_name="training_quiz_attempts",
                        to="core.mentor",
                    ),
                ),
            ],
            options={
                "ordering": ["-started_at", "-id"],
            },
        ),
    ]

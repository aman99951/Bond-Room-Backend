from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0026_mentoridentityverification_new_proof_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="mentoridentityverification",
            name="document_review_comments",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="mentoridentityverification",
            name="document_review_status",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]

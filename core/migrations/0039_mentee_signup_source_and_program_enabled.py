from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0038_mentee_volunteer_access"),
    ]

    operations = [
        migrations.AddField(
            model_name="mentee",
            name="mentee_program_enabled",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="mentee",
            name="signup_source",
            field=models.CharField(
                choices=[("regular", "Regular"), ("event_flow", "Event Flow")],
                default="regular",
                max_length=20,
            ),
        ),
    ]


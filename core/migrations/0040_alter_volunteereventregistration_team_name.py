from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0039_mentee_signup_source_and_program_enabled"),
    ]

    operations = [
        migrations.AlterField(
            model_name="volunteereventregistration",
            name="team_name",
            field=models.CharField(blank=True, max_length=150),
        ),
    ]


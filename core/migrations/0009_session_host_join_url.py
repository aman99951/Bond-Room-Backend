from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0008_menteepreferences_mentoravailabilityslot_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="session",
            name="host_join_url",
            field=models.URLField(blank=True),
        ),
    ]

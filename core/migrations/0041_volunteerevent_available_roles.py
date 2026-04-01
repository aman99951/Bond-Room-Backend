from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0040_alter_volunteereventregistration_team_name"),
    ]

    operations = [
        migrations.AddField(
            model_name="volunteerevent",
            name="available_roles",
            field=models.JSONField(blank=True, default=list),
        ),
    ]


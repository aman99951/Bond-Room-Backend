from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0037_mentee_city_mentee_country_mentee_postal_code_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="mentee",
            name="volunteer_access",
            field=models.BooleanField(default=False),
        ),
    ]


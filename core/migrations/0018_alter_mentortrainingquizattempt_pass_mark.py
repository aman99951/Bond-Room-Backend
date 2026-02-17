from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0017_mentortrainingquizattempt"),
    ]

    operations = [
        migrations.AlterField(
            model_name="mentortrainingquizattempt",
            name="pass_mark",
            field=models.PositiveSmallIntegerField(default=7),
        ),
    ]

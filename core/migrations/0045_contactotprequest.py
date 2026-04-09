from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0044_volunteerevent_completion_brief_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="ContactOtpRequest",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("channel", models.CharField(choices=[("email", "Email"), ("phone", "Phone")], max_length=16)),
                ("normalized_contact", models.CharField(db_index=True, max_length=320)),
                ("otp_hash", models.CharField(max_length=128)),
                ("expires_at", models.DateTimeField()),
                ("attempts", models.PositiveSmallIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={},
        ),
        migrations.AddConstraint(
            model_name="contactotprequest",
            constraint=models.UniqueConstraint(
                fields=("channel", "normalized_contact"),
                name="uniq_contact_otp_request_channel_contact",
            ),
        ),
    ]

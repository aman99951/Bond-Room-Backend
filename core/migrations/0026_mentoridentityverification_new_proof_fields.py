from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0025_session_mentee_joined_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="mentoridentityverification",
            name="address_proof_document",
            field=models.FileField(blank=True, null=True, upload_to="mentor_verification/address_proof/"),
        ),
        migrations.AddField(
            model_name="mentoridentityverification",
            name="address_proof_number",
            field=models.CharField(blank=True, max_length=32),
        ),
        migrations.AddField(
            model_name="mentoridentityverification",
            name="address_proof_type",
            field=models.CharField(
                blank=True,
                choices=[
                    ("ration_card", "Ration Card"),
                    ("aadhaar", "Aadhaar"),
                    ("passport", "Passport"),
                    ("pan_card", "PAN Card"),
                    ("driving_license", "Driving License"),
                ],
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="mentoridentityverification",
            name="id_proof_document",
            field=models.FileField(blank=True, null=True, upload_to="mentor_verification/id_proof/"),
        ),
        migrations.AddField(
            model_name="mentoridentityverification",
            name="id_proof_number",
            field=models.CharField(blank=True, max_length=32),
        ),
        migrations.AddField(
            model_name="mentoridentityverification",
            name="id_proof_type",
            field=models.CharField(
                blank=True,
                choices=[
                    ("ration_card", "Ration Card"),
                    ("aadhaar", "Aadhaar"),
                    ("passport", "Passport"),
                    ("pan_card", "PAN Card"),
                    ("driving_license", "Driving License"),
                ],
                max_length=32,
            ),
        ),
    ]

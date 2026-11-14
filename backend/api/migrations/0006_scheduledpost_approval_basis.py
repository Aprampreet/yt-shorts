from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('api', '0005_alter_scheduledpost_generated_short')]

    operations = [
        migrations.AddField(
            model_name='scheduledpost',
            name='approval_basis',
            field=models.CharField(default='manual_review', max_length=30),
        ),
    ]

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    """Deleting a connection must not silently cascade away saved analyses."""

    dependencies = [
        ('advisor', '0005_recommendation_all_indexes_applied_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='queryhistory',
            name='connection',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name='queries',
                to='advisor.connection',
            ),
        ),
    ]

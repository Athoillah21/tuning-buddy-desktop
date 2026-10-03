from django.db import migrations, models


class Migration(migrations.Migration):
    """Store every repeat measurement and whether a difference is significant."""

    dependencies = [
        ('advisor', '0006_queryhistory_connection_protect'),
    ]

    operations = [
        migrations.AddField(
            model_name='queryhistory',
            name='original_execution_times',
            field=models.JSONField(blank=True, default=list, null=True,
                                   help_text='Every repeat measurement of the baseline'),
        ),
        migrations.AddField(
            model_name='recommendation',
            name='verdict',
            field=models.CharField(blank=True, default='', max_length=20,
                                   choices=[('faster', 'Faster'), ('slower', 'Slower'),
                                            ('within_noise', 'Within measurement noise'),
                                            ('already_fast', 'Already below the noise floor'),
                                            ('unknown', 'Not measured')],
                                   help_text='Whether the difference is significant'),
        ),
        migrations.AddField(
            model_name='recommendation',
            name='measurement_spread_ms',
            field=models.FloatField(blank=True, null=True,
                                    help_text='Spread between repeated measurements'),
        ),
        migrations.AddField(
            model_name='recommendation',
            name='tested_execution_times',
            field=models.JSONField(blank=True, default=list, null=True,
                                   help_text='Every repeat measurement of the tested query'),
        ),
        migrations.AlterField(
            model_name='queryhistory',
            name='original_execution_time',
            field=models.FloatField(blank=True, null=True,
                                    help_text='Median execution time in milliseconds'),
        ),
    ]

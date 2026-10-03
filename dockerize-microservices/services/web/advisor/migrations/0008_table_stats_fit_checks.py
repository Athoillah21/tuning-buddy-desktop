from django.db import migrations, models


class Migration(migrations.Migration):
    """Table sizes per analysis; result comparison and fit checks per recommendation."""

    dependencies = [
        ('advisor', '0007_measurement_significance'),
    ]

    operations = [
        migrations.AddField(
            model_name='queryhistory',
            name='table_stats',
            field=models.JSONField(blank=True, default=dict, null=True,
                                   help_text='Catalog facts about each table in the query'),
        ),
        migrations.AddField(
            model_name='recommendation',
            name='result_check',
            field=models.CharField(blank=True, default='', max_length=20,
                                   choices=[('same', 'Same results'), ('different', 'Different results'),
                                            ('unchecked', 'Not compared')],
                                   help_text="Whether the rewritten query returns the original's rows"),
        ),
        migrations.AddField(
            model_name='recommendation',
            name='result_check_note',
            field=models.TextField(blank=True, default='', help_text='What the result comparison found'),
        ),
        migrations.AddField(
            model_name='recommendation',
            name='fit_checks',
            field=models.JSONField(blank=True, default=list, null=True,
                                   help_text='Findings about existing indexes, table size and partitioning'),
        ),
        migrations.AddField(
            model_name='recommendation',
            name='index_sizes',
            field=models.JSONField(blank=True, default=list, null=True,
                                   help_text='Measured size of each suggested index'),
        ),
    ]

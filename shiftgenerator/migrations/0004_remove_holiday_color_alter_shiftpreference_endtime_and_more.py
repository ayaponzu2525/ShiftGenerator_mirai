# Generated by Django 5.1.1 on 2024-10-05 08:29

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shiftgenerator', '0003_holiday_color'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='holiday',
            name='color',
        ),
        migrations.AlterField(
            model_name='shiftpreference',
            name='endtime',
            field=models.TimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='shiftpreference',
            name='starttime',
            field=models.TimeField(blank=True, null=True),
        ),
    ]

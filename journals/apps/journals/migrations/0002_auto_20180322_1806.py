# -*- coding: utf-8 -*-
# Generated by Django 1.11.10 on 2018-03-22 18:06
from __future__ import unicode_literals

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('journals', '0001_initial'),
    ]

    operations = [
        migrations.AlterField(
            model_name='journalaboutpage',
            name='journal',
            field=models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to='journals.Journal'),
        ),
    ]

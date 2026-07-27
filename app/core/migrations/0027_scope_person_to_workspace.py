"""Scope the Person enrichment cache per workspace.

Adds a non-nullable ``workspace`` foreign key to Person, replaces the
global unique constraint on ``email`` with a unique constraint on
``(workspace, email)``, and keeps a plain index on ``email`` for lookups.

The ``workspace`` column can be added without a default because migration
0026 deleted all existing (unattributable) Person rows, guaranteeing the
table is empty when this migration runs.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0026_delete_unattributable_person_rows"),
    ]

    operations = [
        migrations.AddField(
            model_name="person",
            name="workspace",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="enriched_people",
                to="core.workspace",
            ),
        ),
        migrations.AlterField(
            model_name="person",
            name="email",
            field=models.EmailField(db_index=True, max_length=254),
        ),
        migrations.AddConstraint(
            model_name="person",
            constraint=models.UniqueConstraint(
                fields=("workspace", "email"),
                name="uniq_person_workspace_email",
            ),
        ),
    ]

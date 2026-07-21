"""Delete Person enrichment cache rows that cannot be attributed to a workspace.

Person rows were historically keyed by email alone, with no workspace
foreign key, so enrichment data fetched under one tenant's Hunter.io
contract (names, LinkedIn profiles, locations - personal data) was served
to every other tenant, and a GDPR erasure request could not be honored
per tenant.

No other model holds a foreign key to Person and the rows record nothing
about which workspace's API key fetched them, so existing rows cannot be
cleanly attributed to a workspace. The defensible GDPR move is to delete
them: this table is purely a cache of Hunter.io responses, and any row
that is still needed will be re-fetched (now workspace-scoped) on the
next webhook for that customer email.

This migration must run before 0027, which adds a non-nullable workspace
foreign key to the (now empty) table.
"""

from django.db import migrations


def delete_unattributable_person_rows(apps, schema_editor):
    """Delete all cached Person rows; they have no workspace attribution."""
    person_model = apps.get_model("core", "Person")
    person_model.objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0025_reencrypt_existing_credentials"),
    ]

    operations = [
        migrations.RunPython(
            delete_unattributable_person_rows,
            migrations.RunPython.noop,
        ),
    ]

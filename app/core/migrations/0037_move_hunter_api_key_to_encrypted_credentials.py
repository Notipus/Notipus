"""Move Hunter.io API keys into the encrypted credentials field.

Hunter.io keys were saved in ``Integration.integration_settings``, a plain
JSON column, even though the connect page tells the user the key is stored
encrypted. They now live in the encrypted ``oauth_credentials`` field; this
migration moves existing keys there and removes the plaintext copy.
"""

from django.db import migrations

HUNTER_INTEGRATION_TYPE = "hunter_enrichment"
API_KEY_FIELD = "api_key"


def move_keys_to_credentials(apps, schema_editor):
    """Move each Hunter.io API key from integration_settings to oauth_credentials.

    Args:
        apps: The historical app registry.
        schema_editor: The schema editor (unused).
    """
    Integration = apps.get_model("core", "Integration")
    for integration in Integration.objects.filter(
        integration_type=HUNTER_INTEGRATION_TYPE
    ):
        settings_blob = dict(integration.integration_settings or {})
        if API_KEY_FIELD not in settings_blob:
            continue
        api_key = settings_blob.pop(API_KEY_FIELD)
        credentials = dict(integration.oauth_credentials or {})
        # A key already in the encrypted field is newer; keep it.
        if api_key and not credentials.get(API_KEY_FIELD):
            credentials[API_KEY_FIELD] = api_key
        integration.integration_settings = settings_blob
        # oauth_credentials is an encrypted field, so it must be assigned
        # through the model rather than with a queryset .update().
        integration.oauth_credentials = credentials
        integration.save(update_fields=["integration_settings", "oauth_credentials"])


def move_keys_back_to_settings(apps, schema_editor):
    """Reverse: put the keys back where the previous code reads them.

    Args:
        apps: The historical app registry.
        schema_editor: The schema editor (unused).
    """
    Integration = apps.get_model("core", "Integration")
    for integration in Integration.objects.filter(
        integration_type=HUNTER_INTEGRATION_TYPE
    ):
        credentials = dict(integration.oauth_credentials or {})
        if API_KEY_FIELD not in credentials:
            continue
        settings_blob = dict(integration.integration_settings or {})
        settings_blob[API_KEY_FIELD] = credentials.pop(API_KEY_FIELD)
        integration.integration_settings = settings_blob
        integration.oauth_credentials = credentials
        integration.save(update_fields=["integration_settings", "oauth_credentials"])


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0036_integration_first_delivery_at"),
    ]

    operations = [
        migrations.RunPython(move_keys_to_credentials, move_keys_back_to_settings),
    ]

"""Delete stored Shopify Admin API tokens.

Notipus no longer calls the Shopify Admin API: webhook subscriptions are
declared in the app configuration, so Shopify delivers events without
any credential being held. Tokens issued under the previous flow are
still sitting in the database, where they remain a live read capability
over each merchant's orders and customers until revoked or expired.

Nothing reads them any more, so they are removed rather than left to
linger. The per-integration webhook secret goes too: app-level
deliveries are verified against the app's client secret.

Integrations stay active - the shop domain is what routes events, and
that is kept.
"""

from django.db import migrations


def purge_tokens(apps, schema_editor):
    """Strip Shopify credentials from every stored integration.

    Args:
        apps: The historical app registry.
        schema_editor: The schema editor (unused).
    """
    Integration = apps.get_model("core", "Integration")
    for integration in Integration.objects.filter(integration_type="shopify"):
        # oauth_credentials and webhook_secret are encrypted fields, so
        # they must be assigned through the model rather than updated in
        # bulk with a queryset .update().
        integration.oauth_credentials = {}
        integration.webhook_secret = ""
        settings_blob = integration.integration_settings or {}
        # Ids of subscriptions created through the old Admin API flow.
        # They are Shopify's to clean up when the app is uninstalled.
        settings_blob.pop("webhook_ids", None)
        integration.integration_settings = settings_blob
        integration.save(
            update_fields=[
                "oauth_credentials",
                "webhook_secret",
                "integration_settings",
            ]
        )


def noop_reverse(apps, schema_editor):
    """Do nothing on reverse.

    The tokens are gone and cannot be reconstructed; a merchant who needs
    one again reconnects.

    Args:
        apps: The historical app registry.
        schema_editor: The schema editor (unused).
    """


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0034_add_onboarding_nudge_sent_at"),
    ]

    operations = [
        migrations.RunPython(purge_tokens, noop_reverse),
    ]

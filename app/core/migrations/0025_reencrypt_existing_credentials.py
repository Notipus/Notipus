"""Re-encrypt existing credential rows in place.

The preceding schema migration (0024) switched ``oauth_credentials`` and
``webhook_secret`` to encrypted fields (ChaCha20-Poly1305), but existing rows
still hold **plaintext** (the ``jsonb``/``varchar`` values cast to ``text``).
The encrypted fields read legacy plaintext transparently (values without the
``pqc1:`` ciphertext prefix), so simply loading and re-saving each row encrypts
it with the primary key.

This iterates rows (not ``.update()``) so the custom fields' ``get_prep_value``
runs and actually encrypts. It is idempotent: re-saving an already-encrypted
row just re-encrypts it. The reverse is a no-op — encrypted values remain
readable, so there is no need to write plaintext back.
"""

from django.db import migrations


def reencrypt_credentials(apps, schema_editor):
    """Load and re-save every Integration / GlobalBillingIntegration row."""
    for model_name in ("Integration", "GlobalBillingIntegration"):
        model = apps.get_model("core", model_name)
        for obj in model.objects.all().iterator():
            # Re-saving runs the encrypted fields' get_prep_value, which
            # encrypts the (transparently-decrypted) plaintext values.
            obj.save(update_fields=["oauth_credentials", "webhook_secret"])


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0024_encrypt_credential_fields"),
    ]

    operations = [
        migrations.RunPython(
            reencrypt_credentials,
            migrations.RunPython.noop,
        ),
    ]

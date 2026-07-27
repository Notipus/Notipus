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

For scalability on large tables the migration is **non-atomic** (``atomic =
False``): each row's ``save()`` commits on its own rather than accumulating one
giant long-running transaction (which would bloat locks / WAL). Because the
re-save is idempotent, a partial run can simply be re-applied. The queryset is
narrowed with ``.only(...)`` to the columns written and streamed with
``.iterator(chunk_size=...)`` to bound memory and DB load.
"""

from django.db import migrations

# Columns loaded and written per row. Must include every field save() writes so
# the encrypted custom fields round-trip correctly under update_fields.
_CREDENTIAL_FIELDS = ("oauth_credentials", "webhook_secret")

# Stream rows in bounded batches rather than materializing whole tables.
_CHUNK_SIZE = 500


def reencrypt_credentials(apps, schema_editor):
    """Load and re-save every Integration / GlobalBillingIntegration row."""
    for model_name in ("Integration", "GlobalBillingIntegration"):
        model = apps.get_model("core", model_name)
        queryset = model.objects.only("id", *_CREDENTIAL_FIELDS)
        for obj in queryset.iterator(chunk_size=_CHUNK_SIZE):
            # Re-saving runs the encrypted fields' get_prep_value, which
            # encrypts the (transparently-decrypted) plaintext values. Commits
            # per row because the migration is non-atomic.
            obj.save(update_fields=list(_CREDENTIAL_FIELDS))


class Migration(migrations.Migration):
    # Non-atomic so each row commits incrementally on large tables.
    atomic = False

    dependencies = [
        ("core", "0024_encrypt_credential_fields"),
    ]

    operations = [
        migrations.RunPython(
            reencrypt_credentials,
            migrations.RunPython.noop,
        ),
    ]

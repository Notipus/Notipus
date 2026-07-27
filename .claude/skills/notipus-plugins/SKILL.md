---
name: notipus-plugins
description: >-
  How the Notipus plugin system works and the step-by-step recipe for adding a
  new source (webhook provider) or destination (notification target) plugin.
  Use this whenever adding/modifying a plugin — e.g. a new destination like
  Telegram/Discord/Teams/email, or a new source like Paddle/LemonSqueezy — or
  when touching plugins/, the PLUGINS setting, plugin registration/discovery,
  Integration credentials, or webhook delivery wiring.
---

# Notipus plugin system

Notipus processes payment webhooks and delivers enriched notifications through a
unified plugin architecture (ADR-001, `docs/adr/001-unified-plugin-architecture.md`).
Everything lives under `app/plugins/`.

## The four plugin types

| Type (`PluginType`) | Subpackage | Base class | Job |
|---|---|---|---|
| `SOURCE` | `plugins/sources/` | `BaseSourcePlugin` | Validate + parse incoming provider webhooks |
| `DESTINATION` | `plugins/destinations/` | `BaseDestinationPlugin` | Format + deliver a notification |
| `ENRICHMENT` | `plugins/enrichment/` | `BaseEnrichmentPlugin` | Enrich company data from a domain |
| `EMAIL_ENRICHMENT` | `plugins/enrichment/` | `BaseEmailEnrichmentPlugin` | Enrich person data from an email |

Every plugin subclasses `BasePlugin` (`app/plugins/base.py`) and must implement the
classmethod `get_metadata() -> PluginMetadata`:

```python
PluginMetadata(
    name="telegram",  # unique id, used as the registry key
    display_name="Telegram",
    version="1.0.0",
    description="...",
    plugin_type=PluginType.DESTINATION,
    capabilities={PluginCapability.RICH_FORMATTING, PluginCapability.ACTIONS},
    priority=100,
)
```

## Discovery and enablement (two independent gates)

1. **Discovery** — `PluginRegistry.discover()` (`app/plugins/registry.py`) *scans the
   module files* in each subpackage and registers every `BasePlugin` subclass it
   finds. Discovery is by file scan, **not** by `__init__.py` exports — a plugin is
   found because its `.py` file exists in the right folder, regardless of `__all__`.
2. **Enablement** — a plugin only runs if it's enabled in `PLUGINS` in
   `app/django_notipus/settings.py`. Add your plugin under the matching type:

   ```python
   PLUGINS = {
       "destination": {"slack": {"enabled": True}, "telegram": {"enabled": True}},
       "source": {"stripe": {"enabled": True}, ...},
       ...
   }
   ```

Resolve a plugin at runtime with `PluginRegistry.instance().get(PluginType.DESTINATION, "telegram")`.

## Credentials

Per-workspace credentials live on the `Integration` model (`app/core/models.py`) in
`oauth_credentials`, which is an **`EncryptedJSONField`** (encrypted at rest, same
dict API as JSON — read/write it like a normal dict; do not encrypt/decrypt yourself).
Each provider is one entry in `Integration.INTEGRATION_TYPES` (e.g.
`"telegram_notifications"`, `"slack_notifications"`, `"chargify"`).

---

# Recipe: add a DESTINATION plugin

Worked example: Telegram (`plugins/destinations/telegram.py`), mirroring Slack
(`plugins/destinations/slack.py`) — Slack is the reference implementation.

1. **Plugin** — `app/plugins/destinations/<name>.py`, subclass `BaseDestinationPlugin`:
   - `get_metadata()` → `PluginMetadata(plugin_type=PluginType.DESTINATION, ...)`.
   - `format(self, n: RichNotification) -> dict` — convert the target-agnostic
     `RichNotification` (`webhooks/models/rich_notification.py`) into the platform's
     payload. Escape/format for that platform here.
   - `send(self, formatted, credentials: dict) -> bool` — deliver it. Raise on
     failure (delivery failures must surface, not be swallowed).
2. **Enable** it in `PLUGINS["destination"]` in settings.
3. **Model + migration** — add `("<name>_notifications", "…")` to
   `Integration.INTEGRATION_TYPES`, then **generate** the migration (never hand-number):
   ```bash
   DEBUG=True PYTHONPATH=app DJANGO_SETTINGS_MODULE=django_notipus.test_settings \
     uv run python app/manage.py makemigrations core --name add_<name>_notifications
   ```
   Verify with `makemigrations --check --dry-run` (must say "No changes detected").
4. **Connect UI** — `app/core/views/integrations/<name>.py` (connect/disconnect/
   test/status views, using helpers from `views/integrations/base.py`:
   `get_user_workspace`, `require_admin_role`, `require_post_method`), export them from
   `views/integrations/__init__.py` and re-export via `core/views/__init__.py`, add
   routes in `app/core/urls.py`, and a `templates/core/<name>_connect.html.j2`
   template. Store creds into `integration.oauth_credentials`.
5. **Wire delivery into BOTH paths** (this is the step most easily missed):
   - **Immediate** — `webhooks/webhook_router.py::_process_immediately`
   - **Delayed** — `webhooks/services/pending_event_queue.py::_send_notification`
   Both build the notification **once** via
   `settings.EVENT_PROCESSOR.build_rich_notification(...)`, then for each configured
   destination call `plugin.format(notification)` + `plugin.send(formatted, creds)`.
   Add a `_get_<name>_credentials(workspace)` helper next to `_get_slack_webhook_url`
   in each file. Attempt every destination; if any fails, surface it (immediate: raise
   → 5xx; delayed: return `False` → orphan-recovery retries) so nothing is silently lost.
6. **Dashboard** — add an entry in `app/core/services/dashboard.py` using
   `"<name>_notifications" in integration_lookup` for the `connected` flag (reuse the
   shared lookup — don't add a fresh `.filter().exists()` query).
7. **Tests** — `tests/test_<name>_destination.py` for `format`/`send`, plus a
   router-level test in `tests/test_webhook_retry_semantics.py` asserting the plugin is
   invoked with the right credentials and that a send failure returns 5xx.

# Recipe: add a SOURCE plugin

1. **Plugin** — `app/plugins/sources/<name>.py`, subclass `BaseSourcePlugin`:
   `get_metadata()`, `validate_webhook(request) -> bool` (HMAC/signature check), and
   `parse_webhook(request) -> dict` (normalize into the standard event dict). Set
   `content_hash` (via `signed_content_hash`) when the provider's event id only travels
   in an unsigned header, so replay-with-fresh-header can't defeat dedup.
2. **Enable** in `PLUGINS["source"]`.
3. **Model + migration** for the new `INTEGRATION_TYPES` entry (same `makemigrations`
   step as above).
4. **Router endpoint** — add the webhook view/route in `webhooks/webhook_router.py` +
   `urls`. If the provider sends complete data in one webhook, add it to
   `_IMMEDIATE_PROCESSING_PROVIDERS`; otherwise it queues for aggregation.
5. **Tests** — signature validation, parsing, and dedup (`tests/test_signed_content_dedup.py`).

---

# Gotchas (learned the hard way)

- **Migrations: never hardcode the number.** Run `makemigrations` so it lands at the
  current tip and depends on the latest migration. A hand-numbered migration collides
  the moment master moves (e.g. two `0018_*` files → conflicting leaves).
- **Two delivery paths.** A destination that's only wired into `_process_immediately`
  silently never fires for Stripe (which goes through the delayed
  `pending_event_queue` path), and vice-versa.
- **Build once, format per destination.** `process_event_rich(target=...)` builds AND
  *stores* the enriched record and formats for one target. For multiple destinations
  use `build_rich_notification(...)` once, then `plugin.format()` per destination —
  otherwise you double-store and double-run enrichment (extra API calls).
- **Discovery ignores `__init__.py`.** Adding your import to `destinations/__init__.py`
  is optional; dropping another plugin's import from it does not unregister that plugin.
- **Credentials are encrypted transparently.** Just read/write
  `integration.oauth_credentials["key"]`; `EncryptedJSONField` handles encryption.
- **CI gate:** `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy app/`, `uv run djlint app/core/templates --check` and `--lint`.

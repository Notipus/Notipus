# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build & Development Commands

```bash
# Package management (uses uv)
uv sync --all-groups                    # Install all dependencies
uv lock --upgrade && uv sync --all-groups  # Update dependencies

# Testing
uv run pytest                           # Run all tests
uv run pytest tests/path/test_file.py   # Run specific file
uv run pytest -k "test_name"            # Run by pattern
uv run pytest --cov=app                 # With coverage

# Code quality
uv run ruff check --fix .               # Lint and auto-fix
uv run ruff format .                    # Format code
uv run mypy app/                        # Type checking
uv run pre-commit run --all-files       # Run all pre-commit hooks

# Django
uv run python app/manage.py migrate     # Run migrations
uv run python app/manage.py runserver   # Start dev server

# Generate migrations (uses SQLite to avoid needing PostgreSQL)
PYTHONPATH=app DJANGO_SETTINGS_MODULE=django_notipus.test_settings uv run python app/manage.py makemigrations core --name migration_name

# Frontend (Tailwind CSS)
bun run build                           # Build CSS and fonts
bun run dev                             # Watch mode
```

## Architecture

**Multi-tenant SaaS** for processing payment webhooks (Stripe, Shopify, Chargify) and delivering enriched Slack notifications.

### Core Flow
```
Webhook → Validation (HMAC) → Parsing → Enrichment (Brandfetch) → Formatting → Slack Delivery
```

### Plugin System (ADR-001)

Unified architecture in `app/plugins/` with auto-discovery. Adding a new plugin is just creating a file in the right directory.

**Type Hierarchy:**
```
BasePlugin (abstract)
├── BaseEnrichmentPlugin    → enrich_domain(domain) -> dict
├── BaseSourcePlugin        → validate_webhook(request), parse_webhook(request)
└── BaseDestinationPlugin   → format(notification), send(formatted, credentials)
```

**Plugin Types:**
- **Sources** (`plugins/sources/`): Webhook providers - validate signatures, parse into normalized events
- **Destinations** (`plugins/destinations/`): Notification targets - format and deliver
- **Enrichment** (`plugins/enrichment/`): Domain data enrichment

**Configuration:** Global enablement via `PLUGINS` setting in Django settings. Source and destination plugins also have per-workspace credentials stored in the `Integration` model.

**Registry:** `PluginRegistry.instance()` singleton with `register()`, `get()`, `get_all()`, and `discover()` methods.

### Key Directories
- `app/core/` - Users, workspaces, integrations, billing, dashboard
- `app/webhooks/` - Event processing, insight detection, notification models
- `app/plugins/` - Extensible plugin architecture
- `app/django_notipus/` - Django settings and URL routing
- `tests/` - pytest test suite

### Models
- **Workspace**: Multi-tenant isolation unit
- **Integration**: Webhook source/destination configs per workspace
- **RichNotification**: Processed notifications with company enrichment
- **Company**: Enriched company data from Brandfetch

## Code Standards

- **Python 3.12+** with type hints on all functions
- **Django 5.x** with Class-Based Views for complex logic, FBVs for simple
- **pytest only** (never unittest), all tests need type hints and docstrings
- **Ruff** for linting/formatting (line length: 88, double quotes)
- **Templates** use `.html.j2` extension (Jinja2)
- **Never edit existing migrations** - always create new ones
- **DRY**: When a pattern is shared across plugins/modules, extract it into a utility function rather than duplicating code

## Testing

Tests run against SQLite via `django_notipus.test_settings`. Run `uv run pytest` frequently. Pre-commit hooks require all tests to pass before commit.

Key test fixtures are in `tests/conftest.py`.

## External Services

- **PostgreSQL 17**: Primary database
- **Redis 7**: Rate limiting, caching, session storage
- **Slack OAuth**: Authentication and notification delivery
- **Stripe Connect**: Payment provider OAuth
- **Brandfetch**: Company data enrichment

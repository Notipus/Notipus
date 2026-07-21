# Notipus — Enriched Slack Notifications for Payment Events

Notipus turns payment and subscription webhooks from **Stripe**, **Shopify**, and **Maxio (formerly Chargify)** into enriched Slack notifications. It's more than a webhook relay: every alert tells you who the customer is, what they're worth, and whether they need attention — company background, contact name and role, lifetime value, tenure, and churn-risk flags, delivered where your team actually looks.

Managed service: [notipus.com](https://notipus.com) · Self-hosting: [see below](#self-hosting)

## Features

- ⚡ **Three payment sources**: Stripe, Shopify, and Maxio (Chargify) webhooks, with signature validation on every request
- 🔗 **One-click setup**: OAuth-based connections for Slack, Stripe (Stripe Connect creates the webhook endpoint for you), and Shopify — no manual token copying
- 💡 **Automatic enrichment**: company data (logo, industry, size) via Brandfetch and person data (name, job title, seniority, LinkedIn) via Hunter.io
- 📊 **Insight detection**: VIP and at-risk flags, lifetime-value milestones ($1k–$100k), trial start/conversion, upgrade and downgrade detection, payment-retry tracking, customer anniversaries
- 🔄 **Noise reduction**: related events are consolidated and deduplicated into a single message instead of five pings
- 🎯 **Full event coverage**: new subscriptions, payment success/failure, refunds, cancellations and reactivations, Shopify orders and fulfillment
- 🎨 **Engaging messages**: Slack Block Kit formatting with action buttons that deep-link back to the originating dashboard

## Self-Hosting

Notipus is source-available and free to self-host. The full stack (Django, PostgreSQL, Redis) runs with Docker — follow the [Installation](#installation) steps below. Prefer not to run it yourself? Use the managed service at [notipus.com](https://notipus.com).

## Tech Stack

- **Backend**: Django 5.x, Python 3.12+
- **Database**: PostgreSQL 17
- **Cache**: Redis 7
- **Package Manager**: [uv](https://docs.astral.sh/uv/) (fast Python package manager from Astral)
- **Linting/Formatting**: [ruff](https://docs.astral.sh/ruff/)
- **Testing**: pytest with pytest-django
- **Containerization**: Docker with docker-compose

## Installation

### Using Docker (Recommended)

1. **Clone the repository**

```bash
git clone git@github.com:notipus/notipus.git
cd notipus
```

2. **Build the Docker images**

```bash
docker-compose build
```

3. **Set environment variables**

The application reads configuration from environment variables. Set these before running:

```bash
export SECRET_DJANGO_KEY=your-secure-secret-key-here
export DEBUG=True

# Notipus billing (for subscription revenue)
export NOTIPUS_STRIPE_SECRET_KEY=sk_test_your_stripe_key
export NOTIPUS_STRIPE_WEBHOOK_SECRET=whsec_your_webhook_secret

# Optional: Override other settings
export DB_PASSWORD=secure_db_password
```

> **Note**: Webhook secrets for customer integrations are managed per-tenant through the web interface.

4. **Run the containers**

```bash
docker-compose up -d
```

5. **Verify the setup**

```bash
docker-compose logs -f
```

Access the application at http://localhost:8000.

6. **Stop the containers**

```bash
docker-compose down
```

### Local Development

1. **Clone the repository**

```bash
git clone git@github.com:Viktopia/notipus.git
cd notipus
```

2. **Install uv** (Python package manager)

```bash
# macOS/Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Or with Homebrew
brew install uv
```

3. **Install dependencies**

```bash
uv sync --all-groups
```

4. **Set environment variables**

```bash
export SECRET_DJANGO_KEY=your-secure-secret-key-here
export DEBUG=True
export DB_NAME=notipus_dev
export DB_USER=postgres
export DB_PASSWORD=postgres
export DB_HOST=localhost
```

5. **Start PostgreSQL and Redis** (using Docker)

```bash
docker-compose up -d db redis
```

6. **Run migrations**

```bash
uv run python app/manage.py migrate
```

7. **Start the development server**

```bash
uv run python app/manage.py runserver
```

## Development

### Install dependencies

```bash
# Install all dependencies (including dev tools)
uv sync --all-groups

# Update all dependencies to latest versions
uv lock --upgrade && uv sync --all-groups
```

### Run tests

```bash
# Run all tests
uv run pytest

# Run with coverage report
uv run pytest --cov=app --cov-report=term-missing

# Run a specific test file
uv run pytest tests/test_webhooks.py

# Run tests matching a pattern
uv run pytest -k "test_shopify"
```

### Code quality

```bash
# Format code
uv run ruff format .

# Run linter
uv run ruff check .

# Auto-fix linting issues
uv run ruff check --fix .

# Type checking
uv run mypy app/
```

### Pre-commit hooks

```bash
# Install pre-commit hooks
uv run pre-commit install

# Run hooks manually on all files
uv run pre-commit run --all-files
```

### Django management commands

```bash
# Create a superuser
uv run python app/manage.py createsuperuser

# Make migrations (requires PostgreSQL running)
uv run python app/manage.py makemigrations

# Make migrations using SQLite (no PostgreSQL needed)
PYTHONPATH=app DJANGO_SETTINGS_MODULE=django_notipus.test_settings uv run python app/manage.py makemigrations core --name your_migration_name

# Run migrations
uv run python app/manage.py migrate

# Collect static files
uv run python app/manage.py collectstatic
```

### Stripe Plan Setup

The `setup_stripe_plans` management command creates Products and Prices in Stripe for your subscription plans, then updates the database with the actual Stripe Price IDs.

```bash
# Preview what will be created (dry-run mode)
uv run python app/manage.py setup_stripe_plans --dry-run

# Create all plans in Stripe
uv run python app/manage.py setup_stripe_plans

# Sync a specific plan only
uv run python app/manage.py setup_stripe_plans --plan basic

# Force recreate prices (useful when updating pricing)
uv run python app/manage.py setup_stripe_plans --force
```

**What it does:**

- Creates a Stripe Product for each paid plan with metadata (plan name, limits, features)
- Creates monthly and yearly recurring Prices with lookup keys (`{plan}_monthly`, `{plan}_yearly`)
- Updates the `Plan` model with the returned Stripe Price IDs
- Skips plans that already have valid Stripe prices (idempotent by default)
- Reuses existing Products if found by metadata

**Options:**

| Option | Description |
|--------|-------------|
| `--dry-run` | Show what would be created without making changes |
| `--force` | Recreate prices even if they already exist |
| `--plan NAME` | Only sync a specific plan (e.g., `basic`, `pro`, `enterprise`) |

**Note:** Free and trial plans (price = $0) are automatically skipped.

## Architecture

The service is built with a modular, multi-tenant architecture that separates concerns and makes it easy to extend:

```
app/
├── core/              # Core application (users, workspaces, billing)
│   ├── models.py          # Workspace, Integration, Company, WorkspaceMember, Plan, etc.
│   ├── services/
│   │   ├── stripe.py      # Stripe billing API client
│   │   ├── shopify.py     # Shopify Admin API client
│   │   ├── enrichment.py  # Domain enrichment orchestration
│   │   ├── dashboard.py   # Dashboard data aggregation
│   │   └── webauthn.py    # Passwordless authentication
│   └── views/
│       └── integrations/  # OAuth integration handlers (Slack, Stripe, Shopify)
├── plugins/           # Unified plugin architecture (see ADR-001)
│   ├── base.py            # BasePlugin, PluginMetadata, PluginType
│   ├── registry.py        # PluginRegistry singleton with auto-discovery
│   ├── enrichment/        # Data enrichment plugins
│   │   ├── base.py            # BaseEnrichmentPlugin
│   │   └── brandfetch.py      # Domain/brand enrichment via Brandfetch API
│   ├── sources/           # Webhook source plugins (payment providers)
│   │   ├── base.py            # BaseSourcePlugin
│   │   ├── stripe.py          # Stripe webhook processor
│   │   ├── shopify.py         # Shopify webhook processor
│   │   └── chargify.py        # Chargify/Maxio webhook processor
│   └── destinations/      # Notification destination plugins
│       ├── base.py            # BaseDestinationPlugin
│       └── slack.py           # Slack Block Kit formatter
├── webhooks/          # Webhook routing and processing
│   ├── webhook_router.py      # Multi-tenant webhook routing
│   ├── services/
│   │   ├── event_processor.py     # Event processing orchestration
│   │   ├── notification_builder.py # RichNotification construction
│   │   ├── insight_detector.py    # Business insight detection
│   │   ├── slack_client.py        # Slack API client
│   │   ├── billing.py             # Notipus subscription billing handler
│   │   ├── rate_limiter.py        # Rate limiting with circuit breaker
│   │   └── database_lookup.py     # Cross-reference lookups (Redis)
│   └── models/
│       ├── notification.py        # Legacy Notification model
│       └── rich_notification.py   # RichNotification with sections/insights
└── django_notipus/    # Django project settings
```

### Multi-Tenant Architecture

Notipus is designed as a multi-tenant SaaS platform where each workspace manages their own integrations:

- **Workspaces**: Each tenant (workspace) has their own webhook endpoints, integrations, and Slack configurations
- **Webhook Endpoints**: Workspace-specific webhooks at `/webhook/customer/{org_uuid}/{provider}/`
- **Integration Storage**: Credentials and settings stored per-workspace in the `Integration` model
- **Isolation**: Each workspace's data is isolated and rate-limited independently

### Plugin Architecture

Notipus uses a unified plugin system (documented in [ADR-001](docs/adr/001-unified-plugin-architecture.md)) that provides a consistent pattern for extending the platform:

**Plugin Types:**

| Type | Purpose | Examples |
|------|---------|----------|
| **Sources** | Receive and validate webhooks from payment providers | Stripe, Shopify, Chargify |
| **Destinations** | Format and deliver notifications | Slack (with Block Kit) |
| **Enrichment** | Enhance data with external information | Brandfetch (company logos/branding) |

**Key Features:**

- **Auto-Discovery**: Plugins are automatically discovered from `app/plugins/` subdirectories
- **Unified Registry**: Single `PluginRegistry` singleton manages all plugin types
- **Consistent Configuration**: All plugins configured via the `PLUGINS` Django setting
- **Type Safety**: Base classes with abstract methods ensure consistent plugin contracts

**Plugin Configuration Example:**

```python
# In settings.py
PLUGINS = {
    "enrichment": {
        "brandfetch": {
            "enabled": True,
            "priority": 100,
            "config": {"api_key": "...", "timeout": 10}
        }
    },
    "sources": {
        "stripe": {"enabled": True},
        "shopify": {"enabled": True},
        "chargify": {"enabled": True},
    },
    "destinations": {
        "slack": {"enabled": True},
    },
}
```

### Authentication

Notipus supports multiple authentication methods:

1. **Slack OAuth (Sign in with Slack)**: Primary authentication method using OpenID Connect
2. **WebAuthn/Passkeys**: Passwordless authentication for enhanced security
3. **Django Sessions**: Traditional session-based auth for API access

### Slack App Configuration

To set up the Slack integration, create a Slack app at [api.slack.com/apps](https://api.slack.com/apps) with the following App Manifest:

```json
{
    "display_information": {
        "name": "Notipus",
        "description": "Smart webhook notifications for payments and subscriptions",
        "background_color": "#4A154B"
    },
    "features": {
        "bot_user": {
            "display_name": "Notipus",
            "always_online": true
        }
    },
    "oauth_config": {
        "redirect_urls": [
            "https://your-domain.com/accounts/slack/login/callback/",
            "https://your-domain.com/api/connect/slack/callback/"
        ],
        "scopes": {
            "bot": [
                "incoming-webhook",
                "chat:write",
                "channels:read"
            ]
        }
    },
    "settings": {
        "org_deploy_enabled": false,
        "socket_mode_enabled": false,
        "token_rotation_enabled": false
    }
}
```

**Required environment variables for Slack:**

- `SLACK_CLIENT_ID`: OAuth client ID from your Slack app
- `SLACK_CLIENT_SECRET`: OAuth client secret from your Slack app
- `SLACK_REDIRECT_URI`: Login callback URL (e.g., `https://your-domain.com/accounts/slack/login/callback/`)
- `SLACK_CONNECT_REDIRECT_URI`: Workspace connect callback URL (e.g., `https://your-domain.com/api/connect/slack/callback/`)

**Scope explanations:**

| Scope | Purpose |
|-------|---------|
| `incoming-webhook` | Send notifications via webhook URL to a selected channel |
| `chat:write` | Post messages to channels the bot is a member of |
| `channels:read` | List public channels so users can select notification destinations |

### Stripe Connect OAuth Configuration

To enable customers to connect their Stripe accounts with one click (automatic webhook setup), configure Stripe Connect:

1. **Enable Stripe Connect** in your [Stripe Dashboard](https://dashboard.stripe.com/settings/connect)
2. **Enable OAuth** at [Connect OAuth settings](https://dashboard.stripe.com/settings/connect/onboarding-options/oauth)
3. **Copy your `client_id`** from the OAuth settings (starts with `ca_`)
4. **Add redirect URI**: `https://your-domain.com/api/connect/stripe/callback/`
5. Choose **Standard accounts** connection type

**Required environment variables for Stripe Connect:**

- `STRIPE_CONNECT_CLIENT_ID`: Platform client ID from Connect settings (starts with `ca_`)
- `STRIPE_CONNECT_REDIRECT_URI`: OAuth callback URL (e.g., `https://your-domain.com/api/connect/stripe/callback/`)
- `BASE_URL`: Base URL of your application (e.g., `https://your-domain.com`)

**How it works:**

1. User clicks "Connect Stripe" on the integrations page
2. User is redirected to Stripe and authorizes the connection
3. Notipus automatically creates a webhook endpoint on the user's Stripe account
4. User is redirected back with everything configured - no manual setup required

**Permissions requested:**

| Scope | Purpose |
|-------|---------|
| `read_write` | Create webhook endpoints on connected accounts |

### Shopify App Configuration

To enable Shopify OAuth integration, you need to create a Shopify app in the [Shopify Partner Dashboard](https://partners.shopify.com/):

1. **Create a new app** in the Partner Dashboard
2. **Configure App URLs**:
   - App URL: `https://your-domain.com`
   - Allowed redirection URLs: `https://your-domain.com/api/connect/shopify/callback/`
3. **Copy credentials** from the app's API credentials page

**Required environment variables:**

| Variable | Description |
|----------|-------------|
| `SHOPIFY_CLIENT_ID` | Client ID from Shopify Partner Dashboard |
| `SHOPIFY_CLIENT_SECRET` | Client secret from Shopify Partner Dashboard |
| `SHOPIFY_REDIRECT_URI` | OAuth callback URL (e.g., `https://your-domain.com/api/connect/shopify/callback/`) |

**Shopify CLI Configuration:**

The app uses `shopify.app.toml` for Shopify CLI configuration. Copy the example file and configure it:

```bash
cp shopify.app.toml.example shopify.app.toml
# Edit shopify.app.toml with your client_id and URLs
```

Then deploy your app configuration to sync with Shopify:

```bash
shopify app deploy
```

**OAuth Scopes:**

| Scope | Purpose |
|-------|---------|
| `read_orders` | Subscribe to order webhooks (`orders/create`, `orders/paid`, etc.) |
| `read_customers` | Subscribe to customer webhooks (`customers/create`, `customers/update`, etc.) |

> **Note:** There is no `write_webhooks` scope in Shopify. Webhook creation permissions are controlled by having the appropriate read scope for the resource you want to subscribe to.

### Domain Enrichment (Brandfetch)

The Brandfetch integration enriches company domains with brand information:

- Automatically fetches company logos, colors, and descriptions
- Caches results in the `Company` model to reduce API calls
- Used to enhance Slack notifications with company branding

### Rate Limiting & Circuit Breaker

The webhook system includes robust rate limiting:

- **Per-Workspace Limits**: Configurable limits based on subscription plan
- **Redis-Backed**: Uses Redis for distributed rate limit tracking
- **Circuit Breaker**: Automatically disables failing integrations to prevent cascading failures
- **Graceful Degradation**: Falls back to in-memory counting if Redis is unavailable

### Caching (Redis)

Redis is used for multiple purposes:

- **Rate Limit Tracking**: Per-workspace request counts with TTL
- **Recent Activity**: Last 7 days of webhook activity for dashboard display
- **Session Cache**: Django session storage (configurable)
- **Circuit Breaker State**: Tracks integration health status

## Contributing

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/amazing-feature`
3. Install dependencies: `uv sync --all-groups`
4. Make your changes and ensure tests pass: `uv run pytest`
5. Format and lint: `uv run ruff format . && uv run ruff check .`
6. Commit your changes: `git commit -m 'Add amazing feature'`
7. Push to the branch: `git push origin feature/amazing-feature`
8. Open a pull request

## Configuration

### Core Environment Variables

**Required for production:**

- `SECRET_DJANGO_KEY`: Django secret key for cryptographic operations
- `DB_PASSWORD`: Database password for PostgreSQL

**Notipus billing (subscription revenue):**

- `STRIPE_SECRET_KEY`: Stripe secret key for processing subscriptions
- `STRIPE_PUBLISHABLE_KEY`: Stripe publishable key for frontend integration
- `STRIPE_API_VERSION`: Stripe API version (default: `2025-12-15.clover`)
- `NOTIPUS_STRIPE_WEBHOOK_SECRET`: Webhook secret for Stripe billing events (from Stripe Dashboard → Webhooks)

> **Important**: Set `NOTIPUS_STRIPE_WEBHOOK_SECRET` before running migrations.
> The migration `0017_add_stripe_billing_integration` creates the required
> `GlobalBillingIntegration` record using this environment variable.

**Stripe Checkout and Portal URLs:**

- `STRIPE_SUCCESS_URL`: Redirect URL after successful checkout (default: `http://localhost:8000/billing/checkout/success/`)
- `STRIPE_CANCEL_URL`: Redirect URL after cancelled checkout (default: `http://localhost:8000/billing/checkout/cancel/`)
- `STRIPE_PORTAL_RETURN_URL`: Return URL from Customer Portal (default: `http://localhost:8000/billing/`)

**Setting up Stripe Plans:**

After configuring your Stripe keys, run the setup command to create Products and Prices in Stripe:

```bash
uv run python app/manage.py setup_stripe_plans
```

This creates Stripe Products/Prices for your plans and updates the database with the Price IDs. See [Stripe Plan Setup](#stripe-plan-setup) for more details.

**Legacy plan ID mapping (optional, prefer using the setup command above):**

- `STRIPE_BASIC_PLAN`: Stripe price ID for basic plan
- `STRIPE_PRO_PLAN`: Stripe price ID for pro plan
- `STRIPE_ENTERPRISE_PLAN`: Stripe price ID for enterprise plan

**Stripe Connect OAuth (for customer integrations):**

- `STRIPE_CONNECT_CLIENT_ID`: Platform client ID from Stripe Connect settings (starts with `ca_`)
- `STRIPE_CONNECT_REDIRECT_URI`: OAuth callback URL (default: `http://localhost:8000/api/connect/stripe/callback/`)
- `BASE_URL`: Base URL for webhook endpoints (default: `http://localhost:8000`)

**Optional overrides:**

- `DEBUG`: Set to "False" for production (defaults to "True")
- `ALLOWED_HOSTS`: Comma-separated list of allowed hostnames
- `DB_NAME`, `DB_USER`, `DB_HOST`, `DB_PORT`: Database connection parameters
- `REDIS_URL`: Redis connection URL

### Per-Tenant Configuration

Customer webhook integrations are configured per-tenant through the web interface. Each workspace manages their own:

- **Stripe**: One-click OAuth connection (automatic webhook setup)
- **Slack**: One-click OAuth connection for notifications
- **Shopify**: OAuth connection with automatic webhook subscription setup
- **Chargify**: Webhook secret configuration

### Webhook Endpoints

**Customer Webhooks** (per-workspace):

- `POST /webhook/customer/{org_uuid}/shopify/` - Shopify order and customer events
- `POST /webhook/customer/{org_uuid}/chargify/` - Chargify/Maxio subscription events
- `POST /webhook/customer/{org_uuid}/stripe/` - Stripe payment events

**Global Webhooks** (Notipus billing):

- `POST /webhook/billing/stripe/` - Notipus subscription billing events

> **Important**: The billing webhook requires a `GlobalBillingIntegration` database record.
> This is created automatically by running migrations. Ensure `NOTIPUS_STRIPE_WEBHOOK_SECRET`
> is set in your environment before running `python manage.py migrate`.

### Supported Events

**Shopify**:

- `orders/paid` - New order payments
- `customers/create`, `customers/update` - Customer lifecycle events

**Chargify/Maxio**:

- `payment_success`, `payment_failure` - Payment outcomes
- `subscription_created`, `subscription_updated` - Subscription lifecycle
- `renewal_success`, `renewal_failure` - Renewal events

**Stripe**:

- `customer.subscription.created` - New subscription created
- `customer.subscription.trial_will_end` - Trial ending notification (3 days before)
- `invoice.payment_succeeded` - Invoice payment successful
- `invoice.payment_failed` - Invoice payment failed
- `invoice.paid` - Invoice paid confirmation
- `invoice.payment_action_required` - Payment requires customer action (3DS)
- `checkout.session.completed` - Checkout completion

### Security Features

- **HMAC Signature Validation**: All webhooks require valid signatures
- **Timestamp Validation**: Prevents replay attacks (Chargify)
- **Production-Only Enforcement**: Webhook validation cannot be bypassed in production
- **Request Timeouts**: All external API calls have configured timeouts

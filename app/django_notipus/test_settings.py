from .settings import *

# Override database to use SQLite for tests
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

# Test-specific environment overrides
SECRET_KEY = "test-secret-key-for-testing-only"
DEBUG = True

# Remove whitenoise middleware for tests (staticfiles directory doesn't exist)
MIDDLEWARE = [m for m in MIDDLEWARE if "whitenoise" not in m]

# Override settings for tests
DISABLE_BILLING = True

# Keep GA4 off in tests regardless of the developer's environment;
# individual tests opt in via override_settings.
GA4_MEASUREMENT_ID = ""
GA4_API_SECRET = ""
# Note: Webhook secrets now managed per-tenant, not globally


# Enable migrations for testing - we need to test migrations on in-memory SQL
# MIGRATION_MODULES removed to enable migrations

# Use console email backend for tests
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# Disable caching for tests
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.dummy.DummyCache",
    }
}

# Speed up password hashing for tests
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.MD5PasswordHasher",
]

# Disable logging during tests
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "null": {
            "class": "logging.NullHandler",
        },
    },
    "root": {
        "handlers": ["null"],
    },
}

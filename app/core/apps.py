from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"
    label = "core"

    def ready(self) -> None:
        """Import signals when the app is ready."""
        from . import signals  # noqa: F401

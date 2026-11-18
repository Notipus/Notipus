"""Django project package; loads the Celery app with Django."""

from .celery import app as celery_app

__all__ = ("celery_app",)

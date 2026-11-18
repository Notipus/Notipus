"""Celery app: background and scheduled jobs.

Run a worker with the embedded beat scheduler (one per deployment, so
beat never runs twice):

    celery -A django_notipus worker -B --loglevel=INFO

The broker is the same Redis the cache uses (``REDIS_URL``).
"""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "django_notipus.settings")

app = Celery("django_notipus")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

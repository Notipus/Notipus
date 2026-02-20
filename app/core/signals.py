from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import (
    NotificationSettings,
    Workspace,
)


@receiver(post_save, sender=Workspace)
def create_workspace_notification_settings(sender, instance, created, **kwargs):
    """Create notification settings when a new workspace is created."""
    if created:
        NotificationSettings.objects.create(workspace=instance)

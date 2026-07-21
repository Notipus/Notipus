from allauth.account.signals import user_signed_up
from django.contrib.auth.signals import user_logged_in
from django.db.models.signals import post_save
from django.dispatch import receiver

from . import analytics
from .models import (
    NotificationSettings,
    Workspace,
)


@receiver(post_save, sender=Workspace)
def create_workspace_notification_settings(sender, instance, created, **kwargs):
    """Create notification settings when a new workspace is created."""
    if created:
        NotificationSettings.objects.create(workspace=instance)


@receiver(user_signed_up)
def track_sign_up(request, user, **kwargs):
    """Send a GA4 sign_up event for allauth signups (email and social).

    Custom flows that bypass allauth (Slack OIDC, passkeys) track their
    own sign_up events at the point where they create the user.
    """
    sociallogin = kwargs.get("sociallogin")
    method = sociallogin.account.provider if sociallogin else "email"
    analytics.track_event(request, "sign_up", {"method": method})


@receiver(user_logged_in)
def track_login(sender, request, user, **kwargs):
    """Send a GA4 login event for every authentication flow.

    Django's user_logged_in signal fires for all flows (allauth, Slack
    OIDC, passkeys); custom flows label themselves via
    analytics.set_login_method before calling login().
    """
    if request is None:
        return
    analytics.track_event(
        request, "login", {"method": analytics.get_login_method(request)}
    )

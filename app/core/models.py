"""Core Django models for the Notipus application.

This module contains all the core domain models including workspaces,
users, integrations, billing, and authentication-related models.
"""

import re
import uuid
from datetime import datetime, timedelta
from typing import Any, ClassVar

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone
from django.utils.text import slugify


def get_trial_end_date() -> datetime:
    """Return trial end date 14 days from now.

    Returns:
        datetime: Trial end date.
    """
    return timezone.now() + timezone.timedelta(days=14)


def get_invitation_expiry() -> datetime:
    """Return invitation expiry date 7 days from now.

    Returns:
        datetime: Invitation expiry date.
    """
    return timezone.now() + timedelta(days=7)


class Workspace(models.Model):
    """A workspace represents a tenant in our multi-tenant SaaS.

    Each workspace has its own integrations, users, and settings.
    Workspaces are the primary billing and access control entity.

    Attributes:
        uuid: Unique identifier for webhook URLs.
        name: Display name of the workspace.
        slug: URL-friendly identifier.
        shop_domain: Associated Shopify domain.
        subscription_plan: Current billing plan.
        subscription_status: Current subscription state.
    """

    STRIPE_PLANS: ClassVar[tuple[tuple[str, str], ...]] = (
        ("free", "Free Plan"),
        ("basic", "Basic Plan - $29/month"),
        ("pro", "Pro Plan - $99/month"),
        ("enterprise", "Enterprise Plan - $299/month"),
    )

    STATUS_CHOICES: ClassVar[tuple[tuple[str, str], ...]] = (
        ("active", "Active"),
        ("trial", "Trial"),
        ("suspended", "Suspended"),
        ("cancelled", "Cancelled"),
    )

    # Basic fields
    uuid = models.UUIDField(
        default=uuid.uuid4, editable=False, unique=True, db_index=True
    )
    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=200, unique=True, blank=True)
    shop_domain = models.CharField(max_length=255, unique=True, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Billing and subscription
    # max_length=100 to accommodate Stripe price IDs (e.g., price_1NvT4OJADkUcvxXxx)
    subscription_plan = models.CharField(
        max_length=100, choices=STRIPE_PLANS, default="free"
    )
    subscription_status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default="active"
    )
    trial_end_date = models.DateTimeField(default=get_trial_end_date)
    billing_cycle_anchor = models.IntegerField(null=True, blank=True)
    stripe_customer_id = models.CharField(max_length=255, blank=True, default="")
    payment_method_added = models.BooleanField(default=False)

    class Meta:
        app_label = "core"
        db_table = "core_organization"  # Keep existing table name

    def __str__(self) -> str:
        """Return string representation of the workspace.

        Returns:
            Workspace name and shop domain.
        """
        return f"{self.name} ({self.shop_domain})"

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Save the workspace, generating a unique slug if needed.

        Args:
            *args: Positional arguments for parent save method.
            **kwargs: Keyword arguments for parent save method.

        Raises:
            ValidationError: If model validation fails.
        """
        if not self.slug:
            self._generate_unique_slug()
        self.full_clean()
        super().save(*args, **kwargs)

    def clean(self) -> None:
        """Validate subscription state combinations.

        Raises:
            ValidationError: If free plan has trial status.
        """
        super().clean()
        if self.subscription_plan == "free" and self.subscription_status == "trial":
            raise ValidationError(
                "Free plan cannot have trial status. "
                "Use 'active' status for free plans."
            )

    def _generate_unique_slug(self) -> None:
        """Generate a unique slug in a race-condition-safe manner.

        Uses atomic transactions and select_for_update to prevent
        duplicate slugs in concurrent requests.
        """
        base_slug = slugify(self.name)
        slug = base_slug
        counter = 1

        # Use atomic transaction to prevent race conditions
        with transaction.atomic():
            while True:
                try:
                    # Try to save with current slug by using select_for_update
                    # to lock potential conflicting records
                    existing = (
                        Workspace.objects.select_for_update()
                        .filter(slug=slug)
                        .exclude(pk=self.pk)
                        .first()
                    )

                    if not existing:
                        self.slug = slug
                        break

                    # Slug exists, try next variation
                    slug = f"{base_slug}-{counter}"
                    counter += 1

                    # Prevent infinite loops
                    if counter > 1000:
                        # Fallback to UUID if we can't find a unique slug
                        self.slug = f"{base_slug}-{uuid.uuid4().hex[:8]}"
                        break

                except Exception:
                    # If there's any database error, use UUID fallback
                    self.slug = f"{base_slug}-{uuid.uuid4().hex[:8]}"
                    break

    @property
    def webhook_token(self) -> str:
        """Return UUID as webhook token for URL obfuscation.

        Returns:
            String representation of the UUID.
        """
        return str(self.uuid)

    @property
    def is_trial(self) -> bool:
        """Check if workspace is on trial.

        Returns:
            True if on trial, False otherwise.
        """
        return self.subscription_status == "trial"

    @property
    def is_active(self) -> bool:
        """Check if workspace is active.

        Returns:
            True if active or on trial, False otherwise.
        """
        return self.subscription_status in ["active", "trial"]


class WorkspaceMember(models.Model):
    """Junction table for workspace membership with roles.

    A user can belong to multiple workspaces with different roles.
    This enables multi-workspace membership for enterprise users.

    Attributes:
        user: The Django user.
        workspace: The workspace they belong to.
        role: Their role within the workspace (owner, admin, user).
        is_active: Whether membership is currently active.
    """

    ROLE_CHOICES: ClassVar[tuple[tuple[str, str], ...]] = (
        ("owner", "Owner"),
        ("admin", "Admin"),
        ("user", "User"),
    )

    user = models.ForeignKey(User, on_delete=models.CASCADE)
    workspace = models.ForeignKey(
        Workspace, on_delete=models.CASCADE, related_name="members"
    )
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default="user")
    joined_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        app_label = "core"
        db_table = "core_organizationuser"  # Keep existing table name
        unique_together = ("user", "workspace")

    def __str__(self) -> str:
        """Return string representation of the membership.

        Returns:
            Username, workspace name, and role.
        """
        return f"{self.user.username} - {self.workspace.name} ({self.role})"

    @property
    def is_owner(self) -> bool:
        """Check if member is an owner."""
        return self.role == "owner"

    @property
    def is_admin(self) -> bool:
        """Check if member is an admin or owner."""
        return self.role in ("owner", "admin")


class WorkspaceInvitation(models.Model):
    """Invitation to join a workspace.

    Allows workspace admins/owners to invite users by email.
    Invitations expire after 7 days.

    Attributes:
        workspace: The workspace being invited to.
        email: Email address of the invitee.
        role: Role the invitee will have upon accepting.
        token: Unique token for the invitation URL.
        invited_by: User who sent the invitation.
        expires_at: When the invitation expires.
        accepted_at: When the invitation was accepted (null if pending).
    """

    workspace = models.ForeignKey(
        Workspace, on_delete=models.CASCADE, related_name="invitations"
    )
    email = models.EmailField(db_index=True)  # Indexed for faster invitation lookups
    role = models.CharField(
        max_length=20, choices=WorkspaceMember.ROLE_CHOICES, default="user"
    )
    token = models.UUIDField(default=uuid.uuid4, unique=True, db_index=True)
    invited_by = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="sent_invitations"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(default=get_invitation_expiry)
    accepted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        app_label = "core"
        db_table = "core_workspaceinvitation"

    def __str__(self) -> str:
        """Return string representation of the invitation.

        Returns:
            Email and workspace name.
        """
        return f"Invitation for {self.email} to {self.workspace.name}"

    @property
    def is_expired(self) -> bool:
        """Check if invitation has expired."""
        return timezone.now() > self.expires_at

    @property
    def is_pending(self) -> bool:
        """Check if invitation is still pending (not accepted and not expired)."""
        return self.accepted_at is None and not self.is_expired


class Integration(models.Model):
    """Integrations for workspaces.

    Supports both customer payment providers and workspace-specific
    notification integrations. Each workspace can have one of each type.

    Attributes:
        workspace: The owning workspace.
        integration_type: Type of integration (stripe, shopify, etc.).
        oauth_credentials: OAuth tokens and credentials.
        webhook_secret: Secret for webhook validation.
        is_active: Whether the integration is currently enabled.
    """

    INTEGRATION_TYPES: ClassVar[tuple[tuple[str, str], ...]] = (
        # Customer payment providers (workspace-specific)
        ("stripe_customer", "Stripe Customer Payments"),
        ("shopify", "Shopify Ecommerce"),
        ("chargify", "Chargify / Maxio Advanced Billing"),
        # Support integrations (workspace-specific)
        ("zendesk", "Zendesk Support"),
        # Notification integrations (workspace-specific)
        ("slack_notifications", "Slack Notifications"),
        # Enrichment integrations (workspace-specific, user-provided API keys)
        ("hunter_enrichment", "Hunter.io Email Enrichment"),
    )

    workspace = models.ForeignKey(
        Workspace, on_delete=models.CASCADE, related_name="integrations"
    )
    integration_type = models.CharField(max_length=50, choices=INTEGRATION_TYPES)

    # OAuth and authentication data
    oauth_credentials = models.JSONField(default=dict, blank=True)
    webhook_secret = models.CharField(max_length=255, blank=True)

    # Integration-specific settings
    integration_settings = models.JSONField(default=dict, blank=True)

    # Status
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Legacy field for backward compatibility
    auth_data = models.JSONField(default=dict, blank=True)

    class Meta:
        app_label = "core"
        unique_together = ["workspace", "integration_type"]
        # Keep existing table - the column rename will be handled by migration
        db_table = "core_integration"

    def __str__(self) -> str:
        """Return string representation of the integration.

        Returns:
            Workspace name and integration type display.
        """
        return f"{self.workspace.name} - {self.get_integration_type_display()}"

    @property
    def slack_team_id(self) -> str | None:
        """Get Slack team ID from OAuth credentials.

        Returns:
            Slack team ID or None if not available.
        """
        return self.oauth_credentials.get("team", {}).get("id")

    @property
    def slack_channel(self) -> str:
        """Get Slack channel from integration settings.

        Returns:
            Slack channel name, defaults to #general.
        """
        return self.integration_settings.get("channel", "#general")

    @property
    def slack_bot_token(self) -> str | None:
        """Get Slack bot token from OAuth credentials.

        Returns:
            Slack bot token or None if not available.
        """
        return self.oauth_credentials.get("access_token")


class GlobalBillingIntegration(models.Model):
    """Global integrations for Notipus's own billing and authentication.

    These are not tied to any specific organization and are used for
    platform-wide functionality like Stripe billing for Notipus itself.

    Attributes:
        integration_type: Type of global integration.
        oauth_credentials: OAuth tokens and credentials.
        webhook_secret: Secret for webhook validation.
        is_active: Whether the integration is enabled.
    """

    INTEGRATION_TYPES: ClassVar[tuple[tuple[str, str], ...]] = (
        ("stripe_billing", "Stripe Billing (Notipus Revenue)"),
        ("slack_auth", "Slack Authentication (Global)"),
    )

    integration_type = models.CharField(
        max_length=50, choices=INTEGRATION_TYPES, unique=True
    )

    # OAuth and authentication data
    oauth_credentials = models.JSONField(default=dict, blank=True)
    webhook_secret = models.CharField(max_length=255, blank=True)

    # Integration-specific settings
    integration_settings = models.JSONField(default=dict, blank=True)

    # Status
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Global Billing Integration"
        verbose_name_plural = "Global Billing Integrations"
        ordering = ["integration_type"]

    def __str__(self) -> str:
        """Return string representation of the global integration.

        Returns:
            Integration type display name.
        """
        return f"Global {self.get_integration_type_display()}"


def validate_domain(value: str) -> str:
    """Validate domain format and return cleaned domain.

    Removes protocol prefixes and validates the domain format
    against a standard domain name pattern.

    Args:
        value: Raw domain input string.

    Returns:
        Cleaned and validated domain string.

    Raises:
        ValidationError: If domain format is invalid.
    """
    # Remove protocol if present and clean
    domain = (
        value.replace("https://", "").replace("http://", "").replace("www.", "").lower()
    )

    # Validate format - must have at least one dot for TLD
    domain_pattern = re.compile(
        r"^[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
        r"(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)+$"
    )

    if not domain_pattern.match(domain):
        raise ValidationError("Enter a valid domain name.")

    return domain


class Company(models.Model):
    """Company model for storing enriched brand/company data.

    Used by DomainEnrichmentService to cache brand information
    retrieved from enrichment providers like Brandfetch.

    Attributes:
        name: Company display name.
        domain: Unique domain identifier.
        logo_url: Original external URL to company logo (for reference).
        logo_data: Binary logo data stored in database.
        logo_content_type: MIME type of the stored logo.
        brand_info: JSON blob with additional brand data.
        created_at: When the record was created.
        updated_at: When the record was last updated.
    """

    name = models.CharField(max_length=255, blank=True, default="")
    domain = models.CharField(max_length=255, unique=True, validators=[validate_domain])
    logo_url = models.URLField(max_length=500, blank=True, default="")
    logo_data = models.BinaryField(blank=True, null=True)
    logo_content_type = models.CharField(max_length=50, blank=True, default="")
    brand_info = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "core"
        verbose_name_plural = "Companies"
        indexes = [
            # Index on name for search queries
            models.Index(fields=["name"], name="company_name_idx"),
            # Index on created_at for date filtering and date_hierarchy in admin
            models.Index(fields=["created_at"], name="company_created_at_idx"),
            # Index on updated_at for sorting by recent updates
            models.Index(fields=["-updated_at"], name="company_updated_at_idx"),
            # Composite index for common query pattern: has logo + recent
            models.Index(
                fields=["logo_content_type", "-updated_at"],
                name="company_logo_updated_idx",
            ),
        ]

    def __str__(self) -> str:
        """Return string representation of the company.

        Returns:
            Company name (or domain) with domain in parentheses.
        """
        display_name = self.name or self.domain
        return f"{display_name} ({self.domain})"

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Save the company after validation.

        Args:
            *args: Positional arguments for parent save method.
            **kwargs: Keyword arguments for parent save method.
        """
        self.full_clean()  # This calls clean() and validators
        super().save(*args, **kwargs)

    def clean(self) -> None:
        """Clean and validate model fields.

        Ensures domain is properly formatted and validated.
        """
        # Clean and validate domain
        if self.domain:
            self.domain = validate_domain(self.domain)

    @property
    def has_logo(self) -> bool:
        """Check if company has a stored logo."""
        return bool(self.logo_data)

    def get_logo_url(self, request=None, absolute: bool = True) -> str:
        """Get URL to serve the logo.

        Args:
            request: Optional request object for building absolute URL.
            absolute: If True, returns absolute URL using BASE_URL setting.
                     Required for Slack to fetch logos externally.

        Returns:
            URL to the logo endpoint, or empty string if no logo.
        """
        if not self.logo_data:
            return ""
        from django.conf import settings
        from django.urls import reverse

        url = reverse("core:company-logo", kwargs={"domain": self.domain})

        if request:
            return request.build_absolute_uri(url)

        if absolute:
            # Use BASE_URL setting for absolute URLs (needed for Slack)
            base_url = getattr(settings, "BASE_URL", "").rstrip("/")
            if base_url:
                return f"{base_url}{url}"

        return url


class Person(models.Model):
    """Person model for storing enriched contact/person data from Hunter.io.

    Used by EmailEnrichmentService to cache person information
    retrieved from Hunter.io's email enrichment API.

    This data is only collected when:
    1. The workspace is on Pro or Enterprise plan
    2. The workspace has configured their own Hunter.io API key
    3. Hunter.io returns data for the email address

    Note: Hunter.io respects GDPR - emails where the person has requested
    data removal return a 451 status and are not enriched.

    Attributes:
        email: Unique email identifier (indexed for lookups).
        first_name: Person's first/given name.
        last_name: Person's last/family name.
        position: Job title (e.g., "VP of Engineering").
        seniority: Seniority level (e.g., "senior", "executive").
        company_domain: Company domain from employment data.
        linkedin_url: LinkedIn profile URL.
        twitter_handle: Twitter/X handle (without @).
        github_handle: GitHub username.
        location: Location string (e.g., "San Francisco, CA").
        hunter_data: Full Hunter.io API response for reference.
        created_at: When the record was created.
        updated_at: When the record was last updated.
    """

    email = models.EmailField(unique=True, db_index=True)
    first_name = models.CharField(max_length=100, blank=True, default="")
    last_name = models.CharField(max_length=100, blank=True, default="")
    position = models.CharField(max_length=255, blank=True, default="")
    seniority = models.CharField(max_length=50, blank=True, default="")
    company_domain = models.CharField(max_length=255, blank=True, default="")
    linkedin_url = models.URLField(max_length=500, blank=True, default="")
    twitter_handle = models.CharField(max_length=100, blank=True, default="")
    github_handle = models.CharField(max_length=100, blank=True, default="")
    location = models.CharField(max_length=255, blank=True, default="")
    hunter_data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "core"
        verbose_name_plural = "People"
        indexes = [
            models.Index(fields=["created_at"], name="person_created_at_idx"),
            models.Index(fields=["-updated_at"], name="person_updated_at_idx"),
        ]

    def __str__(self) -> str:
        """Return string representation of the person.

        Returns:
            Person's full name or email.
        """
        if self.first_name or self.last_name:
            return f"{self.first_name} {self.last_name}".strip()
        return self.email

    @property
    def full_name(self) -> str | None:
        """Get the person's full name if available."""
        if self.first_name or self.last_name:
            return f"{self.first_name} {self.last_name}".strip()
        return None

    @property
    def has_enrichment(self) -> bool:
        """Check if person has enrichment data from Hunter.io."""
        return bool(self.hunter_data.get("_enriched_at"))


class UsageLimit(models.Model):
    """Usage limits per subscription plan.

    Defines the monthly limits for registrations and notifications
    for each subscription plan tier.

    Attributes:
        plan: The subscription plan name.
        max_monthly_registrations: Maximum registrations per month.
        max_monthly_notifications: Maximum notifications per month.
    """

    plan = models.CharField(max_length=20, choices=Workspace.STRIPE_PLANS)
    max_monthly_registrations = models.IntegerField()
    max_monthly_notifications = models.IntegerField()

    def __str__(self) -> str:
        """Return string representation of the usage limit.

        Returns:
            Plan name and registration limit.
        """
        return (
            f"{self.get_plan_display()} - "
            f"{self.max_monthly_registrations} registrations"
        )


class UserProfile(models.Model):
    """Extended user profile with workspace membership.

    Links Django users to their primary workspace and
    stores Slack integration data.

    Note: This model is being deprecated in favor of WorkspaceMember.
    It is kept for backward compatibility with slack_user_id storage.

    Attributes:
        user: The associated Django user.
        slack_user_id: Slack user identifier.
        workspace: Primary workspace membership (legacy).
    """

    user = models.OneToOneField(User, on_delete=models.CASCADE)
    slack_user_id = models.CharField(max_length=255, unique=True, null=True, blank=True)
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE)

    class Meta:
        app_label = "core"
        db_table = "core_userprofile"

    def __str__(self) -> str:
        """Return string representation of the user profile.

        Returns:
            Username and workspace name.
        """
        return f"{self.user.username} ({self.workspace.name})"


class NotificationSettings(models.Model):
    """Notification preferences for a workspace.

    Allows workspaces to customize which event types
    generate Slack notifications.

    Attributes:
        workspace: The workspace these settings belong to.
        notify_*: Boolean flags for each notification type.
    """

    workspace = models.OneToOneField(
        Workspace, on_delete=models.CASCADE, related_name="notification_settings"
    )

    # Payment events
    notify_payment_success = models.BooleanField(default=True)
    notify_payment_failure = models.BooleanField(default=True)

    # Subscription events
    notify_subscription_created = models.BooleanField(default=True)
    notify_subscription_updated = models.BooleanField(default=True)
    notify_subscription_canceled = models.BooleanField(default=True)

    # Trial events
    notify_trial_ending = models.BooleanField(default=True)
    notify_trial_expired = models.BooleanField(default=True)

    # Customer events
    notify_customer_updated = models.BooleanField(default=True)
    notify_signups = models.BooleanField(default=True)

    # Shopify events
    notify_shopify_order_created = models.BooleanField(default=True)
    notify_shopify_order_updated = models.BooleanField(default=True)
    notify_shopify_order_paid = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "core"
        db_table = "core_notificationsettings"

    def __str__(self) -> str:
        """Return string representation of notification settings.

        Returns:
            Workspace name with settings label.
        """
        return f"Notification Settings for {self.workspace.name}"


class Plan(models.Model):
    """Plan definitions with usage limits for organizations.

    Defines available subscription plans with their pricing,
    limits, and feature sets.

    Attributes:
        name: Internal plan identifier.
        display_name: Human-readable plan name.
        price_monthly: Monthly price in USD.
        max_users: Maximum users allowed.
        max_integrations: Maximum integrations allowed.
        features: List of included features.
    """

    name = models.CharField(max_length=50, unique=True)
    display_name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    price_monthly = models.DecimalField(max_digits=10, decimal_places=2)
    price_yearly = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )

    # Limits
    max_users = models.IntegerField(default=1)
    max_integrations = models.IntegerField(default=1)
    max_monthly_notifications = models.IntegerField(default=1000)

    # Features
    features = models.JSONField(default=list)

    # Stripe integration
    stripe_price_id_monthly = models.CharField(max_length=100, blank=True)
    stripe_price_id_yearly = models.CharField(max_length=100, blank=True)

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "core"

    def __str__(self) -> str:
        """Return string representation of the plan.

        Returns:
            Plan display name.
        """
        return self.display_name


class WebAuthnCredential(models.Model):
    """Store WebAuthn credentials for passwordless authentication.

    Stores the public key and metadata for registered passkeys,
    enabling passwordless login via WebAuthn/FIDO2.

    Attributes:
        user: The user who owns this credential.
        credential_id: Base64-encoded credential ID.
        public_key: Base64-encoded public key.
        sign_count: Counter to detect credential cloning.
        name: User-friendly name for the credential.
    """

    user = models.ForeignKey(
        "auth.User", on_delete=models.CASCADE, related_name="webauthn_credentials"
    )

    # WebAuthn credential data
    credential_id = models.TextField(unique=True)  # Base64 encoded credential ID
    public_key = models.TextField()  # Base64 encoded public key
    sign_count = models.BigIntegerField(default=0)  # Authentication counter

    # Credential metadata
    name = models.CharField(
        max_length=100, help_text="User-friendly name for this credential"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    last_used = models.DateTimeField(null=True, blank=True)

    # Device info (optional)
    user_agent = models.TextField(blank=True)

    class Meta:
        app_label = "core"
        verbose_name = "WebAuthn Credential"
        verbose_name_plural = "WebAuthn Credentials"

    def __str__(self) -> str:
        """Return string representation of the credential.

        Returns:
            Username and credential name.
        """
        return f"{self.user.username} - {self.name}"


class WebAuthnChallenge(models.Model):
    """Temporary storage for WebAuthn challenges during authentication flow.

    Stores challenges for both registration and authentication flows,
    with automatic cleanup of expired challenges.

    Attributes:
        challenge: Base64-encoded challenge string.
        user: Associated user (null for registration challenges).
        challenge_type: Type of challenge (registration or authentication).
    """

    challenge = models.CharField(max_length=255, unique=True)  # Base64 encoded
    user = models.ForeignKey(
        "auth.User", on_delete=models.CASCADE, null=True, blank=True
    )  # Null for registration challenges
    created_at = models.DateTimeField(auto_now_add=True)

    # Challenge type
    CHALLENGE_TYPES: ClassVar[tuple[tuple[str, str], ...]] = (
        ("registration", "Registration"),
        ("authentication", "Authentication"),
        ("signup_registration", "Signup Registration"),
    )
    challenge_type = models.CharField(max_length=20, choices=CHALLENGE_TYPES)

    class Meta:
        app_label = "core"
        verbose_name = "WebAuthn Challenge"
        verbose_name_plural = "WebAuthn Challenges"

    def __str__(self) -> str:
        """Return string representation of the challenge.

        Returns:
            Challenge type and associated user (or Anonymous).
        """
        user_str = self.user.username if self.user else "Anonymous"
        return f"{self.challenge_type} challenge for {user_str}"

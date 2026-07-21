"""Notification builder for creating target-agnostic RichNotification objects.

This module provides the NotificationBuilder class that transforms raw event
and customer data into RichNotification objects ready for formatting.
"""

import logging
import re
from datetime import datetime
from typing import Any

from core.models import Company, Person
from webhooks.models.rich_notification import (
    ActionButton,
    CompanyInfo,
    CustomerInfo,
    NotificationSeverity,
    NotificationType,
    PaymentInfo,
    PersonInfo,
    RichNotification,
)
from webhooks.utils.currency import CURRENCY_SYMBOLS, format_money
from webhooks.utils.email_classifier import classify_email

from .insight_detector import InsightDetector
from .utils import get_display_name
from .utils import interval_suffix as _interval_suffix

logger = logging.getLogger(__name__)

# A Chargify site subdomain must be a single DNS label: no dots, no
# slashes, no whitespace. Anything else could point the dashboard
# button at an unintended host.
_CHARGIFY_SUBDOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


# A Shopify shop domain is always the store's admin host on the
# ".myshopify.com" suffix (e.g. "acme.myshopify.com"). It gets
# interpolated into a URL host, so restrict it to a single shop-name
# label followed by that exact suffix. This mirrors _is_valid_shop_domain
# in app/core/views/integrations/shopify.py and prevents an
# attacker-controlled value from pointing the dashboard button at an
# arbitrary host (e.g. "evil.com"), a path, port, or credentials.
_SHOPIFY_SHOP_DOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9\-_]*\.myshopify\.com$")


def _normalize_chargify_subdomain(value: Any) -> str | None:
    """Normalize and validate a Chargify site subdomain.

    The subdomain originates from webhook payload data and is
    interpolated into a URL host, so it is normalized (surrounding
    whitespace stripped, lowercased) and then required to be a single
    valid DNS label. Values that still do not match (dots, slashes,
    interior whitespace, ...) are rejected.

    Args:
        value: Raw subdomain value from event metadata.

    Returns:
        The normalized subdomain, or None when the value is missing or
        not a safe single DNS label.
    """
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if not _CHARGIFY_SUBDOMAIN_RE.fullmatch(normalized):
        return None
    return normalized


def _normalize_shopify_shop_domain(value: Any) -> str | None:
    """Normalize and validate a Shopify shop domain.

    The shop domain originates from webhook payload data and is
    interpolated into a URL host, so it is normalized (surrounding
    whitespace stripped, lowercased) and then required to be a shop-name
    label on the ".myshopify.com" admin suffix (e.g. "acme.myshopify.com").
    Any other host (e.g. "evil.com"), or a value carrying a scheme, port,
    path, credentials, interior whitespace, or other unexpected
    characters, is rejected.

    Args:
        value: Raw shop domain value from event metadata.

    Returns:
        The normalized shop domain, or None when the value is missing or
        not a safe hostname.
    """
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if not _SHOPIFY_SHOP_DOMAIN_RE.fullmatch(normalized):
        return None
    return normalized


# Provider display configurations
PROVIDER_DISPLAY: dict[str, str] = {
    "shopify": "Shopify",
    "chargify": "Chargify",
    "stripe": "Stripe",
    "stripe_customer": "Stripe",
}

# Event type to notification type mapping
EVENT_TYPE_MAP: dict[str, NotificationType] = {
    # Payment events
    "payment_success": NotificationType.PAYMENT_SUCCESS,
    "payment_failure": NotificationType.PAYMENT_FAILURE,
    "refund_issued": NotificationType.REFUND_ISSUED,
    # Subscription events
    "subscription_created": NotificationType.SUBSCRIPTION_CREATED,
    "subscription_canceled": NotificationType.SUBSCRIPTION_CANCELED,
    "subscription_deleted": NotificationType.SUBSCRIPTION_CANCELED,
    "subscription_updated": NotificationType.SUBSCRIPTION_UPDATED,
    "subscription_renewed": NotificationType.SUBSCRIPTION_RENEWED,
    "trial_started": NotificationType.TRIAL_STARTED,
    "trial_ending": NotificationType.TRIAL_ENDING,
    "trial_converted": NotificationType.TRIAL_CONVERTED,
    # Customer events
    "customer_created": NotificationType.CUSTOMER_CREATED,
    "customer_updated": NotificationType.CUSTOMER_UPDATED,
    "customer_churned": NotificationType.CUSTOMER_CHURNED,
    # Usage events
    "feature_adopted": NotificationType.FEATURE_ADOPTED,
    "usage_milestone": NotificationType.USAGE_MILESTONE,
    "quota_warning": NotificationType.QUOTA_WARNING,
    "quota_exceeded": NotificationType.QUOTA_EXCEEDED,
    # Support events
    "feedback_received": NotificationType.FEEDBACK_RECEIVED,
    "nps_response": NotificationType.NPS_RESPONSE,
    "support_ticket": NotificationType.SUPPORT_TICKET,
    # System events
    "integration_connected": NotificationType.INTEGRATION_CONNECTED,
    "integration_error": NotificationType.INTEGRATION_ERROR,
    "webhook_received": NotificationType.WEBHOOK_RECEIVED,
    # Checkout events
    "checkout_started": NotificationType.CHECKOUT_STARTED,
    # Logistics events
    "order_created": NotificationType.ORDER_CREATED,
    "order_cancelled": NotificationType.ORDER_CANCELLED,
    "order_fulfilled": NotificationType.ORDER_FULFILLED,
    "fulfillment_created": NotificationType.FULFILLMENT_CREATED,
    "fulfillment_updated": NotificationType.FULFILLMENT_UPDATED,
    "shipment_delivered": NotificationType.SHIPMENT_DELIVERED,
}

# Event type to severity mapping
EVENT_SEVERITY_MAP: dict[str, NotificationSeverity] = {
    # Payment events
    "payment_success": NotificationSeverity.SUCCESS,
    "payment_failure": NotificationSeverity.ERROR,
    "refund_issued": NotificationSeverity.WARNING,
    # Subscription events
    "subscription_created": NotificationSeverity.SUCCESS,
    "subscription_canceled": NotificationSeverity.WARNING,
    "subscription_deleted": NotificationSeverity.WARNING,
    "subscription_updated": NotificationSeverity.INFO,
    "subscription_renewed": NotificationSeverity.SUCCESS,
    "trial_started": NotificationSeverity.INFO,
    "trial_ending": NotificationSeverity.WARNING,
    "trial_converted": NotificationSeverity.SUCCESS,
    # Customer events
    "customer_created": NotificationSeverity.SUCCESS,
    "customer_updated": NotificationSeverity.INFO,
    "customer_churned": NotificationSeverity.ERROR,
    # Usage events
    "feature_adopted": NotificationSeverity.SUCCESS,
    "usage_milestone": NotificationSeverity.SUCCESS,
    "quota_warning": NotificationSeverity.WARNING,
    "quota_exceeded": NotificationSeverity.ERROR,
    # Support events
    "feedback_received": NotificationSeverity.INFO,
    "nps_response": NotificationSeverity.INFO,
    "support_ticket": NotificationSeverity.INFO,
    # System events
    "integration_connected": NotificationSeverity.SUCCESS,
    "integration_error": NotificationSeverity.ERROR,
    "webhook_received": NotificationSeverity.INFO,
    # Checkout events
    "checkout_started": NotificationSeverity.INFO,
    # Logistics events
    "order_created": NotificationSeverity.SUCCESS,
    "order_cancelled": NotificationSeverity.WARNING,
    "order_fulfilled": NotificationSeverity.SUCCESS,
    "fulfillment_created": NotificationSeverity.INFO,
    "fulfillment_updated": NotificationSeverity.INFO,
    "shipment_delivered": NotificationSeverity.SUCCESS,
}

# Event type to headline icon mapping (semantic names)
EVENT_ICON_MAP: dict[str, str] = {
    # Payment events
    "payment_success": "money",
    "payment_failure": "error",
    "refund_issued": "money",
    # Subscription events
    "subscription_created": "celebration",
    "subscription_canceled": "warning",
    "subscription_deleted": "warning",
    "subscription_updated": "info",
    "subscription_renewed": "celebration",
    "trial_started": "rocket",
    "trial_ending": "warning",
    "trial_converted": "celebration",
    # Customer events
    "customer_created": "user",
    "customer_updated": "user",
    "customer_churned": "warning",
    # Usage events
    "feature_adopted": "feature",
    "usage_milestone": "chart",
    "quota_warning": "quota",
    "quota_exceeded": "error",
    # Support events
    "feedback_received": "feedback",
    "nps_response": "star",
    "support_ticket": "support",
    # System events
    "integration_connected": "check",
    "integration_error": "error",
    "webhook_received": "integration",
    # Checkout events
    "checkout_started": "cart",
    # Logistics events
    "order_created": "cart",
    "order_cancelled": "warning",
    "order_fulfilled": "package",
    "fulfillment_created": "truck",
    "fulfillment_updated": "truck",
    "shipment_delivered": "package",
}


class NotificationBuilder:
    """Builds RichNotification objects from raw event/customer data.

    This class encapsulates the logic for transforming webhook event data
    and customer data into target-agnostic RichNotification objects.
    """

    def __init__(self) -> None:
        """Initialize the notification builder."""
        self.insight_detector = InsightDetector()

    def build(
        self,
        event_data: dict[str, Any],
        customer_data: dict[str, Any],
        company: Company | None = None,
        person: Person | None = None,
    ) -> RichNotification:
        """Build a RichNotification from event and customer data.

        Args:
            event_data: Event data dictionary from provider.
            customer_data: Customer data dictionary.
            company: Optional enriched Company model.
            person: Optional enriched Person model (from Hunter.io).

        Returns:
            RichNotification ready for formatting.

        Raises:
            ValueError: If required data is missing.
        """
        if not event_data:
            raise ValueError("Missing event data")
        if not customer_data:
            raise ValueError("Missing customer data")

        event_type = event_data.get("type", "")
        if not event_type:
            raise ValueError("Missing event type")

        # Extract common fields
        provider = event_data.get("provider", "unknown")
        provider_display = PROVIDER_DISPLAY.get(provider, provider.title())

        # Build sub-models
        customer_info = self._build_customer_info(
            customer_data, event_data.get("currency") or "USD"
        )
        payment_info = self._build_payment_info(event_data)
        company_info = self._build_company_info(company) if company else None
        person_info = self._build_person_info(person) if person else None

        # Detect insights and risk status
        insight = self.insight_detector.detect(event_data, customer_data)
        risk_flags = self.insight_detector.detect_risk_status(event_data, customer_data)
        customer_info.status_flags = risk_flags

        # Build headline
        headline = self._build_headline(event_data, customer_data, company)

        # Determine notification type and severity. Unknown event types
        # fall back to CUSTOM so they never render as successful payments.
        notification_type = EVENT_TYPE_MAP.get(event_type, NotificationType.CUSTOM)
        severity = EVENT_SEVERITY_MAP.get(event_type, NotificationSeverity.INFO)
        headline_icon = EVENT_ICON_MAP.get(event_type, "info")

        # Detect recurring status
        is_recurring, billing_interval = self._detect_recurring(event_data)

        # Build action buttons
        actions = self._build_actions(event_data, customer_data, company)

        return RichNotification(
            type=notification_type,
            severity=severity,
            headline=headline,
            headline_icon=headline_icon,
            provider=provider,
            provider_display=provider_display,
            customer=customer_info,
            insight=insight,
            payment=payment_info,
            company=company_info,
            person=person_info,
            actions=actions,
            is_recurring=is_recurring,
            billing_interval=billing_interval,
        )

    def _build_customer_info(
        self, customer_data: dict[str, Any], currency: str = "USD"
    ) -> CustomerInfo:
        """Build CustomerInfo from customer data.

        Args:
            customer_data: Customer data dictionary.
            currency: Currency code of the triggering event, used for
                the LTV display.

        Returns:
            CustomerInfo dataclass.
        """
        email = customer_data.get("email", "")
        first_name = customer_data.get("first_name", "")
        last_name = customer_data.get("last_name", "")
        name = f"{first_name} {last_name}".strip() or None

        # Use smart display name fallback (no more "Individual")
        company_name = get_display_name(customer_data)

        # Calculate tenure display
        tenure_display = self._format_tenure(customer_data)

        # Calculate LTV display. Explicit None checks so a legitimate
        # zero lifetime value (0, 0.0, Decimal("0")) is not treated as
        # missing and does not fall back to the other key or warn.
        total_spent_raw = customer_data.get("total_spent")
        if total_spent_raw is None:
            total_spent_raw = customer_data.get("lifetime_value")
        if total_spent_raw is None:
            total_spent = 0.0
        else:
            try:
                total_spent = float(total_spent_raw)
            except (ValueError, TypeError):
                logger.warning(
                    "Could not parse customer lifetime value; defaulting to 0.0",
                    extra={"total_spent_raw": repr(total_spent_raw)},
                )
                total_spent = 0.0
        ltv_display = self._format_ltv(total_spent, currency) if total_spent else None

        return CustomerInfo(
            email=email,
            name=name,
            company_name=company_name or None,
            tenure_display=tenure_display,
            ltv_display=ltv_display,
            orders_count=customer_data.get("orders_count"),
            total_spent=total_spent if total_spent else None,
            status_flags=[],  # Will be set by insight detector
            email_tags=[tag.value for tag in classify_email(email)],
        )

    def _build_payment_info(self, event_data: dict[str, Any]) -> PaymentInfo | None:
        """Build PaymentInfo from event data.

        Args:
            event_data: Event data dictionary.

        Returns:
            PaymentInfo or None if no payment data.
        """
        # Don't show payment info for trials - no payment has occurred
        metadata = event_data.get("metadata", {})
        if metadata.get("is_trial"):
            return None

        amount = event_data.get("amount")
        if amount is None:
            return None

        currency = event_data.get("currency", "USD")

        # Detect billing interval
        _, interval = self._detect_recurring(event_data)

        # Extract payment method details
        payment_method, card_last4 = self._extract_payment_method(event_data)

        return PaymentInfo(
            amount=amount,
            currency=currency,
            interval=interval,
            plan_name=metadata.get("plan_name"),
            subscription_id=metadata.get("subscription_id"),
            payment_method=payment_method,
            card_last4=card_last4,
            order_number=metadata.get("order_number"),
            line_items=metadata.get("line_items", []),
            failure_reason=metadata.get("failure_reason"),
        )

    def _build_company_info(self, company: Company) -> CompanyInfo:
        """Build CompanyInfo from enriched Company model.

        Args:
            company: Enriched Company model.

        Returns:
            CompanyInfo dataclass.
        """
        brand_info = company.brand_info or {}

        # Get logo URL - prefer model method, fallback to brand_info
        logo_url = None
        if company.has_logo:
            logo_url = company.get_logo_url()
        elif brand_info.get("logo_url"):
            logo_url = brand_info["logo_url"]

        # Extract LinkedIn URL from links array
        linkedin_url = None
        for link in brand_info.get("links", []):
            if link.get("name") == "linkedin":
                linkedin_url = link.get("url")
                break

        return CompanyInfo(
            name=brand_info.get("name") or company.name or company.domain,
            domain=company.domain,
            industry=brand_info.get("industry"),
            year_founded=brand_info.get("year_founded"),
            employee_count=brand_info.get("employee_count"),
            description=brand_info.get("description"),
            logo_url=logo_url,
            linkedin_url=linkedin_url,
        )

    def _build_person_info(self, person: Person) -> PersonInfo:
        """Build PersonInfo from enriched Person model (Hunter.io).

        Args:
            person: Enriched Person model.

        Returns:
            PersonInfo dataclass.
        """
        return PersonInfo(
            email=person.email,
            first_name=person.first_name or None,
            last_name=person.last_name or None,
            position=person.position or None,
            seniority=person.seniority or None,
            company_domain=person.company_domain or None,
            linkedin_url=person.linkedin_url or None,
            twitter_handle=person.twitter_handle or None,
            github_handle=person.github_handle or None,
            location=person.location or None,
        )

    def _build_headline(  # noqa: C901
        self,
        event_data: dict[str, Any],
        customer_data: dict[str, Any],
        company: Company | None,
    ) -> str:
        """Build the headline text for the notification.

        Args:
            event_data: Event data dictionary.
            customer_data: Customer data dictionary.
            company: Optional enriched Company model.

        Returns:
            Headline string.
        """
        # Note: company and customer_data params kept for interface compatibility
        # but company name is now shown in body, not headline
        _ = company  # unused
        _ = customer_data  # unused

        event_type = event_data.get("type", "")
        amount = event_data.get("amount")
        metadata = event_data.get("metadata", {})
        currency = event_data.get("currency") or "USD"

        # Event-focused headlines (company/customer info shown in body)
        if event_type == "payment_success":
            # Check for trial conversion (first real payment after trial)
            if metadata.get("is_trial_conversion"):
                return "Trial converted!"
            # "is not None" so $0 payments (trial confirmations, promo
            # comps) still render as "$0.00 received".
            if amount is not None:
                return f"{format_money(amount, currency)} received"
            return "Payment received"

        elif event_type == "payment_failure":
            attempt_count = metadata.get("attempt_count")
            if amount is not None and attempt_count and attempt_count > 1:
                money = format_money(amount, currency)
                # Stripe's attempt_count includes the initial attempt, so
                # attempt_count=2 is the first retry.
                return f"{money} payment failed (retry #{attempt_count - 1})"
            elif amount is not None:
                return f"{format_money(amount, currency)} payment failed"
            return "Payment failed"

        elif event_type == "subscription_created":
            # "New subscription", not "New customer": the webhook proves a
            # subscription was created, but an existing customer adding a
            # second subscription fires the same event - never claim more
            # than the payload can prove.
            return "New subscription!"

        elif event_type == "subscription_updated":
            # Check for upgrade/downgrade
            direction = metadata.get("change_direction", "")
            plan_name = metadata.get("plan_name")
            previous_amount = metadata.get("previous_amount")
            suffix = _interval_suffix(metadata.get("billing_period"))
            # The "old" side of an upgrade/downgrade may be denominated in
            # a different currency or interval than the current plan.
            prev_currency = metadata.get("previous_currency") or currency
            prev_suffix = _interval_suffix(
                metadata.get("previous_billing_period")
                or metadata.get("billing_period")
            )

            # "is not None" throughout so $0 amounts (e.g. a $299 -> $0
            # cancel-in-place downgrade or a $0 -> $99 upgrade from a
            # free tier) keep the "from X to Y" framing.
            if direction == "upgrade":
                # Show plan name if available (Chargify), otherwise amount change
                if plan_name and amount is not None:
                    money = format_money(amount, currency)
                    return f"Upgraded to {plan_name} ({money}{suffix})"
                elif previous_amount is not None and amount is not None:
                    old = format_money(previous_amount, prev_currency)
                    new = format_money(amount, currency)
                    return f"Upgraded: {old}{prev_suffix} to {new}{suffix}"
                elif amount is not None:
                    money = format_money(amount, currency)
                    return f"Subscription upgraded to {money}{suffix}"
                return "Subscription upgraded"
            elif direction == "downgrade":
                if plan_name and amount is not None:
                    money = format_money(amount, currency)
                    return f"Downgraded to {plan_name} ({money}{suffix})"
                elif previous_amount is not None and amount is not None:
                    old = format_money(previous_amount, prev_currency)
                    new = format_money(amount, currency)
                    return f"Downgraded: {old}{prev_suffix} to {new}{suffix}"
                elif amount is not None:
                    money = format_money(amount, currency)
                    return f"Subscription downgraded to {money}{suffix}"
                return "Subscription downgraded"
            return "Subscription updated"

        elif event_type == "subscription_renewed":
            # No parser currently emits this type (Chargify acknowledges
            # and skips subscription_renewed webhooks), but it remains a
            # valid event type so a renewal must not collapse to a bare
            # title without amount/plan context.
            plan_name = metadata.get("plan_name")
            suffix = _interval_suffix(metadata.get("billing_period"))
            if plan_name and amount is not None:
                money = format_money(amount, currency)
                return f"Subscription renewed: {plan_name} ({money}{suffix})"
            elif amount is not None:
                money = format_money(amount, currency)
                return f"Subscription renewed at {money}{suffix}"
            return "Subscription renewed"

        elif event_type in ("subscription_canceled", "subscription_deleted"):
            return "Subscription canceled"

        elif event_type == "trial_started":
            return "Trial started!"

        elif event_type == "trial_ending":
            return "Trial ending soon"

        # Logistics event headlines (e-commerce/Shopify)
        elif event_type == "order_created":
            order_number = metadata.get("order_number") or metadata.get("order_ref")
            # "is not None" so comped orders (Shopify sends total_price
            # "0.00") still show the formatted amount.
            if order_number and amount is not None:
                return f"New order #{order_number} ({format_money(amount, currency)})"
            elif order_number:
                return f"New order #{order_number}"
            elif amount is not None:
                return f"New order ({format_money(amount, currency)})"
            return "New order"

        elif event_type == "order_cancelled":
            order_number = metadata.get("order_number") or metadata.get("order_ref")
            if order_number and amount is not None:
                money = format_money(amount, currency)
                return f"Order #{order_number} canceled ({money})"
            elif order_number:
                return f"Order #{order_number} canceled"
            return "Order canceled"

        elif event_type == "order_fulfilled":
            order_number = metadata.get("order_number") or metadata.get("order_ref")
            if order_number:
                return f"Order #{order_number} fulfilled"
            return "Order fulfilled"

        elif event_type == "fulfillment_created":
            order_number = metadata.get("order_number") or metadata.get("order_ref")
            tracking_number = metadata.get("tracking_number")
            if order_number and tracking_number:
                return f"Order #{order_number} shipped"
            elif order_number:
                return f"Order #{order_number} fulfillment created"
            return "Fulfillment created"

        elif event_type == "fulfillment_updated":
            order_number = metadata.get("order_number") or metadata.get("order_ref")
            status = metadata.get("shipment_status") or metadata.get(
                "fulfillment_status"
            )
            if order_number and status:
                return f"Order #{order_number} - {status.replace('_', ' ').title()}"
            elif order_number:
                return f"Order #{order_number} shipment updated"
            return "Shipment updated"

        elif event_type == "shipment_delivered":
            order_number = metadata.get("order_number") or metadata.get("order_ref")
            if order_number:
                return f"Order #{order_number} delivered"
            return "Shipment delivered"

        else:
            title: str = event_type.replace("_", " ").title()
            return title

    def _build_actions(
        self,
        event_data: dict[str, Any],
        customer_data: dict[str, Any],
        company: Company | None,
    ) -> list[ActionButton]:
        """Build action buttons for the notification.

        Args:
            event_data: Event data dictionary.
            customer_data: Customer data dictionary.
            company: Optional enriched Company model.

        Returns:
            List of ActionButton objects.
        """
        # Note: the company website is linked inline in the Slack company
        # section, so it no longer gets its own button - buttons are
        # reserved for actions.
        _ = company

        event_type = event_data.get("type", "")

        actions: list[ActionButton] = []

        # Provider-specific dashboard link
        dashboard_action = self._build_provider_dashboard_action(event_data)

        # On a payment failure the account-saving action is contacting
        # the customer, so it takes the primary style and first position;
        # the dashboard link becomes secondary.
        email = customer_data.get("email")
        if event_type == "payment_failure" and email:
            actions.append(
                ActionButton(
                    text="Contact Customer",
                    url=f"mailto:{email}",
                    style="primary",
                )
            )
            if dashboard_action:
                dashboard_action.style = "default"
                actions.append(dashboard_action)
        elif dashboard_action:
            actions.append(dashboard_action)

        return actions

    def _build_provider_dashboard_action(
        self, event_data: dict[str, Any]
    ) -> ActionButton | None:
        """Build the provider-specific dashboard link button.

        Args:
            event_data: Event data dictionary.

        Returns:
            ActionButton linking to the provider dashboard, or None when
            the data needed to build a working link is missing.
        """
        provider = event_data.get("provider", "")
        metadata = event_data.get("metadata", {})

        if provider in ("stripe", "stripe_customer"):
            # Parsers write the customer id at the top level of the
            # event, not in metadata.
            customer_id = event_data.get("customer_id")
            if customer_id:
                return ActionButton(
                    text="View in Stripe",
                    url=f"https://dashboard.stripe.com/customers/{customer_id}",
                    style="primary",
                )

        elif provider == "chargify":
            subscription_id = metadata.get("subscription_id")
            # Chargify (Maxio) dashboards live on per-site subdomains;
            # a hardcoded app.chargify.com URL does not resolve. Omit
            # the button when the site subdomain is unknown or is not a
            # safe single DNS label (it comes from webhook payload data
            # and is interpolated into the URL host).
            subdomain = _normalize_chargify_subdomain(metadata.get("site_subdomain"))
            if subscription_id and subdomain:
                return ActionButton(
                    text="View in Chargify",
                    url=(
                        f"https://{subdomain}.chargify.com"
                        f"/subscriptions/{subscription_id}"
                    ),
                    style="primary",
                )

        elif provider == "shopify":
            order_id = metadata.get("order_id")
            # The shop domain comes from webhook payload data and is
            # interpolated into the URL host, so reject anything that is
            # not a plain dotted hostname (defends against smuggling a
            # scheme, path, port, or alternate host into the link).
            shop_domain = _normalize_shopify_shop_domain(metadata.get("shop_domain"))
            if order_id and shop_domain:
                return ActionButton(
                    text="View Order",
                    url=f"https://{shop_domain}/admin/orders/{order_id}",
                    style="primary",
                )

        return None

    def _detect_recurring(self, event_data: dict[str, Any]) -> tuple[bool, str | None]:
        """Detect if payment is recurring and extract billing interval.

        Args:
            event_data: Event data dictionary.

        Returns:
            Tuple of (is_recurring, billing_interval).
        """
        metadata = event_data.get("metadata", {})

        # Note: Chargify renewal_success/renewal_failure are normalized
        # to payment_success/payment_failure by the parser (with
        # billing_period defaulted to "monthly" in metadata), so no
        # renewal-specific branch is needed here.

        # Check for subscription_id presence
        if metadata.get("subscription_id"):
            interval = metadata.get("billing_period")
            return True, interval

        # Shopify: check for subscription info
        if metadata.get("subscription_contract_id"):
            interval = metadata.get("billing_period")
            return True, interval

        # Check for explicit interval
        if metadata.get("billing_period"):
            return True, metadata["billing_period"]

        return False, None

    def _extract_payment_method(
        self, event_data: dict[str, Any]
    ) -> tuple[str | None, str | None]:
        """Extract payment method and card last4 from event data.

        Args:
            event_data: Event data dictionary.

        Returns:
            Tuple of (payment_method, card_last4).
        """
        metadata = event_data.get("metadata", {})
        provider = event_data.get("provider", "")

        if provider == "shopify":
            card_brand = metadata.get("credit_card_company")
            if card_brand:
                return card_brand.lower(), metadata.get("card_last4")
            return metadata.get("payment_gateway"), None

        elif provider == "chargify":
            card_type = metadata.get("card_type")
            if card_type:
                return card_type.lower(), metadata.get("card_last4")
            return metadata.get("payment_method"), None

        elif provider in ("stripe", "stripe_customer"):
            card_brand = metadata.get("card_brand")
            if card_brand:
                return card_brand.lower(), metadata.get("card_last4")
            return metadata.get("payment_method_type"), None

        return None, None

    def _format_tenure(self, customer_data: dict[str, Any]) -> str | None:
        """Format customer tenure for display.

        Args:
            customer_data: Customer data dictionary.

        Returns:
            Formatted tenure string like "Since Mar 2024" or None.
        """
        created_at = customer_data.get("created_at") or customer_data.get(
            "subscription_start"
        )
        if not created_at:
            return None

        try:
            if isinstance(created_at, str):
                created_at = created_at.replace("Z", "+00:00")
                created_date = datetime.fromisoformat(created_at)
            elif isinstance(created_at, datetime):
                created_date = created_at
            else:
                return None

            return f"Since {created_date.strftime('%b %Y')}"

        except (ValueError, TypeError):
            logger.warning(
                "Could not parse customer created_at for tenure display",
                extra={"created_at": repr(created_at)},
            )
            return None

    def _format_ltv(self, total_spent: float, currency: str = "USD") -> str:
        """Format lifetime value for display.

        Note: LTV is aggregated in the workspace provider's currency;
        mixed-currency payment history is not converted (out of scope),
        so the triggering event's currency is used for display.

        Args:
            total_spent: Total amount spent.
            currency: Currency code the total is denominated in.

        Returns:
            Formatted LTV string like "$7.1k", "€150", or "CHF 150".
        """
        if total_spent >= 1000:
            code = (currency or "USD").upper()
            symbol = CURRENCY_SYMBOLS.get(code)
            abbreviated = f"{total_spent / 1000:.1f}k"
            if symbol:
                return f"{symbol}{abbreviated}"
            return f"{code} {abbreviated}"
        formatted: str = format_money(total_spent, currency, 0)
        return formatted

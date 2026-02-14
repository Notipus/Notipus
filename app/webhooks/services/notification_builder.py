"""Notification builder for creating target-agnostic RichNotification objects.

This module provides the NotificationBuilder class that transforms raw event
and customer data into RichNotification objects ready for formatting.
"""

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
    SentimentInfo,
    TicketInfo,
)

from .insight_detector import InsightDetector
from .utils import get_display_name

# Provider display configurations
PROVIDER_DISPLAY: dict[str, str] = {
    "shopify": "Shopify",
    "chargify": "Chargify",
    "stripe": "Stripe",
    "stripe_customer": "Stripe",
    "zendesk": "Zendesk",
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
    "support_ticket_created": NotificationType.SUPPORT_TICKET_CREATED,
    "support_ticket_updated": NotificationType.SUPPORT_TICKET_UPDATED,
    "support_ticket_comment": NotificationType.SUPPORT_TICKET_COMMENT,
    "support_ticket_resolved": NotificationType.SUPPORT_TICKET_RESOLVED,
    "support_ticket_assigned": NotificationType.SUPPORT_TICKET_ASSIGNED,
    "support_ticket_reopened": NotificationType.SUPPORT_TICKET_REOPENED,
    "support_ticket_priority_changed": NotificationType.SUPPORT_TICKET_PRIORITY_CHANGED,
    "support_ticket_status_changed": NotificationType.SUPPORT_TICKET_STATUS_CHANGED,
    # System events
    "integration_connected": NotificationType.INTEGRATION_CONNECTED,
    "integration_error": NotificationType.INTEGRATION_ERROR,
    "webhook_received": NotificationType.WEBHOOK_RECEIVED,
    # Logistics events
    "order_created": NotificationType.ORDER_CREATED,
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
    "support_ticket_created": NotificationSeverity.INFO,
    "support_ticket_updated": NotificationSeverity.INFO,
    "support_ticket_comment": NotificationSeverity.INFO,
    "support_ticket_resolved": NotificationSeverity.SUCCESS,
    "support_ticket_assigned": NotificationSeverity.INFO,
    "support_ticket_reopened": NotificationSeverity.WARNING,
    "support_ticket_priority_changed": NotificationSeverity.INFO,
    "support_ticket_status_changed": NotificationSeverity.INFO,
    # System events
    "integration_connected": NotificationSeverity.SUCCESS,
    "integration_error": NotificationSeverity.ERROR,
    "webhook_received": NotificationSeverity.INFO,
    # Logistics events
    "order_created": NotificationSeverity.SUCCESS,
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
    "support_ticket_created": "support",
    "support_ticket_updated": "support",
    "support_ticket_comment": "feedback",
    "support_ticket_resolved": "check",
    "support_ticket_assigned": "user",
    "support_ticket_reopened": "warning",
    "support_ticket_priority_changed": "warning",
    "support_ticket_status_changed": "info",
    # System events
    "integration_connected": "check",
    "integration_error": "error",
    "webhook_received": "integration",
    # Logistics events
    "order_created": "cart",
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
        sentiment: SentimentInfo | None = None,
    ) -> RichNotification:
        """Build a RichNotification from event and customer data.

        Args:
            event_data: Event data dictionary from provider.
            customer_data: Customer data dictionary.
            company: Optional enriched Company model.
            person: Optional enriched Person model (from Hunter.io).
            sentiment: Optional sentiment analysis results (from Ollama).

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
        customer_info = self._build_customer_info(customer_data)
        payment_info = self._build_payment_info(event_data)
        ticket_info = self._build_ticket_info(event_data)
        company_info = self._build_company_info(company) if company else None
        person_info = self._build_person_info(person) if person else None

        # Detect insights and risk status
        insight = self.insight_detector.detect(event_data, customer_data)
        risk_flags = self.insight_detector.detect_risk_status(event_data, customer_data)
        customer_info.status_flags = risk_flags

        # Build headline
        headline = self._build_headline(event_data, customer_data, company)

        # Determine notification type and severity
        notification_type = EVENT_TYPE_MAP.get(
            event_type, NotificationType.PAYMENT_SUCCESS
        )
        severity = EVENT_SEVERITY_MAP.get(event_type, NotificationSeverity.INFO)
        headline_icon = EVENT_ICON_MAP.get(event_type, "info")

        # Adjust severity based on ticket priority for support events
        if ticket_info and ticket_info.priority == "urgent":
            severity = NotificationSeverity.ERROR
        elif ticket_info and ticket_info.priority == "high":
            severity = NotificationSeverity.WARNING

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
            ticket=ticket_info,
            sentiment=sentiment,
            company=company_info,
            person=person_info,
            actions=actions,
            is_recurring=is_recurring,
            billing_interval=billing_interval,
        )

    def _build_customer_info(self, customer_data: dict[str, Any]) -> CustomerInfo:
        """Build CustomerInfo from customer data.

        Args:
            customer_data: Customer data dictionary.

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

        # Calculate LTV display
        total_spent_raw = customer_data.get("total_spent") or customer_data.get(
            "lifetime_value", 0
        )
        try:
            total_spent = float(total_spent_raw) if total_spent_raw else 0.0
        except (ValueError, TypeError):
            total_spent = 0.0
        ltv_display = self._format_ltv(total_spent) if total_spent else None

        return CustomerInfo(
            email=email,
            name=name,
            company_name=company_name or None,
            tenure_display=tenure_display,
            ltv_display=ltv_display,
            orders_count=customer_data.get("orders_count"),
            total_spent=total_spent if total_spent else None,
            status_flags=[],  # Will be set by insight detector
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
        metadata = event_data.get("metadata", {})

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

    def _build_ticket_info(self, event_data: dict[str, Any]) -> TicketInfo | None:
        """Build TicketInfo from event data.

        Args:
            event_data: Event data dictionary.

        Returns:
            TicketInfo or None if not a support ticket event.
        """
        event_type = event_data.get("type", "")
        if not event_type.startswith("support_ticket"):
            return None

        metadata = event_data.get("metadata", {})

        # Extract requester info
        requester = metadata.get("requester", {})
        requester_email = (
            requester.get("email") if isinstance(requester, dict) else None
        )
        requester_name = requester.get("name") if isinstance(requester, dict) else None

        # Extract assignee info
        assignee = metadata.get("assignee", {})
        assignee_name = assignee.get("name") if isinstance(assignee, dict) else None

        # Extract tags
        tags = metadata.get("tags", [])
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",")]

        return TicketInfo(
            ticket_id=metadata.get("ticket_id", event_data.get("external_id", "")),
            subject=metadata.get("subject", ""),
            status=metadata.get("ticket_status", "open"),
            priority=metadata.get("priority"),
            requester_email=requester_email,
            requester_name=requester_name,
            assignee_name=assignee_name,
            description=metadata.get("description"),
            channel=metadata.get("channel"),
            tags=tags,
            latest_comment=metadata.get("latest_comment"),
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

        # Event-focused headlines (company/customer info shown in body)
        if event_type == "payment_success":
            # Check for trial conversion (first real payment after trial)
            if metadata.get("is_trial_conversion"):
                return "Trial converted!"
            if amount:
                return f"${amount:,.2f} received"
            return "Payment received"

        elif event_type == "payment_failure":
            if amount:
                return f"${amount:,.2f} payment failed"
            return "Payment failed"

        elif event_type == "subscription_created":
            return "New customer!"

        elif event_type == "subscription_updated":
            # Check for upgrade/downgrade
            direction = metadata.get("change_direction", "")
            plan_name = metadata.get("plan_name")
            previous_amount = metadata.get("previous_amount")

            if direction == "upgrade":
                # Show plan name if available (Chargify), otherwise amount change
                if plan_name and amount:
                    return f"Upgraded to {plan_name} (${amount:,.2f}/mo)"
                elif previous_amount and amount:
                    old = f"${previous_amount:,.2f}"
                    new = f"${amount:,.2f}"
                    return f"Upgraded: {old}/mo to {new}/mo"
                elif amount:
                    return f"Subscription upgraded to ${amount:,.2f}/mo"
                return "Subscription upgraded"
            elif direction == "downgrade":
                if plan_name and amount:
                    return f"Downgraded to {plan_name} (${amount:,.2f}/mo)"
                elif previous_amount and amount:
                    old = f"${previous_amount:,.2f}"
                    new = f"${amount:,.2f}"
                    return f"Downgraded: {old}/mo to {new}/mo"
                elif amount:
                    return f"Subscription downgraded to ${amount:,.2f}/mo"
                return "Subscription downgraded"
            return "Subscription updated"

        elif event_type in ("subscription_canceled", "subscription_deleted"):
            return "Subscription canceled"

        elif event_type == "trial_started":
            return "Trial started!"

        elif event_type == "trial_ending":
            return "Trial ending soon"

        # Logistics event headlines (e-commerce/Shopify)
        elif event_type == "order_created":
            metadata = event_data.get("metadata", {})
            order_number = metadata.get("order_number") or metadata.get("order_ref")
            if order_number and amount:
                return f"New order #{order_number} (${amount:,.2f})"
            elif order_number:
                return f"New order #{order_number}"
            elif amount:
                return f"New order (${amount:,.2f})"
            return "New order"

        elif event_type == "order_fulfilled":
            metadata = event_data.get("metadata", {})
            order_number = metadata.get("order_number") or metadata.get("order_ref")
            if order_number:
                return f"Order #{order_number} fulfilled"
            return "Order fulfilled"

        elif event_type == "fulfillment_created":
            metadata = event_data.get("metadata", {})
            order_number = metadata.get("order_number") or metadata.get("order_ref")
            tracking_number = metadata.get("tracking_number")
            if order_number and tracking_number:
                return f"Order #{order_number} shipped"
            elif order_number:
                return f"Order #{order_number} fulfillment created"
            return "Fulfillment created"

        elif event_type == "fulfillment_updated":
            metadata = event_data.get("metadata", {})
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
            metadata = event_data.get("metadata", {})
            order_number = metadata.get("order_number") or metadata.get("order_ref")
            if order_number:
                return f"Order #{order_number} delivered"
            return "Shipment delivered"

        # Support ticket headlines - extract common metadata once
        elif event_type.startswith("support_ticket"):
            metadata = event_data.get("metadata", {})
            subject = metadata.get("subject", "")

            if event_type == "support_ticket_created":
                priority = metadata.get("priority", "").lower()
                if priority == "urgent":
                    return (
                        f"Urgent ticket: {subject}"
                        if subject
                        else "Urgent support ticket"
                    )
                elif priority == "high":
                    return (
                        f"High priority: {subject}"
                        if subject
                        else "High priority ticket"
                    )
                return f"New ticket: {subject}" if subject else "New support ticket"

            elif event_type == "support_ticket_comment":
                return f"Reply on: {subject}" if subject else "New ticket reply"

            elif event_type == "support_ticket_resolved":
                return f"Resolved: {subject}" if subject else "Ticket resolved"

            elif event_type == "support_ticket_assigned":
                assignee = metadata.get("assignee", {})
                assignee_name = (
                    assignee.get("name") if isinstance(assignee, dict) else ""
                )
                if assignee_name:
                    return (
                        f"Assigned to {assignee_name}: {subject}"
                        if subject
                        else f"Ticket assigned to {assignee_name}"
                    )
                return f"Ticket assigned: {subject}" if subject else "Ticket assigned"

            elif event_type == "support_ticket_reopened":
                return f"Reopened: {subject}" if subject else "Ticket reopened"

            elif event_type == "support_ticket_priority_changed":
                priority = metadata.get("priority", "")
                if priority:
                    return (
                        f"Priority changed to {priority}: {subject}"
                        if subject
                        else f"Priority: {priority}"
                    )
                return f"Priority changed: {subject}" if subject else "Priority changed"

            else:  # support_ticket_updated, support_ticket_status_changed
                status = metadata.get("ticket_status", "")
                if status:
                    return (
                        f"{status.title()}: {subject}"
                        if subject
                        else f"Ticket {status}"
                    )
                return f"Updated: {subject}" if subject else "Ticket updated"

        else:
            title = event_type.replace("_", " ").title()
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
        actions: list[ActionButton] = []

        # Provider-specific dashboard link
        provider_action = self._build_provider_action(event_data)
        if provider_action:
            actions.append(provider_action)

        # Add company website link if enriched
        if company and company.domain:
            actions.append(
                ActionButton(
                    text="Website",
                    url=f"https://{company.domain}",
                    style="default",
                )
            )

        # Add contact customer link for failures
        event_type = event_data.get("type", "")
        email = customer_data.get("email")
        if event_type == "payment_failure" and email:
            actions.append(
                ActionButton(
                    text="Contact Customer",
                    url=f"mailto:{email}",
                    style="default",
                )
            )

        return actions

    def _build_provider_action(self, event_data: dict[str, Any]) -> ActionButton | None:
        """Build provider-specific action button.

        Args:
            event_data: Event data dictionary.

        Returns:
            ActionButton for the provider's dashboard or None.
        """
        provider = event_data.get("provider", "")
        metadata = event_data.get("metadata", {})

        if provider == "stripe":
            return self._build_stripe_action(metadata)
        elif provider == "chargify":
            return self._build_chargify_action(metadata)
        elif provider == "shopify":
            return self._build_shopify_action(metadata)
        elif provider == "zendesk":
            return self._build_zendesk_action(metadata)
        return None

    def _build_stripe_action(self, metadata: dict[str, Any]) -> ActionButton | None:
        """Build Stripe dashboard action button."""
        customer_id = metadata.get("stripe_customer_id")
        if not customer_id:
            return None
        return ActionButton(
            text="View in Stripe",
            url=f"https://dashboard.stripe.com/customers/{customer_id}",
            style="primary",
        )

    def _build_chargify_action(self, metadata: dict[str, Any]) -> ActionButton | None:
        """Build Chargify dashboard action button."""
        subscription_id = metadata.get("subscription_id")
        if not subscription_id:
            return None
        return ActionButton(
            text="View in Chargify",
            url=f"https://app.chargify.com/subscriptions/{subscription_id}",
            style="primary",
        )

    def _build_shopify_action(self, metadata: dict[str, Any]) -> ActionButton | None:
        """Build Shopify order action button."""
        order_id = metadata.get("order_id")
        shop_domain = metadata.get("shop_domain")
        if not (order_id and shop_domain):
            return None
        return ActionButton(
            text="View Order",
            url=f"https://{shop_domain}/admin/orders/{order_id}",
            style="primary",
        )

    def _build_zendesk_action(self, metadata: dict[str, Any]) -> ActionButton | None:
        """Build Zendesk ticket action button."""
        ticket_id = metadata.get("ticket_id")
        zendesk_subdomain = metadata.get("zendesk_subdomain", "").lower()

        if not (ticket_id and zendesk_subdomain):
            return None

        # Validate subdomain format (alphanumeric and hyphens only)
        if not re.match(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$|^[a-z0-9]$", zendesk_subdomain):
            return None

        return ActionButton(
            text="View in Zendesk",
            url=f"https://{zendesk_subdomain}.zendesk.com/agent/tickets/{ticket_id}",
            style="primary",
        )

    def _detect_recurring(self, event_data: dict[str, Any]) -> tuple[bool, str | None]:
        """Detect if payment is recurring and extract billing interval.

        Args:
            event_data: Event data dictionary.

        Returns:
            Tuple of (is_recurring, billing_interval).
        """
        event_type = event_data.get("type", "")
        metadata = event_data.get("metadata", {})

        # Renewal events are always recurring
        if event_type in ("renewal_success", "renewal_failure"):
            interval = metadata.get("billing_period", "monthly")
            return True, interval

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
            return None

    def _format_ltv(self, total_spent: float) -> str:
        """Format lifetime value for display.

        Args:
            total_spent: Total amount spent.

        Returns:
            Formatted LTV string like "$7.1k" or "$150".
        """
        if total_spent >= 1000:
            return f"${total_spent / 1000:.1f}k"
        return f"${total_spent:,.0f}"

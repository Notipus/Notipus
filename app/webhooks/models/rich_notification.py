"""Target-agnostic notification model for multi-target delivery.

This module defines the RichNotification dataclass and supporting types
that represent a notification in a platform-independent format. Formatters
then convert this to platform-specific formats (Slack, Email, Discord, etc.).
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EventCategory(Enum):
    """High-level categories of events.

    Used to determine how to render and route notifications.
    """

    PAYMENT = "payment"  # Payment and billing events
    SUBSCRIPTION = "subscription"  # Subscription lifecycle events
    CUSTOMER = "customer"  # Customer profile/activity events
    USAGE = "usage"  # Feature usage and quota events
    SUPPORT = "support"  # Support and feedback events
    SYSTEM = "system"  # System and integration events
    LOGISTICS = "logistics"  # Fulfillment and shipping events
    CUSTOM = "custom"  # Custom/webhook events


class NotificationType(Enum):
    """Types of notification events.

    Extensible enum covering payment, subscription, and other event types.
    """

    # Payment events
    PAYMENT_SUCCESS = "payment_success"
    PAYMENT_FAILURE = "payment_failure"
    REFUND_ISSUED = "refund_issued"

    # Subscription events
    SUBSCRIPTION_CREATED = "subscription_created"
    SUBSCRIPTION_CANCELED = "subscription_canceled"
    SUBSCRIPTION_UPDATED = "subscription_updated"
    SUBSCRIPTION_RENEWED = "subscription_renewed"
    TRIAL_STARTED = "trial_started"
    TRIAL_ENDING = "trial_ending"
    TRIAL_CONVERTED = "trial_converted"

    # Customer events
    CUSTOMER_CREATED = "customer_created"
    CUSTOMER_UPDATED = "customer_updated"
    CUSTOMER_CHURNED = "customer_churned"

    # Usage events
    FEATURE_ADOPTED = "feature_adopted"
    USAGE_MILESTONE = "usage_milestone"
    QUOTA_WARNING = "quota_warning"
    QUOTA_EXCEEDED = "quota_exceeded"

    # Support events
    FEEDBACK_RECEIVED = "feedback_received"
    NPS_RESPONSE = "nps_response"
    SUPPORT_TICKET = "support_ticket"
    SUPPORT_TICKET_CREATED = "support_ticket_created"
    SUPPORT_TICKET_UPDATED = "support_ticket_updated"
    SUPPORT_TICKET_COMMENT = "support_ticket_comment"
    SUPPORT_TICKET_RESOLVED = "support_ticket_resolved"
    SUPPORT_TICKET_ASSIGNED = "support_ticket_assigned"
    SUPPORT_TICKET_REOPENED = "support_ticket_reopened"
    SUPPORT_TICKET_PRIORITY_CHANGED = "support_ticket_priority_changed"
    SUPPORT_TICKET_STATUS_CHANGED = "support_ticket_status_changed"

    # System events
    INTEGRATION_CONNECTED = "integration_connected"
    INTEGRATION_ERROR = "integration_error"
    WEBHOOK_RECEIVED = "webhook_received"

    # Logistics events
    ORDER_CREATED = "order_created"
    ORDER_FULFILLED = "order_fulfilled"
    FULFILLMENT_CREATED = "fulfillment_created"
    FULFILLMENT_UPDATED = "fulfillment_updated"
    SHIPMENT_DELIVERED = "shipment_delivered"

    # Generic
    CUSTOM = "custom"


# Map notification types to categories
EVENT_CATEGORY_MAP: dict[NotificationType, EventCategory] = {
    NotificationType.PAYMENT_SUCCESS: EventCategory.PAYMENT,
    NotificationType.PAYMENT_FAILURE: EventCategory.PAYMENT,
    NotificationType.REFUND_ISSUED: EventCategory.PAYMENT,
    NotificationType.SUBSCRIPTION_CREATED: EventCategory.SUBSCRIPTION,
    NotificationType.SUBSCRIPTION_CANCELED: EventCategory.SUBSCRIPTION,
    NotificationType.SUBSCRIPTION_UPDATED: EventCategory.SUBSCRIPTION,
    NotificationType.SUBSCRIPTION_RENEWED: EventCategory.SUBSCRIPTION,
    NotificationType.TRIAL_STARTED: EventCategory.SUBSCRIPTION,
    NotificationType.TRIAL_ENDING: EventCategory.SUBSCRIPTION,
    NotificationType.TRIAL_CONVERTED: EventCategory.SUBSCRIPTION,
    NotificationType.CUSTOMER_CREATED: EventCategory.CUSTOMER,
    NotificationType.CUSTOMER_UPDATED: EventCategory.CUSTOMER,
    NotificationType.CUSTOMER_CHURNED: EventCategory.CUSTOMER,
    NotificationType.FEATURE_ADOPTED: EventCategory.USAGE,
    NotificationType.USAGE_MILESTONE: EventCategory.USAGE,
    NotificationType.QUOTA_WARNING: EventCategory.USAGE,
    NotificationType.QUOTA_EXCEEDED: EventCategory.USAGE,
    NotificationType.FEEDBACK_RECEIVED: EventCategory.SUPPORT,
    NotificationType.NPS_RESPONSE: EventCategory.SUPPORT,
    NotificationType.SUPPORT_TICKET: EventCategory.SUPPORT,
    NotificationType.SUPPORT_TICKET_CREATED: EventCategory.SUPPORT,
    NotificationType.SUPPORT_TICKET_UPDATED: EventCategory.SUPPORT,
    NotificationType.SUPPORT_TICKET_COMMENT: EventCategory.SUPPORT,
    NotificationType.SUPPORT_TICKET_RESOLVED: EventCategory.SUPPORT,
    NotificationType.SUPPORT_TICKET_ASSIGNED: EventCategory.SUPPORT,
    NotificationType.SUPPORT_TICKET_REOPENED: EventCategory.SUPPORT,
    NotificationType.SUPPORT_TICKET_PRIORITY_CHANGED: EventCategory.SUPPORT,
    NotificationType.SUPPORT_TICKET_STATUS_CHANGED: EventCategory.SUPPORT,
    NotificationType.INTEGRATION_CONNECTED: EventCategory.SYSTEM,
    NotificationType.INTEGRATION_ERROR: EventCategory.SYSTEM,
    NotificationType.WEBHOOK_RECEIVED: EventCategory.SYSTEM,
    NotificationType.ORDER_CREATED: EventCategory.LOGISTICS,
    NotificationType.ORDER_FULFILLED: EventCategory.LOGISTICS,
    NotificationType.FULFILLMENT_CREATED: EventCategory.LOGISTICS,
    NotificationType.FULFILLMENT_UPDATED: EventCategory.LOGISTICS,
    NotificationType.SHIPMENT_DELIVERED: EventCategory.LOGISTICS,
    NotificationType.CUSTOM: EventCategory.CUSTOM,
}


class NotificationSeverity(Enum):
    """Notification severity levels for visual styling."""

    SUCCESS = "success"  # Green - positive outcomes
    WARNING = "warning"  # Yellow - attention needed
    ERROR = "error"  # Red - failures/critical
    INFO = "info"  # Blue - neutral information


@dataclass
class ActionButton:
    """An action button for the notification.

    Attributes:
        text: Button label text.
        url: URL to open when clicked.
        style: Visual style (default, primary, danger).
    """

    text: str
    url: str
    style: str = "default"  # default, primary, danger


@dataclass
class CompanyInfo:
    """Enriched company information from domain enrichment.

    Attributes:
        name: Company display name.
        domain: Company domain (e.g., notion.so).
        industry: Industry classification.
        year_founded: Year the company was founded.
        employee_count: Employee count range (e.g., "51-200").
        description: Brief company description.
        logo_url: URL to company logo image.
        linkedin_url: LinkedIn company page URL.
    """

    name: str
    domain: str
    industry: str | None = None
    year_founded: int | None = None
    employee_count: str | None = None
    description: str | None = None
    logo_url: str | None = None
    linkedin_url: str | None = None


@dataclass
class PersonInfo:
    """Enriched person information from email enrichment (Hunter.io).

    This data is only available for Pro/Enterprise workspaces with
    Hunter.io integration configured. Works for all emails including
    free email providers like Gmail.

    Attributes:
        email: The enriched email address.
        first_name: Person's first/given name.
        last_name: Person's last/family name.
        full_name: Computed full name (first + last).
        position: Job title (e.g., "VP of Engineering").
        seniority: Seniority level (e.g., "senior", "executive").
        company_domain: Company domain from employment data.
        linkedin_url: LinkedIn profile URL.
        twitter_handle: Twitter/X handle (without @).
        github_handle: GitHub username.
        location: Location string (e.g., "San Francisco, CA").
    """

    email: str
    first_name: str | None = None
    last_name: str | None = None
    position: str | None = None
    seniority: str | None = None
    company_domain: str | None = None
    linkedin_url: str | None = None
    twitter_handle: str | None = None
    github_handle: str | None = None
    location: str | None = None

    @property
    def full_name(self) -> str | None:
        """Get the person's full name if available."""
        if self.first_name or self.last_name:
            parts = [p for p in [self.first_name, self.last_name] if p]
            return " ".join(parts)
        return None

    @property
    def display_name(self) -> str:
        """Get the best available display name."""
        return self.full_name or self.email


@dataclass
class PaymentInfo:
    """Payment/order details.

    Attributes:
        amount: Payment amount.
        currency: Currency code (e.g., USD).
        interval: Billing interval (monthly, annual, one-time).
        plan_name: Subscription plan name.
        subscription_id: Subscription identifier.
        payment_method: Payment method type (visa, mastercard, bank).
        card_last4: Last 4 digits of card.
        order_number: Order number for e-commerce.
        line_items: Order line items for e-commerce.
        failure_reason: Reason for payment failure.
    """

    amount: float
    currency: str
    interval: str | None = None  # monthly, annual, one-time
    plan_name: str | None = None
    subscription_id: str | None = None
    payment_method: str | None = None  # visa, mastercard, bank
    card_last4: str | None = None
    order_number: str | None = None
    line_items: list[dict] = field(default_factory=list)
    failure_reason: str | None = None

    def get_arr(self) -> float | None:
        """Calculate Annual Recurring Revenue if applicable.

        Returns:
            ARR value or None if not a recurring payment.
        """
        if self.interval == "monthly":
            return self.amount * 12
        elif self.interval == "annual":
            return self.amount
        elif self.interval == "quarterly":
            return self.amount * 4
        return None

    def format_amount_with_arr(self) -> str:
        """Format amount with ARR if applicable.

        Returns:
            Formatted string like "$299.00/mo = $3,588 ARR".
        """
        arr = self.get_arr()
        if self.interval == "monthly" and arr:
            return f"{self.currency} {self.amount:,.2f}/mo = ${arr:,.0f} ARR"
        elif self.interval == "annual" and arr:
            return f"{self.currency} {self.amount:,.2f}/yr ARR"
        elif self.interval == "quarterly" and arr:
            return f"{self.currency} {self.amount:,.2f}/qtr = ${arr:,.0f} ARR"
        return f"{self.currency} {self.amount:,.2f}"


@dataclass
class CustomerInfo:
    """Customer information for the notification.

    Attributes:
        email: Customer email address.
        name: Customer full name.
        company_name: Customer's company name (from webhook, not enrichment).
        tenure_display: Formatted tenure (e.g., "Since Mar 2024").
        ltv_display: Formatted lifetime value (e.g., "$7.1k").
        orders_count: Total number of orders.
        total_spent: Total amount spent.
        status_flags: Status indicators (at_risk, vip, etc.).
    """

    email: str
    name: str | None = None
    company_name: str | None = None
    tenure_display: str | None = None  # "Since Mar 2024"
    ltv_display: str | None = None  # "$7.1k"
    orders_count: int | None = None
    total_spent: float | None = None
    status_flags: list[str] = field(default_factory=list)  # ["at_risk", "vip"]


@dataclass
class InsightInfo:
    """Milestone or insight information.

    Attributes:
        icon: Semantic icon name (celebration, warning, trophy, etc.).
        text: Insight message text.
    """

    icon: str  # Semantic: "celebration", "warning", "trophy", "new", "chart"
    text: str  # "Crossed $5,000 lifetime!"


@dataclass
class TicketInfo:
    """Support ticket information for ticket-related notifications.

    Attributes:
        ticket_id: External ticket identifier.
        subject: Ticket subject/title.
        status: Current ticket status (new, open, pending, solved, etc.).
        priority: Ticket priority (low, normal, high, urgent).
        requester_email: Email of the person who submitted the ticket.
        requester_name: Name of the person who submitted the ticket.
        assignee_name: Name of the assigned support agent.
        description: Ticket description/body text.
        channel: Channel through which the ticket was created (web, email, etc.).
        tags: List of tags associated with the ticket.
        latest_comment: Most recent comment on the ticket.
    """

    ticket_id: str
    subject: str
    status: str
    priority: str | None = None
    requester_email: str | None = None
    requester_name: str | None = None
    assignee_name: str | None = None
    description: str | None = None
    channel: str | None = None
    tags: list[str] = field(default_factory=list)
    latest_comment: str | None = None


@dataclass
class SentimentInfo:
    """Sentiment analysis results for support content.

    Attributes:
        sentiment: Detected sentiment (positive, negative, neutral).
        score: Confidence score between 0.0 and 1.0.
        urgency: Detected urgency level (low, medium, high).
        topics: List of detected topics/categories.
        summary: Brief AI-generated summary of the content.
    """

    sentiment: str  # positive, negative, neutral
    score: float  # 0.0-1.0 confidence
    urgency: str  # low, medium, high
    topics: list[str] = field(default_factory=list)
    summary: str | None = None


@dataclass
class DetailField:
    """A key-value field for detail sections.

    Attributes:
        label: Field label/name.
        value: Field value (will be converted to string).
        icon: Optional semantic icon for the field.
    """

    label: str
    value: str
    icon: str | None = None


@dataclass
class DetailSection:
    """A generic detail section for extensible content.

    Used for non-payment events or additional context that doesn't
    fit the payment/subscription model.

    Attributes:
        title: Section title.
        icon: Semantic icon for the section.
        fields: List of key-value fields.
        text: Optional freeform text content.
        accessory_url: Optional image/icon URL.
    """

    title: str
    icon: str = "info"
    fields: list[DetailField] = field(default_factory=list)
    text: str | None = None
    accessory_url: str | None = None

    def add_field(self, label: str, value: str, icon: str | None = None) -> None:
        """Add a field to the section.

        Args:
            label: Field label/name.
            value: Field value.
            icon: Optional semantic icon.
        """
        self.fields.append(DetailField(label=label, value=value, icon=icon))


@dataclass
class RichNotification:
    """Target-agnostic notification model.

    This is the core model that captures all notification data in a
    platform-independent format. Formatters convert this to specific
    output formats (Slack Block Kit, HTML Email, Discord Embeds, etc.).

    Supports both payment events and non-payment events (usage, support,
    system events) through the generic detail_sections field.

    Attributes:
        type: Notification type enum.
        severity: Visual severity level.
        headline: Main headline text (e.g., "$299.00 from Notion Labs").
        headline_icon: Semantic icon for headline (money, error, celebration).
        insight: Optional milestone insight.
        provider: Provider identifier (chargify, stripe, shopify).
        provider_display: Human-readable provider name.
        payment: Payment/order details (for payment events).
        detail_sections: Generic detail sections (for non-payment events).
        company: Enriched company information.
        customer: Customer information.
        actions: Action buttons.
        metadata: Extensible metadata for custom fields.
    """

    type: NotificationType
    severity: NotificationSeverity

    # Header
    headline: str  # "$299.00 from Notion Labs" or "NPS Response: 9"
    headline_icon: str  # Semantic: "money", "error", "celebration", "feedback"

    # Provider/Source badge
    provider: str  # "chargify", "stripe", "shopify", "intercom", "segment"
    provider_display: str  # "Chargify", "Intercom"

    # Customer (required for most events, optional for system events)
    customer: CustomerInfo | None = None

    # Insight (optional)
    insight: InsightInfo | None = None

    # Payment/Order details (optional - for payment/subscription events)
    payment: PaymentInfo | None = None

    # Support ticket details (optional - for support events)
    ticket: TicketInfo | None = None

    # Sentiment analysis (optional - for support events with AI analysis)
    sentiment: SentimentInfo | None = None

    # Generic detail sections (optional - for non-payment events or extras)
    detail_sections: list[DetailSection] = field(default_factory=list)

    # Company enrichment (optional)
    company: CompanyInfo | None = None

    # Person enrichment (optional - requires Pro/Enterprise + Hunter.io)
    person: PersonInfo | None = None

    # Actions (optional)
    actions: list[ActionButton] = field(default_factory=list)

    # Payment-specific badge info (optional)
    is_recurring: bool = False
    billing_interval: str | None = None  # monthly, annual, quarterly

    # Extensible metadata for custom fields
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def category(self) -> EventCategory:
        """Get the event category for this notification.

        Returns:
            EventCategory enum value.
        """
        return EVENT_CATEGORY_MAP.get(self.type, EventCategory.CUSTOM)

    @property
    def is_payment_event(self) -> bool:
        """Check if this is a payment-related event.

        Returns:
            True if this is a payment or subscription event.
        """
        return self.category in (EventCategory.PAYMENT, EventCategory.SUBSCRIPTION)

    @property
    def is_support_event(self) -> bool:
        """Check if this is a support-related event.

        Returns:
            True if this is a support ticket or feedback event.
        """
        return self.category == EventCategory.SUPPORT

    def get_payment_type_display(self) -> str:
        """Get formatted payment type display.

        Returns:
            String like "Recurring (Monthly)" or "One-Time".
        """
        if self.is_recurring:
            if self.billing_interval:
                return f"Recurring ({self.billing_interval.title()})"
            return "Recurring"
        return "One-Time"

    def add_detail_section(
        self,
        title: str,
        icon: str = "info",
        fields: list[tuple[str, str]] | None = None,
        text: str | None = None,
    ) -> DetailSection:
        """Add a generic detail section to the notification.

        Args:
            title: Section title.
            icon: Semantic icon name.
            fields: Optional list of (label, value) tuples.
            text: Optional freeform text.

        Returns:
            The created DetailSection.
        """
        section = DetailSection(title=title, icon=icon, text=text)
        if fields:
            for label, value in fields:
                section.add_field(label, value)
        self.detail_sections.append(section)
        return section

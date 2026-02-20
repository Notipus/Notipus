"""Dashboard service for handling dashboard data aggregation and processing.

This module provides services for preparing dashboard, billing, and
integration overview data for the application frontend.
"""

import logging
from collections import defaultdict
from datetime import datetime
from datetime import timezone as dt_timezone
from typing import Any

from core.models import Integration, Plan, UserProfile, Workspace, WorkspaceMember
from django.contrib.auth.models import User
from django.db.models import QuerySet
from django.utils import timezone
from webhooks.services.database_lookup import DatabaseLookupService
from webhooks.services.rate_limiter import rate_limiter

logger = logging.getLogger(__name__)


class DashboardService:
    """Service class for dashboard data preparation and business logic.

    Aggregates data from various sources to prepare dashboard displays.
    """

    def __init__(self) -> None:
        """Initialize the dashboard service with dependencies."""
        self.db_service = DatabaseLookupService()

    def get_dashboard_data(self, user: User) -> dict[str, Any] | None:
        """Get all dashboard data for a user.

        Args:
            user: Django User instance.

        Returns:
            Dict with dashboard data or None if user has no workspace.
        """
        # Try to get workspace from WorkspaceMember first
        member = WorkspaceMember.objects.filter(user=user, is_active=True).first()
        workspace = None
        user_profile = None

        if member:
            workspace = member.workspace
        else:
            # Fall back to UserProfile for backward compatibility
            try:
                user_profile = UserProfile.objects.get(user=user)
                workspace = user_profile.workspace
            except UserProfile.DoesNotExist:
                return None

        if not workspace:
            return None

        # Try to get user_profile if we don't have it yet
        if not user_profile:
            try:
                user_profile = UserProfile.objects.get(user=user)
            except UserProfile.DoesNotExist:
                user_profile = None

        return {
            "workspace": workspace,
            "user_profile": user_profile,
            "member": member,
            "integrations": self._get_integration_data(workspace),
            "recent_activity": self._get_recent_activity(workspace),
            "usage_data": self._get_usage_data(workspace),
            "trial_info": self._get_trial_info(workspace),
        }

    def _get_integration_data(self, workspace: Workspace) -> dict[str, Any]:
        """Get integration status and data for the workspace.

        Args:
            workspace: Workspace model instance.

        Returns:
            Dictionary with integration status flags.
        """
        integrations: QuerySet[Integration] = Integration.objects.filter(
            workspace=workspace, is_active=True
        )

        return {
            "integrations": integrations,
            "has_slack": integrations.filter(
                integration_type="slack_notifications"
            ).exists(),
            "has_shopify": integrations.filter(integration_type="shopify").exists(),
            "has_chargify": integrations.filter(integration_type="chargify").exists(),
            "has_stripe": integrations.filter(
                integration_type="stripe_customer"
            ).exists(),
        }

    def _get_recent_activity(self, workspace: Workspace) -> list[dict[str, Any]]:
        """Get and process recent webhook activity for the workspace.

        Args:
            workspace: Workspace model instance.

        Returns:
            List of recent activity records, deduplicated with counts.
        """
        try:
            # Fetch more records than needed to allow for deduplication
            recent_activity_raw = self.db_service.get_recent_webhook_activity(
                workspace_id=str(workspace.uuid), days=7, limit=100
            )
            transformed = self._transform_activity_data(recent_activity_raw)
            deduplicated = self._deduplicate_activity(transformed)
            # Return limited results after deduplication
            return deduplicated[:15]
        except Exception as e:
            logger.warning(f"Error getting recent activity: {e!s}")
            return []

    def _transform_activity_data(
        self, raw_activity: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Transform raw Redis activity data to dashboard format.

        Args:
            raw_activity: List of raw activity records from Redis.

        Returns:
            List of transformed activity records with enriched fields.
        """
        recent_activity: list[dict[str, Any]] = []

        for record in raw_activity:
            try:
                # Parse timestamp
                if "timestamp" in record:
                    timestamp = datetime.fromtimestamp(
                        record["timestamp"], tz=timezone.get_current_timezone()
                    )
                else:
                    timestamp = timezone.now()

                activity_item: dict[str, Any] = {
                    # Basic fields
                    "type": record.get("type"),
                    "event_type": record.get("event_type"),
                    "provider": record.get("provider"),
                    "status": record.get("status"),
                    "severity": record.get("severity", "info"),
                    "amount": record.get("amount"),
                    "currency": record.get("currency"),
                    "processed_at": timestamp,
                    "external_id": record.get("external_id"),
                    "customer_id": record.get("customer_id"),
                    # Enriched fields
                    "headline": record.get("headline"),
                    "company_name": record.get("company_name"),
                    "company_logo_url": record.get("company_logo_url"),
                    "company_domain": record.get("company_domain"),
                    "customer_email": record.get("customer_email"),
                    "customer_name": record.get("customer_name"),
                    "customer_ltv": record.get("customer_ltv"),
                    "customer_tenure": record.get("customer_tenure"),
                    "customer_status_flags": record.get("customer_status_flags", []),
                    "insight_text": record.get("insight_text"),
                    "insight_icon": record.get("insight_icon"),
                    "plan_name": record.get("plan_name"),
                    "payment_method": record.get("payment_method"),
                    "card_last4": record.get("card_last4"),
                }

                # Add type-specific fields
                if record.get("type") == "order":
                    activity_item["order_number"] = record.get("order_number", "")

                recent_activity.append(activity_item)

            except Exception as e:
                logger.warning(f"Error processing webhook activity record: {e!s}")
                continue

        return recent_activity

    def _deduplicate_activity(
        self, activity: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Deduplicate activity records by grouping similar events.

        Groups events by customer_email + event_type + date (same day).
        Keeps the most recent event as representative and adds event_count.

        Args:
            activity: List of transformed activity records.

        Returns:
            List of deduplicated activity records with event_count field.
        """
        if not activity:
            return []

        # Group events by deduplication key
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)

        for item in activity:
            # Create deduplication key: customer_email + event_type + date
            customer_email = item.get("customer_email") or item.get("customer_id") or ""
            event_type = item.get("event_type") or item.get("type") or ""
            timestamp = item.get("processed_at")

            if timestamp:
                date_str = timestamp.strftime("%Y-%m-%d")
            else:
                date_str = timezone.now().strftime("%Y-%m-%d")

            dedup_key = f"{customer_email}:{event_type}:{date_str}"
            groups[dedup_key].append(item)

        # Build deduplicated list
        deduplicated: list[dict[str, Any]] = []

        # Use a minimum datetime as fallback for sorting
        # (avoids creating new datetime per comparison)
        min_time = datetime.min.replace(tzinfo=dt_timezone.utc)

        for _dedup_key, items in groups.items():
            # Sort by timestamp descending to get most recent first
            items.sort(key=lambda x: x.get("processed_at") or min_time, reverse=True)

            # Use the most recent event as representative
            representative = items[0].copy()
            representative["event_count"] = len(items)

            # Aggregate total amount if there are multiple events
            if len(items) > 1:
                total_amount = sum(
                    item.get("amount") or 0
                    for item in items
                    if item.get("amount") is not None
                )
                if total_amount > 0:
                    representative["total_amount"] = total_amount

            deduplicated.append(representative)

        # Sort by most recent timestamp
        deduplicated.sort(key=lambda x: x.get("processed_at") or min_time, reverse=True)

        return deduplicated

    def _get_usage_data(self, workspace: Workspace) -> dict[str, Any]:
        """Get rate limiting and usage statistics for the workspace.

        Args:
            workspace: Workspace model instance.

        Returns:
            Dictionary with usage data and rate limit info.
        """
        try:
            # Get rate limit info and usage stats
            is_allowed, rate_limit_info = rate_limiter.check_rate_limit(workspace)
            usage_stats = rate_limiter.get_usage_stats(workspace, months=3)

            # Calculate usage percentage
            current_usage = rate_limit_info.get("current_usage", 0)
            limit = rate_limit_info.get("limit", 1000)
            usage_percentage = (current_usage / limit * 100) if limit > 0 else 0

            return {
                "is_allowed": is_allowed,
                "rate_limit_info": rate_limit_info,
                "usage_stats": usage_stats,
                "usage_percentage": min(usage_percentage, 100),  # Cap at 100%
            }
        except Exception as e:
            logger.error(f"Error getting usage data: {e!s}")
            return {
                "is_allowed": True,
                "rate_limit_info": {},
                "usage_stats": {},
                "usage_percentage": 0,
            }

    def _get_trial_info(self, workspace: Workspace) -> dict[str, Any]:
        """Get trial information for the workspace.

        Args:
            workspace: Workspace model instance.

        Returns:
            Dictionary with trial status and remaining days.
        """
        trial_days_remaining = 0
        is_trial = workspace.subscription_status == "trial"

        if is_trial and workspace.trial_end_date:
            trial_days_remaining = max(
                0, (workspace.trial_end_date - timezone.now()).days
            )

        return {
            "is_trial": is_trial,
            "trial_days_remaining": trial_days_remaining,
            "trial_end_date": workspace.trial_end_date,
        }


class BillingService:
    """Service class for billing-related operations.

    Provides methods for retrieving plan information and
    billing dashboard data, with Stripe as the source of truth.
    """

    def get_available_plans(
        self, current_plan: str, use_stripe: bool = True, include_free: bool = False
    ) -> list[dict[str, Any]]:
        """Get available plans for upgrade, excluding current plan.

        Fetches plans from Stripe if available, falls back to local database.

        Args:
            current_plan: Name of the current subscription plan.
            use_stripe: Whether to fetch from Stripe (default True).
            include_free: Whether to include free plan (default False).

        Returns:
            List of available plan dictionaries.
        """
        if use_stripe:
            plans = self._get_plans_from_stripe(current_plan, include_free)
            if plans:
                return plans

        # Fall back to local database
        return self._get_plans_from_database(current_plan, include_free)

    def _get_plans_from_stripe(
        self, current_plan: str, include_free: bool = False
    ) -> list[dict[str, Any]]:
        """Fetch available plans from Stripe.

        Args:
            current_plan: Name of the current subscription plan to exclude.
            include_free: Whether to include the free plan.

        Returns:
            List of plan dictionaries from Stripe, or empty list on failure.
        """
        try:
            from core.services.stripe import StripeAPI

            stripe_api = StripeAPI()
            prices = stripe_api.list_prices(active_only=True)

            # Filter to monthly recurring prices and exclude current plan
            plans = []
            for price in prices:
                if not price.get("recurring"):
                    continue
                if price["recurring"].get("interval") != "month":
                    continue

                # Get plan name from metadata or product name
                metadata = price.get("metadata", {})
                plan_name = metadata.get("plan_name", "").lower()

                # Skip current plan
                if plan_name == current_plan:
                    continue

                # Skip free plans (free is added from database if needed)
                if price.get("unit_amount", 0) == 0:
                    continue

                price_amount = (price.get("unit_amount", 0) or 0) / 100
                # Format price as integer if it's a whole number (e.g., 29 not 29.0)
                is_whole_number = price_amount == int(price_amount)
                formatted_price = int(price_amount) if is_whole_number else price_amount
                plans.append(
                    {
                        "id": plan_name or price["product_id"],
                        "name": price.get("product_name", "Unknown Plan"),
                        "description": price.get("product_description", ""),
                        "price": formatted_price,
                        "currency": "USD",
                        "interval": "month",
                        "features": price.get("features", []),
                        "stripe_price_id": price["id"],
                        "recommended": plan_name == "pro",
                    }
                )

            # Add free plan from database if requested and not current plan
            if include_free and current_plan != "free":
                free_plan = self._get_free_plan_from_database()
                if free_plan:
                    plans.insert(0, free_plan)  # Put free plan first

            # Sort by price ascending (small to large, left to right)
            return sorted(plans, key=lambda p: p["price"])

        except Exception as e:
            logger.warning(f"Error fetching plans from Stripe: {e!s}")
            return []

    def _get_free_plan_from_database(self) -> dict[str, Any] | None:
        """Get the free plan from the database.

        Returns:
            Free plan dictionary or None if not found.
        """
        try:
            plan = Plan.objects.get(name="free", is_active=True)
            return {
                "id": plan.name,
                "name": plan.display_name,
                "description": plan.description,
                "price": 0,
                "currency": "USD",
                "interval": "month",
                "features": plan.features,
                "stripe_price_id": "",
                "recommended": False,
            }
        except Plan.DoesNotExist:
            return None

    def _get_plans_from_database(
        self, current_plan: str, include_free: bool = False
    ) -> list[dict[str, Any]]:
        """Fetch available plans from local database.

        Args:
            current_plan: Name of the current subscription plan to exclude.
            include_free: Whether to include the free plan.

        Returns:
            List of plan dictionaries from database.
        """
        try:
            # Build exclusion list
            exclude_plans = [current_plan]
            if not include_free:
                exclude_plans.append("free")

            plans = Plan.objects.filter(is_active=True).exclude(name__in=exclude_plans)
            result = []
            for plan in plans:
                price = float(plan.price_monthly)
                result.append(
                    {
                        "id": plan.name,
                        "name": plan.display_name,
                        "description": plan.description,
                        "price": int(price) if price == int(price) else price,
                        "currency": "USD",
                        "interval": "month",
                        "features": plan.features,
                        "stripe_price_id": plan.stripe_price_id_monthly,
                        "recommended": plan.name == "pro",
                    }
                )
            # Sort by price ascending (small to large, left to right)
            return sorted(result, key=lambda p: p["price"])
        except Exception as e:
            logger.error(f"Error getting plans from database: {e!s}")
            return []

    def get_stripe_subscription_info(
        self, workspace: Workspace
    ) -> dict[str, Any] | None:
        """Get current subscription info from Stripe.

        Args:
            workspace: Workspace model instance.

        Returns:
            Subscription info dictionary or None if not found.
        """
        if not workspace.stripe_customer_id:
            return None

        try:
            from core.services.stripe import StripeAPI

            stripe_api = StripeAPI()
            subscriptions = stripe_api.get_customer_subscriptions(
                workspace.stripe_customer_id, status="active"
            )

            if not subscriptions:
                return None

            # Return the first active subscription
            sub = subscriptions[0]
            return {
                "id": sub["id"],
                "status": sub["status"],
                "current_period_end": sub.get("current_period_end"),
                "cancel_at_period_end": sub.get("cancel_at_period_end", False),
                "items": sub.get("items", []),
            }

        except Exception as e:
            logger.warning(f"Error fetching subscription from Stripe: {e!s}")
            return None

    def get_billing_dashboard_data(self, workspace: Workspace) -> dict[str, Any]:
        """Get billing dashboard data for a workspace.

        Includes real-time data from Stripe when available.

        Args:
            workspace: Workspace model instance.

        Returns:
            Dictionary with billing dashboard information.
        """
        dashboard_service = DashboardService()
        usage_data = dashboard_service._get_usage_data(workspace)
        trial_info = dashboard_service._get_trial_info(workspace)

        # Get Stripe subscription info if available
        stripe_subscription = self.get_stripe_subscription_info(workspace)

        # For billing dashboard, include Free plan as option (except if already on free)
        available_plans = self.get_available_plans(
            workspace.subscription_plan, include_free=True
        )

        return {
            "workspace": workspace,
            "usage_data": usage_data,
            "trial_info": trial_info,
            "available_plans": available_plans,
            "current_plan": workspace.subscription_plan,
            "stripe_subscription": stripe_subscription,
        }


class IntegrationService:
    """Service class for integration-related operations.

    Provides methods for retrieving integration status and
    overview data.
    """

    def get_integration_overview(self, workspace: Workspace) -> dict[str, Any]:
        """Get integration overview data for the integrations page.

        Args:
            workspace: Workspace model instance.

        Returns:
            Dictionary with integration sources and destinations.
        """
        current_integrations: QuerySet[Integration] = Integration.objects.filter(
            workspace=workspace, is_active=True
        )

        # Event Sources - Services that send webhooks TO Notipus
        event_sources: list[dict[str, Any]] = [
            {
                "id": "shopify",
                "name": "Shopify",
                "description": (
                    "E-commerce events from your Shopify store "
                    "(orders, payments, customers)"
                ),
                "connected": current_integrations.filter(
                    integration_type="shopify"
                ).exists(),
                "category": "E-commerce",
            },
            {
                "id": "chargify",
                "name": "Chargify / Maxio Advanced Billing",
                "description": (
                    "Subscription billing events (renewals, cancellations, upgrades)"
                ),
                "connected": current_integrations.filter(
                    integration_type="chargify"
                ).exists(),
                "category": "Billing",
            },
            {
                "id": "stripe_customer",
                "name": "Stripe Payments",
                "description": (
                    "Customer payment events (successful payments, failed charges)"
                ),
                "connected": current_integrations.filter(
                    integration_type="stripe_customer"
                ).exists(),
                "category": "Payments",
            },
        ]

        # Notification Destinations - Services that receive notifications FROM Notipus
        notification_destinations: list[dict[str, Any]] = [
            {
                "id": "slack_notifications",
                "name": "Slack",
                "description": (
                    "Real-time notifications sent to your team's Slack workspace"
                ),
                "connected": current_integrations.filter(
                    integration_type="slack_notifications"
                ).exists(),
                "category": "Team Communication",
            },
        ]

        # Enrichment Services - Services that enhance customer data
        enrichment_services: list[dict[str, Any]] = [
            {
                "id": "hunter",
                "name": "Hunter.io",
                "description": (
                    "Enrich customer data with person info (name, job title, LinkedIn)"
                ),
                "connected": current_integrations.filter(
                    integration_type="hunter_enrichment"
                ).exists(),
                "category": "Email Enrichment",
            },
        ]

        return {
            "workspace": workspace,
            "event_sources": event_sources,
            "notification_channels": notification_destinations,
            "enrichment_services": enrichment_services,
            "current_integrations": current_integrations,
        }

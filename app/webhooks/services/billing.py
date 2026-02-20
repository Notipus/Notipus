"""Billing service for handling Stripe webhook events.

This module processes billing-related webhook events from Stripe
and updates workspace subscription status accordingly.
"""

import logging
from datetime import datetime, timezone
from typing import Any, cast

from core.models import Workspace
from core.services.stripe import StripeAPI

logger = logging.getLogger(__name__)

# Map Stripe statuses to our internal statuses
STRIPE_STATUS_MAPPING: dict[str, str] = {
    "active": "active",
    "trialing": "trial",
    "past_due": "past_due",
    "canceled": "cancelled",
    "unpaid": "past_due",
    "incomplete": "trial",
    "incomplete_expired": "cancelled",
}


class BillingService:
    """Service for handling billing-related webhook events from Stripe.

    Provides static methods for processing various subscription and
    payment events and updating workspace records.
    """

    @staticmethod
    def _get_active_subscription(subscriptions: list[dict[str, Any]]) -> dict[str, Any]:
        """Get the most relevant subscription from a list.

        Prefers active/trialing subscriptions over cancelled ones.

        Args:
            subscriptions: List of subscription dictionaries.

        Returns:
            The most relevant subscription dictionary.
        """
        for sub in subscriptions:
            if sub["status"] in ("active", "trialing", "past_due"):
                return sub
        return subscriptions[0]

    @staticmethod
    def _extract_plan_name_from_subscription(
        subscription: dict[str, Any],
    ) -> str | None:
        """Extract plan name from subscription items.

        Prefers Product metadata.plan_name (most reliable) over
        normalizing the product name string.

        Args:
            subscription: Subscription dictionary with items.

        Returns:
            Plan name string or None.
        """
        if not subscription.get("items"):
            return None

        first_item = subscription["items"][0]

        # Prefer plan_name from Product metadata (most reliable)
        plan_name = first_item.get("plan_name")
        if plan_name:
            return cast(str, plan_name)

        # Fall back to normalizing product name
        product_name = first_item.get("product_name", "")
        if not product_name:
            return None

        # Convert "Notipus Pro Plan" or "Pro Plan" to "pro"
        normalized: str = (
            product_name.lower().replace("notipus ", "").replace(" plan", "").strip()
        )
        return normalized

    @staticmethod
    def sync_workspace_from_stripe(customer_id: str) -> bool:
        """Sync workspace subscription state from Stripe.

        Fetches the current subscription state directly from Stripe API
        and updates the workspace. This ensures we have accurate state
        even if webhooks were missed or processed out of order.

        Args:
            customer_id: The Stripe customer ID.

        Returns:
            True if sync was successful, False otherwise.
        """
        try:
            workspace = Workspace.objects.filter(stripe_customer_id=customer_id).first()

            if not workspace:
                logger.warning(
                    f"No workspace found for customer {customer_id} during sync"
                )
                return False

            stripe_api = StripeAPI()
            subscriptions = stripe_api.get_customer_subscriptions(
                customer_id, status="all"
            )

            if not subscriptions:
                logger.info(f"No subscriptions found for customer {customer_id}")
                return True

            active_sub = BillingService._get_active_subscription(subscriptions)
            stripe_status = active_sub.get("status", "active")
            internal_status = STRIPE_STATUS_MAPPING.get(stripe_status, "active")
            plan_name = BillingService._extract_plan_name_from_subscription(active_sub)

            # Build update data
            update_data: dict[str, Any] = {"subscription_status": internal_status}

            if plan_name:
                update_data["subscription_plan"] = plan_name

            if active_sub.get("current_period_end"):
                update_data["billing_cycle_anchor"] = active_sub["current_period_end"]

            if stripe_status == "trialing" and active_sub.get("current_period_end"):
                update_data["trial_end_date"] = datetime.fromtimestamp(
                    active_sub["current_period_end"], tz=timezone.utc
                )

            Workspace.objects.filter(id=workspace.id).update(**update_data)

            logger.info(
                f"Synced workspace {workspace.name} from Stripe: "
                f"status={internal_status}, plan={plan_name}"
            )
            return True

        except Exception as e:
            logger.error(f"Error syncing workspace from Stripe: {e!s}")
            return False

    @staticmethod
    def _get_customer_id(data: dict[str, Any], data_type: str) -> str | None:
        """Extract customer ID from webhook data.

        Args:
            data: Webhook data dictionary.
            data_type: Description of data type for logging.

        Returns:
            Customer ID string, or None if not found.
        """
        customer_id = data.get("customer")
        if not customer_id:
            logger.error(f"Missing customer ID in {data_type} data")
            return None
        return cast(str, customer_id)

    @staticmethod
    def handle_subscription_created(subscription: dict[str, Any]) -> None:
        """Handle subscription created event.

        Args:
            subscription: Subscription data from Stripe webhook.
        """
        try:
            customer_id = BillingService._get_customer_id(subscription, "subscription")
            if not customer_id:
                return

            # Don't set subscription_plan here - sync_workspace_from_stripe will
            # properly extract and normalize the plan name from the Product.
            # Previously this was setting plan_id (a Price ID) which is wrong.
            updated_count = Workspace.objects.filter(
                stripe_customer_id=customer_id
            ).update(
                subscription_status="active",
                billing_cycle_anchor=subscription.get("current_period_start"),
            )

            if updated_count > 0:
                logger.info(
                    f"Subscription created for customer {customer_id}, syncing..."
                )
                # Sync full state from Stripe (properly extracts plan name)
                BillingService.sync_workspace_from_stripe(customer_id)
            else:
                logger.warning(f"No workspace found for customer {customer_id}")

        except Exception as e:
            logger.error(f"Error handling subscription created: {e!s}")

    @staticmethod
    def handle_subscription_updated(subscription: dict[str, Any]) -> None:
        """Handle subscription updated event (plan changes, status changes).

        Args:
            subscription: Subscription data from Stripe webhook.
        """
        try:
            customer_id = BillingService._get_customer_id(subscription, "subscription")
            if not customer_id:
                return

            # Extract subscription status
            status = subscription.get("status", "active")

            # Map Stripe statuses to our internal statuses
            internal_status = STRIPE_STATUS_MAPPING.get(status, "active")

            # Don't set subscription_plan here - sync_workspace_from_stripe will
            # properly extract and normalize the plan name from the Product.
            # Previously this was setting plan_id (a Price ID) which is wrong.
            update_data: dict[str, Any] = {"subscription_status": internal_status}

            # Update billing cycle anchor if present
            if subscription.get("current_period_end"):
                update_data["billing_cycle_anchor"] = subscription.get(
                    "current_period_end"
                )

            updated_count = Workspace.objects.filter(
                stripe_customer_id=customer_id
            ).update(**update_data)

            if updated_count > 0:
                logger.info(
                    f"Updated subscription status to {internal_status} "
                    f"for customer {customer_id}, syncing..."
                )
                # Sync full state from Stripe (properly extracts plan name)
                BillingService.sync_workspace_from_stripe(customer_id)
            else:
                logger.warning(f"No workspace found for customer {customer_id}")

        except Exception as e:
            logger.error(f"Error handling subscription updated: {e!s}")

    @staticmethod
    def handle_subscription_deleted(subscription: dict[str, Any]) -> None:
        """Handle subscription deleted/cancelled event.

        Args:
            subscription: Subscription data from Stripe webhook.
        """
        try:
            customer_id = BillingService._get_customer_id(subscription, "subscription")
            if not customer_id:
                return

            updated_count = Workspace.objects.filter(
                stripe_customer_id=customer_id
            ).update(subscription_status="cancelled")

            if updated_count > 0:
                logger.info(
                    f"Marked subscription as cancelled for customer {customer_id}"
                )
            else:
                logger.warning(f"No workspace found for customer {customer_id}")

        except Exception as e:
            logger.error(f"Error handling subscription deleted: {e!s}")

    @staticmethod
    def handle_payment_success(invoice: dict[str, Any]) -> None:
        """Handle successful payment event.

        Args:
            invoice: Invoice data from Stripe webhook.
        """
        try:
            customer_id = invoice.get("customer")
            if not customer_id:
                logger.error("Missing customer ID in invoice data")
                return

            period_end = invoice.get("period_end")

            # Prepare update data
            update_data: dict[str, Any] = {"subscription_status": "active"}
            if period_end:
                update_data["billing_cycle_anchor"] = period_end

            updated_count = Workspace.objects.filter(
                stripe_customer_id=customer_id
            ).update(**update_data)

            if updated_count > 0:
                logger.info(
                    f"Updated payment status to active for customer {customer_id}"
                )
            else:
                logger.warning(f"No workspace found for customer {customer_id}")

        except Exception as e:
            logger.error(f"Error handling payment success: {e!s}")

    @staticmethod
    def handle_payment_failed(invoice: dict[str, Any]) -> None:
        """Handle failed payment event.

        Args:
            invoice: Invoice data from Stripe webhook.
        """
        try:
            customer_id = invoice.get("customer")
            if not customer_id:
                logger.error("Missing customer ID in invoice data")
                return

            updated_count = Workspace.objects.filter(
                stripe_customer_id=customer_id
            ).update(subscription_status="past_due")

            if updated_count > 0:
                logger.warning(
                    f"Updated payment status to past_due for customer {customer_id}"
                )
            else:
                logger.warning(f"No workspace found for customer {customer_id}")

        except Exception as e:
            logger.error(f"Error handling payment failure: {e!s}")

    @staticmethod
    def handle_checkout_completed(session: dict[str, Any]) -> None:
        """Handle checkout.session.completed event.

        This is triggered when a customer completes checkout and the
        subscription is created. Links the subscription to the organization.

        Args:
            session: Checkout session data from Stripe webhook.
        """
        try:
            customer_id = session.get("customer")
            if not customer_id:
                logger.error("Missing customer ID in checkout session")
                return

            # Extract metadata with workspace and plan info
            metadata = session.get("metadata", {})
            workspace_id = metadata.get("workspace_id") or metadata.get(
                "organization_id"
            )
            plan_name = metadata.get("plan_name")

            subscription_id = session.get("subscription")

            # Update workspace with new subscription status
            update_data: dict[str, Any] = {
                "subscription_status": "active",
                "payment_method_added": True,
            }

            if plan_name:
                update_data["subscription_plan"] = plan_name

            # Find workspace by customer ID or workspace ID from metadata
            if workspace_id:
                updated_count = Workspace.objects.filter(id=workspace_id).update(
                    **update_data
                )
            else:
                updated_count = Workspace.objects.filter(
                    stripe_customer_id=customer_id
                ).update(**update_data)

            if updated_count > 0:
                logger.info(
                    f"Checkout completed for customer {customer_id}, "
                    f"subscription: {subscription_id}, plan: {plan_name}"
                )
                # Verify/sync full state from Stripe (catches any drift)
                BillingService.sync_workspace_from_stripe(customer_id)
            else:
                logger.warning(
                    f"No workspace found for checkout session. "
                    f"Customer: {customer_id}, Workspace ID: {workspace_id}"
                )

        except Exception as e:
            logger.error(f"Error handling checkout completed: {e!s}")

    @staticmethod
    def handle_trial_ending(subscription: dict[str, Any]) -> None:
        """Handle trial ending notification (3 days before trial ends).

        This event is fired when a subscription's trial is about to end.
        Can be used to send reminder notifications to customers.

        Args:
            subscription: Subscription data from Stripe webhook.
        """
        try:
            customer_id = BillingService._get_customer_id(subscription, "trial ending")
            if not customer_id:
                return

            trial_end = subscription.get("trial_end")

            # Find workspace and log the event
            ws = Workspace.objects.filter(stripe_customer_id=customer_id).first()

            if ws:
                logger.info(
                    f"Trial ending soon for workspace {ws.name} "
                    f"(customer: {customer_id}), trial_end: {trial_end}"
                )
                # TODO: Send notification email to workspace admins
                # TODO: Trigger Slack notification if configured
            else:
                logger.warning(f"Trial ending for unknown customer {customer_id}")

        except Exception as e:
            logger.error(f"Error handling trial ending: {e!s}")

    @staticmethod
    def handle_invoice_paid(invoice: dict[str, Any]) -> None:
        """Handle invoice.paid event.

        This confirms that an invoice was paid successfully.
        Similar to payment_success but for invoice-specific events.

        Args:
            invoice: Invoice data from Stripe webhook.
        """
        try:
            customer_id = invoice.get("customer")
            if not customer_id:
                logger.error("Missing customer ID in paid invoice")
                return

            # Mark organization as active with payment method confirmed
            update_data: dict[str, Any] = {
                "subscription_status": "active",
                "payment_method_added": True,
            }

            # Update billing cycle anchor if period_end is present
            period_end = invoice.get("period_end")
            if period_end:
                update_data["billing_cycle_anchor"] = period_end

            updated_count = Workspace.objects.filter(
                stripe_customer_id=customer_id
            ).update(**update_data)

            if updated_count > 0:
                logger.info(f"Invoice paid for customer {customer_id}")
            else:
                logger.warning(f"No workspace found for paid invoice: {customer_id}")

        except Exception as e:
            logger.error(f"Error handling invoice paid: {e!s}")

    @staticmethod
    def handle_payment_action_required(invoice: dict[str, Any]) -> None:
        """Handle invoice.payment_action_required event.

        This is triggered when a payment requires customer action,
        such as 3D Secure authentication.

        Args:
            invoice: Invoice data from Stripe webhook.
        """
        try:
            customer_id = invoice.get("customer")
            if not customer_id:
                logger.error("Missing customer ID in action required invoice")
                return

            hosted_invoice_url = invoice.get("hosted_invoice_url")

            # Find workspace and log the event
            ws = Workspace.objects.filter(stripe_customer_id=customer_id).first()

            if ws:
                logger.warning(
                    f"Payment action required for workspace {ws.name} "
                    f"(customer: {customer_id}). Invoice URL: {hosted_invoice_url}"
                )
                # TODO: Send notification email to workspace admins
                # with link to complete payment
            else:
                logger.warning(
                    f"Payment action required for unknown customer: {customer_id}"
                )

        except Exception as e:
            logger.error(f"Error handling payment action required: {e!s}")

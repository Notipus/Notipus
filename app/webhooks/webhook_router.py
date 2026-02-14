import json
import logging
from typing import Any

from core.models import Integration, Workspace
from django.conf import settings
from django.http import HttpRequest, JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .exceptions import WebhookError, WebhookSignatureError
from .services.event_consolidation import event_consolidation_service
from .services.pending_event_queue import pending_event_queue
from .services.rate_limiter import RateLimitException, rate_limiter
from .services.thread_mapping import thread_mapping_service
from .services.webhook_storage import webhook_storage_service

logger = logging.getLogger(__name__)

# Event types that benefit from aggregation even without idempotency_key.
# Subscription events need invoice events for customer email.
_AGGREGATABLE_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "subscription_created",
        "trial_started",
        "invoice_paid",
        "payment_success",
    }
)

# Providers that send complete data in one webhook and don't need aggregation.
# Process these immediately instead of queuing with in-memory timers that
# die on worker recycling.
_IMMEDIATE_PROCESSING_PROVIDERS: frozenset[str] = frozenset(
    {
        "customer_shopify",
        "customer_chargify",
    }
)


def _log_webhook_payload(
    request: HttpRequest, provider_name: str, workspace_uuid: Optional[str] = None
) -> None:
    """
    Log the raw webhook payload for analysis when LOG_WEBHOOKS is enabled.

    This logs the complete request body and relevant headers to help with
    debugging and understanding webhook patterns from providers.
    """
    if not settings.LOG_WEBHOOKS:
        return

    try:
        # Get the raw body
        raw_body = request.body.decode("utf-8")

        # Try to parse as JSON for prettier logging
        try:
            body_data = json.loads(raw_body)
            body_str = json.dumps(body_data, indent=2, default=str)
        except (json.JSONDecodeError, TypeError):
            # Not JSON, log as-is (could be form data)
            body_str = raw_body

        # Extract relevant headers (excluding sensitive auth headers)
        relevant_headers = {
            "Content-Type": request.headers.get("Content-Type"),
            "Content-Length": request.headers.get("Content-Length"),
            "User-Agent": request.headers.get("User-Agent"),
            "X-Forwarded-For": request.headers.get("X-Forwarded-For"),
        }

        # Add provider-specific signature headers for reference
        signature_headers = [
            "X-Shopify-Hmac-SHA256",
            "Stripe-Signature",
            "X-Chargify-Webhook-Signature-Hmac-Sha-256",
            "X-Zendesk-Webhook-Signature",
        ]
        for header in signature_headers:
            if header in request.headers:
                # Log presence of signature but not the value for security
                relevant_headers[header] = "[PRESENT]"

        logger.info(
            f"WEBHOOK_LOG [{provider_name}] "
            f"workspace={workspace_uuid or 'global'} "
            f"method={request.method} path={request.path}",
            extra={
                "webhook_provider": provider_name,
                "workspace_uuid": workspace_uuid,
                "headers": relevant_headers,
                "body": body_str,
            },
        )

        # Store raw webhook in Redis for debugging (7-day retention)
        webhook_storage_service.store_webhook(request, provider_name, workspace_uuid)

    except Exception as e:
        # Don't let logging errors break webhook processing
        logger.warning(f"Failed to log webhook payload: {e}")


def create_success_response(message: str) -> dict:
    """Create standardized success response"""
    return {"status": "success", "message": message}


def create_error_response(error: Exception, status_code: int = 500) -> dict:
    """Create standardized error response"""
    return {
        "status": "error",
        "error": type(error).__name__,
        "message": str(error),
        "code": status_code,
    }


def _handle_rate_limiting(workspace: Workspace) -> JsonResponse | None:
    """
    Handle rate limiting for workspace.
    Returns response if rate limited, None otherwise.
    """
    if not workspace:
        return None

    try:
        rate_limit_info = rate_limiter.enforce_rate_limit(workspace)
        logger.info(
            f"Rate limit check passed for workspace {workspace.uuid}: "
            f"{rate_limit_info['current_usage']}/{rate_limit_info['limit']}"
        )
        return None  # No rate limiting
    except RateLimitException as e:
        logger.warning(f"Rate limit exceeded for workspace {workspace.uuid}: {str(e)}")
        error_response = create_error_response(e, 429)
        response = JsonResponse(error_response, status=429)

        # Add rate limit headers
        rate_limit_headers = rate_limiter.get_rate_limit_headers(
            {
                "limit": e.limit,
                "current_usage": e.current_usage,
                "remaining": 0,
                "reset_time": e.reset_time,
                "plan": workspace.subscription_plan,
            }
        )
        for header_name, header_value in rate_limit_headers.items():
            response[header_name] = header_value

        return response


def _validate_and_parse_webhook(
    request: HttpRequest, provider: Any
) -> dict[str, Any] | None:
    """Validate and parse webhook. Returns None for test webhooks."""
    if not provider.validate_webhook(request):
        raise WebhookSignatureError()

    event_data = provider.parse_webhook(request)
    return event_data


def _add_rate_limit_headers(
    response: JsonResponse, rate_limit_info: dict[str, Any] | None
) -> None:
    """Add rate limit headers to response if rate_limit_info is provided."""
    if rate_limit_info:
        rate_limit_headers = rate_limiter.get_rate_limit_headers(rate_limit_info)
        for header_name, header_value in rate_limit_headers.items():
            response[header_name] = header_value


def _get_slack_webhook_url(workspace: Workspace | None) -> str | None:
    """Get Slack webhook URL for a workspace."""
    if not workspace:
        return None

    try:
        slack_integration = Integration.objects.get(
            workspace=workspace,
            integration_type="slack_notifications",
            is_active=True,
        )
        # Get webhook URL from incoming_webhook in oauth_credentials
        incoming_webhook = slack_integration.oauth_credentials.get(
            "incoming_webhook", {}
        )
        return incoming_webhook.get("url")
    except Integration.DoesNotExist:
        logger.warning(
            f"No active Slack integration found for workspace {workspace.uuid}"
        )
        return None


def _get_slack_credentials(workspace: Workspace | None) -> dict[str, Any] | None:
    """Get Slack credentials for a workspace (webhook URL, bot token, channel).

    For threading support, we need the bot_token and channel_id.
    Falls back to webhook-only if bot token isn't available.

    Args:
        workspace: Workspace model instance.

    Returns:
        Dict with 'webhook_url' and optionally 'bot_token' and 'channel',
        or None if not configured.
    """
    if not workspace:
        return None

    try:
        slack_integration = Integration.objects.get(
            workspace=workspace,
            integration_type="slack_notifications",
            is_active=True,
        )
        oauth_credentials = slack_integration.oauth_credentials or {}
        incoming_webhook = oauth_credentials.get("incoming_webhook", {})

        credentials: dict[str, Any] = {}

        # Get webhook URL
        webhook_url = incoming_webhook.get("url")
        if webhook_url:
            credentials["webhook_url"] = webhook_url

        # Get bot token for API-based sending (enables threading)
        bot_token = oauth_credentials.get("access_token")
        if bot_token:
            credentials["bot_token"] = bot_token

        # Get channel ID (from webhook or explicit setting)
        channel_id = incoming_webhook.get("channel_id")
        if channel_id:
            credentials["channel"] = channel_id

        return credentials if credentials else None

    except Integration.DoesNotExist:
        logger.warning(
            f"No active Slack integration found for workspace {workspace.uuid}"
        )
        return None


def _get_thread_info(
    workspace: Workspace,
    event_data: dict[str, Any],
) -> tuple[str | None, str | None]:
    """Get thread info for Zendesk ticket events.

    Args:
        workspace: Workspace model instance.
        event_data: Event data dictionary.

    Returns:
        Tuple of (entity_type, entity_id) for thread lookup, or (None, None).
    """
    event_type = event_data.get("type", "")
    if not event_type.startswith("support_ticket"):
        return None, None

    provider = event_data.get("provider", "")
    metadata = event_data.get("metadata", {})
    ticket_id = metadata.get("ticket_id") or event_data.get("external_id")

    if provider == "zendesk" and ticket_id:
        return f"{provider}_ticket", str(ticket_id)

    return None, None


def _process_webhook_data(
    event_data: dict[str, Any],
    provider: Any,
    provider_name: str,
    workspace: Workspace | None = None,
) -> JsonResponse:
    """Process webhook data and return success response.

    For events with an idempotency_key (Stripe), queues the event for
    delayed processing to allow related events to arrive first.
    Events without idempotency_key are processed immediately.

    The delayed processing ensures we have complete data (like customer
    email from invoice events) before sending notifications.
    """
    # Get customer data from webhook payload
    customer_data = provider.get_customer_data(event_data.get("customer_id", ""))

    event_type = event_data.get("type", "")
    workspace_id = str(workspace.uuid) if workspace else "global"
    external_id = event_data.get("external_id", "")
    idempotency_key = event_data.get("idempotency_key")

    # Check for exact duplicate (same external_id) - applies to all events
    if event_consolidation_service.is_duplicate(workspace_id, external_id):
        logger.info(
            f"Skipping duplicate event {external_id} for workspace {workspace_id}"
        )
        return JsonResponse(
            create_success_response(
                f"{provider_name} webhook processed (duplicate suppressed)"
            ),
            status=200,
        )

    # Record event ID to prevent exact duplicates
    event_consolidation_service.record_event(
        event_type=event_type,
        customer_id=event_data.get("customer_id", ""),
        workspace_id=workspace_id,
        external_id=external_id,
    )

    # Providers with complete data in one webhook - process immediately
    if provider_name in _IMMEDIATE_PROCESSING_PROVIDERS:
        return _process_immediately(event_data, customer_data, provider_name, workspace)

    # Queue Stripe for delayed processing (needs invoice + subscription aggregation)
    should_aggregate = event_type in _AGGREGATABLE_EVENT_TYPES
    customer_id = event_data.get("customer_id", "")

    if idempotency_key or (should_aggregate and customer_id):
        if idempotency_key:
            aggregation_key = idempotency_key
        else:
            aggregation_key = f"customer:{customer_id}"

        pending_event_queue.queue_event(
            idempotency_key=aggregation_key,
            workspace_id=workspace_id,
            event_data=event_data,
            customer_data=customer_data,
            provider_name=provider_name,
            workspace=workspace,
        )

        # Log with truncated key for readability
        if len(aggregation_key) > 20:
            key_preview = f"{aggregation_key[:20]}..."
        else:
            key_preview = aggregation_key
        logger.info(f"Queued {event_type} for delayed processing (key: {key_preview})")

        return JsonResponse(
            create_success_response(f"{provider_name} webhook queued for processing"),
            status=200,
        )

    # Events WITHOUT idempotency_key that don't benefit from aggregation
    return _process_immediately(event_data, customer_data, provider_name, workspace)


def _process_immediately(
    event_data: dict[str, Any],
    customer_data: dict[str, Any],
    provider_name: str,
    workspace: Workspace | None = None,
) -> JsonResponse:
    """Process webhook immediately (for events without idempotency_key).

    This is the fallback for non-Stripe webhooks or Stripe events
    that don't have an idempotency_key.
    """
    from plugins.base import PluginType
    from plugins.destinations.base import BaseDestinationPlugin
    from plugins.registry import PluginRegistry

    event_type = event_data.get("type", "")
    customer_id = event_data.get("customer_id", "")
    workspace_id = str(workspace.uuid) if workspace else ""

    # Add workspace_id to event_data for insight detection
    event_data["workspace_id"] = workspace_id

    # Check if this event should be suppressed due to consolidation
    should_notify = event_consolidation_service.should_send_notification(
        event_type=event_type,
        customer_id=customer_id,
        workspace_id=workspace_id,
        amount=event_data.get("amount"),
    )

    if not should_notify:
        return JsonResponse(
            create_success_response(
                f"{provider_name} webhook processed (consolidated)"
            ),
            status=200,
        )

    # Build and format rich notification
    formatted = settings.EVENT_PROCESSOR.process_event_rich(
        event_data, customer_data, target="slack", workspace=workspace
    )

    # Get Slack credentials (webhook URL and optionally bot_token for threading)
    slack_credentials = _get_slack_credentials(workspace)

    if slack_credentials:
        registry = PluginRegistry.instance()
        slack_plugin = registry.get(PluginType.DESTINATION, "slack")
        if slack_plugin is None or not isinstance(slack_plugin, BaseDestinationPlugin):
            logger.error("Slack destination plugin not found or not configured")
            return JsonResponse(
                create_success_response(
                    f"{provider_name} webhook processed (Slack plugin unavailable)"
                ),
                status=200,
            )

        # Check for thread mapping (for Zendesk ticket updates)
        options: dict[str, Any] = {}
        entity_type, entity_id = _get_thread_info(workspace, event_data)

        if entity_type and entity_id and workspace:
            # Look up existing thread
            thread_info = thread_mapping_service.get_thread_ts(
                workspace, entity_type, entity_id
            )
            if thread_info:
                options["thread_ts"] = thread_info.thread_ts
                options["channel"] = thread_info.channel_id
                logger.debug(
                    f"Found existing thread for {entity_type}:{entity_id}: "
                    f"{thread_info.thread_ts}"
                )

        try:
            result = slack_plugin.send(formatted, slack_credentials, options)

            # Store thread mapping for new threads (Zendesk tickets)
            if (
                entity_type
                and entity_id
                and workspace
                and result.get("thread_ts")
                and not options.get("thread_ts")  # Only store if this is a new thread
            ):
                channel_id = result.get("channel") or slack_credentials.get("channel")
                if channel_id:
                    thread_mapping_service.store_thread_ts(
                        workspace=workspace,
                        entity_type=entity_type,
                        entity_id=entity_id,
                        channel_id=channel_id,
                        thread_ts=result["thread_ts"],
                    )
        except Exception as e:
            logger.error(
                f"Failed to send Slack notification for workspace "
                f"{workspace.uuid if workspace else 'unknown'}: {str(e)}"
            )
    else:
        logger.warning(
            f"No Slack credentials configured for workspace "
            f"{workspace.uuid if workspace else 'unknown'}, "
            f"skipping notification"
        )

    return JsonResponse(
        create_success_response(f"{provider_name} webhook processed successfully"),
        status=200,
    )


def _handle_webhook_exceptions(e: Exception, provider_name: str) -> JsonResponse:
    """Handle different types of webhook exceptions."""
    from webhooks.services.rate_limiter import RateLimitException

    if isinstance(e, WebhookSignatureError):
        logger.warning(f"Invalid signature for {provider_name} webhook")
        error_response = create_error_response(e, 400)
        return JsonResponse(error_response, status=400)
    elif isinstance(e, RateLimitException):
        # Rate limiting is expected behavior, not an error
        logger.info(f"Rate limit exceeded for {provider_name} webhook: {e!s}")
        error_response = create_error_response(e, 429)
        return JsonResponse(error_response, status=429)
    elif isinstance(e, WebhookError):
        logger.warning(f"Webhook validation error for {provider_name}: {str(e)}")
        error_response = create_error_response(e, 400)
        return JsonResponse(error_response, status=400)
    else:
        logger.error(f"Unexpected error in {provider_name} webhook", exc_info=True)
        error_response = create_error_response(e, 500)
        return JsonResponse(error_response, status=500)


def _process_webhook(
    request: HttpRequest,
    provider: Any,
    provider_name: str,
    workspace: Workspace | None = None,
) -> JsonResponse:
    """
    Common webhook processing logic with standardized error handling
    and rate limiting.

    Uses workspace-specific Slack integration for notifications.
    """
    try:
        # Handle rate limiting
        rate_limit_response = _handle_rate_limiting(workspace)
        if rate_limit_response:
            return rate_limit_response

        # Get rate limit info for headers
        rate_limit_info = None
        if workspace:
            rate_limit_info = rate_limiter.enforce_rate_limit(workspace)

        # Validate and parse webhook
        event_data = _validate_and_parse_webhook(request, provider)

        # Handle test webhooks
        if not event_data:
            response = JsonResponse(
                create_success_response("Test webhook received"), status=200
            )
            _add_rate_limit_headers(response, rate_limit_info)
            return response

        # Process webhook data with workspace for Slack notifications
        response = _process_webhook_data(event_data, provider, provider_name, workspace)
        _add_rate_limit_headers(response, rate_limit_info)
        return response

    except Exception as e:
        return _handle_webhook_exceptions(e, provider_name)


@csrf_exempt
@require_http_methods(["GET"])
def health_check(request: HttpRequest) -> JsonResponse:
    """Health check endpoint"""
    return JsonResponse({"status": "healthy", "service": "webhook-processor"})


# Legacy webhook endpoints removed to enforce multi-tenancy
# All webhooks must now use organization-specific endpoints:
# - /webhook/customer/{uuid}/shopify/
# - /webhook/customer/{uuid}/chargify/
# - /webhook/customer/{uuid}/stripe/
# - /webhook/billing/stripe/ (for Notipus billing only)


# === CUSTOMER-SPECIFIC WEBHOOKS ===


@csrf_exempt
@require_http_methods(["POST"])
def customer_shopify_webhook(
    request: HttpRequest, organization_uuid: str
) -> JsonResponse:
    """Handle customer-specific Shopify webhook requests with rate limiting"""
    # Log raw webhook payload before any processing
    _log_webhook_payload(request, "shopify", organization_uuid)

    logger.info(
        f"Processing customer Shopify webhook for workspace {organization_uuid}",
        extra={
            "content_type": request.content_type,
            "workspace_uuid": organization_uuid,
        },
    )

    try:
        # Get workspace for rate limiting
        workspace = get_object_or_404(Workspace, uuid=organization_uuid)

        # Get workspace's Shopify integration
        integration = get_object_or_404(
            Integration,
            workspace=workspace,
            integration_type="shopify",
            is_active=True,
        )

        from plugins.sources.shopify import ShopifySourcePlugin

        provider = ShopifySourcePlugin(webhook_secret=integration.webhook_secret)

        return _process_webhook(request, provider, "customer_shopify", workspace)

    except Exception as e:
        logger.error(f"Error in customer Shopify webhook: {str(e)}", exc_info=True)
        error_response = create_error_response(e, 500)
        return JsonResponse(error_response, status=500)


@csrf_exempt
@require_http_methods(["POST"])
def customer_chargify_webhook(
    request: HttpRequest, organization_uuid: str
) -> JsonResponse:
    """Handle customer-specific Chargify/Maxio webhook requests with rate limiting"""
    # Log raw webhook payload before any processing
    _log_webhook_payload(request, "chargify", organization_uuid)

    logger.info(
        f"Processing customer Chargify/Maxio webhook for workspace {organization_uuid}",
        extra={
            "workspace_uuid": organization_uuid,
            "content_type": request.content_type,
        },
    )

    try:
        # Get workspace
        workspace = Workspace.objects.get(uuid=organization_uuid)

        # Get workspace's Chargify/Maxio integration
        integration = Integration.objects.get(
            workspace=workspace, integration_type="chargify", is_active=True
        )

        from plugins.sources.chargify import ChargifySourcePlugin

        provider = ChargifySourcePlugin(webhook_secret=integration.webhook_secret)

        return _process_webhook(request, provider, "customer_chargify", workspace)

    except Exception as e:
        logger.error(
            f"Error in customer Chargify/Maxio webhook: {str(e)}", exc_info=True
        )
        error_response = create_error_response(e, 500)
        return JsonResponse(error_response, status=500)


@csrf_exempt
@require_http_methods(["POST"])
def customer_stripe_webhook(
    request: HttpRequest, organization_uuid: str
) -> JsonResponse:
    """Handle customer-specific Stripe webhook requests with rate limiting"""
    # Log raw webhook payload before any processing
    _log_webhook_payload(request, "stripe", organization_uuid)

    logger.info(
        f"Processing customer Stripe webhook for workspace {organization_uuid}",
        extra={
            "content_type": request.content_type,
            "workspace_uuid": organization_uuid,
        },
    )

    try:
        # Get workspace for rate limiting
        workspace = get_object_or_404(Workspace, uuid=organization_uuid)

        # Get workspace's Stripe integration
        integration = get_object_or_404(
            Integration,
            workspace=workspace,
            integration_type="stripe_customer",
            is_active=True,
        )

        from plugins.sources.stripe import StripeSourcePlugin

        provider = StripeSourcePlugin(webhook_secret=integration.webhook_secret)

        return _process_webhook(request, provider, "customer_stripe", workspace)

    except Exception as e:
        logger.error(f"Error in customer Stripe webhook: {str(e)}", exc_info=True)
        error_response = create_error_response(e, 500)
        return JsonResponse(error_response, status=500)


@csrf_exempt
@require_http_methods(["POST"])
def customer_zendesk_webhook(
    request: HttpRequest, organization_uuid: str
) -> JsonResponse:
    """Handle customer-specific Zendesk webhook requests with rate limiting"""
    # Log raw webhook payload before any processing
    _log_webhook_payload(request, "zendesk", organization_uuid)

    logger.info(
        f"Processing customer Zendesk webhook for workspace {organization_uuid}",
        extra={
            "content_type": request.content_type,
            "workspace_uuid": organization_uuid,
        },
    )

    try:
        # Get workspace for rate limiting
        workspace = get_object_or_404(Workspace, uuid=organization_uuid)

        # Get workspace's Zendesk integration
        integration = get_object_or_404(
            Integration,
            workspace=workspace,
            integration_type="zendesk",
            is_active=True,
        )

        from plugins.sources.zendesk import ZendeskSourcePlugin

        # Get Zendesk subdomain from integration settings
        integration_settings = integration.settings or {}
        zendesk_subdomain = integration_settings.get("zendesk_subdomain", "")

        provider = ZendeskSourcePlugin(
            webhook_secret=integration.webhook_secret,
            zendesk_subdomain=zendesk_subdomain,
        )

        return _process_webhook(request, provider, "customer_zendesk", workspace)

    except Exception as e:
        logger.error(f"Error in customer Zendesk webhook: {str(e)}", exc_info=True)
        error_response = create_error_response(e, 500)
        return JsonResponse(error_response, status=500)


# === GLOBAL BILLING WEBHOOKS (Notipus revenue) ===


@csrf_exempt
@require_http_methods(["POST"])
def billing_stripe_webhook(request: HttpRequest) -> JsonResponse:
    """Handle global Stripe billing webhooks for Notipus revenue"""
    # Log raw webhook payload before any processing
    _log_webhook_payload(request, "stripe_billing")

    logger.info(
        "Processing global billing Stripe webhook",
        extra={"content_type": request.content_type},
    )

    try:
        from core.models import GlobalBillingIntegration
        from plugins.sources.stripe import StripeSourcePlugin

        billing_integration = GlobalBillingIntegration.objects.filter(
            integration_type="stripe_billing", is_active=True
        ).first()

        if not billing_integration:
            # Return 200 to acknowledge receipt - don't trigger Stripe retries
            # Log error so we know configuration is missing
            logger.error(
                "GlobalBillingIntegration not configured for stripe_billing. "
                "Create record with integration_type='stripe_billing', is_active=True."
            )
            return JsonResponse(
                {"status": "error", "message": "Billing integration not configured"},
                status=200,  # 200 to prevent Stripe retries
            )

        provider = StripeSourcePlugin(webhook_secret=billing_integration.webhook_secret)

        return _process_webhook(request, provider, "billing_stripe")

    except Exception as e:
        logger.error(f"Error in billing Stripe webhook: {str(e)}", exc_info=True)
        # Return 200 to acknowledge receipt - prevents infinite retries
        return JsonResponse(
            {"status": "error", "message": "Internal error processing webhook"},
            status=200,
        )

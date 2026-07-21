"""Server-side Google Analytics 4 tracking via the Measurement Protocol.

All analytics events are sent from the server, never from browser
JavaScript: webhook-driven events (plan changes from Stripe) have no
browser to send from, server-side events can't be dropped by ad
blockers, and no third-party script runs on users' pages.

Requires two settings (both empty disables tracking entirely):

- ``GA4_MEASUREMENT_ID`` (``GA4_ID`` env var): the ``G-XXXXXXX`` id of
  the GA4 web data stream.
- ``GA4_API_SECRET`` (``GA4_API_SECRET`` env var): a Measurement
  Protocol API secret created under Admin -> Data Streams -> stream ->
  Measurement Protocol API secrets.

Identity model: anonymous visitors get a first-party ``np_ga_cid``
cookie holding a GA-style client id (minted by :class:`GA4Middleware`);
if a ``_ga`` cookie from gtag.js ever exists it wins, so a client-side
snippet can be added later and sessions will stitch together. Logged-in
users additionally send ``user_id``. Events with no request context
(Stripe webhooks) use the workspace UUID as a stable client id via
:func:`track_workspace_event`.

PII policy: nothing personally identifying is ever sent to Google.
``user_id`` is a salted HMAC of the user's pk (``GA4_USER_ID_SALT``,
falling back to ``SECRET_KEY`` — set the dedicated salt in production
so key rotation doesn't reset user continuity), page locations are
stripped to path + whitelisted campaign params so tokens and emails in
query strings never leak, referrers are reduced to their origin, and
any email-shaped string in event params is redacted as a last line of
defense.

Delivery is fire-and-forget on a small thread pool so a slow or down
Google endpoint can never block a request or a webhook handler. Note
the Measurement Protocol returns 2xx even for payloads it discards;
use GA4's realtime report or the /debug/mp/collect endpoint when
diagnosing missing events.
"""

import hashlib
import hmac
import logging
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests
from django.conf import settings
from django.http import HttpRequest, HttpResponse

logger = logging.getLogger(__name__)

GA4_ENDPOINT = "https://www.google-analytics.com/mp/collect"

# First-party cookie carrying the GA4 client id for visitors without a
# gtag.js ``_ga`` cookie. HttpOnly: no JavaScript ever needs to read it.
CLIENT_ID_COOKIE = "np_ga_cid"
CLIENT_ID_COOKIE_MAX_AGE = 60 * 60 * 24 * 730  # 2 years, matching _ga

# Django session key holding the GA4 session id (epoch seconds at first
# tracked request — GA4's own session id convention).
SESSION_KEY = "ga4_session_id"

# Request attributes used to pass state between middleware, views and
# signal receivers without mypy-visible monkeypatching.
_CLIENT_ID_ATTR = "_ga4_client_id"
_LOGIN_METHOD_ATTR = "_ga4_login_method"

_REQUEST_TIMEOUT_SECONDS = 5

# GA-style client id: two dot-separated integers.
_CLIENT_ID_RE = re.compile(r"^[0-9]{1,20}\.[0-9]{1,20}$")

# Loose email shape, used to redact PII that sneaks into param values.
_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")

# Query params worth keeping on page_location: campaign attribution
# only. Everything else (invitation tokens, Stripe session ids, email
# prefills) is dropped before the URL leaves our infrastructure.
_ALLOWED_QUERY_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "gclid",
    }
)

# Substring markers of user agents that must not count as active users.
# The Measurement Protocol has no bot filtering of its own (gtag relies
# on browser signals we don't have), so this coarse filter is all that
# keeps crawlers out of the page_view numbers.
_BOT_UA_MARKERS = (
    "bot",
    "crawl",
    "spider",
    "slurp",
    "curl",
    "wget",
    "python-requests",
    "headless",
    "monitor",
    "pingdom",
    "lighthouse",
)

# Path prefixes never tracked as page views: machine traffic, not users.
_EXCLUDED_PATH_PREFIXES = ("/admin/", "/static/", "/webhook/")

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ga4")

# Backpressure for the fire-and-forget queue: at most this many
# payloads may be queued or in flight; beyond it, events are dropped.
_MAX_PENDING_DELIVERIES = 1000
_pending_deliveries = 0
_pending_lock = threading.Lock()


def is_configured() -> bool:
    """Return whether GA4 tracking is enabled.

    Returns:
        True when both the measurement id and API secret are set.
    """
    return bool(settings.GA4_MEASUREMENT_ID and settings.GA4_API_SECRET)


def set_login_method(request: HttpRequest, method: str) -> None:
    """Record which auth flow is logging the user in.

    Custom login flows (Slack OIDC, passkeys) call this right before
    ``django.contrib.auth.login`` so the ``user_logged_in`` signal
    receiver can label the ``login`` event with the real method.

    Args:
        request: The current HTTP request.
        method: Auth method label, e.g. ``"slack"`` or ``"passkey"``.
    """
    setattr(request, _LOGIN_METHOD_ATTR, method)


def get_login_method(request: HttpRequest, default: str = "email") -> str:
    """Return the auth method recorded by :func:`set_login_method`.

    Args:
        request: The current HTTP request.
        default: Label to use when no flow recorded a method.

    Returns:
        The auth method label for the ``login`` event.
    """
    return getattr(request, _LOGIN_METHOD_ATTR, default)


def _new_client_id() -> str:
    """Mint a GA-style client id (``<random>.<epoch seconds>``)."""
    return f"{secrets.randbelow(2**31)}.{int(time.time())}"


def _client_id_from_cookies(cookies: dict[str, str]) -> str | None:
    """Extract a GA4 client id from request cookies.

    Prefers gtag.js's ``_ga`` cookie (``GA1.1.<random>.<epoch>``) so
    server events stitch into client-side sessions if a snippet is ever
    added, then falls back to our first-party cookie.

    Args:
        cookies: The request's cookie dict.

    Returns:
        The client id, or None when no usable cookie exists.
    """
    ga_cookie = cookies.get("_ga", "")
    parts = ga_cookie.split(".")
    if len(parts) >= 4:
        candidate = ".".join(parts[-2:])
        if _CLIENT_ID_RE.match(candidate):
            return candidate

    own = cookies.get(CLIENT_ID_COOKIE, "")
    if _CLIENT_ID_RE.match(own):
        return own
    return None


def _client_id_for_request(request: HttpRequest) -> str:
    """Resolve (or mint) the GA4 client id for this request.

    The resolved id is cached on the request so every event in the
    request lifecycle — including a cookie-setting first visit — uses
    the same id.

    Args:
        request: The current HTTP request.

    Returns:
        The GA4 client id.
    """
    cached = getattr(request, _CLIENT_ID_ATTR, None)
    if cached:
        return str(cached)

    client_id = _client_id_from_cookies(request.COOKIES) or _new_client_id()
    setattr(request, _CLIENT_ID_ATTR, client_id)
    return client_id


def _session_id_for_request(request: HttpRequest) -> str | None:
    """Return the GA4 session id, creating one in the Django session.

    Deliberately touches the session for anonymous visitors too:
    without a session id GA4 cannot compute sessions or engagement for
    the pre-signup half of the funnel, which is the part worth
    measuring. The bot filter in the middleware keeps crawler traffic
    from churning session storage.

    Args:
        request: The current HTTP request.

    Returns:
        Epoch-seconds session id, or None when the request has no
        session (e.g. error handlers before middleware ran).
    """
    session = getattr(request, "session", None)
    if session is None:
        return None

    session_id = session.get(SESSION_KEY)
    if not session_id:
        session_id = str(int(time.time()))
        session[SESSION_KEY] = session_id
    return str(session_id)


def _post(payload: dict[str, Any]) -> None:
    """Deliver one Measurement Protocol payload (runs on the pool)."""
    try:
        response = requests.post(
            GA4_ENDPOINT,
            params={
                "measurement_id": settings.GA4_MEASUREMENT_ID,
                "api_secret": settings.GA4_API_SECRET,
            },
            json=payload,
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        if response.status_code >= 300:
            logger.warning(f"GA4 Measurement Protocol returned {response.status_code}")
    except requests.RequestException as e:
        # Analytics is never worth an error page or a webhook retry.
        logger.warning(f"GA4 event delivery failed: {e!s}")


def _release_pending_slot(_future: Any) -> None:
    """Free a delivery slot once a payload finishes (done callback)."""
    global _pending_deliveries
    with _pending_lock:
        _pending_deliveries -= 1


def _submit(payload: dict[str, Any]) -> None:
    """Queue a payload for asynchronous delivery.

    Bounded: when GA4 is slow or unreachable and the backlog reaches
    ``_MAX_PENDING_DELIVERIES``, new events are dropped with a warning
    instead of queueing without limit — losing analytics beats memory
    pressure from an ever-growing work queue under sustained traffic.
    """
    global _pending_deliveries
    with _pending_lock:
        if _pending_deliveries >= _MAX_PENDING_DELIVERIES:
            logger.warning("GA4 event dropped: delivery backlog full")
            return
        _pending_deliveries += 1
    try:
        _executor.submit(_post, payload).add_done_callback(_release_pending_slot)
    except RuntimeError:
        # Interpreter shutdown; the slot leaks but nothing runs anymore.
        pass


def hashed_user_id(user: Any) -> str:
    """Return the pseudonymous GA4 user id for a Django user.

    A salted HMAC of the pk rather than a bare pk or an email hash:
    it stays stable when the user changes their email, and cannot be
    reversed or joined against other datasets without the salt. Also
    shown in the Django admin user list so a GA4 user_id can be mapped
    back to the account when needed.

    Args:
        user: An authenticated Django user.

    Returns:
        Hex digest to use as the GA4 ``user_id``.
    """
    salt = settings.GA4_USER_ID_SALT or settings.SECRET_KEY
    return hmac.new(salt.encode(), str(user.pk).encode(), hashlib.sha256).hexdigest()


def _redact_pii(value: Any) -> Any:
    """Redact email-shaped substrings anywhere in a param value.

    Recurses into dicts/lists/tuples so nested structures (e.g. the
    ecommerce ``items`` list) get the same guarantee as flat strings.
    """
    if isinstance(value, str):
        return _EMAIL_RE.sub("[redacted]", value)
    if isinstance(value, dict):
        return {key: _redact_pii(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_pii(item) for item in value]
    return value


def sanitize_page_location(url: str) -> str:
    """Strip a URL down to what analytics may see.

    Keeps scheme, host and path; drops every query param except
    campaign attribution and the fragment entirely. Invitation tokens,
    Stripe session ids and prefilled emails all travel in query
    strings, and none of them belong in Google's logs.

    Args:
        url: The full request URL.

    Returns:
        The sanitized URL.
    """
    parts = urlsplit(url)
    kept = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key in _ALLOWED_QUERY_PARAMS
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), ""))


def _build_event_params(
    params: dict[str, Any] | None, session_id: str | None
) -> dict[str, Any]:
    """Assemble event params with the fields GA4 needs for reporting.

    ``engagement_time_msec`` is what makes Measurement Protocol events
    count toward active users; ``session_id`` is what groups them into
    sessions. String values are scrubbed of email-shaped substrings as
    a final PII guard.
    """
    event_params: dict[str, Any] = {"engagement_time_msec": 100}
    if params:
        event_params.update({k: _redact_pii(v) for k, v in params.items()})
    if session_id:
        event_params.setdefault("session_id", session_id)
    return event_params


def track_event(
    request: HttpRequest, name: str, params: dict[str, Any] | None = None
) -> None:
    """Send a GA4 event attributed to the request's visitor.

    Fire-and-forget: never raises, no-op when GA4 is not configured.

    Args:
        request: The current HTTP request (supplies client id, session
            id and, for authenticated users, user id).
        name: GA4 event name (recommended names like ``sign_up`` where
            one exists, snake_case custom names otherwise).
        params: Optional event parameters.
    """
    if not is_configured():
        return

    payload: dict[str, Any] = {
        "client_id": _client_id_for_request(request),
        "events": [
            {
                "name": name,
                "params": _build_event_params(params, _session_id_for_request(request)),
            }
        ],
    }

    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        payload["user_id"] = hashed_user_id(user)

    _submit(payload)


def track_workspace_event(
    workspace: Any, name: str, params: dict[str, Any] | None = None
) -> None:
    """Send a GA4 event with no browser context (webhook handlers).

    Uses the workspace UUID as a stable client id so all server-driven
    billing events for a workspace correlate, even though they can't be
    stitched to a specific member's browsing session.

    Args:
        workspace: The Workspace the event belongs to.
        name: GA4 event name.
        params: Optional event parameters.
    """
    if not is_configured():
        return

    _submit(
        {
            "client_id": str(workspace.uuid),
            "events": [{"name": name, "params": _build_event_params(params, None)}],
        }
    )


def _is_bot(request: HttpRequest) -> bool:
    """Return whether the request's user agent looks like a bot."""
    user_agent = request.META.get("HTTP_USER_AGENT", "").lower()
    if not user_agent:
        # No UA at all is never a real browser.
        return True
    return any(marker in user_agent for marker in _BOT_UA_MARKERS)


class GA4Middleware:
    """Mints the client-id cookie and tracks page views server-side.

    Must sit after session/auth middleware in ``MIDDLEWARE`` so events
    can read ``request.session`` and ``request.user``.
    """

    def __init__(self, get_response: Any) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if not is_configured():
            response: HttpResponse = self.get_response(request)
            return response

        had_cookie = CLIENT_ID_COOKIE in request.COOKIES
        client_id = _client_id_for_request(request)

        response = self.get_response(request)

        if not had_cookie:
            response.set_cookie(
                CLIENT_ID_COOKIE,
                client_id,
                max_age=CLIENT_ID_COOKIE_MAX_AGE,
                secure=request.is_secure(),
                httponly=True,
                samesite="Lax",
            )

        if self._should_track_page_view(request, response):
            params: dict[str, Any] = {
                "page_location": sanitize_page_location(request.build_absolute_uri())
            }
            referrer = request.META.get("HTTP_REFERER")
            if referrer:
                # Origin only: referrer paths/queries can carry tokens.
                referrer_parts = urlsplit(referrer)
                if referrer_parts.scheme and referrer_parts.netloc:
                    params["page_referrer"] = (
                        f"{referrer_parts.scheme}://{referrer_parts.netloc}/"
                    )
            track_event(request, "page_view", params)

        return response

    @staticmethod
    def _should_track_page_view(request: HttpRequest, response: HttpResponse) -> bool:
        """Return whether this request/response pair is a real page view."""
        if request.method != "GET":
            return False
        if not 200 <= response.status_code < 300:
            return False
        content_type = response.get("Content-Type", "")
        if not content_type.startswith("text/html"):
            return False
        if request.path.startswith(_EXCLUDED_PATH_PREFIXES):
            return False
        return not _is_bot(request)

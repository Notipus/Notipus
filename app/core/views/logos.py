"""Views for serving company logos from the database."""

import hashlib
import logging

from core.models import Company
from django.http import Http404, HttpResponse, HttpResponseNotModified
from django.utils.decorators import method_decorator
from django.utils.http import http_date
from django.views import View
from django.views.decorators.cache import cache_control

logger = logging.getLogger(__name__)


@method_decorator(
    cache_control(
        max_age=2592000,  # 30 days for browsers
        s_maxage=31536000,  # 1 year for CDN (Cloudflare)
        public=True,
        immutable=True,  # Content won't change at this URL
    ),
    name="dispatch",
)
class CompanyLogoView(View):
    """Serve company logos from database storage.

    This view serves logos stored as binary data in the Company model.
    Logos are publicly accessible (no authentication required) so that
    Slack can fetch them for message previews.

    URL: /logos/<domain>/
    Example: https://app.notipus.com/logos/acme.com/

    Includes caching headers for browser (30 days) and CDN caching (1 year).
    Supports conditional requests via ETag for efficient revalidation.
    """

    def get(self, request, domain: str) -> HttpResponse:
        """Serve logo for the given domain.

        Args:
            request: HTTP request.
            domain: Company domain to serve logo for.

        Returns:
            HttpResponse with logo data, 304 Not Modified, or 404.
        """
        try:
            company = Company.objects.only(
                "logo_data", "logo_content_type", "domain", "updated_at"
            ).get(domain=domain)
        except Company.DoesNotExist as e:
            raise Http404("Company not found") from e

        if not company.logo_data:
            raise Http404("Logo not found")

        # Generate ETag based on domain and updated_at timestamp
        etag = hashlib.sha256(
            f"{company.domain}-{company.updated_at.isoformat()}".encode()
        ).hexdigest()[:32]  # Truncate to 32 chars for reasonable length

        # Handle conditional request (If-None-Match)
        # HTTP spec allows multiple ETags comma-separated, or "*" for any
        if_none_match = request.META.get("HTTP_IF_NONE_MATCH")
        if if_none_match:
            if if_none_match == "*":
                return HttpResponseNotModified()
            # Check if our ETag matches any of the provided ETags
            client_etags = [e.strip().strip('"') for e in if_none_match.split(",")]
            if etag in client_etags:
                return HttpResponseNotModified()

        content_type = company.logo_content_type or "image/png"

        response = HttpResponse(company.logo_data, content_type=content_type)
        response["Content-Length"] = len(company.logo_data)
        response["ETag"] = f'"{etag}"'
        response["Last-Modified"] = http_date(company.updated_at.timestamp())

        return response

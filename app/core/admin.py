"""Django admin configuration for core models.

This module provides admin interfaces for managing core models,
including Company enrichment data with search, filters, and bulk actions.
"""

import json
from typing import TYPE_CHECKING

from django.contrib import admin, messages
from django.contrib.admin import helpers
from django.template.response import TemplateResponse
from django.utils import timezone
from django.utils.html import format_html

from .models import Company

if TYPE_CHECKING:
    from django.db.models import QuerySet
    from django.http import HttpRequest


@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    """Admin interface for Company enrichment data."""

    list_display = [
        "domain",
        "name",
        "has_logo_display",
        "has_brand_info_display",
        "enrichment_sources_display",
        "created_at",
        "updated_at",
    ]
    list_filter = [
        "created_at",
        "updated_at",
    ]
    search_fields = ["domain", "name"]
    readonly_fields = [
        "created_at",
        "updated_at",
        "brand_info_pretty",
        "logo_preview",
        "enrichment_sources_display",
    ]
    ordering = ["-updated_at"]
    date_hierarchy = "created_at"

    fieldsets = [
        (
            "Company Info",
            {
                "fields": ["domain", "name", "logo_preview"],
            },
        ),
        (
            "Brand Data",
            {
                "fields": ["brand_info_pretty", "enrichment_sources_display"],
                "classes": ["collapse"],
            },
        ),
        (
            "Metadata",
            {
                "fields": ["created_at", "updated_at"],
            },
        ),
    ]

    # Cap on how many rows the purge confirmation page will enumerate, to
    # bound the response size (and avoid timeouts) on huge "select all" sets.
    PURGE_CONFIRM_LIMIT = 500

    actions = [
        "purge_enrichment_data",
        "refresh_enrichment",
    ]

    @admin.display(boolean=True, description="Has Logo")
    def has_logo_display(self, obj: Company) -> bool:
        """Check if company has a logo."""
        return obj.has_logo

    @admin.display(boolean=True, description="Has Brand Info")
    def has_brand_info_display(self, obj: Company) -> bool:
        """Check if company has brand info."""
        return bool(obj.brand_info)

    @admin.display(description="Logo Preview")
    def logo_preview(self, obj: Company) -> str:
        """Display logo preview in admin."""
        if obj.has_logo:
            logo_url = obj.get_logo_url()
            if logo_url:
                return format_html(
                    '<img src="{}" style="max-height: 50px; max-width: 100px;" />',
                    logo_url,
                )
        return "-"

    @admin.display(description="Brand Info (JSON)")
    def brand_info_pretty(self, obj: Company) -> str:
        """Display formatted brand info JSON."""
        if obj.brand_info:
            # Filter out internal fields for cleaner display
            display_info = {
                k: v for k, v in obj.brand_info.items() if not k.startswith("_")
            }
            return format_html(
                '<pre style="white-space: pre-wrap; max-height: 300px; '
                'overflow-y: auto;">{}</pre>',
                json.dumps(display_info, indent=2),
            )
        return "-"

    @admin.display(description="Enrichment Sources")
    def enrichment_sources_display(self, obj: Company) -> str:
        """Display which enrichment sources have contributed data."""
        if not obj.brand_info:
            return "-"

        sources = obj.brand_info.get("_sources", {})
        if not sources:
            return "Legacy (no source tracking)"

        source_names = list(sources.keys())
        blended_at = obj.brand_info.get("_blended_at", "Unknown")

        return format_html(
            "<strong>Sources:</strong> {}<br><strong>Last blended:</strong> {}",
            ", ".join(source_names),
            blended_at,
        )

    @admin.action(description="Purge enrichment data (keep domain)")
    def purge_enrichment_data(
        self,
        request: "HttpRequest",
        queryset: "QuerySet[Company]",
    ) -> "TemplateResponse | None":
        """Clear enrichment data but keep the domain record.

        Shows an intermediate confirmation page before applying the
        (irreversible) purge, mirroring Django's built-in ``delete_selected``
        flow. The purge only runs once the confirmation form is submitted.
        """
        # Enforce the safety cap FIRST, before the confirm short-circuit, so a
        # crafted confirmed submission (e.g. select_across) can't bypass it.
        # Detect over-limit cheaply with a LIMITed PK fetch rather than a full
        # COUNT(*) over a potentially large table.
        limit = self.PURGE_CONFIRM_LIMIT
        over_limit = len(queryset.values_list("pk", flat=True)[: limit + 1]) > limit
        if over_limit:
            self.message_user(
                request,
                (
                    f"Too many companies selected to purge at once "
                    f"(limit is {limit}). "
                    "Please narrow your selection and try again."
                ),
                level=messages.WARNING,
            )
            return None

        if request.POST.get("confirm_purge") == "yes":
            count = queryset.update(
                name="",
                brand_info={},
                logo_url="",
                logo_data=None,
                logo_content_type="",
                # .update() bypasses auto_now, so set updated_at explicitly.
                updated_at=timezone.now(),
            )
            self.message_user(request, f"Purged enrichment data for {count} companies.")
            return None

        context = {
            **self.admin_site.each_context(request),
            "title": "Purge enrichment data",
            "queryset": queryset,
            "companies": queryset,
            "opts": self.model._meta,
            "action_checkbox_name": helpers.ACTION_CHECKBOX_NAME,
            "media": self.media,
        }
        return TemplateResponse(
            request,
            "admin/core/company/purge_enrichment_confirmation.html",
            context,
        )

    @admin.action(description="Refresh enrichment (re-fetch from sources)")
    def refresh_enrichment(
        self,
        request: "HttpRequest",
        queryset: "QuerySet[Company]",
    ) -> None:
        """Clear blended timestamp to trigger re-enrichment on next access."""
        # Filter to companies with brand_info and clear _blended_at in memory
        companies_to_update = []
        for company in queryset.filter(brand_info__isnull=False):
            if company.brand_info:
                company.brand_info.pop("_blended_at", None)
                companies_to_update.append(company)

        # Bulk update to avoid N+1 queries
        if companies_to_update:
            Company.objects.bulk_update(companies_to_update, ["brand_info"])

        self.message_user(
            request,
            f"Marked {len(companies_to_update)} companies for re-enrichment.",
        )

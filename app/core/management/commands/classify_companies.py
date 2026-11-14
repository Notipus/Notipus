"""Backfill the LLM company profile for already enriched companies.

New companies get an LLM profile (description, firmographics, industry
classification) as they are enriched. Companies enriched before an LLM module
was configured only get one on their next refresh; this command adds it now,
reusing the stored Brandfetch data as input. Only the
domain and public company metadata are sent to the LLM provider.

    python manage.py classify_companies --dry-run
    python manage.py classify_companies --limit 50
    python manage.py classify_companies --force
"""

from typing import Any

from core.models import Company
from core.services.enrichment import DomainEnrichmentService
from django.core.management.base import BaseCommand, CommandError, CommandParser


class Command(BaseCommand):
    """Classify the industry of enriched companies with an LLM module."""

    help = "Backfill LLM industry classification for enriched companies"

    def add_arguments(self, parser: CommandParser) -> None:
        """Register command-line flags.

        Args:
            parser: Argument parser supplied by Django.
        """
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List the companies that would be classified",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Classify at most this many companies",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Regenerate existing profiles, ignoring cached LLM answers",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        """Run the command.

        Args:
            *args: Unused positional arguments.
            **options: Parsed command-line options.
        """
        service = DomainEnrichmentService()
        if not service.has_llm_modules:
            raise CommandError(
                "No LLM module is configured. Set OPENAI_API_KEY, "
                "ANTHROPIC_API_KEY or OLLAMA_BASE_URL."
            )

        companies = Company.objects.exclude(domain="").order_by("id")
        if not options["force"]:
            companies = companies.exclude(brand_info__has_key="industry_classification")
        if options["limit"]:
            companies = companies[: options["limit"]]

        classified = failed = 0
        for company in companies:
            if options["dry_run"]:
                self.stdout.write(company.domain)
                continue
            if service.classify_company(company, use_cache=not options["force"]):
                classified += 1
                industry = company.brand_info["industry_classification"]["industry"]
                self.stdout.write(f"{company.domain}: {industry}")
            else:
                failed += 1
                self.stdout.write(f"{company.domain}: not classified")

        if not options["dry_run"]:
            self.stdout.write(
                self.style.SUCCESS(f"Classified {classified}, failed {failed}")
            )

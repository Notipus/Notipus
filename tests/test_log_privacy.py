"""Tests for the log-safe email masking helper."""

import logging
from unittest.mock import MagicMock, patch

import pytest
from core.utils.log_privacy import mask_email
from plugins.enrichment.base_email import EmailNotFoundError, GDPRClaimedError


def test_mask_email_keeps_domain_and_hides_local_part() -> None:
    """The domain survives; the local part is replaced by a short digest."""
    masked = mask_email("Jane.Doe@Example.com")

    assert masked.endswith("@example.com")
    assert "jane" not in masked.lower()
    assert masked == mask_email("jane.doe@example.com")


def test_mask_email_distinguishes_addresses() -> None:
    """Different addresses on one domain get different digests."""
    assert mask_email("a@example.com") != mask_email("b@example.com")


def test_mask_email_handles_empty_and_malformed_values() -> None:
    """Empty, non-string and domain-less inputs never raise or leak."""
    assert mask_email("") == "<none>"
    assert mask_email(None) == "<none>"
    masked = mask_email("not-an-email")
    assert "not-an-email" not in masked
    assert "@" not in masked


@pytest.mark.parametrize("raised", [None, RuntimeError("jane.doe@example.com")])
def test_event_processor_person_enrichment_logs_mask_email(
    raised: Exception | None, caplog: pytest.LogCaptureFixture
) -> None:
    """Success and failure logs for person enrichment never show the address."""
    from webhooks.services.event_processor import EventProcessor

    processor = EventProcessor()
    service = MagicMock()
    if raised is None:
        service.enrich_email.return_value = MagicMock()
    else:
        service.enrich_email.side_effect = raised
    processor.email_enrichment_service = service

    with caplog.at_level(logging.DEBUG):
        processor._enrich_person({"email": "jane.doe@example.com"}, MagicMock())

    assert caplog.records
    assert "jane.doe" not in caplog.text
    assert "@example.com" in caplog.text


@pytest.mark.parametrize(
    "raised",
    [
        GDPRClaimedError("jane.doe@example.com"),
        EmailNotFoundError("jane.doe@example.com"),
        RuntimeError("boom for jane.doe@example.com"),
    ],
)
def test_email_enrichment_service_logs_mask_email(
    raised: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    """EmailEnrichmentService log lines carry a masked address only."""
    from core.services.email_enrichment import EmailEnrichmentService

    service = EmailEnrichmentService()
    with (
        patch.object(service, "_check_tier", return_value=True),
        patch.object(service, "_get_hunter_api_key", return_value="test-key"),
        patch.object(service, "_call_hunter_api", side_effect=raised),
        patch("core.services.email_enrichment.Person") as person_model,
        caplog.at_level(logging.DEBUG, logger="core.services.email_enrichment"),
    ):
        person_model.objects.filter.return_value.first.return_value = None
        result = service.enrich_email("jane.doe@example.com", MagicMock())

    assert result is None
    assert caplog.records
    for record in caplog.records:
        assert "jane.doe" not in record.getMessage()

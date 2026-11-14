"""Tests for the log-safe email masking helper."""

from core.utils.log_privacy import mask_email


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

"""Classify customer email addresses by domain type.

Tags an email's domain as government, education, military, healthcare,
free-provider, and/or disposable so notifications can badge the address
(e.g. ``jane@stanford.edu -> Education``).

Design notes:
    - Institutional detection uses a curated suffix table plus a few
      generic country-code patterns (``gov.XX``, ``edu.XX``, ``ac.XX``,
      ``mil.XX`` and US ``k12.XX.us``). This is deliberately NOT the
      full public-suffix list - a curated table keeps the module
      dependency-free and easy to audit. Add new suffixes to
      ``_SUFFIX_TAGS`` below (1- to 3-label suffixes are supported).
    - Matching is label-based and longest-match-first, so ``cam.ac.uk``
      matches ``ac.uk`` (not ``uk``) and ``bundesliga.de`` does NOT
      match ``bund.de``.
    - Free-provider domains come from ``free_email_domains``;
      disposable domains from the ``disposable-email-domains`` package.
      Disposable wins over free (a domain is never double-tagged).
    - Pure domain logic: no network calls, subaddressing is irrelevant.
"""

from enum import StrEnum

from disposable_email_domains import blocklist

from .free_email_domains import FREE_EMAIL_DOMAINS


class EmailTag(StrEnum):
    """Domain-type tag for a customer email address."""

    GOVERNMENT = "government"
    EDUCATION = "education"
    MILITARY = "military"
    HEALTHCARE = "healthcare"
    FREE = "free"
    DISPOSABLE = "disposable"


# Curated suffix table: exact domain suffixes (1-3 labels) mapped to a
# tag. A domain matches when it *is* the suffix or ends with
# ".<suffix>". Generic ``gov.XX``/``edu.XX``/``ac.XX``/``mil.XX``
# country patterns are handled separately (see ``_GENERIC_CC_LABELS``),
# so only suffixes that don't fit those patterns need entries here.
_SUFFIX_TAGS: dict[str, EmailTag] = {
    # --- Government ---
    "gov": EmailTag.GOVERNMENT,  # US federal/state (.gov)
    "gouv.fr": EmailTag.GOVERNMENT,  # France
    "gob.es": EmailTag.GOVERNMENT,  # Spain
    "gob.mx": EmailTag.GOVERNMENT,  # Mexico
    "gob.ar": EmailTag.GOVERNMENT,  # Argentina
    "gob.cl": EmailTag.GOVERNMENT,  # Chile
    "gob.pe": EmailTag.GOVERNMENT,  # Peru
    "gob.ec": EmailTag.GOVERNMENT,  # Ecuador
    "gob.bo": EmailTag.GOVERNMENT,  # Bolivia
    "gob.ve": EmailTag.GOVERNMENT,  # Venezuela
    "gob.hn": EmailTag.GOVERNMENT,  # Honduras
    "bund.de": EmailTag.GOVERNMENT,  # German federal government
    "admin.ch": EmailTag.GOVERNMENT,  # Swiss federal administration
    "gv.at": EmailTag.GOVERNMENT,  # Austria
    "fgov.be": EmailTag.GOVERNMENT,  # Belgian federal government
    "governo.it": EmailTag.GOVERNMENT,  # Italian government
    "gc.ca": EmailTag.GOVERNMENT,  # Government of Canada
    "canada.ca": EmailTag.GOVERNMENT,  # Government of Canada
    "go.jp": EmailTag.GOVERNMENT,  # Japan central government
    "lg.jp": EmailTag.GOVERNMENT,  # Japan local government
    "go.kr": EmailTag.GOVERNMENT,  # South Korea
    "go.id": EmailTag.GOVERNMENT,  # Indonesia
    "go.th": EmailTag.GOVERNMENT,  # Thailand
    "govt.nz": EmailTag.GOVERNMENT,  # New Zealand
    "gov.scot": EmailTag.GOVERNMENT,  # Scottish government
    "gov.wales": EmailTag.GOVERNMENT,  # Welsh government
    "parliament.uk": EmailTag.GOVERNMENT,  # UK Parliament
    "police.uk": EmailTag.GOVERNMENT,  # UK police forces
    "nic.in": EmailTag.GOVERNMENT,  # India (National Informatics Centre)
    "europa.eu": EmailTag.GOVERNMENT,  # EU institutions
    # --- Education ---
    "edu": EmailTag.EDUCATION,  # US higher education (.edu)
    "school.nz": EmailTag.EDUCATION,  # New Zealand schools
    "sch.uk": EmailTag.EDUCATION,  # UK schools (*.sch.uk)
    "sch.ae": EmailTag.EDUCATION,  # UAE schools
    "sch.sa": EmailTag.EDUCATION,  # Saudi schools
    "sch.id": EmailTag.EDUCATION,  # Indonesian schools
    "ed.jp": EmailTag.EDUCATION,  # Japanese schools
    # --- Military ---
    "mil": EmailTag.MILITARY,  # US military (.mil)
    "mod.uk": EmailTag.MILITARY,  # UK Ministry of Defence
    "forces.gc.ca": EmailTag.MILITARY,  # Canadian Armed Forces
    "idf.il": EmailTag.MILITARY,  # Israel Defense Forces
    "tsk.tr": EmailTag.MILITARY,  # Turkish Armed Forces
    # --- Healthcare (public health services) ---
    "nhs.uk": EmailTag.HEALTHCARE,  # UK National Health Service
    "nhs.scot": EmailTag.HEALTHCARE,  # NHS Scotland
    "nhs.net": EmailTag.HEALTHCARE,  # NHSmail
}

# Generic country-code patterns: ``<label>.<cc>`` where ``cc`` is any
# two-letter country code, e.g. gov.uk, gov.br, edu.au, ac.jp, mil.be.
# Curated entries in _SUFFIX_TAGS take precedence at equal length.
_GENERIC_CC_LABELS: dict[str, EmailTag] = {
    "gov": EmailTag.GOVERNMENT,
    "edu": EmailTag.EDUCATION,
    "ac": EmailTag.EDUCATION,
    "mil": EmailTag.MILITARY,
}

# Longest suffix length (in labels) present in the curated table.
_MAX_SUFFIX_LABELS = max(suffix.count(".") + 1 for suffix in _SUFFIX_TAGS)


def _extract_domain(email: str | None) -> str | None:
    """Extract a normalized domain from an email address.

    Deliberately lenient (tagging is best-effort); the strict validator
    for enrichment lives in ``core.utils.email_domain``.

    Args:
        email: Raw email address, possibly messy or invalid.

    Returns:
        Lowercase domain, or None when no plausible domain exists.
    """
    if not isinstance(email, str):
        return None
    candidate = email.strip().lower()
    if "@" not in candidate:
        return None
    domain = candidate.rsplit("@", 1)[1].strip().strip(".")
    if not domain or "." not in domain:
        return None
    return domain


def _institutional_tag(labels: list[str]) -> EmailTag | None:
    """Find the institutional tag for a domain's labels, if any.

    Checks candidate suffixes longest-first so that e.g. ``forces.gc.ca``
    beats ``gc.ca`` and ``ac.uk`` beats ``uk``. Matching is label-based,
    which makes substring false-positives (``bundesliga.de`` vs
    ``bund.de``) impossible.

    Args:
        labels: Domain labels, e.g. ["cam", "ac", "uk"].

    Returns:
        Matching tag or None.
    """
    for size in range(min(_MAX_SUFFIX_LABELS, len(labels)), 0, -1):
        candidate_labels = labels[-size:]
        candidate = ".".join(candidate_labels)
        if candidate in _SUFFIX_TAGS:
            return _SUFFIX_TAGS[candidate]
        # Generic <label>.<cc> country pattern (gov.uk, ac.jp, mil.be...)
        if (
            size == 2
            and candidate_labels[0] in _GENERIC_CC_LABELS
            and len(candidate_labels[1]) == 2
            and candidate_labels[1].isalpha()
        ):
            return _GENERIC_CC_LABELS[candidate_labels[0]]
        # US K-12 school districts: <anything>.k12.<state>.us
        if (
            size == 3
            and candidate_labels[0] == "k12"
            and len(candidate_labels[1]) == 2
            and candidate_labels[1].isalpha()
            and candidate_labels[2] == "us"
        ):
            return EmailTag.EDUCATION
    return None


def classify_email(email: str | None) -> list[EmailTag]:
    """Classify an email address by its domain.

    Args:
        email: Email address to classify. Case and surrounding
            whitespace are ignored; invalid or missing values yield no
            tags.

    Returns:
        Zero or more tags: at most one institutional tag (government,
        education, military, healthcare) followed by at most one
        provider tag (disposable beats free).
    """
    domain = _extract_domain(email)
    if not domain:
        return []

    tags: list[EmailTag] = []

    institutional = _institutional_tag(domain.split("."))
    if institutional:
        tags.append(institutional)

    if domain in blocklist:
        tags.append(EmailTag.DISPOSABLE)
    elif domain in FREE_EMAIL_DOMAINS:
        tags.append(EmailTag.FREE)

    return tags

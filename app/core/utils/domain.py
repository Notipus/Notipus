"""Canonical form of a company domain.

A ``Company`` is keyed by its domain, so every code path that stores or
looks one up must agree on the same spelling. ``www.example.com`` and
``example.com`` are the same company.
"""

_SCHEMES = ("https://", "http://")
_WWW_PREFIX = "www."


def normalize_company_domain(value: str) -> str:
    """Return the canonical lookup key for a company domain.

    Lower-cases the value, strips surrounding whitespace, a leading
    ``http://`` or ``https://``, anything from the first ``/`` onwards,
    and a single leading ``www.`` label. ``www`` is only removed as a
    whole leading label in front of a registrable name, so
    ``news-www.example.com`` and ``www.com`` are left alone.

    This only normalizes; it does not validate. Callers that need a
    valid hostname must still check the result.

    Args:
        value: Raw domain or URL, e.g. ``https://www.Example.com/about``.

    Returns:
        Normalized domain, e.g. ``example.com``. May be empty.
    """
    domain = value.strip().lower()
    for scheme in _SCHEMES:
        if domain.startswith(scheme):
            domain = domain[len(scheme) :]
            break
    domain = domain.split("/", 1)[0]
    # Keep "www.com": dropping its "www." would leave a bare TLD
    remainder = domain[len(_WWW_PREFIX) :]
    if domain.startswith(_WWW_PREFIX) and "." in remainder:
        domain = remainder
    return domain

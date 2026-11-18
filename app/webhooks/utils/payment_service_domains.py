"""Curated database of payment-service (bill-pay / AP automation) domains.

Some businesses don't pay their invoices themselves: an accounts-payable
platform such as Bill.com, Tipalti or AvidXchange pays on their behalf,
and the payment arrives with the platform's address as the customer
email (``payments@bill.com``). That domain identifies the intermediary,
not the buyer, so it must never be enriched as the customer's company,
counted in a company's revenue, or treated as a consumer.

Maintenance:
    - Add registrable domains lowercase, without a leading ``@`` or dot.
      Subdomains match automatically (``hq.bill.com`` matches
      ``bill.com``), so list only the registrable domain.
    - Only list services that send or receive payments *on behalf of*
      a business customer. Card issuers and spend-management tools whose
      cardholders keep their own email (Ramp, Brex, ...) don't belong
      here, and listing them would hide those companies when they are
      real customers.
    - This module must stay importable without Django settings (see the
      package docstring).
"""

PAYMENT_SERVICE_DOMAINS: frozenset[str] = frozenset(
    {
        # --- Bill.com (BILL) ---
        "bill.com",
        "bills.com",
        # --- AP automation / supplier payment platforms ---
        "avidxchange.com",
        "avidpay.com",
        "billtrust.com",
        "corpay.com",
        "melio.com",
        "meliopayments.com",
        "nvoicepay.com",
        "paymode.com",  # Bottomline Paymode-X
        "paystand.com",
        "plooto.com",
        "routable.com",
        "stampli.com",
        "tipalti.com",
        "versapay.com",
        # --- Cross-border B2B payouts ---
        "payoneer.com",
        "veem.com",
        # --- Procurement / e-invoicing networks ---
        "ariba.com",  # SAP Ariba Network
        "coupahost.com",  # Coupa supplier portal / Coupa Pay
        "taulia.com",
        "tradeshift.com",
    }
)


def is_payment_service_domain(domain: str | None) -> bool:
    """Check whether a domain belongs to a payment service.

    Matches the domain itself and any subdomain of a listed domain,
    label by label, so ``hq.bill.com`` matches but ``mybill.com`` does
    not.

    Args:
        domain: Domain to check (case and a trailing dot are ignored).

    Returns:
        True if the domain is a known payment intermediary.
    """
    if not domain:
        return False
    labels = domain.strip().lower().rstrip(".").split(".")
    return any(
        ".".join(labels[i:]) in PAYMENT_SERVICE_DOMAINS for i in range(len(labels) - 1)
    )

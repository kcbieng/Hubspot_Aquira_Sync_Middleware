from __future__ import annotations

from typing import Any, Protocol


class InvoiceProvider(Protocol):
    def fetch(self, client_id: str | int | None = None) -> list[dict[str, Any]]:
        ...


class NullInvoiceProvider:
    """Default v1 provider. Aquira has no first-class Invoice resource."""

    def fetch(self, client_id: str | int | None = None) -> list[dict[str, Any]]:
        return []


class AquiraReportInvoiceProvider:
    """Reserved for a later Report/RunDirectReport mapping. Unused in v1."""

    def fetch(self, client_id: str | int | None = None) -> list[dict[str, Any]]:
        return []


def get_invoice_provider() -> InvoiceProvider:
    return NullInvoiceProvider()

"""Price calculation rules."""

from decimal import Decimal

TAX_RATE = Decimal("0.18")


def apply_discount(price: Decimal, percent: int) -> Decimal:
    """Return the price after a percentage discount."""
    if not 0 <= percent <= 100:
        raise ValueError("percent must be between 0 and 100")
    return price * (100 - percent) / 100


class Cart:
    """Holds items and computes the total including tax."""

    currency = "INR"

    def __init__(self) -> None:
        self.items: list[tuple[str, Decimal]] = []

    def add_item(self, name: str, price: Decimal) -> None:
        self.items.append((name, price))

    def total_with_tax(self) -> Decimal:
        subtotal = sum((p for _, p in self.items), Decimal(0))
        return subtotal * (1 + TAX_RATE)

"""Seeded mock customers and an in memory refund ledger."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, localcontext

REPLAY_WINDOW_SECONDS = 60.0


@dataclass(frozen=True)
class Customer:
    customer_id: str
    name: str
    email: str
    tier: str
    customer_since: str


@dataclass(frozen=True)
class Refund:
    refund_id: str
    customer_id: str
    amount: Decimal
    reason: str
    created_at: float


class UnknownCustomer(Exception):
    """No customer has the given id."""


class RefundRejected(Exception):
    """The refund is well formed but the business rules refuse it."""


SEED: tuple[tuple[Customer, Decimal], ...] = (
    (
        Customer("CUST-10001", "Ada Lindqvist", "ada.lindqvist@example.com", "gold", "2021-04-12"),
        Decimal("420.00"),
    ),
    (
        Customer("CUST-10002", "Marcus Obi", "marcus.obi@example.com", "silver", "2022-09-30"),
        Decimal("120.00"),
    ),
    (
        Customer("CUST-10003", "Priya Raman", "priya.raman@example.org", "standard", "2023-01-17"),
        Decimal("75.50"),
    ),
    (
        Customer(
            "CUST-10004", "Tomas Herrera", "tomas.herrera@example.net", "standard", "2024-06-02"
        ),
        Decimal("0.00"),
    ),
    (
        Customer("CUST-10005", "Lena Fischer", "lena.fischer@example.com", "gold", "2020-11-08"),
        Decimal("1250.00"),
    ),
)


class Ledger:
    """Customers, their refundable balances and the refunds made against them."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._customers = {customer.customer_id: customer for customer, _ in SEED}
        self._refundable = {customer.customer_id: balance for customer, balance in SEED}
        self._refunds: list[Refund] = []

    def customer(self, customer_id: str) -> Customer | None:
        return self._customers.get(customer_id)

    def refundable(self, customer_id: str) -> Decimal:
        return self._refundable[customer_id]

    def refund(self, customer_id: str, amount: Decimal, reason: str) -> tuple[Refund, bool]:
        """Record a refund. Returns the refund and whether it replays an earlier identical one."""
        if customer_id not in self._customers:
            raise UnknownCustomer(customer_id)
        now = self._clock()
        # Agents retry. The same refund asked for again within the window is the
        # same refund, so the caller gets the original id instead of a second payout.
        key = (customer_id, amount, reason)
        for earlier in reversed(self._refunds):
            if now - earlier.created_at >= REPLAY_WINDOW_SECONDS:
                break
            if (earlier.customer_id, earlier.amount, earlier.reason) == key:
                return earlier, True
        balance = self._refundable[customer_id]
        if amount > balance:
            raise RefundRejected(
                f"A refund of {amount} is more than the refundable balance of {balance}."
            )
        refund = Refund(f"RF-{len(self._refunds) + 1:06d}", customer_id, amount, reason, now)
        self._refunds.append(refund)
        # An amount can be far smaller than a cent, down to 5e-324. The default
        # decimal precision would round a refund that small away, so the
        # subtraction gets enough digits for both numbers.
        with localcontext() as context:
            context.prec = len(format(balance, "f")) + len(format(amount, "f")) + 2
            self._refundable[customer_id] = balance - amount
        return refund, False

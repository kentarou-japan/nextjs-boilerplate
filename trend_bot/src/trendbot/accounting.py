"""Multi-currency futures ledger (JPY base).

Cash balances are kept per currency. Futures variation margin (qty × multiplier × Δsettle) is
credited to the contract currency every day, so open trade equity is always zero after the
daily settlement and futures P&L is never counted twice. NAV (JPY) = Σ balance_ccy × FX_ccy.

Daily attribution identity (tested):
    NAV_t − NAV_{t−1} = futures_pnl + costs + interest + fx_translation + fx_conversion_cost
where
    futures_pnl    = Σ VM_ccy × FX_t
    costs          = −Σ cost_ccy × FX_t
    interest       = Σ interest_ccy × FX_t
    fx_translation = Σ balance_ccy(t−1) × (FX_t − FX_{t−1})
Cash interest accrues on actual balances only (collateral), never on futures notional, so the
futures excess return and the cash return are not double counted.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

DAY_COUNT = {"JPY": 365, "GBP": 365, "AUD": 365, "CAD": 365, "USD": 360, "EUR": 360}


@dataclass
class Ledger:
    base: str = "JPY"
    balances: dict = field(default_factory=lambda: defaultdict(float))
    positions: dict = field(default_factory=dict)       # (market, contract) -> qty
    last_price: dict = field(default_factory=dict)      # (market, contract) -> last settle used for VM
    fx_prev: dict = field(default_factory=dict)

    def deposit(self, amount: float, ccy: str = "JPY") -> None:
        self.balances[ccy] += amount

    def nav(self, fx: dict) -> float:
        return float(sum(b * fx[c] for c, b in self.balances.items()))

    # --- daily steps --------------------------------------------------------------------
    def fx_translation(self, fx: dict) -> float:
        pnl = 0.0
        for c, b in self.balances.items():
            if c in self.fx_prev:
                pnl += b * (fx[c] - self.fx_prev[c])
        self.fx_prev = {c: fx[c] for c in set(self.balances) | set(fx)}
        return pnl

    def accrue_interest(self, days: int, rates: dict, spread_neg: float, fx: dict) -> float:
        total = 0.0
        for c, b in list(self.balances.items()):
            r = rates.get(c)
            if r is None or days <= 0 or b == 0:
                continue
            rate = r + spread_neg if b < 0 else r
            amt = b * rate * days / DAY_COUNT.get(c, 365)
            self.balances[c] += amt
            total += amt * fx[c]
        return total

    def mark_to_market(self, prices: dict, specs: dict, fx: dict) -> dict:
        """prices: (market, contract) -> settle (missing key = no settlement today, carry).
        specs: market -> (currency, multiplier). Returns market -> JPY VM."""
        out: dict = defaultdict(float)
        for key, qty in self.positions.items():
            if qty == 0 or key not in prices:
                continue
            px = prices[key]
            if px is None or px != px:  # NaN
                continue
            ccy, mult = specs[key[0]]
            vm = qty * mult * (px - self.last_price[key])
            self.balances[ccy] += vm
            self.last_price[key] = px
            out[key[0]] += vm * fx[ccy]
        return dict(out)

    def fill(self, market: str, contract: str, qty: int, fill_price: float, settle: float, ccy: str,
             multiplier: float, cost_ccy: float, fx: dict) -> tuple[float, float]:
        """Apply a fill after today's MTM. Returns (JPY vm from fill→settle, JPY cost)."""
        key = (market, contract)
        prev = self.positions.get(key, 0)
        new = prev + qty
        vm = qty * multiplier * (settle - fill_price)
        self.balances[ccy] += vm - cost_ccy
        if new == 0:
            self.positions.pop(key, None)
            self.last_price.pop(key, None)
        else:
            self.positions[key] = new
            self.last_price[key] = settle
        return vm * fx[ccy], -cost_ccy * fx[ccy]

    def sweep_to_base(self, fx: dict, cost_bps: float) -> float:
        """Convert all non-base balances to base currency. Returns JPY conversion cost (negative)."""
        cost = 0.0
        for c in list(self.balances):
            if c == self.base or self.balances[c] == 0:
                continue
            jpy = self.balances[c] * fx[c]
            fee = abs(jpy) * cost_bps / 1e4
            self.balances[self.base] += jpy - fee
            self.balances[c] = 0.0
            cost -= fee
        return cost

    def market_exposure(self) -> dict:
        out: dict = defaultdict(int)
        for (m, _), q in self.positions.items():
            out[m] += q
        return dict(out)

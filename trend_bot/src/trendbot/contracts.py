"""Contract (instrument) specifications, expiry rules and production blockers.

Expiry-rule implementations follow the rule *descriptions* in ``config/instruments.yaml``.
Those descriptions are themselves unverified against primary exchange sources (see the
file header); any market whose verification status is not ``verified`` is reported as a
production blocker.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pandas as pd

from .config import CONFIG_DIR, deep_merge, load_yaml
from .data.calendars import BusinessCalendar, get_calendar

MONTH_CODES = {"F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6, "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12}
CODE_OF_MONTH = {v: k for k, v in MONTH_CODES.items()}


@dataclass(frozen=True)
class Instrument:
    key: str
    name: str
    asset_class: str
    sector: str
    exchange: str
    symbol_root: str
    currency: str
    multiplier: float
    tick_size: float
    contract_months: tuple[str, ...]
    timezone: str
    calendar: str | None
    last_trade_rule: str | None
    first_notice_rule: str | None
    delivery: str
    allow_negative_prices: bool
    min_trade_qty: int
    roll_method: str
    roll_business_days_before: int
    verification: dict = field(default_factory=dict, compare=False, hash=False)
    raw: dict = field(default_factory=dict, compare=False, hash=False)

    @property
    def verified(self) -> bool:
        return self.verification.get("status") == "verified"

    def contract_id(self, year: int, month: int) -> str:
        return f"{self.key}{CODE_OF_MONTH[month]}{year}"


def parse_contract_id(cid: str) -> tuple[str, int, int]:
    """'ESZ2025' -> ('ES', 2025, 12)."""
    year = int(cid[-4:])
    code = cid[-5]
    return cid[:-5], year, MONTH_CODES[code]


def load_instruments(path: str | Path | None = None) -> dict[str, Instrument]:
    raw = load_yaml(path or CONFIG_DIR / "instruments.yaml")
    defaults = raw.get("defaults", {})
    out: dict[str, Instrument] = {}
    for key, spec in raw["markets"].items():
        merged = deep_merge(defaults, spec)
        for req in ("asset_class", "sector", "currency", "multiplier", "tick_size", "contract_months"):
            if merged.get(req) in (None, ""):
                raise ValueError(f"instrument {key}: required field '{req}' missing")
        if float(merged["multiplier"]) <= 0 or float(merged["tick_size"]) <= 0:
            raise ValueError(f"instrument {key}: multiplier/tick must be positive")
        out[key] = Instrument(
            key=key,
            name=merged["name"],
            asset_class=merged["asset_class"],
            sector=merged["sector"],
            exchange=merged["exchange"],
            symbol_root=str(merged["symbol_root"]),
            currency=merged["currency"],
            multiplier=float(merged["multiplier"]),
            tick_size=float(merged["tick_size"]),
            contract_months=tuple(merged["contract_months"]),
            timezone=merged["timezone"],
            calendar=merged.get("calendar"),
            last_trade_rule=merged.get("last_trade_rule"),
            first_notice_rule=merged.get("first_notice_rule"),
            delivery=merged["delivery"],
            allow_negative_prices=bool(merged.get("allow_negative_prices", False)),
            min_trade_qty=int(merged.get("min_trade_qty", 1)),
            roll_method=merged["roll"]["method"],
            roll_business_days_before=int(merged["roll"]["business_days_before"]),
            verification=merged.get("verification", {}),
            raw=merged,
        )
    return out


# ---------------------------------------------------------------------------
# Expiry rules. Each takes (calendar, year, month-of-contract) and returns a date.
# ---------------------------------------------------------------------------

def _nth_weekday(year: int, month: int, weekday: int, n: int) -> pd.Timestamp:
    first = pd.Timestamp(year=year, month=month, day=1)
    shift = (weekday - first.weekday()) % 7
    return first + pd.Timedelta(days=shift + 7 * (n - 1))


def _prior_month(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def _third_friday(cal: BusinessCalendar, y: int, m: int) -> pd.Timestamp:
    return cal.previous_or_same(_nth_weekday(y, m, 4, 3))


def _bd_before_second_friday(cal, y, m):
    return cal.offset(_nth_weekday(y, m, 4, 2), -1)


def _cme_fx_two(cal, y, m):
    return cal.offset(_nth_weekday(y, m, 2, 3), -2)


def _cme_fx_one(cal, y, m):
    return cal.offset(_nth_weekday(y, m, 2, 3), -1)


def _seventh_bd_before_last_bd(cal, y, m):
    return cal.offset(cal.last_session_of_month(y, m), -7)


def _last_bd_prior_month(cal, y, m):
    py, pm = _prior_month(y, m)
    return cal.last_session_of_month(py, pm)


def _eurex_bond(cal, y, m):
    delivery = cal.next_or_same(pd.Timestamp(year=y, month=m, day=10))
    return cal.offset(delivery, -2)


def _jgb(cal, y, m):
    delivery = cal.next_or_same(pd.Timestamp(year=y, month=m, day=20))
    return cal.offset(delivery, -5)


def _second_bd_before_last_bd(cal, y, m):
    return cal.offset(cal.last_session_of_month(y, m), -2)


def _two_bd_before_first_day(cal, y, m):
    return cal.offset(pd.Timestamp(year=y, month=m, day=1), -2)


def _third_last_bd(cal, y, m):
    return cal.offset(cal.last_session_of_month(y, m), -2)


def _cl_rule(cal, y, m):
    py, pm = _prior_month(y, m)
    d25 = pd.Timestamp(year=py, month=pm, day=25)
    base = d25 if cal.is_session(d25) else cal.offset(d25, -1)
    return cal.offset(base, -3)


def _ng_rule(cal, y, m):
    return cal.offset(pd.Timestamp(year=y, month=m, day=1), -3)


def _bd_before_fifteenth(cal, y, m):
    return cal.offset(pd.Timestamp(year=y, month=m, day=15), -1)


RULES: dict[str, Callable[[BusinessCalendar, int, int], pd.Timestamp]] = {
    "third_friday": _third_friday,
    "business_day_before_second_friday": _bd_before_second_friday,
    "cme_fx_two_bd_before_third_wednesday": _cme_fx_two,
    "cme_fx_one_bd_before_third_wednesday": _cme_fx_one,
    "seventh_bd_before_last_bd": _seventh_bd_before_last_bd,
    "last_bd_of_prior_month": _last_bd_prior_month,
    "eurex_bond_two_days_before_tenth": _eurex_bond,
    "jgb_fifth_bd_before_twentieth": _jgb,
    "second_bd_before_last_bd": _second_bd_before_last_bd,
    "two_bd_before_first_day": _two_bd_before_first_day,
    "third_last_bd": _third_last_bd,
    "cl_three_bd_before_25th_prior_month": _cl_rule,
    "ng_three_bd_before_first_day": _ng_rule,
    "bd_before_fifteenth": _bd_before_fifteenth,
}


def contract_calendar(inst: Instrument, start_year: int, end_year: int) -> pd.DataFrame:
    """Listed contracts with last-trade / first-notice / roll-reference dates."""
    cal = get_calendar(inst.calendar)
    rows = []
    months = sorted(MONTH_CODES[c] for c in inst.contract_months)
    for y in range(start_year, end_year + 1):
        for m in months:
            ltd = RULES[inst.last_trade_rule](cal, y, m) if inst.last_trade_rule else pd.NaT
            fnd = RULES[inst.first_notice_rule](cal, y, m) if inst.first_notice_rule else pd.NaT
            ref = min(d for d in (ltd, fnd) if pd.notna(d))
            rows.append({
                "market": inst.key,
                "contract": inst.contract_id(y, m),
                "year": y,
                "month": m,
                "last_trade_date": ltd,
                "first_notice_date": fnd,
                "roll_reference_date": ref,
            })
    return pd.DataFrame(rows).sort_values("roll_reference_date").reset_index(drop=True)


def production_blockers(instruments: dict[str, Instrument], universe: list[str], data_label: str,
                        assumptions: dict) -> list[dict]:
    """Everything that must be resolved before any live connection. Empty list = no blockers."""
    blockers = []
    if data_label != "REAL_FUTURES":
        blockers.append({"scope": "data", "item": "futures_prices",
                         "reason": f"判断に使うデータが実際の先物限月別データではない（{data_label}）"})
    for key in universe:
        inst = instruments[key]
        if not inst.verified:
            blockers.append({"scope": key, "item": "contract_spec",
                             "reason": "契約仕様（乗数・呼値・限月・最終取引日・受渡・取引時間）が一次資料で未確認"})
        blockers.append({"scope": key, "item": "costs_margin_liquidity",
                         "reason": "手数料・証拠金・出来高が研究用仮定（assumptions.yaml）のまま"})
    for ccy, rate in (assumptions.get("cash_rates") or {}).items():
        if ccy.isupper() and len(ccy) == 3 and rate is None:
            blockers.append({"scope": ccy, "item": "cash_rate", "reason": f"{ccy} 短期金利データ未入手"})
    blockers.append({"scope": "broker", "item": "live_broker",
                     "reason": "本番ブローカー接続は未実装（LiveBrokerStub）。API仕様・口座・秘密情報管理の確定が必要"})
    return blockers

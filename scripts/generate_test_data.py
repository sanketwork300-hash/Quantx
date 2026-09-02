#!/usr/bin/env python
"""Regenerate the committed test fixtures.

Deterministic: the synthetic market is seeded, so re-running this produces
byte-identical files. That is what lets the regression suite pin exact outputs.

Usage:
    python scripts/generate_test_data.py
"""

from __future__ import annotations

import asyncio
import csv
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from domains.instruments.enums import OptionType  # noqa: E402
from domains.market_data.providers.synthetic import (  # noqa: E402
    SyntheticMarketConfig,
    SyntheticMarketDataProvider,
)

DATA_DIR = ROOT / "tests" / "data"
AS_OF = datetime(2026, 9, 24, 9, 20, tzinfo=UTC)

# Intentionally messy headers: real chain exports look like this, and the
# fixtures must exercise column-mapping inference rather than a tidy schema we
# would never meet in practice.
HEADERS = [
    "EXPIRY_DT",
    "STRIKE_PRICE",
    "CE_PE",
    "BID",
    "ASK",
    "LTP",
    "BIDQTY",
    "ASKQTY",
    "VOL",
    "OI",
    "UNDERLYING_VALUE",
]


def build_rows() -> list[dict]:
    config = SyntheticMarketConfig(
        as_of=AS_OF,
        underlying_symbol="NIFTY",
        exchange="SYNTH",
        expiry_days=(35, 91),
        strikes_each_side=7,
        strike_step=Decimal("200"),
    )
    provider = SyntheticMarketDataProvider(config)
    chain = asyncio.run(provider.get_option_chain(provider.underlying.id))

    rows = []
    for option in chain.quotes:
        quote = option.quote
        rows.append(
            {
                "EXPIRY_DT": option.expiry.isoformat(),
                "STRIKE_PRICE": format(option.strike, "f"),
                "CE_PE": "CE" if option.option_type is OptionType.CALL else "PE",
                "BID": format(quote.bid_price, "f"),
                "ASK": format(quote.ask_price, "f"),
                "LTP": format(quote.last_price, "f"),
                "BIDQTY": format(quote.bid_size, "f"),
                "ASKQTY": format(quote.ask_size, "f"),
                "VOL": format(quote.volume, "f"),
                "OI": format(quote.open_interest, "f"),
                "UNDERLYING_VALUE": format(chain.underlying_price, "f"),
            }
        )
    return rows


def write(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=HEADERS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {path.relative_to(ROOT)} ({len(rows)} rows)")


def corrupt(rows: list[dict]) -> list[dict]:
    """Produce a chain with one instance of each failure the pipeline must catch.

    Every corruption below is a real thing that appears in real exports. The
    fixture exists so that "every excluded quote has a reason" is verified
    against data that actually triggers each reason.
    """
    bad = [dict(row) for row in rows[:24]]
    spot = Decimal(bad[0]["UNDERLYING_VALUE"])

    # --- EXCLUDED (a quote is formed, scored, then set aside with a reason) ---
    bad[0]["BID"], bad[0]["ASK"] = "420.00", "415.00"  # CROSSED_MARKET
    bad[1]["ASK"] = "0"  # ZERO_ASK
    bad[2]["BID"], bad[2]["ASK"] = "", ""  # MISSING_BOTH_SIDES
    bad[3]["BID"] = "-5.00"  # NEGATIVE_PRICE

    # A deep in-the-money call quoted far below its intrinsic value.
    deep_itm = dict(rows[0])
    deep_itm["STRIKE_PRICE"] = format(spot - Decimal(4000), "f")
    deep_itm["CE_PE"] = "CE"
    deep_itm["BID"], deep_itm["ASK"], deep_itm["LTP"] = "3.00", "4.00", "3.50"
    bad.append(deep_itm)  # PRICE_BELOW_INTRINSIC

    duplicate = dict(bad[5])
    bad.append(duplicate)  # DUPLICATE_OBSERVATION

    # --- KEPT (flagged, but usable) ---
    # Widen the spread around the original mid rather than moving the price, so
    # this row exercises WIDE_SPREAD and nothing else.
    mid = (Decimal(bad[6]["BID"]) + Decimal(bad[6]["ASK"])) / 2
    bad[6]["BID"] = format((mid * Decimal("0.7")).quantize(Decimal("0.05")), "f")
    bad[6]["ASK"] = format((mid * Decimal("1.3")).quantize(Decimal("0.05")), "f")
    bad[7]["OI"], bad[7]["VOL"] = "0", "0"  # ILLIQUID_CONTRACT, kept

    # --- REJECTED (no quote can be formed at all) ---
    bad.append({**rows[8], "STRIKE_PRICE": "0"})  # NON_POSITIVE_STRIKE
    bad.append({**rows[9], "STRIKE_PRICE": "not-a-number"})  # UNPARSEABLE_ROW
    bad.append({**rows[10], "EXPIRY_DT": ""})  # MISSING_EXPIRY
    bad.append({**rows[11], "BID": "", "ASK": "", "LTP": ""})  # NO_PRICE_FIELDS
    bad.append({**rows[12], "CE_PE": ""})  # MISSING_OPTION_TYPE
    return bad


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    rows = build_rows()
    write(DATA_DIR / "options_chain_clean.csv", rows)
    write(DATA_DIR / "options_chain_bad_quotes.csv", corrupt(rows))


if __name__ == "__main__":
    main()

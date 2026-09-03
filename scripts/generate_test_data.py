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
import uuid  # noqa: F401  (referenced in a return annotation)
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from domains.instruments.enums import OptionType  # noqa: E402
from domains.market_data.models import OrderBookLevel, OrderBookSnapshot  # noqa: E402
from domains.market_data.providers.synthetic import (  # noqa: E402
    SyntheticMarketConfig,
    SyntheticMarketDataProvider,
)
from domains.microstructure.storage import snapshots_to_parquet  # noqa: E402
from quant.microstructure.intensity import (  # noqa: E402
    HawkesParameters,
    simulate_hawkes,
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


# ------------------------------------------------- microstructure (Phase 10)
#: Deliberately messy, in the two orderings a capture tool actually writes:
#: ``BID_PX_1`` on one side and ``ASK1PRICE`` on the other. The importer has to
#: recognise both, and the fixture is what proves it does.
BOOK_HEADERS = [
    "TIMESTAMP",
    "SEQ",
    *[f"BID_PX_{level}" for level in range(1, 6)],
    *[f"BID_SZ_{level}" for level in range(1, 6)],
    *[f"ASK{level}PRICE" for level in range(1, 6)],
    *[f"ASK{level}QTY" for level in range(1, 6)],
]

EVENT_HEADERS = ["TIME", "SEQNO", "ACTION", "BIDASK", "PX", "QTY", "ORDERREF"]

#: The book is generated around the at-the-money call in
#: ``options_chain_clean.csv``, so the L2 fixture and the chain fixture describe
#: the same contract rather than two unrelated synthetic markets.
BOOK_SNAPSHOT_INTERVAL_SECONDS = 5
BOOK_WINDOW_SECONDS = 1800
BOOK_TICK = Decimal("0.05")
BOOK_LEVELS = 5

#: A self-exciting tape, so the held-out comparison has something real to adopt
#: and the fixture exercises the branch that reports a Hawkes fit. A Poisson
#: tape — the branch that must *refuse* one — is generated inside the tests,
#: because what it proves is a property of the gate rather than of any file.
BOOK_EVENT_PROCESS = HawkesParameters(mu=0.5, alpha=1.0, beta=1.5)
BOOK_EVENT_SEED = 20_260_924


def _tick(value: Decimal) -> Decimal:
    return (value / BOOK_TICK).quantize(Decimal(1), rounding=ROUND_HALF_UP) * BOOK_TICK


def build_book() -> tuple[list[dict], list[dict], list[OrderBookSnapshot], uuid.UUID]:
    """A seeded depth series and a self-exciting event tape for one contract."""
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
    # The at-the-money call of the near expiry: the most liquid contract in the
    # chain, which is the one an L2 capture would be pointed at.
    near = chain.expiries[0]
    calls = [
        option
        for option in chain.quotes
        if option.expiry == near and option.option_type is OptionType.CALL
    ]
    atm = min(calls, key=lambda option: abs(option.strike - config.spot))
    instrument_id = atm.instrument_id
    start_mid = _tick(atm.quote.mid_price)

    rng = np.random.default_rng(BOOK_EVENT_SEED)
    ticks = int(start_mid / BOOK_TICK)

    snapshot_rows: list[dict] = []
    snapshots: list[OrderBookSnapshot] = []
    for step in range(BOOK_WINDOW_SECONDS // BOOK_SNAPSHOT_INTERVAL_SECONDS + 1):
        stamp = AS_OF + timedelta(seconds=step * BOOK_SNAPSHOT_INTERVAL_SECONDS)
        ticks += int(rng.integers(-2, 3))
        half = 1 if rng.random() < 0.8 else 2

        bids: list[OrderBookLevel] = []
        asks: list[OrderBookLevel] = []
        for level in range(BOOK_LEVELS):
            # Depth thickens away from the touch, which is what makes the book
            # slope a positive number rather than noise.
            size = Decimal(int(rng.integers(120, 400) * (1 + 0.35 * level)))
            bids.append(OrderBookLevel(price=BOOK_TICK * (ticks - half - level), quantity=size))
            size = Decimal(int(rng.integers(120, 400) * (1 + 0.35 * level)))
            asks.append(OrderBookLevel(price=BOOK_TICK * (ticks + half + level), quantity=size))

        snapshots.append(
            OrderBookSnapshot(
                instrument_id=instrument_id,
                exchange_timestamp=stamp,
                receive_timestamp=stamp,
                bids=tuple(bids),
                asks=tuple(asks),
                source="synthetic:book",
                sequence_number=step + 1,
            )
        )
        row = {"TIMESTAMP": stamp.isoformat(), "SEQ": str(step + 1)}
        for index, (bid, ask) in enumerate(zip(bids, asks, strict=True), start=1):
            row[f"BID_PX_{index}"] = format(bid.price, "f")
            row[f"BID_SZ_{index}"] = format(bid.quantity, "f")
            row[f"ASK{index}PRICE"] = format(ask.price, "f")
            row[f"ASK{index}QTY"] = format(ask.quantity, "f")
        snapshot_rows.append(row)

    # --- the event tape -----------------------------------------------------
    times = simulate_hawkes(
        BOOK_EVENT_PROCESS, float(BOOK_WINDOW_SECONDS), np.random.default_rng(BOOK_EVENT_SEED + 1)
    )
    actions = np.random.default_rng(BOOK_EVENT_SEED + 2)
    event_rows: list[dict] = []
    for index, offset in enumerate(times, start=1):
        stamp = AS_OF + timedelta(seconds=float(offset))
        snapshot = snapshots[min(int(offset) // BOOK_SNAPSHOT_INTERVAL_SECONDS, len(snapshots) - 1)]
        is_bid = bool(actions.random() < 0.5)
        levels = snapshot.bids if is_bid else snapshot.asks
        # Most messages land on the touch; a minority one level behind it.
        depth_index = 0 if actions.random() < 0.75 else 1
        action = str(actions.choice(["A", "D", "T"], p=[0.50, 0.35, 0.15]))
        event_rows.append(
            {
                "TIME": stamp.isoformat(),
                "SEQNO": str(index),
                "ACTION": action,
                "BIDASK": "B" if is_bid else "A",
                "PX": format(levels[depth_index].price, "f"),
                "QTY": str(int(actions.integers(25, 150))),
                "ORDERREF": f"O{index:07d}",
            }
        )
    return snapshot_rows, event_rows, snapshots, instrument_id


def corrupt_book(rows: list[dict]) -> list[dict]:
    """The whole clean series, plus one instance of each snapshot rejection.

    The corruptions are appended after the series and timestamped past its end,
    so they are additions rather than replacements: the kept snapshots are
    exactly the clean ones, and ``input == kept + rejected`` is checkable by
    hand against the two counts.
    """
    bad = [dict(row) for row in rows]
    tail = AS_OF + timedelta(seconds=BOOK_WINDOW_SECONDS + 60)

    def spare(offset: int, sequence: int) -> dict:
        row = dict(rows[offset])
        row["TIMESTAMP"] = (tail + timedelta(seconds=offset)).isoformat()
        row["SEQ"] = str(sequence)
        return row

    missing_time = spare(21, 9001)
    missing_time["TIMESTAMP"] = ""
    bad.append(missing_time)  # MISSING_TIMESTAMP

    negative_price = spare(22, 9002)
    negative_price["BID_PX_1"] = "-1.00"
    bad.append(negative_price)  # NEGATIVE_PRICE

    negative_size = spare(23, 9003)
    negative_size["ASK1QTY"] = "-40"
    bad.append(negative_size)  # NEGATIVE_QUANTITY

    # Bid level 2 priced above level 1: exactly what a transposed export looks
    # like, and the reason the importer refuses rather than sorting.
    out_of_order = spare(24, 9004)
    out_of_order["BID_PX_1"], out_of_order["BID_PX_2"] = (
        out_of_order["BID_PX_2"],
        out_of_order["BID_PX_1"],
    )
    bad.append(out_of_order)  # LEVELS_OUT_OF_ORDER

    no_size = spare(25, 9005)
    no_size["BID_SZ_1"] = ""
    bad.append(no_size)  # PRICE_WITHOUT_QUANTITY

    empty = spare(26, 9006)
    for header in BOOK_HEADERS[2:]:
        empty[header] = ""
    bad.append(empty)  # NO_LEVELS

    unparseable = spare(27, 9007)
    unparseable["ASK1PRICE"] = "not-a-price"
    bad.append(unparseable)  # UNPARSEABLE_ROW

    bad.append(dict(bad[3]))  # DUPLICATE_OBSERVATION
    return bad


def corrupt_events(rows: list[dict]) -> list[dict]:
    """One instance of each event rejection the importer must catch."""
    bad = [dict(row) for row in rows]
    next_sequence = len(rows) + 1

    def spare(**overrides) -> dict:
        row = dict(rows[10])
        nonlocal next_sequence
        row["SEQNO"] = str(next_sequence)
        next_sequence += 1
        row.update(overrides)
        return row

    bad.append(spare(TIME=""))  # MISSING_TIMESTAMP
    bad.append(spare(ACTION=""))  # MISSING_EVENT_TYPE
    bad.append(spare(ACTION="REPRICE"))  # UNRECOGNISED_EVENT_TYPE
    bad.append(spare(BIDASK="MIDDLE"))  # UNRECOGNISED_SIDE
    bad.append(spare(PX="-12.50"))  # NEGATIVE_PRICE
    bad.append(spare(QTY="-30"))  # NEGATIVE_QUANTITY
    bad.append(spare(QTY="thirty"))  # UNPARSEABLE_ROW
    return bad


def write_rows(path: Path, headers: list[str], rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {path.relative_to(ROOT)} ({len(rows)} rows)")


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    rows = build_rows()
    write(DATA_DIR / "options_chain_clean.csv", rows)
    write(DATA_DIR / "options_chain_bad_quotes.csv", corrupt(rows))

    snapshot_rows, event_rows, snapshots, instrument_id = build_book()
    write_rows(DATA_DIR / "orderbook_snapshots.csv", BOOK_HEADERS, corrupt_book(snapshot_rows))
    write_rows(DATA_DIR / "orderbook_events.csv", EVENT_HEADERS, corrupt_events(event_rows))
    parquet = DATA_DIR / "orderbook.parquet"
    parquet.write_bytes(
        # A fixed write stamp, so regenerating the fixture is a no-op rather
        # than a diff. Everything outside this generator stamps the real instant.
        snapshots_to_parquet(
            tuple(snapshots),
            instrument_id,
            "synthetic:book",
            "fixture-generator",
            written_at=AS_OF,
        )
    )
    print(f"wrote {parquet.relative_to(ROOT)} ({len(snapshots)} snapshots)")


if __name__ == "__main__":
    main()

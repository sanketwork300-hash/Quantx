"""Canonical identity is the foundation everything else resolves against."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from domains.instruments.enums import (
    AssetClass,
    ExerciseStyle,
    OptionType,
    SettlementType,
)
from domains.instruments.errors import CanonicalKeyError, InvalidInstrument
from domains.instruments.identity import (
    build_canonical_key,
    format_strike,
    instrument_id_for,
    parse_canonical_key,
)
from domains.instruments.models import make_instrument

NIFTY_ID = instrument_id_for("NSE:INDEX:NIFTY")


def option(**overrides):
    kwargs = {
        "asset_class": AssetClass.OPTION,
        "exchange": "NSE",
        "symbol": "NIFTY",
        "currency": "INR",
        "multiplier": Decimal(75),
        "expiry": date(2026, 9, 24),
        "strike": Decimal(24000),
        "option_type": OptionType.CALL,
        "exercise_style": ExerciseStyle.EUROPEAN,
        "settlement_type": SettlementType.CASH,
        "underlying_id": NIFTY_ID,
    }
    kwargs.update(overrides)
    return make_instrument(**kwargs)


class TestCanonicalKey:
    def test_option_key_shape(self):
        assert option().canonical_key == "NSE:OPTION:NIFTY:2026-09-24:24000:C"

    def test_index_key_shape(self):
        instrument = make_instrument(
            asset_class=AssetClass.INDEX, exchange="NSE", symbol="NIFTY", currency="INR"
        )
        assert instrument.canonical_key == "NSE:INDEX:NIFTY"

    def test_exchange_and_symbol_are_normalised(self):
        assert option(exchange="nse ", symbol=" nifty").canonical_key.startswith(
            "NSE:OPTION:NIFTY:"
        )

    @pytest.mark.parametrize("strike", [Decimal("24000"), Decimal("24000.00"), Decimal("2.4E+4")])
    def test_equivalent_strikes_produce_one_identity(self, strike):
        """24000, 24000.00 and 2.4E+4 must not become three contracts."""
        assert option(strike=strike).canonical_key == "NSE:OPTION:NIFTY:2026-09-24:24000:C"

    def test_fractional_strike_is_preserved(self):
        assert option(strike=Decimal("24000.50")).canonical_key.endswith(":24000.5:C")

    def test_option_key_requires_full_specification(self):
        with pytest.raises(CanonicalKeyError):
            build_canonical_key(exchange="NSE", asset_class=AssetClass.OPTION, symbol="NIFTY")

    def test_index_key_rejects_contract_fields(self):
        with pytest.raises(CanonicalKeyError):
            build_canonical_key(
                exchange="NSE",
                asset_class=AssetClass.INDEX,
                symbol="NIFTY",
                expiry=date(2026, 9, 24),
            )

    def test_symbol_may_not_contain_the_separator(self):
        with pytest.raises(CanonicalKeyError):
            build_canonical_key(exchange="NSE", asset_class=AssetClass.INDEX, symbol="NIF:TY")


class TestDeterministicIds:
    def test_id_is_uuid5_of_canonical_key(self):
        instrument = option()
        assert instrument.id == uuid.uuid5(
            uuid.UUID("6f6b9d1e-6a1e-5b6e-9a3d-0c2f1b7a4e10"), instrument.canonical_key
        )

    def test_same_contract_always_gets_the_same_id(self):
        """Reproducibility across processes, environments and databases."""
        assert option().id == option().id
        assert option(strike=Decimal("24000.00")).id == option(strike=Decimal(24000)).id

    def test_different_contracts_get_different_ids(self):
        assert option().id != option(option_type=OptionType.PUT).id
        assert option().id != option(expiry=date(2026, 10, 29)).id
        assert option().id != option(strike=Decimal(24100)).id


class TestInvariants:
    @pytest.mark.parametrize(
        "missing", ["expiry", "strike", "option_type", "exercise_style", "underlying_id"]
    )
    def test_option_requires_every_contract_field(self, missing):
        with pytest.raises((InvalidInstrument, CanonicalKeyError)):
            option(**{missing: None})

    def test_future_rejects_strike(self):
        with pytest.raises((InvalidInstrument, CanonicalKeyError)):
            make_instrument(
                asset_class=AssetClass.FUTURE,
                exchange="NSE",
                symbol="NIFTY",
                currency="INR",
                expiry=date(2026, 9, 24),
                strike=Decimal(24000),
                underlying_id=NIFTY_ID,
            )

    def test_perpetual_rejects_expiry(self):
        with pytest.raises((InvalidInstrument, CanonicalKeyError)):
            make_instrument(
                asset_class=AssetClass.CRYPTO_PERPETUAL,
                exchange="BINANCE",
                symbol="BTCUSDT",
                currency="USD",
                expiry=date(2026, 9, 24),
            )

    @pytest.mark.parametrize("field", ["multiplier", "tick_size", "lot_size"])
    def test_contract_sizes_must_be_positive(self, field):
        with pytest.raises(InvalidInstrument):
            option(**{field: Decimal(0)})

    def test_strike_must_be_positive(self):
        with pytest.raises(InvalidInstrument):
            option(strike=Decimal("-1"))

    def test_currency_must_be_iso_length(self):
        with pytest.raises(InvalidInstrument):
            option(currency="RUPEE")

    def test_multiplier_assumption_is_visible(self):
        assumed = option(metadata={"multiplier_source": "platform_default"})
        assert assumed.multiplier_is_assumed
        assert not option().multiplier_is_assumed


class TestRoundTrip:
    @given(
        strike=st.decimals(min_value=Decimal("0.01"), max_value=Decimal("1000000"), places=2),
        expiry=st.dates(min_value=date(2020, 1, 1), max_value=date(2040, 12, 31)),
        option_type=st.sampled_from(list(OptionType)),
    )
    def test_option_key_round_trips(self, strike, expiry, option_type):
        key = build_canonical_key(
            exchange="NSE",
            asset_class=AssetClass.OPTION,
            symbol="NIFTY",
            expiry=expiry,
            strike=strike,
            option_type=option_type,
        )
        parsed = parse_canonical_key(key)
        assert parsed.exchange == "NSE"
        assert parsed.asset_class is AssetClass.OPTION
        assert parsed.symbol == "NIFTY"
        assert parsed.expiry == expiry
        assert parsed.option_type is option_type
        assert format_strike(parsed.strike) == format_strike(strike)

    @pytest.mark.parametrize(
        "token,expected",
        [
            ("CE", OptionType.CALL),
            ("ce", OptionType.CALL),
            ("C", OptionType.CALL),
            ("Call", OptionType.CALL),
            ("PE", OptionType.PUT),
            ("p", OptionType.PUT),
            ("PUT", OptionType.PUT),
        ],
    )
    def test_option_type_parsing_accepts_real_world_spellings(self, token, expected):
        assert OptionType.parse(token) is expected

    def test_option_type_parsing_rejects_nonsense(self):
        with pytest.raises(ValueError):
            OptionType.parse("STRADDLE")


class TestStrikeNormalisation:
    """The model's strike and its canonical key must never disagree."""

    @pytest.mark.parametrize("supplied", [Decimal("24000"), Decimal("24000.00"), Decimal("2.4E+4")])
    def test_equivalent_strikes_normalise_to_one_value(self, supplied):
        instrument = option(strike=supplied)
        assert instrument.strike == Decimal("24000")
        assert format_strike(instrument.strike) == "24000"
        assert instrument.canonical_key.endswith(":24000:C")

    def test_fractional_strikes_keep_their_significant_digits(self):
        assert option(strike=Decimal("24000.5000")).strike == Decimal("24000.5")

    def test_the_normalised_strike_round_trips_through_the_key(self):
        instrument = option(strike=Decimal("24000.50"))
        parsed = parse_canonical_key(instrument.canonical_key)
        assert parsed.strike == instrument.strike

"""Reading a provider's payload without inventing any of it.

The failure these tests exist for has no error message: a provider renames a
field, the reader finds nothing there, and every quote from then on carries a
last price of ``None``. Downstream that is indistinguishable from an instrument
nobody is trading, so it can run for days.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from domains.instruments.enums import AssetClass, ExerciseStyle
from domains.market_data.providers.base import InvalidMarketData
from domains.market_data.providers.normalisation import (
    normalise,
    to_decimal,
    to_timestamp,
)
from domains.market_data.providers.upstox import (
    UPSTOX_FULL_QUOTE_SPEC,
    UpstoxMarketDataProvider,
    _match_entry,
)
from domains.market_data.providers.upstox_master import (
    InstrumentMasterOptions as MasterOptions,
)
from domains.market_data.providers.upstox_master import (
    MidnightExpiryConvention,
    UpstoxInstrumentMaster,
    read_expiry,
)

INSTRUMENT_ID = uuid.uuid4()

#: A full-quote entry shaped like the provider's published response.
ENTRY = {
    "instrument_token": "NSE_INDEX|Nifty 50",
    "symbol": "Nifty 50",
    "last_price": 24512.35,
    "volume": 0,
    "oi": 0,
    "timestamp": "2026-09-09T09:20:11+05:30",
    "ohlc": {"open": 24480.0, "high": 24530.1, "low": 24460.0, "close": 24475.5},
    "depth": {
        "buy": [{"quantity": 50, "price": 24512.30, "orders": 3}],
        "sell": [{"quantity": 75, "price": 24512.40, "orders": 5}],
    },
}


class _Directory:
    """Enough of an instrument directory to exercise the provider."""

    def __init__(self, instrument=None, key: str | None = None) -> None:
        self._instrument = instrument
        self._key = key

    async def provider_key(self, instrument_id):
        return self._key

    async def instrument(self, instrument_id):
        return self._instrument

    async def by_provider_key(self, key):
        return self._instrument

    async def option_contracts(self, underlying_id, expiry=None):
        return ()


class _Transport:
    def __init__(self, status: int, payload) -> None:
        self._status = status
        self._payload = payload
        self.calls: list[tuple[str, dict]] = []

    async def get_json(self, url, params, token):
        self.calls.append((url, dict(params or {})))
        return self._status, self._payload


def _provider(payload, status: int = 200, key: str = "NSE_INDEX|Nifty 50"):
    transport = _Transport(status, payload)
    provider = UpstoxMarketDataProvider(
        directory=_Directory(key=key),
        token_source=_token,
        transport=transport,
    )
    return provider, transport


async def _token() -> str:
    return "a-token"


class TestConvertingValues:
    def test_a_float_price_does_not_carry_binary_error_into_a_decimal(self):
        """``Decimal(24512.35)`` is not 24512.35, and every number downstream
        would inherit the difference."""
        assert to_decimal(24512.35) == Decimal("24512.35")

    def test_an_absent_value_is_none_rather_than_zero(self):
        assert to_decimal(None) is None
        assert to_decimal("") is None
        assert to_decimal("not a number") is None

    def test_a_timestamp_without_an_offset_is_refused(self):
        """Reading an exchange's local time as UTC shifts every staleness
        measurement by hours and looks like a perfectly healthy feed."""
        assert to_timestamp("2026-09-09T09:20:11") is None
        assert to_timestamp("2026-09-09T09:20:11+05:30") is not None

    def test_epoch_seconds_and_milliseconds_are_both_read(self):
        seconds = to_timestamp(1757408411)
        millis = to_timestamp(1757408411000)
        assert seconds == millis


class TestTheMappingReportsWhatItRead:
    def test_a_healthy_payload_reports_nothing_missing_and_nothing_unmapped(self):
        outcome = normalise(ENTRY, UPSTOX_FULL_QUOTE_SPEC)
        assert outcome.missing == ()
        assert outcome.unmapped == ()
        assert outcome.looks_like_a_schema_change is False

    def test_a_renamed_field_shows_up_as_missing_and_unmapped_together(self):
        """The signature of a provider changing their response. Either half
        alone is ordinary; both together is the alarm."""
        renamed = {**ENTRY}
        renamed["ltp"] = renamed.pop("last_price")

        outcome = normalise(renamed, UPSTOX_FULL_QUOTE_SPEC)
        assert "last_price" in outcome.missing
        assert "ltp" in outcome.unmapped
        assert outcome.looks_like_a_schema_change is True

    def test_the_report_names_the_path_each_field_came_from(self):
        outcome = normalise(ENTRY, UPSTOX_FULL_QUOTE_SPEC)
        assert outcome.matched["bid_price"] == "depth.buy.0.price"
        assert outcome.to_provenance()["normalisation_spec"] == UPSTOX_FULL_QUOTE_SPEC.name

    def test_an_operator_can_repoint_a_field_without_a_release(self):
        spec = UPSTOX_FULL_QUOTE_SPEC.with_overrides({"last_price": "ltp"})
        outcome = normalise({**ENTRY, "ltp": 1.5}, spec)
        assert outcome.values["last_price"] == 1.5
        assert "overridden by configuration" in outcome.spec_provenance

    def test_a_present_null_is_not_the_same_as_an_absent_key(self):
        """A provider explicitly sending ``"oi": null`` is telling us something
        a missing key does not."""
        outcome = normalise({**ENTRY, "oi": None}, UPSTOX_FULL_QUOTE_SPEC)
        assert "open_interest" not in outcome.missing
        assert outcome.values["open_interest"] is None


class TestReadingAQuote:
    async def test_the_top_of_book_and_the_print_are_read_from_their_own_places(self):
        provider, _transport = _provider({"status": "success", "data": {"Nifty 50": ENTRY}})
        quote = await provider.get_quote(INSTRUMENT_ID)

        assert quote.bid_price == Decimal("24512.3")
        assert quote.ask_price == Decimal("24512.4")
        assert quote.last_price == Decimal("24512.35")
        assert quote.source == "upstox"

    async def test_the_mid_is_derived_and_the_print_never_stands_in_for_it(self):
        one_sided = {**ENTRY, "depth": {"buy": [], "sell": []}}
        provider, _transport = _provider({"status": "success", "data": {"Nifty 50": one_sided}})
        quote = await provider.get_quote(INSTRUMENT_ID)

        assert quote.bid_price is None
        assert quote.mid_price is None
        assert quote.last_price == Decimal("24512.35")

    async def test_a_quote_whose_age_cannot_be_known_is_refused(self):
        """Dating a quote to the moment we read it would make every stale price
        look fresh, which is the one thing a live feed must never do."""
        undated = {key: value for key, value in ENTRY.items() if key != "timestamp"}
        provider, _transport = _provider({"status": "success", "data": {"Nifty 50": undated}})
        with pytest.raises(InvalidMarketData, match="exchange timestamp"):
            await provider.get_quote(INSTRUMENT_ID)

    async def test_a_clean_reading_names_its_spec_and_nothing_more(self):
        """On a healthy feed the report is identical for every quote from the
        same spec. Carrying it per tick would put a kilobyte of unchanging text
        into every stored quote and every cache write."""
        provider, _transport = _provider({"status": "success", "data": {"Nifty 50": ENTRY}})
        quote = await provider.get_quote(INSTRUMENT_ID)

        assert quote.metadata["normalisation_spec"] == UPSTOX_FULL_QUOTE_SPEC.name
        assert "fields_missing" not in quote.metadata

    async def test_a_reading_that_was_not_clean_carries_the_whole_report(self):
        """Which is the moment an operator needs it."""
        renamed = {**ENTRY}
        renamed["ltp"] = renamed.pop("last_price")
        provider, _transport = _provider({"status": "success", "data": {"Nifty 50": renamed}})
        quote = await provider.get_quote(INSTRUMENT_ID)

        assert "last_price" in quote.metadata["fields_missing"]
        assert "ltp" in quote.metadata["fields_unmapped"]
        assert quote.last_price is None

    async def test_a_refused_token_is_reported_as_a_credential_problem(self):
        provider, _transport = _provider({"message": "unauthorized"}, status=401)
        with pytest.raises(Exception, match="re-authorization"):
            await provider.get_quote(INSTRUMENT_ID)


class TestWhatTheProviderDeclaresItCanDo:
    def test_it_does_not_claim_to_serve_instruments(self):
        """The instrument directory comes from the master loader. Declaring a
        capability the class does not serve would let a caller plan around it
        and then fail halfway through a calculation."""
        from domains.market_data.enums import ProviderCapability
        from domains.market_data.providers.base import CapabilityNotSupported

        provider, _transport = _provider({})
        assert not provider.supports(ProviderCapability.INSTRUMENTS)
        with pytest.raises(CapabilityNotSupported):
            provider.require(ProviderCapability.INSTRUMENTS)

    def test_it_declares_the_ones_it_does_serve(self):
        from domains.market_data.enums import ProviderCapability

        provider, _transport = _provider({})
        for capability in (
            ProviderCapability.QUOTES,
            ProviderCapability.OPTION_CHAINS,
            ProviderCapability.ORDER_BOOK,
            ProviderCapability.BARS,
        ):
            assert provider.supports(capability)

    def test_it_does_not_claim_event_level_book_data(self):
        """A snapshot feed cannot support a queue or intensity model, and the
        microstructure gate reads this declaration to know that."""
        from domains.market_data.enums import ProviderCapability

        provider, _transport = _provider({})
        assert not provider.supports(ProviderCapability.BOOK_EVENTS)


class TestMatchingTheResponseToTheRequest:
    """The provider keys its response by a display symbol, not by the key the
    request used. Attaching the wrong entry to an instrument is silent."""

    def test_an_entry_is_matched_on_the_identifier_it_carries(self):
        data = {
            "Nifty 50": {"instrument_token": "NSE_INDEX|Nifty 50"},
            "Nifty Bank": {"instrument_token": "NSE_INDEX|Nifty Bank"},
        }
        key, entry = _match_entry(data, "NSE_INDEX|Nifty Bank")
        assert entry["instrument_token"] == "NSE_INDEX|Nifty Bank"

    def test_a_single_entry_answering_a_single_request_is_unambiguous(self):
        data = {"Nifty 50": {"last_price": 1.0}}
        assert _match_entry(data, "NSE_INDEX|Nifty 50") is not None

    def test_several_unidentifiable_entries_match_nothing(self):
        """There is no third fallback, because the third fallback is where one
        instrument's price gets attached to another's id."""
        data = {"A": {"last_price": 1.0}, "B": {"last_price": 2.0}}
        assert _match_entry(data, "NSE_INDEX|Nifty 50") is None

    async def test_an_unmatchable_response_is_refused_not_guessed(self):
        payload = {
            "status": "success",
            "data": {"A": {"last_price": 1.0}, "B": {"last_price": 2.0}},
        }
        provider, _transport = _provider(payload)
        with pytest.raises(InvalidMarketData, match="identifiable"):
            await provider.get_quote(INSTRUMENT_ID)


class TestTheInstrumentMaster:
    def _rows(self):
        return [
            {
                "segment": "NSE_INDEX",
                "name": "Nifty 50",
                "exchange": "NSE",
                "instrument_type": "INDEX",
                "instrument_key": "NSE_INDEX|Nifty 50",
                "trading_symbol": "Nifty 50",
            },
            {
                "segment": "NSE_FO",
                "exchange": "NSE",
                "expiry": 1758186000000,
                "instrument_type": "CE",
                "underlying_symbol": "NIFTY",
                "underlying_key": "NSE_INDEX|Nifty 50",
                "instrument_key": "NSE_FO|46833",
                "lot_size": 75,
                "tick_size": 5.0,
                "trading_symbol": "NIFTY 25400 CE 18 SEP 25",
                "strike_price": 25400.0,
            },
        ]

    def test_every_row_is_accounted_for(self):
        rows = [*self._rows(), {"instrument_type": "XX", "exchange": "NSE", "instrument_key": "K"}]
        result = UpstoxInstrumentMaster().load(rows)

        assert result.input_rows == 3
        assert result.accepted == 2
        assert result.conserved is True
        assert result.rejection_counts() == {"UNKNOWN_INSTRUMENT_TYPE": 1}

    def test_a_filtered_row_still_closes_the_sum(self):
        result = UpstoxInstrumentMaster(MasterOptions(segments=("NSE_INDEX",))).load(self._rows())
        assert result.accepted == 1
        assert result.filtered_out == 1
        assert result.conserved is True

    def test_the_multiplier_says_it_came_from_the_lot_size(self):
        """Build spec 1.1: a multiplier is either sourced or declared assumed.
        A wrong one silently scales every Greek and every margin number."""
        result = UpstoxInstrumentMaster().load(self._rows())
        option = next(item for item in result.instruments if item.is_option)
        assert option.multiplier == Decimal(75)
        assert option.metadata["multiplier_source"] == "provider_lot_size"
        assert option.multiplier_is_assumed is False

    def test_an_index_with_no_lot_size_declares_its_multiplier_assumed(self):
        result = UpstoxInstrumentMaster().load(self._rows())
        index = next(item for item in result.instruments if item.asset_class is AssetClass.INDEX)
        assert index.metadata["multiplier_source"] == "platform_default"
        assert index.multiplier_is_assumed is True

    def test_a_contract_whose_underlying_was_filtered_out_is_refused(self):
        """Rather than being attached to nothing, or to a stand-in."""
        result = UpstoxInstrumentMaster(MasterOptions(segments=("NSE_FO",))).load(self._rows())
        assert result.accepted == 0
        assert result.rejection_counts() == {"UNDERLYING_NOT_LOADED": 1}

    def test_an_exchange_with_no_recorded_currency_is_refused(self):
        rows = [
            {
                "segment": "XX_EQ",
                "exchange": "XXX",
                "instrument_type": "EQ",
                "instrument_key": "XX|1",
                "trading_symbol": "THING",
            }
        ]
        result = UpstoxInstrumentMaster().load(rows)
        assert result.rejection_counts() == {"UNKNOWN_EXCHANGE": 1}

    def test_an_option_on_a_venue_with_no_recorded_exercise_style_is_refused(self):
        result = UpstoxInstrumentMaster(MasterOptions(exercise_style={})).load(self._rows())
        assert "UNKNOWN_EXERCISE_STYLE" in result.rejection_counts()

    def test_the_provider_key_is_recorded_for_every_instrument_made(self):
        result = UpstoxInstrumentMaster().load(self._rows())
        assert set(result.provider_keys.values()) == {"NSE_INDEX|Nifty 50", "NSE_FO|46833"}

    def test_the_default_exercise_style_is_european_for_indian_venues(self):
        result = UpstoxInstrumentMaster().load(self._rows())
        option = next(item for item in result.instruments if item.is_option)
        assert option.exercise_style is ExerciseStyle.EUROPEAN


class TestExpiryIsNotGuessed:
    """An off-by-one on an expiry changes the contract's identity, its canonical
    key and its time to expiry. None of that announces itself."""

    def test_an_instant_inside_the_trading_day_names_that_day(self):
        reading = read_expiry(1758186000000, "NSE")  # 15:30 IST
        assert reading.expiry.isoformat() == "2025-09-18"
        assert reading.source == "provider_epoch"
        assert reading.ambiguous is False

    def test_an_instant_on_local_midnight_is_ambiguous(self):
        reading = read_expiry(1758220200000, "NSE")  # 00:00 IST
        assert reading.ambiguous is True
        assert [d.isoformat() for d in reading.candidates] == ["2025-09-18", "2025-09-19"]

    def test_the_contracts_own_name_settles_it_when_it_can(self):
        reading = read_expiry(1758220200000, "NSE", contract_name="NIFTY 25400 CE 18 SEP 25")
        assert reading.expiry.isoformat() == "2025-09-18"
        assert reading.source == "contract_name"

    def test_a_name_that_settles_nothing_leaves_it_to_a_stated_convention(self):
        reading = read_expiry(1758220200000, "NSE", contract_name="NIFTY WEEKLY CE")
        assert reading.source == "convention"
        assert reading.expiry.isoformat() == "2025-09-19"

        other = read_expiry(
            1758220200000,
            "NSE",
            contract_name="NIFTY WEEKLY CE",
            convention=MidnightExpiryConvention.END_OF_PREVIOUS_DAY,
        )
        assert other.expiry.isoformat() == "2025-09-18"

    def test_a_name_can_only_choose_between_the_two_candidates(self):
        """So a contract-name format this code misreads degrades to 'could not
        resolve' rather than to some third date."""
        reading = read_expiry(1758220200000, "NSE", contract_name="NIFTY 25400 CE 04 JUL 99")
        assert reading.source == "convention"
        assert reading.expiry in reading.candidates

    def test_how_the_expiry_was_decided_travels_on_the_instrument(self):
        rows = [
            {
                "segment": "NSE_INDEX",
                "exchange": "NSE",
                "instrument_type": "INDEX",
                "instrument_key": "NSE_INDEX|Nifty 50",
                "trading_symbol": "Nifty 50",
            },
            {
                "segment": "NSE_FO",
                "exchange": "NSE",
                "expiry": 1758220200000,
                "instrument_type": "CE",
                "underlying_symbol": "NIFTY",
                "underlying_key": "NSE_INDEX|Nifty 50",
                "instrument_key": "NSE_FO|1",
                "lot_size": 75,
                "trading_symbol": "NIFTY WEEKLY CE",
                "strike_price": 25400.0,
            },
        ]
        result = UpstoxInstrumentMaster().load(rows)
        option = next(item for item in result.instruments if item.is_option)
        assert option.metadata["expiry_date_source"] == "convention"
        assert option.metadata["expiry_date_candidates"] == ["2025-09-18", "2025-09-19"]
        assert option.metadata["expiry_timestamp"].startswith("2025-09-18T18:30")


def _unused() -> None:  # pragma: no cover - keeps datetime import honest
    assert datetime.now(UTC)

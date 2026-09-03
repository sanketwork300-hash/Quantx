"""Position import: the three buckets, and the rows that must never be guessed.

Carries the Phase 4 acceptance criterion from docs/backlog.md that an ambiguous
row is never auto-resolved.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from domains.instruments.enums import (
    AssetClass,
    ExerciseStyle,
    OptionType,
    SettlementType,
)
from domains.instruments.models import make_instrument
from domains.instruments.resolver import (
    ResolutionMethod,
    ResolutionRequest,
    ResolutionResult,
    ResolutionStatus,
)
from domains.market_data.ingestion.column_mapping import infer_mapping
from domains.market_data.ingestion.parser import TabularParser
from domains.portfolio.enums import PositionSide
from domains.portfolio.importer import (
    POSITION_FIELDS,
    AmbiguousRow,
    ImportDefaults,
    ImportRejection,
    InvalidRow,
    PositionImporter,
    commit_instruments,
)

DATA = Path(__file__).resolve().parents[1] / "data" / "portfolio_options.csv"


class StubInstruments:
    """Stands in for the instrument service. Records what it was asked."""

    def __init__(
        self,
        status: ResolutionStatus = ResolutionStatus.UNRESOLVED,
        instrument=None,
        candidates: tuple = (),
    ) -> None:
        self.status = status
        self.instrument = instrument
        self.candidates = candidates
        self.requests: list[ResolutionRequest] = []
        self.known: dict[uuid.UUID, object] = {}

    async def resolve(self, request: ResolutionRequest) -> ResolutionResult:
        self.requests.append(request)
        return ResolutionResult(
            status=self.status,
            request=request,
            instrument=self.instrument,
            method=(
                ResolutionMethod.STRUCTURED_MATCH
                if self.status is ResolutionStatus.RESOLVED
                else None
            ),
            candidates=self.candidates,
        )

    async def get(self, instrument_id):
        return self.known.get(instrument_id)


def option(strike: str, option_type: OptionType, expiry=date(2026, 10, 29)):
    return make_instrument(
        asset_class=AssetClass.OPTION,
        exchange="SYNTH",
        symbol="NIFTY",
        currency="INR",
        multiplier=Decimal(75),
        expiry=expiry,
        strike=Decimal(strike),
        option_type=option_type,
        exercise_style=ExerciseStyle.EUROPEAN,
        settlement_type=SettlementType.CASH,
        underlying_id=uuid.uuid4(),
    )


def csv_bytes(*rows: str) -> bytes:
    header = "SYMBOL,EXCH,TYPE,NETQTY,AVGPRICE,EXPIRY_DT,STRIKE_PRICE,CE_PE,SIDE,TAG"
    return "\n".join([header, *rows]).encode()


async def preview(data: bytes, instruments, defaults: ImportDefaults | None = None):
    mapping = infer_mapping(TabularParser.read_headers(data), POSITION_FIELDS)
    return await PositionImporter(instruments).preview(
        data, mapping, defaults or ImportDefaults(exchange="SYNTH")
    )


class TestAmbiguity:
    async def test_an_ambiguous_row_is_never_auto_resolved(self):
        """Phase 4 acceptance: two candidates means the user chooses.

        Picking the "most likely" contract is how a portfolio silently acquires
        the wrong expiry and every downstream number is wrong with no error.
        """
        candidates = (
            option("23000", OptionType.CALL),
            option("23000", OptionType.CALL, date(2026, 12, 24)),
        )
        stub = StubInstruments(ResolutionStatus.AMBIGUOUS, candidates=candidates)
        result = await preview(
            csv_bytes("NIFTY,SYNTH,OPTION,2,1180,2026-10-29,23000,CE,LONG,carry"), stub
        )

        assert result.resolved == ()
        assert len(result.ambiguous) == 1
        assert isinstance(result.ambiguous[0], AmbiguousRow)
        assert len(result.ambiguous[0].candidates) == 2

    async def test_a_preview_with_any_ambiguous_row_is_not_committable(self):
        stub = StubInstruments(
            ResolutionStatus.AMBIGUOUS,
            candidates=(option("23000", OptionType.CALL), option("23200", OptionType.PUT)),
        )
        result = await preview(
            csv_bytes(
                "NIFTY,SYNTH,OPTION,2,1180,2026-10-29,23000,CE,LONG,carry",
                "NIFTY,SYNTH,OPTION,-1,60,2026-10-29,23200,PE,SHORT,carry",
            ),
            stub,
        )
        assert result.ambiguous
        assert result.committable is False

    async def test_no_instrument_is_created_for_an_ambiguous_row(self):
        stub = StubInstruments(
            ResolutionStatus.AMBIGUOUS,
            candidates=(option("23000", OptionType.CALL), option("23000", OptionType.PUT)),
        )
        result = await preview(
            csv_bytes("NIFTY,SYNTH,OPTION,2,1180,2026-10-29,23000,CE,LONG,carry"), stub
        )
        assert commit_instruments(result) == []


class TestRejections:
    @pytest.mark.parametrize(
        ("row", "reason"),
        [
            (
                "NIFTY,SYNTH,OPTION,5,88,2026-10-29,25400,CE,SHORT,x",
                ImportRejection.SIDE_DISAGREES_WITH_QUANTITY,
            ),
            (
                "NIFTY,SYNTH,OPTION,2,71.5,2026-10-29,,CE,LONG,x",
                ImportRejection.INCOMPLETE_OPTION,
            ),
            (
                "NIFTY,SYNTH,OPTION,0,71.5,2026-10-29,25200,CE,LONG,x",
                ImportRejection.ZERO_QUANTITY,
            ),
            (
                ",SYNTH,OPTION,2,71.5,2026-10-29,25200,CE,LONG,x",
                ImportRejection.MISSING_SYMBOL,
            ),
            (
                "NIFTY,SYNTH,BANANA,2,71.5,2026-10-29,25200,CE,LONG,x",
                ImportRejection.UNKNOWN_ASSET_CLASS,
            ),
        ],
    )
    async def test_each_bad_row_reports_its_own_reason(self, row, reason):
        result = await preview(csv_bytes(row), StubInstruments())
        assert len(result.invalid) == 1
        invalid = result.invalid[0]
        assert isinstance(invalid, InvalidRow)
        assert invalid.reason is reason

    async def test_a_rejected_row_reports_its_source_row_number(self):
        """Numbered over data rows, so the reason points at a line in the file."""
        result = await preview(
            csv_bytes(
                "NIFTY,SYNTH,OPTION,2,1180,2026-10-29,23000,CE,LONG,carry",
                "NIFTY,SYNTH,OPTION,0,71.5,2026-10-29,25200,CE,LONG,x",
            ),
            StubInstruments(),
        )
        assert [r.row_number for r in result.invalid] == [2]

    async def test_nothing_is_dropped_without_a_reason(self):
        """input == resolved + ambiguous + invalid, as for ingestion."""
        data = DATA.read_bytes()
        result = await preview(data, StubInstruments())
        assert result.rows_in == len(result.resolved) + len(result.ambiguous) + len(result.invalid)
        assert result.rows_in == 10


class TestResolution:
    async def test_an_existing_contract_is_reused_not_recreated(self):
        existing = option("23000", OptionType.CALL)
        stub = StubInstruments(ResolutionStatus.RESOLVED, instrument=existing)
        result = await preview(
            csv_bytes("NIFTY,SYNTH,OPTION,2,1180,2026-10-29,23000,CE,LONG,carry"), stub
        )
        assert result.resolved[0].instrument.id == existing.id
        assert result.resolved[0].creates_instrument is False
        assert commit_instruments(result) == []

    async def test_a_new_contract_is_created_with_its_underlying_first(self):
        result = await preview(
            csv_bytes("NIFTY,SYNTH,OPTION,2,1180,2026-10-29,23000,CE,LONG,carry"),
            StubInstruments(),
        )
        created = commit_instruments(result)
        assert [i.asset_class for i in created] == [AssetClass.INDEX, AssetClass.OPTION]
        assert created[1].underlying_id == created[0].id

    async def test_the_sign_of_the_quantity_decides_the_side(self):
        result = await preview(
            csv_bytes(
                "NIFTY,SYNTH,OPTION,2,1180,2026-10-29,23000,CE,,carry",
                "NIFTY,SYNTH,OPTION,-3,60,2026-10-29,23200,PE,,carry",
            ),
            StubInstruments(),
        )
        assert [r.side for r in result.resolved] == [PositionSide.LONG, PositionSide.SHORT]


class TestMultiplierProvenance:
    """A multiplier rescales every value and Greek, so its source is recorded."""

    async def test_a_platform_default_multiplier_is_marked_assumed(self):
        result = await preview(
            csv_bytes("NIFTY,SYNTH,OPTION,2,1180,2026-10-29,23000,CE,LONG,carry"),
            StubInstruments(),
            ImportDefaults(exchange="SYNTH", multiplier=None),
        )
        instrument = result.resolved[0].instrument
        assert instrument.multiplier == Decimal(1)
        assert instrument.multiplier_is_assumed is True

    async def test_a_user_default_multiplier_is_also_marked_assumed(self):
        """A number the user supplied for a whole file is still an assumption."""
        result = await preview(
            csv_bytes("NIFTY,SYNTH,OPTION,2,1180,2026-10-29,23000,CE,LONG,carry"),
            StubInstruments(),
            ImportDefaults(exchange="SYNTH", multiplier=Decimal(75)),
        )
        instrument = result.resolved[0].instrument
        assert instrument.multiplier == Decimal(75)
        assert instrument.multiplier_is_assumed is True


class TestFixtureFile:
    async def test_the_committed_fixture_splits_seven_three(self):
        result = await preview(DATA.read_bytes(), StubInstruments())
        assert len(result.resolved) == 7
        assert len(result.invalid) == 3
        assert {r.reason for r in result.invalid} == {
            ImportRejection.SIDE_DISAGREES_WITH_QUANTITY,
            ImportRejection.INCOMPLETE_OPTION,
            ImportRejection.ZERO_QUANTITY,
        }

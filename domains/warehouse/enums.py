from __future__ import annotations

from enum import StrEnum


class DatasetLayer(StrEnum):
    """Where a dataset sits in the pipeline from bytes to features.

    The three-layer split is not decoration. A `RAW` partition is what arrived,
    byte-preserved in a columnar form; a `NORMALIZED` one has had timestamps put
    into UTC, duplicates marked and identity resolved; a `DERIVED` one is
    something the platform computed. Keeping them apart is what lets a
    normalisation bug be fixed by rebuilding a layer rather than by re-fetching
    data that may no longer be available.
    """

    RAW = "raw"
    NORMALIZED = "normalized"
    DERIVED = "derived"


class DatasetKind(StrEnum):
    """What kind of series a dataset holds.

    Order books and option chains are deliberately absent. Both already have
    homes — L2 in ``domains/microstructure`` and chains in
    ``option_chain_snapshots`` — and a second store for either would be a second
    thing to keep consistent with the first.
    """

    BARS = "bars"
    TRADES = "trades"
    QUOTES = "quotes"


class CorporateActionTreatment(StrEnum):
    """Whether a price series has been adjusted for splits and dividends.

    Declared by whoever registered the dataset, **never inferred**. The platform
    holds no corporate-action feed, so it cannot adjust a series and will not
    pretend to. What it can do is notice a discontinuity that looks like a
    split and say so, which is what the validator's split-like jump check is
    for.

    This matters more than its size suggests: an unflagged 1:5 split reads as a
    -80% return, and a backtest run across it produces a number that is wrong
    and looks fine.
    """

    #: Prices as the venue printed them on the day.
    UNADJUSTED = "UNADJUSTED"
    #: The source states it has back-adjusted for corporate actions.
    ADJUSTED_BY_SOURCE = "ADJUSTED_BY_SOURCE"
    #: Nobody said. The default, and it is a warning rather than a shrug: a
    #: series whose treatment is unknown cannot safely be joined to one whose
    #: treatment is known.
    UNKNOWN = "UNKNOWN"


class DatasetStatus(StrEnum):
    REGISTERED = "REGISTERED"
    #: Partitions written and validated. The only status a query will serve.
    AVAILABLE = "AVAILABLE"
    #: Validation found something that makes the data unusable as it stands.
    QUARANTINED = "QUARANTINED"


class ValidationCode(StrEnum):
    """What a validator found. Every one of these is reported, never repaired.

    The platform's rule is that suspicious data is flagged and kept. A validator
    that silently dropped bad ticks would produce a clean-looking series and an
    unexplainable backtest.
    """

    #: A row could not be read as the schema at all. These are the only rows
    #: that do not reach a partition, and each one reports its source position.
    SCHEMA_INVALID = "SCHEMA_INVALID"
    #: A timestamp with no offset. Refused rather than assumed to be UTC:
    #: reading an exchange's local time as UTC shifts a whole series.
    TIMESTAMP_NOT_TIMEZONE_AWARE = "TIMESTAMP_NOT_TIMEZONE_AWARE"
    #: Two rows with the same instrument and timestamp.
    DUPLICATE_TIMESTAMP = "DUPLICATE_TIMESTAMP"
    #: Rows are not in ascending time order.
    OUT_OF_ORDER = "OUT_OF_ORDER"
    #: A bar whose high is below its low, or whose open or close sits outside
    #: the range. Structurally impossible, so the row is wrong whatever it says.
    BAR_RANGE_INCONSISTENT = "BAR_RANGE_INCONSISTENT"
    #: A non-positive price where a price is required.
    NON_POSITIVE_PRICE = "NON_POSITIVE_PRICE"
    #: A negative volume or size.
    NEGATIVE_SIZE = "NEGATIVE_SIZE"
    #: A return far outside the series' own dispersion.
    OUTLIER_RETURN = "OUTLIER_RETURN"
    #: A jump close to a simple ratio, on a series that is not declared adjusted.
    SPLIT_LIKE_JUMP = "SPLIT_LIKE_JUMP"
    #: A date with no observation, common to every instrument in the dataset —
    #: which is what a market closure looks like.
    GAP_ALL_INSTRUMENTS = "GAP_ALL_INSTRUMENTS"
    #: A date with no observation for one instrument while others have data —
    #: which is what missing data looks like.
    GAP_SINGLE_INSTRUMENT = "GAP_SINGLE_INSTRUMENT"
    #: The dataset does not say whether its prices are corporate-action adjusted.
    CORPORATE_ACTION_TREATMENT_UNKNOWN = "CORPORATE_ACTION_TREATMENT_UNKNOWN"


class ValidationSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    #: The dataset cannot be served as it stands.
    ERROR = "ERROR"

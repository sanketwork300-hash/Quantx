"""Order-book microstructure numerics.

Everything here operates on plain floats and returns structured results. There
is no market-data type, no database and no provider in this package, for the
same reason as the rest of ``quant/``: the numbers have to be checkable against
hand-worked examples without standing an application up.

Three groups, and they are gated separately because they need genuinely
different data:

``book``
    Snapshot analytics — spread, depth, microprice, imbalance, book slope,
    depth concentration, cost to trade. A depth snapshot is enough.

``intensity``
    Poisson and Hawkes event-arrival models, and the held-out comparison
    between them. Needs a timestamped event stream, not snapshots.

``queue``
    Where a hypothetical resting order sits in a price level's queue and what
    that implies for a fill. Needs event-level data *and* a stated assumption
    about where cancellations come from, so it returns a bracket rather than a
    number.
"""

from quant.microstructure.book import (
    Book,
    BookAnalytics,
    BookSide,
    SlopeEstimate,
    TradeCost,
    analyse_book,
    book_slope,
    depth_concentration,
    imbalance,
    microprice,
    weighted_imbalance,
)
from quant.microstructure.intensity import (
    HawkesParameters,
    HeldOutComparison,
    IntensityFit,
    PoissonParameters,
    compare_held_out,
    fit_hawkes,
    fit_poisson,
    hawkes_log_likelihood,
    poisson_log_likelihood,
    simulate_hawkes,
)
from quant.microstructure.queue import (
    CancellationPriority,
    QueueEstimate,
    QueueOutlook,
    estimate_queue_outlook,
)

__all__ = [
    "Book",
    "BookAnalytics",
    "BookSide",
    "CancellationPriority",
    "HawkesParameters",
    "HeldOutComparison",
    "IntensityFit",
    "PoissonParameters",
    "QueueEstimate",
    "QueueOutlook",
    "SlopeEstimate",
    "TradeCost",
    "analyse_book",
    "book_slope",
    "compare_held_out",
    "depth_concentration",
    "estimate_queue_outlook",
    "fit_hawkes",
    "fit_poisson",
    "hawkes_log_likelihood",
    "imbalance",
    "microprice",
    "poisson_log_likelihood",
    "simulate_hawkes",
    "weighted_imbalance",
]

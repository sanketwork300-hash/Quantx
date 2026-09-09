# The historical data warehouse

## 1. What real-time Phase 3 delivers

```
Upload a historical file
      │
      ▼
POST /warehouse/datasets                one job: read → validate → write → register
      │
      ├─ read       CSV or Parquet, one coercion path, symbols resolved
      ├─ validate   schema, timestamps, duplicates, order, outliers, splits, gaps
      ├─ write      Hive-partitioned Parquet in the object store
      └─ register   a row saying what arrived, what survived and how good it is
      │
      ▼
GET /warehouse/query                    DuckDB over the partitions
      │
      ▼
usable by the research and backtest engines
```

The data lives in the object store. What lives in PostgreSQL is the **registry** —
which datasets exist, what they cover, how good they are and where their files
are. `docs/database.md` draws that line: a tick tape does not go in a relational
row store, but "find me the NIFTY daily dataset" is user-activity sized and
transactional.

## 2. The layout, and why it is Hive-style

```
warehouse/{layer}/{kind}/exchange=NSE/year=2026/month=03/day=02/instrument_id=<uuid>/part.parquet
```

Hive-style because DuckDB reads it natively: the partition values are *columns*,
so a query for one exchange over one week reads the files for that exchange and
that week and no others. A layout that needed a lookup table to prune would have
made the "queryable" half of this phase a wrapper around a full scan.

It also means a file found on its own is still identifiable — from the path, and
again from the parquet footer, which repeats the exchange, day, instrument,
source and code commit.

**Three layers.** `raw` is what arrived, in columnar form. `normalized` has had
timestamps put into UTC, duplicates marked and identity resolved. `derived` is
something the platform computed. Keeping them apart means a normalisation bug is
fixed by rebuilding a layer rather than by re-fetching data that may no longer
be available.

**Order books and option chains are deliberately absent** from the kinds. Both
already have homes — L2 in `domains/microstructure`, chains in
`option_chain_snapshots` — and a second store for either would be a second thing
to keep consistent with the first.

**Partitioning is on the UTC day**, not the venue's session day, so one dataset
spanning several exchanges partitions consistently and a reader does not need
each venue's session boundary to find a file. The venue's own day is recoverable
from the timestamps inside; the partition is only an index.

## 3. Validation: found, reported, never repaired

The output is the platform's usual trichotomy, and it conserves:

```
rows_in == rows_written + rows_excluded + rows_rejected
```

| Outcome | What it means |
| --- | --- |
| **rejected** | the row is not a row of this kind at all — a bar whose high is below its low. It reaches no partition, and reports its source position and reason. |
| **excluded** | well-formed, but must not be served as a second observation of the same instant. In practice only exact duplicates, where keeping both would make every count downstream disagree with the venue's. |
| **written** | everything else, *including everything merely suspicious*, which arrives in the partition with its flags attached. |

That last row is the important one. A validator that quietly removed bad ticks
would hand the research engine a clean-looking series and an unexplainable
backtest. Flags are a column in the schema, so a reader can exclude them, weight
them down, or look at them — and `exclude_flagged` on the query defaults to
**False**, because an outlier is often the most important observation in a
sample and the judgement is the caller's.

### The timestamp rule

A timestamp with no offset is **refused**, not read as UTC. A year of NSE bars
stamped in local time and read as UTC is a year of bars shifted by five and a
half hours, and nothing downstream would ever say so.

This needed care. The shared `TabularParser` defaults a naive timestamp to UTC —
correct for an option chain, whose as-of moment is supplied separately. So the
warehouse reader inspects the *source text* before coercion and puts the naive
value back where the text named no zone, which lets the validator refuse it by
name rather than having the rule silently never fire.

### Outliers

A modified z-score on log returns: robust, about the median, scaled by the median
absolute deviation. Robust because the thing being looked for is exactly what
would inflate a standard deviation — one bad tick raises sigma enough to hide
itself.

The threshold is **ten** robust deviations, deliberately far out. Financial
returns have fat tails, so a threshold catching every three-sigma day would flag
a third of every crisis and train everyone to ignore the flag.

A series that barely moves has a median absolute deviation of exactly zero, and
a lone spike in it would then be invisible. The scale falls back to the mean
absolute deviation about the median, which a spike does move; only a genuinely
constant sample scores zero, and there a spike does not exist to be found.

### Corporate actions

**The platform holds no corporate-action feed. It will not adjust a series, and
it will not pretend to.**

What it does instead is notice. A jump close to a simple split ratio — 2, 2.5, 3,
4, 5, 10, 3/2, 5/4, 20, checked both ways round so a 5:1 and a 1:5 are both
caught — on a series *not declared adjusted*, is reported as an error and the
dataset is quarantined.

`corporate_action_treatment` is declared by whoever registers the dataset:
`UNADJUSTED`, `ADJUSTED_BY_SOURCE` or `UNKNOWN`. `UNKNOWN` is the default and is
itself a warning, because a series whose treatment nobody stated cannot safely be
joined to one whose treatment is known.

This matters more than its size suggests. An unflagged 1:5 split reads as an
-80% return, and a backtest run across it produces a number that is wrong and
looks fine.

### Gaps

The platform holds no trading calendar — exchange calendars come from QuantLib,
which is a test oracle here and not a runtime dependency — so it cannot say which
absent dates are holidays and does not claim to.

What it can say is *which absences are shared*. A date with no observation for
**any** instrument in the dataset looks like a market closure (`INFO`). A date
carrying data for other instruments but not this one looks like missing data
(`WARNING`). That distinction is derived from the data itself, and it is the one
a reader actually needs.

## 4. Quality

Five dimensions and an overall, aggregated as a **weighted geometric mean** — the
same aggregation the per-quote quality engine uses, so one catastrophic dimension
drives the overall to zero rather than being averaged away by four healthy ones.

| Dimension | Measured as |
| --- | --- |
| `completeness` | fraction of supplied rows that survived validation. Deliberately *not* "fraction of the data that should exist", which needs a calendar; the gap findings carry that instead. |
| `consistency` | fraction of rows with no structural defect. |
| `outlier` | fraction of rows carrying no outlier or split-like flag. |
| `source` | how completely the dataset declares its **own provenance** — source named, treatment declared, digest present. Not a ranking of vendors, which the platform has no basis for. |
| `freshness` | **`None` for an archive.** A 2015 tape is not stale, it is history, and a zero there would rank every archive as broken. Scored only for a dataset that declares itself `continuous`. |

A dimension that cannot be measured returns `None`, which is never collapsed
into zero.

## 5. Quarantine

A validation **error** — today, a split-like jump on an undeclared series — sets
the dataset to `QUARANTINED`, and a query naming it is refused.

Quarantine refuses to *serve* the data; it does not destroy it. The partitions
are written, the findings are stored in full, and both are retrievable. What is
prevented is the one thing that matters: serving the series with a warning
attached and hoping the warning is read, which is how an unadjusted split
reaches a backtest.

## 6. Reading it back

Two paths, and **which one ran is reported** rather than left to be inferred from
the latency:

* `DIRECT` — the object store is filesystem-backed, so DuckDB is handed a glob
  and does its own partition pruning, predicate pushdown and column projection.
  Nothing is loaded that the query did not ask for.
* `MATERIALISED` — the store is remote, so partitions are fetched and registered
  as an in-memory table first. Correct, and bounded by memory, which is why the
  query carries a partition limit and **fails** rather than silently truncating
  when it would exceed it.

`partitions_read` counts the files that actually contributed rows, not the number
a glob matched — so a broken prune is visible on the response rather than only in
the latency.

`truncated` is on every response for the same reason: a short answer must never
be mistaken for a complete one.

Prices come back as **strings**, from `decimal128(38, 12)` in the file. A stored
observation is a fact, and a float round trip would re-round the tick prices the
venue published.

## 7. What is deliberately not here

**Corporate-action adjustment.** Detection without a feed is honest; correction
without one is invention.

**A trading calendar.** Gaps are reported as shared or not shared, which is
derivable from the data. Naming holidays is not.

**Automatic outlier removal.** Flagged and kept. The judgement about whether a
30% day is a bad tick or the most interesting row in the sample belongs to
whoever is modelling, not to the loader.

**Compaction and lifecycle.** One file per instrument-day is right for a daily
series and wrong for a year of ticks. Rewriting small partitions into larger ones
is real work with real failure modes, and it needs a workload to be designed
against rather than guessed at.

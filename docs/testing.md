# Test Strategy

A quantitative platform can be 100% green on software tests and still produce
numbers that are wrong. So the suite has two independent axes: **does the code
work** and **are the numbers right**.

---

## 1. Layout

```
tests/
├── unit/              pure logic, no I/O            (fast, run on every save)
├── integration/       API + DB + object store       (SQLite/aiosqlite or Postgres)
├── quant_validation/  numerical correctness         (analytic + reference libraries)
├── regression/        golden files, tolerance-gated
├── performance/       benchmarks, opt-in marker
└── data/              committed fixtures
```

`quant_validation` never touches the database. That is possible only because
`quant/` imports nothing from `domains/` or `infrastructure/`, which
`scripts/check_layering.py` enforces.

## 2. Software tests

- **Unit**: instrument invariants, canonical key/uuid5 determinism, quote
  derived fields, quality scoring, CSV parsing and column mapping, exclusion
  policy, job state machine, JWT/password primitives.
- **Integration**: full HTTP round trips through the real app with a real
  database — register, login, upload, preview, ingest, poll job, read chain.
  Ownership isolation is tested explicitly: user B must get 404 on user A's ids.
- **End-to-end** _(planned)_: Playwright against the compose stack for the
  upload -> chain -> smile -> surface path.

## 3. Quantitative validation

The mandatory checks from build spec §87, phased in with the features:

| Area | Test | Phase |
| --- | --- | --- |
| Synthetic market | generated chain is internally arbitrage-free (bounds, parity, butterfly, calendar) | 0 |
| Quality engine | crossed/locked/stale/sub-intrinsic fixtures produce the expected flags and exclusions | 0 |
| Black-Scholes | prices and Greeks vs `vollib` and QuantLib | 1 `[x]` |
| Put-call parity | `C - P == DF*(F - K)` within tolerance over a grid | 1 `[x]` |
| Black-76 vs BSM | the two parameterizations must price identically | 1 `[x]` |
| Implied volatility | `price(sigma) -> solve -> sigma` recovered to 1e-6 where well conditioned; within a few multiples of the reported `uncertainty` where it is not | 1 `[x]` |
| Greeks | analytic vs central finite differences, every Greek | 1 `[x]` |
| Greek units | vega per vol point, theta per day, rho per bp, each against a re-priced bump | 1 `[x]` |
| Forward estimation | parity regression recovers `F` and `DF` exactly from prices alone | 1 `[x]` |
| Chain analysis | the known generating surface is recovered end to end from tick-rounded quotes | 1 `[x]` |
| SVI parameterization | analytic derivatives vs finite differences; Durrleman `g` catches a steep slice; Lee's bound is the asymptotic slope | 2 `[x]` |
| SVI calibration | known parameters recovered from a wide slice to 1e-4; fitted slices always admissible; deterministic across runs | 2 `[x]` |
| SVI identifiability | a narrow window fits to 0.005 vol points while missing the parameters by 0.05 — asserted, not papered over | 2 `[x]` |
| Arbitrage conditions | clean chain produces none; each seeded corruption is caught by the right detector with the right magnitude | 2 `[x]` |
| Surface pipeline | the generating surface is recovered in sample from tick-rounded quotes; a corrupted market dirties the raw report but not the fit | 2 `[x]` |
| Reference values | reproduced bit-for-bit from persisted parameters, with no re-fitting on read | 2 `[x]` |
| Local vol | constant local vol -> PDE price converges to Black-Scholes; measured order of convergence | 9 |
| Heston | cross-check against QuantLib | 9 |
| Surface characteristics | analytic skew matches a finite difference of the fitted curve; interpolated tenors lie between their neighbours | 3 `[x]` |
| Historical percentiles | thin samples are reported and marked unreliable; a constant sample yields no z-score | 3 `[x]` |
| Anomaly detection | quiet on a market that agrees with its fit; finds a quote nudged off the surface; a wider market or worse fit lowers the score | 3 `[x]` |
| Anomaly language | no advisory word appears anywhere in the serialised output, at domain and HTTP level | 3 `[x]` |
| VaR | recovers analytic quantile of a known synthetic distribution | 5 |
| Monte Carlo | convergence with `1/sqrt(N)`; identical results for identical seeds | 5 |
| Execution | deterministic synthetic price path -> hand-computed IS/VWAP | 7 |

### A tolerance that adapts to conditioning

The implied-volatility round trip is the clearest case where a single fixed
tolerance would have been dishonest. For a deep in-the-money option the price is
nearly flat in volatility, so a float64 price simply does not determine the
volatility to 1e-6 — and asserting that it does would have meant either a failing
test or a tolerance loosened until it passed everywhere, which hides the real
result at the money.

Instead the solver reports `uncertainty` (roughly one price ulp divided by
vega), and the test asserts 1e-6 where the problem is well conditioned and a few
multiples of `uncertainty` where it is not. That is a *stronger* assertion than a
blanket bound: it checks the solver against the best any algorithm could do on
that input, and it fails if the reported conditioning is itself wrong.

### Reference libraries as oracles, not dependencies

`vollib` and `QuantLib` are installed under the `validation` extra and used in
tests only. Production code paths do not import them unless a specific,
documented decision says so (see `docs/references.md`). This gives independent
implementations that can disagree — which is the point of a cross-check. Tests
that need them are marked `requires_reference_libs` and skip cleanly when absent,
so the core suite runs anywhere.

Phase 1 justified that posture concretely: `py_vollib.black.implied_volatility`
returns `inf` or raises on essentially every input in this environment,
including a plain at-the-money quote. A platform that had wrapped it would be
shipping `inf`. The suite uses the working `black_scholes` entry point as the
oracle, skips the cases where vollib itself fails, and asserts the gap.

## 4. Property-based tests (Hypothesis)

Invariants worth stating as properties:

- Call price is non-decreasing in spot and non-increasing in strike.
- Put price is non-decreasing in strike.
- Option value is non-negative and within its no-arbitrage bounds.
- Every quality sub-score lies in `[0, 1]`, and `overall_score <= max(sub-scores)`.
- Canonical key parsing round-trips: `parse(render(instrument)) == key`.
- Ingestion conserves rows: `input == kept + excluded`, and every excluded row
  has exactly one primary reason.
- Total variance from an admissible SVI slice is non-negative everywhere.
- A fitted slice's `g(k)` is non-negative across the checked grid.
- An anomaly's `explained_scale` grows when the market widens or the fit worsens.
- Confidence stays within `[0, 1]` for every combination of inputs.
- Portfolio aggregation: the sum over positions equals the portfolio total, for
  value and for every Greek, over arbitrary mixes of long and short, quoted and
  unquoted legs (`test_portfolio_valuation.py::TestSumProperty`). The value is
  compared exactly because it is `Decimal` throughout; the Greeks are float sums
  and are compared to float tolerance.
- Every position carries a `valuation_method`, and `base_market_value is None`
  exactly when that method is `UNAVAILABLE` — so an unpriced position can never
  be counted as worth zero.
- Position import conserves rows: `input == resolved + ambiguous + invalid`.
- A margin estimate is never negative and its confidence is always in `[0, 1]`,
  for any mix of long and short legs (`test_margin.py`).
- A schedule's slices sum to the parent quantity exactly, for arbitrary weights,
  totals and lot sizes (`test_execution_simulation.py`). Exactly, not to a
  tolerance: `Decimal` throughout, and `Schedule.__post_init__` raises otherwise.
- Every simulated result carries the counterfactual label, for any latency.
- A parent order's average price always lies between its best and worst fill,
  and a shortfall measured against that same average is always exactly zero
  (`test_tca.py`). The second is the check that the thing being measured cannot
  be its own benchmark.
- A Monte Carlo run is reproducible from its seed, for every path count and seed
  (`test_var.py::TestSimulation::test_every_run_is_reproducible`).
- The vectorised repricing agrees with the scalar one for any spot and
  volatility shock (`test_revaluation.py::TestVectorisedAgreesWithScalar`).
- Execution schedules sum to the parent quantity _(Phase 8)_.
- SSVI total variance at the money equals `theta` exactly, and the analytic
  strike derivatives match a Richardson-extrapolated finite difference
  (`test_ssvi.py`). The reference is extrapolated rather than differenced at one
  step because the second difference is squeezed from both sides — cancellation
  at a small step, a sharply peaked curvature at a large one.
- A fitted SSVI term structure is non-decreasing, for any observed input,
  including an inverted one.
- A local-volatility grid conserves its points: `total = valid + flagged`, and
  every invalid point carries at least one flag.
- Every model in a consensus carries exactly one of a value and an
  unavailability reason.

### Testing an order of convergence, not an error

Phase 9's PDE criterion is the *rate* at which the error falls under refinement,
not its size. A coarse grid can be close by luck and a wrong scheme can be close
on one contract; only the order separates a correct second-order scheme from an
incorrect one. `tests/quant_validation/test_pde.py` refines the grid four times,
fits a slope in log-log, and requires at least 1.8 for price, delta and gamma on
both a uniform and a concentrated grid.

It earned its place on the first run: gamma came back at order 1.0 because the
solution was interpolated with a three-point quadratic, whose second derivative
is accurate only to `O(h)`. The price and the delta looked correct throughout.

The same file holds the Dupire round trip — a local-volatility surface must
reprice the implied surface it was derived from — which found two further errors
that no other test could see, both described in `docs/architecture.md` §23.

### Testing an absence

Phase 6's acceptance criteria are mostly about numbers the platform must *not*
produce, which needs a different kind of test. Three shapes are used:

- **Scan the serialised payload.** `test_margin.py` renders the whole result to
  a string and asserts that venue names and affirmative claims do not appear in
  it. This catches a phrase added to a docstring or an assumption three layers
  down, which an assertion on a named field would not.
- **Test the construction, not the word.** "Liquidation" is allowed to appear,
  because the sentence doing the most work is the one that denies it. The test
  walks every occurrence and requires "not a broker" immediately before it.
  Banning the substring outright would have forbidden the disclaimer.
- **Assert the absence of fields.** `required_margin`, `broker_margin` and
  `exchange_margin` must not be keys of the result payload, so a future addition
  has to delete a test to land. Phase 9 does the same for the consensus:
  `best_model`, `true_price`, `fair_value`, `recommendation` and `signal` are
  checked against every key at every depth of the serialised response. Phase 11
  checks the same closed list three ways over the unified order analysis: every
  key at every depth of a live response, every property of the published OpenAPI
  components, and the whole serialised payload against a list of forbidden
  phrasing. The endpoint's guarantee is that there is nowhere for advice to go,
  so the schema is tested as well as the value.

### Testing a gate

Phase 10's central claim is a *negative* one — the self-exciting arrival model
is reported only when it beat a constant rate on data it had not seen — and it
needs a shape of test the others do not.

- **Test the refusal, not only the acceptance.** `TestTheGate` fits the
  comparison to five simulated self-exciting tapes and requires adoption, and
  to ten genuinely Poisson tapes and requires refusal. The second half is the
  one that matters: it is what stops the gate degenerating into a formality.
- **Test that the threshold is doing work.** The first implementation compared
  raw held-out totals and adopted the richer model seven times out of eight on
  arrivals with no clustering whatsoever, by hundredths of a nat.
  `test_a_raw_positive_total_is_not_enough_on_its_own` now sweeps thirty Poisson
  tapes and asserts that at least one has a *positive* raw gain and is still
  refused, so a regression to "read the sign of a difference" fails.
- **Test that there is no way round it.** `test_there_is_no_parameter_that_
  overrides_a_refusal` reads the published OpenAPI schema for every
  microstructure path and asserts no `force`, `override`, `skip_gate` or
  `ignore_availability` appears anywhere in it. An escape hatch added later has
  to delete this test to land.
- **Assert against the enums, not against a list.** The import tests compare the
  set of reasons the fixtures triggered with `set(SnapshotRejection)` and
  `set(EventRejection)` by equality, so a new rejection reason cannot be added
  without a fixture row that produces it.

### Testing a zero that must never be reported

Phase 11's most dangerous output is not a wrong number, it is a **row of
zeros**: if the proposed contract cannot be repriced it never enters the
combined book, both sides of every comparison are identical, and every
difference is exactly zero — which reads as an order that adds no risk.

- **Test the refusal at both levels.**
  `test_an_order_that_cannot_be_repriced_is_reported_not_absorbed` asserts at the
  domain level that the two exposure sets really are identical and that the flag
  says so; `TestAnOrderThatCannotBeRepricedIsRefused` asserts over the wire that
  the risk and margin branches then return `FAILED` with the exclusion reason
  rather than the zeros, while the branches that do not need a repriceable book
  still answer.
- **Test that a difference is a difference.** Doubling the order doubles its
  Greek contribution, and a buy and a sell of the same size move the book by
  equal and opposite amounts. Both would pass trivially against a broken
  implementation that returned zeros, which is why the refusal above is tested
  separately rather than inferred from them.
- **Test the shared snapshot as a set, not as an equality.** The acceptance
  criterion is that all five branches read one `MarketState`, so the test builds
  the *set* of `market_state_id` values across the five provenance blocks and
  asserts it has exactly one element. A pairwise comparison would pass a
  four-branch response.
- **Test the two cost estimators against each other.** The Phase 8 simulator and
  the Phase 11 forward estimate answer different questions but must share one
  convention. `TestOneConvention` runs both over a flat path and compares fill
  price, spread, temporary and permanent impact slice for slice, on both sides,
  so the convention has one definition and two entry points rather than two
  definitions.
- **Test the property the model does *not* have.** It is tempting to assert that
  working an order lowers its cost. It does not, in this model: permanent impact
  accumulates per slice while the temporary term falls, and which dominates is a
  property of uncalibrated coefficients.
  `test_this_model_does_not_say_that_working_an_order_is_cheaper` pins both
  directions, so such a claim cannot appear by accident.

### Two invariants that exist to catch a future change

A null scenario reprices to **exactly** the base value, and every scenario's
risk contributions sum to the total with a zero residual. Both hold by
construction today. They are asserted anyway, because both stop holding the
moment something portfolio-level and non-additive — netted margin, in Phase 6 —
enters the revaluation, and it is better for a test to say so than for a number
to quietly change.

### Testing a file that loads cleanly and is wrong

`tests/unit/test_chain_layout.py` and `tests/integration/test_two_sided_chain.py`
guard a failure with no error message. An exchange option-chain export puts
calls to the left of `STRIKE` and puts to the right, repeating `BID`, `ASK`,
`LTP`, `OI` and `VOLUME` once per side. Read by header name, `csv.DictReader`
keeps the last column of each repeated name: the file parses, every row
validates, the snapshot's row accounting balances, and every call carries the
put's bid and ask.

Nothing downstream can detect this. The implied volatilities converge, the
smile fits, the surface calibrates, and every number is wrong. So the assertion
is made where the mistake would be made — `test_the_call_keeps_the_call_price_and_the_put_keeps_the_put_price`
and `test_no_strike_has_the_same_quote_on_both_sides` — rather than on a
downstream quantity that would absorb it silently.

The companion assertion is that the *long-form* path is unchanged
(`test_a_long_form_file_is_left_alone`), because a layout detector that starts
claiming ordinary files is the way this fix would itself become the bug.

Detection also runs on the commit path, which widens that risk, so
`TestReadingAFileTheCallerDidNotDescribe` pins the boundary directly: a caller
who supplies a mapping gets no detection at all — including a *partial* one
(`test_a_partial_mapping_is_an_instruction_too`), which is answered with the
field it is missing rather than a different reading — a named layout is used
verbatim, and a long-form file is not turned into a two-sided one.

`TestTheFileIsReadWithoutBeingDescribed` then commits the NSE file with an empty
request body and asserts both that it loads and that the calls still carry the
call prices: the same assertion as the confirmed path, because the shortcut must
not be the unsafe one. `TestIngestingWithNothingSaidAboutTheFile` does the same
for a long-form file and asserts that the header-name inference is reported
rather than done quietly.

`tests/integration/test_chain_file_shapes.py` uploads chains the way they
actually arrive, each with an empty request body: a title above the header,
semicolons or tabs between the cells, both forms of the NSE bhavcopy with a
future and another underlying mixed in, headers that say `Call LTP`. Beside
those sit the files that must *not* be read: a date column that reads two ways
is refused until `date_order` is stated and is then read that way throughout, a
field read from a header name used twice is refused, and a decimal comma is set
aside rather than read as a number a hundred times too large. The unit tests
under `TestTheOrderOfANumericDateIsSettledPerColumn` include the case that
matters most for the preview -- the one value that settles a column sitting
past the sample limit -- because a sample and its file must not read the same
column two ways.

### Testing the difference between a bad file and a bad reading

`tests/unit/test_reading_report.py` guards one line: a row that could not be
*read* against a row that is *empty*. Both fail to become a quote, and treating
them the same breaks the feature in one of two ways — count empty rows against
the reading and every real exchange chain is refused, because far strikes carry
no quotes on one side; ignore unreadable rows and a file whose expiry column
holds prices is ingested as an almost-empty market.

So the classification is parametrised over every rejection reason
(`test_each_reason_is_classified`), and a companion test asserts that the set of
reasons is fully partitioned (`test_every_rejection_reason_has_been_considered`)
— a reason added later has to be classified deliberately rather than falling
into whichever branch is the default.

The same line separates a sample from a file. `TestAChainThatOpensOnQuietStrikes`
(`tests/integration/test_two_sided_chain.py`) uploads a chain whose first sixty
strikes are unquoted on both sides: the preview must call it readable, the
ingest must be accepted and keep the quotes further down, and a file of
*nothing but* quiet strikes must still be refused -- by the worker, which is the
only place that has seen the whole file. `TestAnAsOfPastTheExpiryIsCalledOut`
pins the other refusal that is not about reading: a wholly expired chain writes
nothing, a partly expired one is stored with its expired quotes excluded. And
`TestAnOptionalColumnThatCannotBeReadCostsTheCellNotTheFile` asserts that an
unreadable optional cell is reported by column rather than rejecting its row.

`TestAFileThatCouldNotBeReadIsRefused` then asserts the consequence rather than
the mechanism: a chain read with the wrong expiry column returns `422` and
`/market/chains` is still empty. The refusal is worth an explicit "nothing was
written" test, because the failure it prevents is not an exception — it is a
stored snapshot holding four quotes out of forty thousand, which every
downstream analysis reads as a quiet market.

The last of that class,
`test_a_file_that_only_goes_wrong_past_the_sample_is_refused_by_the_worker`,
exists because the rule runs in two places over two different amounts of the
file. Sixty clean rows followed by four hundred broken ones is accepted by the
request, which sees fifty, and refused by the worker, which sees all of them. A
test that only exercised the request path would let the worker's copy of the
rule rot.

### Testing that a secret stays a secret

`tests/unit/test_credential_vault.py` and
`tests/integration/test_broker_connections.py` guard a class of bug that no
functional test would catch, because the feature works perfectly while leaking.

The disclosure assertions are made against the *raw response body* rather than
against named fields — `assert ACCESS_TOKEN not in completed.text` — so a field
added later that happens to carry the token fails the test that already exists,
instead of needing a new one nobody thought to write. The same assertion is made
against the stored row (`test_the_token_is_not_readable_from_the_row_that_holds_it`)
and against the audit log (`test_no_audit_entry_carries_the_credential`), which
are the two places a credential most plausibly ends up in the clear.

The handoff tests assert what must *not* work: a replayed state, another
account's state, an invented state, and an API access token presented as a state.
The last one exists because both tokens are signed with the same key and only a
`typ` claim separates them — a separation that is invisible in the type system
and would survive any amount of ordinary testing.

The expiry tests are the ones that pin the judgement rather than the mechanism.
`test_an_undeclared_expiry_is_not_treated_as_an_expiry` fails if the platform
ever starts assuming a lifetime for a provider that published none, and
`test_a_renewal_that_returns_no_new_refresh_token_keeps_the_old_one` fails if a
connection that should renew indefinitely instead dies at its first renewal —
which is the kind of defect that appears a day after deployment, once, per user.

Every provider exchange in these tests goes through `FakeOAuthClient`. A test
that can be made to reach a real broker is a test that will one day present a
real credential to one.

### Testing a feed nobody can connect to in CI

`tests/unit/test_market_stream.py` and
`tests/integration/test_live_market_data.py` split the problem where the risk
actually is. The connection lifetime — backoff, subscription replay, duplicate
suppression, ordering, staleness — is identical whichever venue is on the other
end, and every one of those fails *silently* when it is wrong, so it is tested
with no provider, socket or database anywhere near it.

The integration tests then run the real thing with one substitution: the
transport is scripted rather than a socket. The normalisation, the quality
scoring, the live store, the `MarketState` assembly and the API are all
production code, so a passing test is a test of the platform and not of a mock.

The assertions worth naming are the ones that pin an absence:
`test_an_older_observation_never_overwrites_a_newer_one` fails if a replayed
quote can make the market move backwards;
`test_a_connection_delivering_nothing_reports_stale` fails if a silent socket
becomes indistinguishable from a quiet session; and
`test_the_counter_resets_only_when_something_arrived` fails if a provider that
accepts and immediately drops would be retried at the floor delay forever.

`test_a_renamed_field_shows_up_as_missing_and_unmapped_together` is the one that
justifies the whole normalisation design. A provider renames a field, the reader
finds nothing there, and from then on every quote carries a null last price —
which downstream is indistinguishable from an instrument nobody is trading. The
test asserts that this state is *reported*, not that it cannot happen.

`TestExpiryIsNotGuessed` covers an off-by-one that changes a contract's identity,
its canonical key and its time to expiry. The interesting case is
`test_a_name_can_only_choose_between_the_two_candidates`: the contract name is
matched against renderings of the two candidate dates rather than parsed, so a
name format the code cannot read degrades to "could not resolve" instead of
producing a third date.

### Testing a surface against the market it was made from

`tests/integration/test_live_options.py` runs the whole Phase 2 path — capture,
implied volatilities, SVI calibration, delta skew — with one substitution: the
live quotes come from the seeded synthetic market rather than a feed.

That substitution is what makes the test worth having. The synthetic market
generates an arbitrage-clean chain from an admissible SVI slice with a **known
negative rho**, so the fitted 25-delta risk reversal must come back negative.
`test_the_skew_has_the_sign_the_generated_market_was_given` is therefore
checking a round trip through the IV solver, the calibrator and the delta solve
against a truth that was put in on purpose. A sign error anywhere along that
path would be invisible in every other test in the suite, because every other
number would still look like a plausible volatility.

`tests/unit/test_delta_skew.py` covers the solve itself, and its most useful
tests are the refusals. `test_a_delta_that_occurs_nowhere_is_reported_not_widened_into`
fails if the search range is ever widened to find a root, which would put a
strike far outside the traded market into a skew number.
`test_a_smile_whose_delta_turns_back_on_itself_is_refused` covers the case where
the wings break the no-arbitrage slope bound and a delta level occurs at more
than one strike — reported as unmeasurable rather than resolved by picking one
of them.

`tests/unit/test_open_interest.py` guards three arithmetic mistakes that all
produce numbers which plot perfectly well: summing absences as zeros, dividing
by a zero denominator, and reporting a change without its window.
`test_the_ratio_is_reported_without_being_interpreted` scans the whole response
body for the vocabulary a reading would use — the language policy enforced as a
test rather than as a review habit.

### Testing a loader that must not tidy up after itself

`tests/unit/test_warehouse_validation.py` is mostly about what is *still there*
after validation. `test_a_bad_tick_is_flagged_and_kept` asserts both halves —
flagged, and `rows_written == 60`. `test_the_split_is_reported_and_never_repaired`
asserts the post-split rows still hold the unadjusted prices the file gave. A
loader that quietly tidied its input would pass a test that only checked the
finding was raised.

Two of these tests were written wrong first and are worth recording. One asserted
that a robust z-score would catch a spike in a *constant* series; it does not,
because `MAD = 0` there by construction — which turned out to be a real blind
spot for a barely-moving series, and the fix was the documented mean-absolute-
deviation fallback. The other, in the integration file, asserted that a naive
timestamp is refused; it was not, because the shared CSV parser defaults naive
timestamps to UTC. That is right for an option chain and wrong for a year of
bars, and the reader now inspects the source text so the rule actually fires.
Both bugs were invisible until a test made a claim about behaviour rather than
about code.

`tests/unit/test_warehouse_storage.py` guards the partition layout, which is what
makes "queryable" a real property rather than a wrapper around a full scan.
`test_a_date_range_reads_only_the_days_it_needs` asserts `partitions_read`, which
counts files that actually contributed rows — so a broken prune fails a test
instead of merely being slow.

`tests/integration/test_warehouse.py` runs the acceptance path and then the
refusals: a quarantined dataset is not served, a symbol that resolves to nothing
is reported rather than attached to the nearest candidate, and a file missing a
required column fails *as a read* rather than registering an empty dataset that
would look exactly like a file with no rows in it.

### Testing that a backtest is not lying

Three ways a backtest reports a return nobody could have earned, and a family of
tests for each.

**It saw the future.** `tests/unit/test_features.py` asserts a *property*:
computing a feature over a truncated series must give the same value at its last
bar as computing it over the whole series. A feature that peeked would disagree,
and would disagree silently. It is parameterised over every feature shipped, so
adding one without the property is a failing test rather than a review comment.
`tests/unit/test_backtest.py::TestFillsCannotSeeTheFuture` covers the other half:
a decision on bar `t` is filled on bar `t+1`, and a signal on the final bar is
recorded rather than executed.

**It traded for free.** `test_no_schedule_means_gross_and_says_so` fails if the
absence of a cost model ever starts reading as zero cost rather than as a gross
run. The cost tests then pin the schedule's own behaviour — a sell-only levy not
charged on a buy, a cap that binds, a derived component seeing only the
components it applies to.

**Its accounting was wrong.** `TestTheAccountingBenchmark` is the most valuable
class in the phase. Buy-and-hold's return has to match the instrument's own
return over the window it was held, and the expected final equity is computed
independently — cash left plus quantity times the last close — rather than by
asking the engine twice. When that fails, the bug is in the engine, and no Sharpe
ratio would have found it. `TestAttribution::test_the_identity_closes` does the
same for the decomposition.

One test in this phase was written wrong and is worth recording:
`test_an_ema_weights_recent_bars_more` asserted that an EMA exceeds an SMA on a
rising series. On a *straight-line* trend the two have identical lag, so it
failed by a float hair. The property that actually holds is about recent
information — after a step change the EMA is nearer the new level — and the test
now says that instead.

### Testing an optimiser, where the inputs are the risk

`tests/unit/test_portfolio_optimisation.py` is mostly not about arithmetic. The
objectives are textbook; what can go wrong is the inputs, so most of the tests
are about what the optimiser declines to invent —
`test_a_return_seeking_objective_with_no_forecast_is_refused`,
`test_mean_variance_without_a_risk_aversion_is_refused`,
`test_black_litterman_without_tau_is_refused`, and
`test_a_turnover_limit_without_a_starting_point_is_refused`.

The arithmetic tests that do matter are the ones with an answer known in advance.
`test_it_beats_every_single_asset` checks that a minimum-variance portfolio is
below the lowest single-asset volatility — the claim of diversification, and a
check that the objective is being minimised rather than merely evaluated.
`test_every_asset_contributes_the_same_risk` checks risk parity against its own
definition. `test_with_no_views_the_posterior_is_the_prior` checks that
Black-Litterman with no views is the identity, which a sign error in the
precision-weighting would break.

`test_it_avoids_the_asset_variance_cannot_see` is the one that justifies having
CVaR at all: an asset is given a fat left tail with an ordinary variance, and the
CVaR optimiser avoids it while a mean-variance one would not. Without that
asymmetry in the fixture the test would pass whatever the objective did.

The infeasibility tests assert on the *message*, not just the exception.
"No solution" is a useless answer when six constraints are in play, and
`test_a_minimum_above_a_maximum_is_named_per_asset` fails if the diagnosis ever
stops naming the offending asset.

## 5. Regression / golden files

Committed fixtures with committed expected outputs:

```
tests/data/options_chain_clean.csv        -> expected_ingestion_clean.json
tests/data/options_chain_bad_quotes.csv   -> expected_ingestion_bad.json
tests/data/portfolio_options.csv          -> expected_risk.json         (P4/P5)
tests/data/trades.csv                     -> expected_tca.json          (P7)
                                          -> expected_iv.json           (P1, planned)
                                          -> expected_svi.json          (P2)
```

A drift beyond the declared tolerance fails CI. Regenerating a golden file is a
deliberate act: `python scripts/regen_golden.py --accept <name>` rewrites it and
the diff must be reviewed and justified in the PR, alongside a model version
bump if a formula changed.

The Phase 10 fixtures — `orderbook_snapshots.csv`, `orderbook_events.csv` and
`orderbook.parquet` — are deliberately **not** golden-filed. What would be
pinned is the output of a maximum-likelihood fit, and a golden file would then
fail on any change to scipy's optimiser for a reason that has nothing to do with
this platform being right. They are asserted against instead: parameter recovery
against the process that generated the tape, the likelihood against a quadrature
of its own intensity, and the vectorised recursion against the plain one.

## 6. Tolerances

Declared centrally in `tests/tolerances.py`, never inline magic numbers:

| Quantity | Tolerance |
| --- | --- |
| Option price vs analytic | 1e-10 relative |
| Option price vs reference library | 1e-8 absolute |
| Implied volatility round-trip | 1e-6 absolute, **or** 8x the solver's reported `uncertainty` where the problem is ill conditioned |
| Greeks vs finite difference | 1e-5 relative |
| PDE vs closed form | 5e-3 relative (grid-dependent, asserted with convergence order) |
| Monte Carlo vs closed form | 3 standard errors |
| Quality scores | 1e-9 |
| SVI parameter recovery (wide slice) | 1e-4 absolute |
| Fitted surface vs generating surface, in sample | 0.1 volatility points |
| Reference IV vs the market IV it was fitted to | 2e-3 |

## 7. Performance benchmarks

Marked `performance`, excluded from the default run. Targets tracked over time
rather than asserted as pass/fail, per build spec §90: 1 / 1e3 / 1e5 option
pricing and IV solve, SVI calibration per expiry, 10k-position portfolio
valuation, Monte Carlo throughput, large trade import, L2 analytics.

Optimization only follows a profile. Numba is not introduced speculatively.

## 8. CI

1. `ruff check` + `ruff format --check`
2. `mypy` on `quant/` and `domains/`
3. `python scripts/check_layering.py`
4. `pytest -m "not performance and not requires_reference_libs"`
5. `pytest -m "requires_reference_libs"` (with the validation extra installed)
6. `pytest -m regression`
7. Alembic: `upgrade head` then `downgrade base` on a scratch database

## 9. What the tests deliberately do not assert

No test asserts that the platform's reference value is "correct" in the sense of
predicting a market price. The reference value is a model output; tests assert it
is *computed correctly, reproducibly, and with honest uncertainty*, which is the
only claim the product makes.


## Trading (Phases 6 and 7)

`tests/unit/test_trading_oms.py` (46) and `tests/unit/test_live_trading.py` (31)
cover the parts that decide things: the order lifecycle's transition table, the
paper fill engine, the pre-trade gate, the Upstox payload mapping and the
schedule. All pure — no database, no event loop, no market — because the rules
that decide whether a fill is honest should be readable without one.

`tests/integration/test_paper_trading.py` (39) runs the acceptance path end to
end: an Upstox frame goes through the real normalisation into the live store, an
order is gated, filled against that quote, booked, and reaches the P&L. The only
substitution is that the frames are scripted rather than arriving on a socket.

Two structural tests are worth naming.

**`test_replaying_the_fills_reproduces_the_stored_position`** rebuilds the book
from the fill rows and compares quantity, average price and realised P&L against
the stored position. Positions are stored rather than replayed because a live
risk view cannot walk a year of fills per request; that optimisation is only safe
while the two agree, so the agreement is asserted rather than assumed.

**`test_the_database_refuses_an_order_that_filled_more_than_it_asked`** exists
because of a dialect bug found while building Phase 6. `DecimalType` is NUMERIC on
Postgres and TEXT elsewhere, so a CHECK written as `filled_quantity <= quantity`
compares *strings* on SQLite — where `'4' <= '10'` is false and `'40' <= '10'` is
true. The constraint would have rejected the honest case and admitted the
impossible one. Every decimal comparison in the trading tables now casts to
NUMERIC, and this test asserts the constraint bites on the dialect where it would
otherwise silently invert.

No test in this repository can reach a live broker. The order transport is a
protocol with a recorded-payload implementation in tests, and the Upstox adapter
additionally refuses to place an order at all unless
`verified_against_documentation` is set — which nothing in this repository sets.

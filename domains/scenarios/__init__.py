"""Scenario and shock definitions, and derivation of scenarios from real data.

Owns the shock vocabulary the risk domain applies to a portfolio. The rule that
shapes it: a scenario labelled ``DERIVED_FROM_HISTORY`` must carry the series,
date range and event date it was computed from, enforced by the model and by a
database CHECK. Shipped templates are labelled ``HYPOTHETICAL`` and none is named
after a real market event.

See the "Scenarios and stress" section of docs/risk.md.
"""

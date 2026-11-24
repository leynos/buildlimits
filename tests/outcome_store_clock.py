"""Injected wall-clock constant for outcome-store tests."""

import datetime as dt

# Writers are given this "now": a day after the fixed observation dates, so
# retention never depends on the real clock.
FIXED_NOW = dt.datetime(2026, 10, 26, tzinfo=dt.UTC)

"""Real-manager acceptance for cache-outcome child sightings."""

import pytest
from pytest_bdd import scenarios
from systemd_harness import systemd_user_marks

pytest_plugins = [
    "cache_outcome_systemd_support",
    "steps.cache_outcome_common_systemd_steps",
    "steps.cache_outcome_sighting_systemd_steps",
]

pytestmark = [
    *systemd_user_marks("needs a systemd user manager and busctl"),
    pytest.mark.timeout(180),
]

scenarios("features/cache_outcome_sightings_on_systemd.feature")

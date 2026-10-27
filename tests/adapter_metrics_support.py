"""Build expected adapter metric summaries for generated history tests."""

from buildlimits.metrics import MAX_COUNTER


def expected_record(
    history: list[str], initial_attempts: int, step: int
) -> dict[str, str]:
    """Derive a summary from the immutable prefix ending at the first exit.

    Parameters
    ----------
    history : list[str]
        Adapter metric actions followed by a terminal action.
    initial_attempts : int
        Attempt count before the generated history.
    step : int
        Clock increment between generated actions.

    Returns
    -------
    dict[str, str]
        Expected adapter event fields through the first terminal action.
    """
    terminal = next(
        index
        for index, action in enumerate(history)
        if action in {"finish", "interrupt"}
    )
    prefix = history[:terminal]
    admissions = [index for index, action in enumerate(prefix) if action == "admit"]
    wait = (admissions[0] + 1) * step if admissions else (terminal + 1) * step
    runtime = (terminal - admissions[0]) * step if admissions else 0
    return {
        "BUILD_LIMITS_SCHEMA": "1",
        "BUILD_LIMITS_SOURCE": "adapter",
        "BUILD_LIMITS_EVENT": "adapter_finished",
        "BUILD_LIMITS_OUTCOME": "interrupted"
        if history[terminal] == "interrupt"
        else "completed",
        "BUILD_LIMITS_CONNECTION_ATTEMPTS": str(
            min(MAX_COUNTER, initial_attempts + prefix.count("attempt"))
        ),
        "BUILD_LIMITS_BUSY_RETRIES": "0",
        "BUILD_LIMITS_RESTART_RETRIES": "0",
        "BUILD_LIMITS_WAIT_SECONDS": f"{wait:.3f}",
        "BUILD_LIMITS_RUNTIME_SECONDS": f"{runtime:.3f}",
    }

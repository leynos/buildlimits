"""Unhealthy shutdown after an unreleased compiler launch fails."""

from buildlimits.admission import Admission, Refuse, ReturnToken, Shutdown


def test_failed_final_launch_allows_unhealthy_shutdown() -> None:
    """A safely abandoned child returns its token and permits drained shutdown."""
    admission = Admission()
    admission.submit(1)
    admission.token_arrived(b"a")
    admission.health_failed()
    effects = admission.launch_failed("g1")
    assert effects == [ReturnToken(b"a"), Refuse(1, "launch-failed"), Shutdown()], (
        "the final failed launch must not strand a drained unhealthy supervisor"
    )
    assert admission.tokens_held() == 0, "the token returns exactly once"

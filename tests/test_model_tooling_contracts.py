"""The model tooling's two boundaries: the installer and the workflows.

``make fizzbee`` is the only networked step in the model-checking workflow, and
the two model workflows are the only place the model targets run unattended.
Both are shell and YAML rather than Python, so nothing else in the suite would
notice a changed URL, a dropped digest check, a writable token, or a workflow
that stopped calling its target.

Every test here is network-free: the installer runs the real recipe against an
archive built in the test's own directory, with the release URL, digest and
cache root overridden, and the workflows are parsed as data.
"""

import hashlib
import io
import subprocess
import tarfile
import typing as typ
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
MAKEFILE = ROOT / "Makefile"
WORKFLOWS = ROOT / ".github" / "workflows"

_ASSET = "fizzbee-v0.0.0-test-linux_x86.tar.gz"
"""The fixture release's asset name.

The recipe unpacks ``<asset without .tar.gz>`` out of the archive, so the
fixture's top-level directory must carry exactly this name.
"""

_ROOT = _ASSET.removesuffix(".tar.gz")
"""The directory the fixture archive must contain."""

_DIGEST_MISMATCH = "do not update FIZZBEE_SHA256 without review"
"""The recipe's refusal when the download does not match the pin."""

_UNSAFE_MEMBERS = "contains unsafe member paths"
"""The recipe's refusal when the archive would escape its unpack directory."""


def _archive(directory: Path, members: dict[str, str]) -> Path:
    """Write a gzipped tar archive holding the given members.

    Parameters
    ----------
    directory : pathlib.Path
        Where to write the archive.
    members : dict[str, str]
        Member name to its text. Names are written verbatim, so a test can
        build an archive that tries to escape its unpack directory.

    Returns
    -------
    pathlib.Path
        The archive, named :data:`_ASSET`.
    """
    path = directory / _ASSET
    with tarfile.open(path, "w:gz") as archive:
        for name, text in members.items():
            payload = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mode = 0o755
            archive.addfile(info, io.BytesIO(payload))
    return path


def _digest(path: Path) -> str:
    """Return the SHA-256 digest the recipe will check the archive against.

    Parameters
    ----------
    path : pathlib.Path
        The archive to digest.

    Returns
    -------
    str
        The lowercase hexadecimal digest.
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _home(directory: Path, digest: str) -> Path:
    """Return a cache root the recipe accepts for one digest.

    Parameters
    ----------
    directory : pathlib.Path
        The test's own directory, which stands in for the user's cache.
    digest : str
        The digest the cache path must end with.

    Returns
    -------
    pathlib.Path
        A path ending in ``/buildlimits/fizzbee/<digest>``, which is the shape
        the recipe insists on.
    """
    return directory / "cache" / "buildlimits" / "fizzbee" / digest


_PAYLOAD = "#!/bin/sh\nexit 0\n"
"""The one file the fixture archive carries, standing in for the checker."""


def _release(
    directory: Path, *, members: dict[str, str] | None = None
) -> tuple[Path, str, Path]:
    """Build the fixture release and the cache root its digest names.

    Every test starts from the same release: a lone ``fizz`` launcher whose
    digest the recipe is told to expect, in a cache named for that digest. The
    helper keeps the two from drifting apart within a test.

    Parameters
    ----------
    directory : pathlib.Path
        The test's own directory.
    members : dict[str, str] | None
        The archive's members, or ``None`` for the lone launcher. A test that
        checks the member guard names the extra member itself.

    Returns
    -------
    tuple[pathlib.Path, str, pathlib.Path]
        The archive, its digest, and the cache root that digest names.
    """
    archive = _archive(directory, members or {f"{_ROOT}/fizz": _PAYLOAD})
    digest = _digest(archive)
    return archive, digest, _home(directory, digest)


def _install(
    directory: Path, archive: Path, *, digest: str, home: Path
) -> subprocess.CompletedProcess[str]:
    """Run ``make fizzbee`` against a local archive and an isolated cache.

    Parameters
    ----------
    directory : pathlib.Path
        The working directory, which receives ``.tools/fizzbee``.
    archive : pathlib.Path
        The archive the overridden release URL points at.
    digest : str
        The digest the recipe is told to expect.
    home : pathlib.Path
        The cache root the recipe is told to unpack into.

    Returns
    -------
    subprocess.CompletedProcess[str]
        The finished run, with its output captured for assertion.
    """
    return subprocess.run(
        [
            "make",
            "--no-print-directory",
            "-f",
            str(MAKEFILE),
            "fizzbee",
            f"FIZZBEE_ASSET={_ASSET}",
            f"FIZZBEE_URL={archive.as_uri()}",
            f"FIZZBEE_SHA256={digest}",
            f"FIZZBEE_HOME={home}",
        ],
        cwd=directory,
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )


def test_a_verified_archive_is_unpacked_and_linked(tmp_path: Path) -> None:
    """A matching digest installs the release and points ``.tools`` at it.

    The three outcomes are one promise: the checker is at the path every other
    target uses. An unpack without the link leaves ``make model-check`` unable
    to find ``fizz``, and a link without the stamp would re-download.
    """
    archive, digest, home = _release(tmp_path)

    result = _install(tmp_path, archive, digest=digest, home=home)

    assert result.returncode == 0, result.stderr
    assert (home / "fizz").read_text(encoding="utf-8") == _PAYLOAD, (
        "the archive's contents are unpacked into the cache"
    )
    assert (home / f".verified-{_ASSET}").read_text(
        encoding="utf-8"
    ).strip() == digest, "the stamp records the digest that was verified"
    link = tmp_path / ".tools" / "fizzbee"
    assert link.is_symlink(), "the target links the cache into .tools"
    assert link.resolve() == home.resolve(), "and the link names the verified cache"
    assert not Path(f"{home}.tar.gz").exists(), "the download is not kept"


def test_a_second_run_reuses_the_verified_cache(tmp_path: Path) -> None:
    """A verified cache is reused, so a second run needs no download.

    The archive is deleted before the second run, so a recipe that fetched again
    would fail rather than quietly succeed: this tests reuse, not the network.
    """
    archive, digest, home = _release(tmp_path)
    _install(tmp_path, archive, digest=digest, home=home)
    archive.unlink()

    result = _install(tmp_path, archive, digest=digest, home=home)

    assert result.returncode == 0, result.stderr
    assert "Fetching" not in result.stdout, "the verified cache is not refetched"


def test_a_digest_mismatch_is_refused_and_leaves_no_cache(tmp_path: Path) -> None:
    """A download that does not match the pin is refused, not unpacked.

    The refusal is the point of pinning: a release that changed under the same
    name must stop the run rather than install whatever arrived. The partial
    download goes too, so a later run cannot mistake it for a verified one.
    """
    archive, _, _ = _release(tmp_path)
    wrong = "0" * 64
    home = _home(tmp_path, wrong)

    result = _install(tmp_path, archive, digest=wrong, home=home)

    assert result.returncode != 0, "a mismatched digest fails the target"
    assert _DIGEST_MISMATCH in result.stderr, "and says the pin must not be edited"
    assert not home.exists(), "nothing is unpacked"
    assert not Path(f"{home}.tar.gz").exists(), "and the download is removed"


def test_an_archive_with_an_escaping_member_is_refused(tmp_path: Path) -> None:
    """An archive that would write outside its unpack directory is refused.

    The digest proves the archive is the reviewed one, not that it is safe to
    extract, so the member list is checked first. The escaping member is what a
    hostile release would contain.
    """
    archive, digest, home = _release(
        tmp_path, members={f"{_ROOT}/fizz": _PAYLOAD, "../escape": "x"}
    )

    result = _install(tmp_path, archive, digest=digest, home=home)

    assert result.returncode != 0, "an escaping member fails the target"
    assert _UNSAFE_MEMBERS in result.stderr, "and says why"
    assert not home.exists(), "nothing is unpacked"
    assert not (home.parent / "escape").exists(), "and nothing escaped"


def test_a_cache_root_outside_the_pinned_prefix_is_refused(tmp_path: Path) -> None:
    """A cache root that does not carry the digest is refused.

    The path is what makes a cache entry self-describing: a root without the
    digest could hold a release the stamp never verified. The recipe checks the
    shape before it downloads anything.
    """
    archive, digest, _ = _release(tmp_path)
    home = tmp_path / "cache" / "fizzbee" / digest

    result = _install(tmp_path, archive, digest=digest, home=home)

    assert result.returncode != 0, "a cache root without the digest is refused"
    assert "FIZZBEE_HOME must end in /buildlimits/fizzbee/" in result.stderr, (
        "and the refusal names the required shape"
    )


class _Workflow(typ.NamedTuple):
    """What one model workflow must declare.

    Attributes
    ----------
    file : str
        The workflow's file name under ``.github/workflows``.
    name : str
        The workflow's display name.
    triggers : frozenset[str]
        The events that start it.
    group : str
        The concurrency group, which is what serializes runs per ref.
    cancel_in_progress : bool
        Whether a newer run replaces an older one.
    timeout_minutes : int
        The job's time limit.
    target : str
        The Make target the job runs.
    """

    file: str
    name: str
    triggers: frozenset[str]
    group: str
    cancel_in_progress: bool
    timeout_minutes: int
    target: str


_EXPECTED = (
    _Workflow(
        file="model-fidelity.yml",
        name="W1 model fidelity",
        triggers=frozenset({"pull_request", "workflow_dispatch"}),
        group="model-fidelity-${{ github.ref }}",
        cancel_in_progress=True,
        timeout_minutes=10,
        target="make model-fidelity",
    ),
    _Workflow(
        file="model-check-nightly.yml",
        name="W1 model check",
        triggers=frozenset({"schedule", "workflow_dispatch"}),
        group="model-check-${{ github.ref }}",
        cancel_in_progress=False,
        timeout_minutes=60,
        target="make model-check",
    ),
)
"""The contract each model workflow must keep.

The split is the plan's: fidelity is cheap enough to gate every pull request,
and the matrix is not, so the two must not drift into each other's trigger.
"""


def _triggers(workflow: dict[object, object]) -> object:
    """Return a workflow's trigger mapping.

    Parameters
    ----------
    workflow : dict[object, object]
        The parsed workflow.

    Returns
    -------
    object
        The value under ``on``. YAML 1.1 reads an unquoted ``on`` as the
        boolean ``True``, so both spellings are accepted rather than requiring
        the file to quote a key GitHub itself writes unquoted.
    """
    return workflow.get("on", workflow.get(True))


@pytest.mark.parametrize("expected", _EXPECTED, ids=lambda case: case.file)
def test_a_model_workflow_declares_its_contract(expected: _Workflow) -> None:
    """Each model workflow keeps the trigger, permissions and target it needs.

    These settings decide whether the obligation is enforced at all: a gained
    ``pull_request`` would spend the matrix on every push, a lost
    ``contents: read`` would run with a writable token, and a step that stopped
    naming its target would pass without checking anything.
    """
    workflow = yaml.safe_load((WORKFLOWS / expected.file).read_text(encoding="utf-8"))
    assert isinstance(workflow, dict), "a workflow has a mapping root"
    assert workflow["name"] == expected.name, "the workflow is the one expected"

    triggers = _triggers(workflow)
    assert isinstance(triggers, dict), "the workflow declares its triggers"
    assert set(triggers) == expected.triggers, "and only the events it should"

    assert workflow["permissions"] == {"contents": "read"}, (
        "the workflow reads the repository and writes nothing"
    )

    concurrency = workflow["concurrency"]
    assert isinstance(concurrency, dict), "the workflow declares concurrency"
    assert concurrency["group"] == expected.group, "runs are grouped per ref"
    assert concurrency["cancel-in-progress"] is expected.cancel_in_progress, (
        "and a newer run replaces an older one only where that is safe"
    )

    jobs = workflow["jobs"]
    assert isinstance(jobs, dict) and len(jobs) == 1, "the workflow has one job"
    job = next(iter(jobs.values()))
    assert isinstance(job, dict), "the job is a mapping"
    assert job["timeout-minutes"] == expected.timeout_minutes, (
        "the job is bounded by the plan's budget"
    )

    steps = job["steps"]
    assert isinstance(steps, list), "the job declares ordered steps"
    checkouts = [
        step
        for step in steps
        if isinstance(step, dict)
        and str(step.get("uses", "")).startswith("actions/checkout@")
    ]
    assert len(checkouts) == 1, "the job checks the repository out once"
    assert checkouts[0]["with"]["persist-credentials"] is False, (
        "and leaves no token in the checkout"
    )
    assert expected.target in [step.get("run") for step in steps], (
        "the job runs the target the plan names"
    )

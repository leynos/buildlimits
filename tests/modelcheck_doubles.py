"""Doubles the ``modelcheck`` behavioural and CLI tests run against.

``FakeChecker`` is deliberately not a stub of the checker's *result*. It is a
stub of the checker's *output*: it holds a captured transcript and its exit
status, and hands them to the real ``parse_output``. The scenarios therefore
exercise the real parser against real recorded output, and a scenario that says
a lasso produces a counterexample is testing the translation the tooling will
really perform — not a translation the double performed for it.

That distinction is the whole reason this module exists rather than a dict of
pre-classified results. A double returning ``Counterexample(...)`` directly
would let a broken parser pass every scenario, because the parser would never
run.

``Verbatim`` and ``Restated`` exist for the two things no recording can supply:
an output shape the capture never produced, and a state count a scenario has to
state to make its floor the thing under test.

This module is test support and is linted as production code, following the
convention in ``docs/repository-layout.md``: ``test_*.py`` modules hold tests,
anything else here holds helpers.
"""

import typing as typ
from pathlib import Path

from modelcheck.adapters import fizzbee_output
from modelcheck.ports import ConfigurationSource

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from modelcheck.domain import CheckResult, Configuration, MaterializedSpec
    from modelcheck.ports import ModelChecker

FIXTURES = Path(__file__).with_name("fixtures") / "fizzbee"
"""Where the recorded checker runs live."""

NEVER_REACHED = (
    "Valid Nodes: 4 Unique states: 4\n"
    "\nFAILED: Expected states never reached\n"
    "Invariant 0: {probe}\n"
)
"""The lines the checker prints when an ``exists`` probe was never satisfied.

``main.go:867`` prints the marker, names each unsatisfied assertion on its own
``Invariant N: <name>`` line, and returns without the pass line. No capture
carries this shape, because no EP-M1 spike model had an ``exists`` assertion
that went unsatisfied; the lines are transcribed from the checker's source
rather than guessed, which is what makes a synthetic transcript here faithful
where a synthetic truncation marker would not be (D22).
"""


def captured(name: str) -> tuple[str, int, str | None]:
    """Return one recording's output, exit status and error graph.

    Parameters
    ----------
    name : str
        The fixture directory to read.

    Returns
    -------
    tuple[str, int, str | None]
        The recorded output, the recorded exit status, and the error graph when
        the run wrote one.

    Raises
    ------
    FileNotFoundError
        If the named class was never recorded, which is a typo rather than an
        empty fixture. ``read_text`` raises it for the missing fixture files.
    """  # ruff: ignore[docstring-extraneous-exception] - read_text raises it.
    directory = FIXTURES / name
    exit_text = (directory / "exit-code.txt").read_text(encoding="utf-8")
    graph = directory / "error-graph.json"
    return (
        (directory / "stdout.txt").read_text(encoding="utf-8"),
        int(exit_text.split("=")[1]),
        graph.read_text(encoding="utf-8") if graph.exists() else None,
    )


def class_of(result: object) -> str:
    """Return the domain class name of a classified result.

    Parameters
    ----------
    result : object
        A classified checker result.

    Returns
    -------
    str
        The class name, which is the variant's name in the domain union. Used
        in preference to ``isinstance`` chains so a new variant is reported by
        name rather than silently failing every branch.

    Examples
    --------
    >>> from modelcheck import domain
    >>> class_of(domain.Errored("no verdict"))
    'Errored'
    """
    return type(result).__name__


class FakeChecker:
    """A checker double that replays one recorded output class.

    Each materialized specification is answered with the same recording, which
    is what lets one scenario state "the checker output is the fixture
    ``lasso``" without a directory of files per scenario. The specifications
    seen are kept, so a scenario can check that the run it expected was the one
    the checker was given.

    Attributes
    ----------
    seen : list[str]
        Each specification text the checker was asked to run, in order.
    """

    def __init__(
        self, output_class: str = "pass", *, property_name: str | None = None
    ) -> None:
        """Record the output class every run will replay.

        Parameters
        ----------
        output_class : str
            The ``tests/fixtures/fizzbee/`` class to answer with.
        property_name : str | None
            The assertion the scenario expects the recording to be about, or
            ``None`` to quote the recording's own name.

        Notes
        -----
        The name is restated because a recording is a *shape*, captured from a
        different model with different assertions. A known-bad configuration
        keeps only its own assertions (D3), so a real run of it could not print
        another model's invariant name; replaying the capture unchanged would
        make every such scenario fail for the fixture's reason rather than the
        scenario's. Substituting the name the scenario declared is what lets
        the recording stand in for a run of the scenario's own model, and it is
        the only field replaced: the trace, the kind and the state count are
        the capture's, so a broken parser still fails the scenario.
        """
        self._captured = captured(output_class)
        self._property_name = property_name
        self.seen: list[str] = []

    def check(self, spec: MaterializedSpec) -> CheckResult:
        """Classify the recorded output as the real parser would.

        Parameters
        ----------
        spec : MaterializedSpec
            The specification this run would have checked. Only its text is
            kept, because the double answers with a recording.

        Returns
        -------
        CheckResult
            The parsed result, with its counterexample named after the scenario's
            property when the scenario declared one. A deadlock is left as it
            was: it names no assertion, and naming one would report a violation
            of a property the checker never mentioned.
        """
        self.seen.append(spec.text)
        return self._restated(fizzbee_output.parse_output(*self._captured))

    def _restated(self, result: CheckResult) -> CheckResult:
        """Return the result named after the scenario's property.

        Parameters
        ----------
        result : CheckResult
            The classified recording.

        Returns
        -------
        CheckResult
            The result unchanged unless it is a counterexample that names an
            assertion, in which case that assertion is the scenario's.
        """
        import dataclasses as dc

        from modelcheck import domain

        names_an_assertion = (
            isinstance(result, domain.Counterexample) and result.property_name
        )
        if self._property_name is None or not names_an_assertion:
            return result
        return dc.replace(result, property_name=self._property_name)


class Verbatim:
    """A checker that answers with one fixed transcript.

    Used by the scenarios whose output no capture carries, so the real parser
    still does the classifying and only the transcript is invented.
    """

    def __init__(self, stdout: str) -> None:
        """Hold the transcript to answer with.

        Parameters
        ----------
        stdout : str
            The checker output to classify.
        """
        self._stdout = stdout

    def check(self, spec: MaterializedSpec) -> CheckResult:
        """Classify the held transcript.

        Parameters
        ----------
        spec : MaterializedSpec
            The specification this run would have checked, which the transcript
            answers for regardless.

        Returns
        -------
        CheckResult
            The parsed result.
        """
        del spec
        return fizzbee_output.parse_output(self._stdout, 0, None)


class Restated:
    """A checker that restates the state count a scenario requires.

    A recorded pass reports its own state count, so a floor scenario cannot use
    it unchanged: the recorded ``pass`` class explored four states, which would
    sit below a floor of 1000 for the fixture's reason rather than the
    scenario's. This double parses the recording with the real parser and then
    replaces only the count, which is the single field the scenario is about.
    """

    def __init__(self, inner: ModelChecker, states: int) -> None:
        """Hold the double to wrap and the count to report.

        Parameters
        ----------
        inner : ModelChecker
            The double whose parsed result supplies everything but the count.
        states : int
            The state count the scenario states.
        """
        self._inner = inner
        self._states = states

    def check(self, spec: MaterializedSpec) -> CheckResult:
        """Classify, then restate the count.

        Parameters
        ----------
        spec : MaterializedSpec
            The specification this run would have checked.

        Returns
        -------
        CheckResult
            The result with its state count restated, or the result unchanged
            when it carries no count.
        """
        import dataclasses as dc

        from modelcheck import domain

        result = self._inner.check(spec)
        if isinstance(result, (domain.Passed, domain.Counterexample)):
            return dc.replace(result, states=self._states)
        return result


class FixedSource(ConfigurationSource):
    """A configuration source holding values the scenario supplied.

    The scenarios are about verdicts rather than about parsing TOML, so the
    source is a value here. ``TomlConfigurations`` has its own tests.

    It subclasses :class:`~modelcheck.ports.ConfigurationSource` so each
    method is an override of a declared port method. Forwarding to a held
    value is the whole point of a double, and the base class is what says the
    indirection is deliberate rather than incidental.
    """

    def __init__(
        self,
        configurations: cabc.Sequence[Configuration],
        *,
        source: str = "",
    ) -> None:
        """Hold the configurations and the source text.

        Parameters
        ----------
        configurations : Sequence[Configuration]
            The configurations the matrix runs, in report order.
        source : str
            The concatenated model source.
        """
        self._source = source
        self._configurations = tuple(configurations)

    @typ.override
    def source_text(self) -> str:
        """Return the concatenated model source.

        Returns
        -------
        str
            The text the drift check reads its mappings from.
        """
        return self._source

    @typ.override
    def configurations(self) -> tuple[Configuration, ...]:
        """Return every configuration with its expectations.

        Returns
        -------
        tuple[Configuration, ...]
            The configurations, in the order they were supplied.
        """
        return self._configurations

    @typ.override
    def exclusions(self) -> cabc.Mapping[str, str]:
        """Return inventory items deliberately unmapped, each with its reason.

        Returns
        -------
        Mapping[str, str]
            No exclusions: a scenario that needs one supplies it through the
            drift values directly.
        """
        return {}

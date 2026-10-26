"""What a configuration expects, and what a checker run means.

The checker answers one question per run: did this specification, with these
constants and this frontmatter, pass, fail with a counterexample, stop early or
fail to compile. The domain names those four outcomes exactly as the checker
can report them, names what a configuration expects to happen, and gives the
pair a verdict. Nothing here reads a file, spawns a process or knows the
checker's text format; the adapter that does that translates into these types.

The expectation types are deliberately narrow. Each names the one assertion or
probe it is about, so a judgement can only be about something the model
actually declares. A configuration whose expectation list is empty is a
configuration defect: no run can satisfy it, and its report line would be
indistinguishable from a pass.

Examples
--------
>>> Verdict.HOLDS in {Verdict.HOLDS, Verdict.AS_EXPECTED, Verdict.REACHED}
True
>>> Judgement("head", Holds("L3"), Passed(1, 0.1, ()), Verdict.HOLDS, ()).is_met
True
"""

# ``dataclasses`` evaluates each field's annotation when it builds the class
# (PEP 649 defers them to that point), so the alias has to exist at runtime.
# This suppression is permanent; moving the import under ``TYPE_CHECKING``
# raises ``NameError`` from the class body.
import collections.abc as cabc  # ruff: ignore[typing-only-standard-library-import]
import dataclasses as dc
import enum
import typing as typ

if typ.TYPE_CHECKING:
    from modelcheck.trace import Trace


class CounterexampleKind(enum.StrEnum):
    """The shape of a counterexample the checker reports.

    Attributes
    ----------
    FINITE : str
        A safety violation: a finite path to a forbidden state.
    LASSO : str
        A liveness violation: a prefix followed by a cycle taken for ever.
    STUTTER : str
        A liveness violation: a path that ends in a state where nothing more
        happens.
    DEADLOCK : str
        A run where no action is enabled before any expectation settles.
    """

    FINITE = "finite"
    LASSO = "lasso"
    STUTTER = "stutter"
    DEADLOCK = "deadlock"


@dc.dataclass(frozen=True, slots=True)
class Passed:
    """A run that checked every retained assertion without a violation.

    Attributes
    ----------
    states : int
        Distinct states explored.
    elapsed_seconds : float
        Wall-clock duration the checker reported.
    unsatisfied_probes : tuple[str, ...]
        ``exists`` assertions the run did not reach, in checker order. A run
        with any of these is not a plain pass: reachability expectations about
        them are unmet.
    """

    states: int
    elapsed_seconds: float
    unsatisfied_probes: tuple[str, ...]


@dc.dataclass(frozen=True, slots=True)
class Counterexample:
    """A run that violated one property.

    Attributes
    ----------
    property_name : str
        The assertion or probe the checker named.
    kind : CounterexampleKind
        The shape of the path that reached it.
    trace : Trace
        The normalized path, with its cycle or terminal state marked.
    states : int
        Distinct states explored before the violation was found.
    elapsed_seconds : float
        Wall-clock duration the checker reported.
    """

    property_name: str
    kind: CounterexampleKind
    trace: Trace
    states: int
    elapsed_seconds: float


@dc.dataclass(frozen=True, slots=True)
class Stopped:
    """A run the checker cut short, so its verdict proves nothing.

    The checker reports this when a signal interrupts it: exploration ends
    early, and it says so rather than claiming a verdict. A run cut short by
    its ``max_actions`` bound is *not* one of these — the checker prints no
    marker for that, so bound truncation is caught by the configuration's
    ``min_states`` floor instead (D22).

    Attributes
    ----------
    detail : str
        The line the interruption was read from.
    """

    detail: str


@dc.dataclass(frozen=True, slots=True)
class Errored:
    """A run whose output could not be interpreted, or that did not compile.

    Attributes
    ----------
    detail : str
        The line, exit status or parse diagnostic the error was read from.
    """

    detail: str


type CheckResult = Passed | Counterexample | Stopped | Errored
"""The four outcomes a checker run can have."""


@dc.dataclass(frozen=True, slots=True)
class Holds:
    """An expectation that a property holds in every reachable state.

    Attributes
    ----------
    property_name : str
        The assertion the model names.
    """

    property_name: str


@dc.dataclass(frozen=True, slots=True)
class FailsAs:
    """An expectation that a known-bad configuration still finds its defect.

    Attributes
    ----------
    property_name : str
        The assertion expected to be violated.
    kind : CounterexampleKind
        The kind of counterexample the defect must produce.
    """

    property_name: str
    kind: CounterexampleKind


@dc.dataclass(frozen=True, slots=True)
class Reachable:
    """An expectation that an ``exists`` probe is satisfied.

    Attributes
    ----------
    probe_name : str
        The probe the model names.
    """

    probe_name: str


type Expectation = Holds | FailsAs | Reachable
"""The three things a configuration can expect of a run."""


@dc.dataclass(frozen=True, slots=True)
class Configuration:
    """One materialized variant of the model and what it expects.

    Attributes
    ----------
    name : str
        The configuration's name, used in every report line.
    constants : collections.abc.Mapping[str, str]
        Top-level model constants to substitute, as source text.
    fairness : collections.abc.Mapping[str, str]
        Fairness tag to level, where the level is ``weak``, ``strong`` or
        ``unfair``. A tag the model does not carry is an error rather than a
        silent no-op.
    options : collections.abc.Mapping[str, object]
        The complete YAML frontmatter, so a variant never inherits a bound by
        accident.
    min_states : int
        The floor below which exploration is too small to prove anything.
    expectations : tuple[Expectation, ...]
        What this configuration expects. Empty is a configuration defect,
        reported by the materializer rather than passed over.
    """

    name: str
    constants: cabc.Mapping[str, str]
    fairness: cabc.Mapping[str, str]
    options: cabc.Mapping[str, object]
    min_states: int
    expectations: tuple[Expectation, ...]


@dc.dataclass(frozen=True, slots=True)
class MaterializedSpec:
    """The exact text one run compiles, and the fairness it carries.

    Attributes
    ----------
    configuration : str
        The configuration name this text was produced for.
    text : str
        The specification to compile.
    fairness_in_force : tuple[str, ...]
        Every fairness tag whose keyword was left on, sorted and deduplicated.
        A report that quoted anything else would drift from the model.
    """

    configuration: str
    text: str
    fairness_in_force: tuple[str, ...]


class MaterializeError(Exception):
    """A configuration cannot be applied to the model it was written for.

    It lives here rather than beside :func:`modelcheck.materialize.materialize`
    because both the source rewriter and the frontmatter renderer raise it, and
    a module that borrowed the other's exception to break the cycle would say
    the wrong thing about where the defect is. It is a domain defect: the
    configuration and the model have drifted apart, and no run of this variant
    would check what its report claims.
    """


class Verdict(enum.StrEnum):
    """What one expectation and one result mean together.

    Attributes
    ----------
    HOLDS : str
        A ``Holds`` expectation met by a pass.
    AS_EXPECTED : str
        A ``FailsAs`` expectation met by the named counterexample.
    REACHED : str
        A ``Reachable`` expectation met by a satisfied probe.
    MISSING : str
        A ``FailsAs`` expectation that the run passed, so the defect is gone.
    WRONG_KIND : str
        The expected property failed, but with another kind of path.
    UNEXPECTED : str
        A ``Holds`` or ``Reachable`` expectation the run violated.
    UNREACHABLE : str
        A ``Reachable`` probe the run reported unsatisfied.
    BELOW_FLOOR : str
        Exploration below the configuration's floor, so a pass proves nothing.
    STOPPED : str
        The run was truncated by a bound.
    ERROR : str
        The run could not be interpreted.
    """

    HOLDS = "holds"
    AS_EXPECTED = "counterexample as expected"
    REACHED = "reached"
    MISSING = "expected counterexample missing"
    WRONG_KIND = "wrong counterexample kind"
    UNEXPECTED = "unexpected counterexample"
    UNREACHABLE = "unreachable"
    BELOW_FLOOR = "state space below floor"
    STOPPED = "stopped"
    ERROR = "error"


_MET = frozenset({Verdict.HOLDS, Verdict.AS_EXPECTED, Verdict.REACHED})
"""The verdicts that satisfy their expectation; every other one is unmet."""


@dc.dataclass(frozen=True, slots=True)
class Judgement:
    """One expectation judged against one result.

    Attributes
    ----------
    configuration : str
        The configuration's name.
    expectation : Expectation
        What was expected.
    result : CheckResult
        What the run produced.
    verdict : Verdict
        What the pair means.
    fairness_in_force : tuple[str, ...]
        The fairness tags the configuration left on, so a report line never
        quotes an assumption the run did not make.
    """

    configuration: str
    expectation: Expectation
    result: CheckResult
    verdict: Verdict
    fairness_in_force: tuple[str, ...]

    @property
    def is_met(self) -> bool:
        """Report whether the verdict satisfies the expectation.

        Returns
        -------
        bool
            Whether the verdict is one of ``HOLDS``, ``AS_EXPECTED`` or
            ``REACHED``.

        Examples
        --------
        >>> Judgement("c", Holds("p"), Stopped("bound"), Verdict.STOPPED, ()).is_met
        False
        """
        return self.verdict in _MET

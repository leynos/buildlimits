"""The optional collaborators an enabled outcome shell accepts.

A shell built by ``build_shell`` takes every port it owns from the composition
root. A test that wants to drive one resource boundary — the store writer, the
statistics child, the server probe or the wall clock — supplies that one and
leaves the rest to their defaults. Naming the seams here keeps that keyword set
beside the shell it configures rather than inside its constructor.
"""

import dataclasses as dc
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt

    from buildlimits.nonblocking_child import ChildReaper
    from buildlimits.outcome_ports import (
        OutcomeStorePort,
        ServerProcessPort,
        StatisticsPort,
    )


@dc.dataclass(slots=True)
class OutcomeDependencies:
    """Optional collaborators used to test the shell's resource boundaries.

    Attributes
    ----------
    environment : Mapping[str, str] | None
        Process environment the store path and statistics child read.
    writer : OutcomeStorePort | None
        Store writer, or the one the shell builds from the settings.
    statistics : StatisticsPort | None
        Statistics child, or the managed server's own.
    server : ServerProcessPort | None
        Managed-server probe, or the one the shell builds.
    reaper : ChildReaper | None
        Helper reaper, or a fresh one owned by the shell.
    wall_clock : Callable[[], datetime] | None
        Wall clock that stamps every event the shell emits, or the system UTC
        clock. The recorder reads it for a reading's label time, so a test can
        prove which moment a row carries.
    """

    environment: cabc.Mapping[str, str] | None = None
    writer: OutcomeStorePort | None = None
    statistics: StatisticsPort | None = None
    server: ServerProcessPort | None = None
    reaper: ChildReaper | None = None
    wall_clock: cabc.Callable[[], dt.datetime] | None = None

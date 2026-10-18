"""The scripted model the recovery search drives and reads.

The recovery property search in :mod:`test_cache_recovery_properties` judges a
running transaction through one hand-kept reading of the gauges, and that
reading is a model rather than production state: it exists so a rule can put
the cache into states the real admission path cannot be made to produce on
demand. Keeping it here separates the model the search reasons *about* from the
vehicle that drives it, which is what lets the vehicle module stay a test
module while this one holds no examples at all.

It is an unnumbered helper module, named like its siblings
:mod:`cache_recovery_harness` and :mod:`cache_recovery_helpers` so pytest
collects nothing from it, and imported by its bare name because the suite puts
this directory on the import path.
"""

import dataclasses as dc

from buildlimits.admission import Counts

# The gauge axes a rule may drive. ``held`` is derived from them by the
# production partition relation (``held == running + draining + server_drain``)
# rather than tracked separately, which is what the search's claim is scoped
# to: the guards are searched over the states those axes can describe. It is
# not a token-conservation proof, and the derivation is exactly why. ``held``
# is assumed to agree with the axes instead of being observed to, the scripted
# harness injects readings rather than acquiring through the pool, and nothing
# here witnesses a return. Token conservation over the real pool is discharged
# by the scripted run in :mod:`test_cache_recovery`, which drives production
# admission and asserts ``pool.max_held`` and the return multiset.
MAX_GROUPS = 2


@dc.dataclass(slots=True)
class Model:
    """What the model knows about the cache and the token pool.

    Attributes
    ----------
    running : int
        Compiler groups whose leaders have not ended.
    draining : int
        Groups whose leader has ended but whose processes may still run.
    server_drain : int
        Empty groups still retaining a token for the server's work.
    queued : int
        Requests still waiting for a token.
    active : bool
        Whether the scripted managed cache is running.
    manager_faulted : bool
        Whether the user-manager transport is faulted, which the live watch
        records as the ``manager`` drain fault.
    roots : int or None
        Compiler roots the diagnostic last established, or None for a count it
        could not read.
    """

    running: int = 0
    draining: int = 0
    server_drain: int = 0
    queued: int = 1
    active: bool = True
    manager_faulted: bool = False
    roots: int | None = 0

    @property
    def moving(self) -> int:
        """Read the compiler groups that are still alive or still winding down.

        Returns
        -------
        int
            Groups a stop would interrupt.
        """
        return self.running + self.draining

    @property
    def counts(self) -> Counts:
        """Read the current gauge snapshot.

        Returns
        -------
        Counts
            A snapshot satisfying the production partition invariant.
        """
        held = self.running + self.draining + self.server_drain
        return Counts(
            self.queued,
            held,
            self.running,
            self.draining,
            self.server_drain,
            capacity=MAX_GROUPS,
            # Every modelled group is cached: the guard the search judges is
            # the one that protects work the stop could interrupt.
            cached_moving=self.moving,
        )

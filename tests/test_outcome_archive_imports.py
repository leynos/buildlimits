"""The maintenance modules import without resolving their annotations.

``outcome_archive_cleanup`` annotates parameters with names it imports only for
typing, which the 3.14 baseline evaluates lazily (PEP 649). Importing the module
and both timer entry points keeps a later change from turning those names into
a failure on the first firing, and the import is what the archiver itself does
before it can report anything.
"""

import buildlimits.outcome_archive as archive
import buildlimits.outcome_archive_cleanup as cleanup


def test_the_cleanup_adapter_imports() -> None:
    """The cleanup adapter's removal seams are present after import."""
    assert callable(cleanup.remove_stale_temps), "the archive cleanup imports"
    assert callable(cleanup.remove_maintenance_files), (
        "the sealed-directory cleanup imports"
    )


def test_the_archive_entry_points_import() -> None:
    """Both timer entry points and the inspection query are importable."""
    assert callable(archive.run_archive), "the archive entry point imports"
    assert callable(archive.run_retain), "the retain entry point imports"
    assert callable(archive.inspect_store), "the inspection entry point imports"

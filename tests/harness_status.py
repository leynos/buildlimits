"""Status CLI queries for the real systemd acceptance harness.

The mixin keeps status decoding separate from harness lifecycle code. It is
intended for ``systemd_harness.Harness`` and other test harnesses with the same
status command and environment contract.
"""

import json
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed fixture argv.
import typing as typ

if typ.TYPE_CHECKING:
    from pathlib import Path


class HarnessStatus:
    """Read supervisor status through the harness's configured CLI."""

    status_command: Path
    env: dict[str, str]

    def status(self) -> dict[str, typ.Any]:
        """Read the supervisor's status document.

        Returns
        -------
        dict[str, Any]
            The decoded status document.

        Examples
        --------
        Read the current held-slot count::

            held = harness.status()["held"]
        """
        result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed argv, no shell.
            [str(self.status_command), "--json"],
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        return json.loads(result.stdout)

    def readiness_status(self) -> tuple[int, dict[str, typ.Any]]:
        """Read status with the admission-readiness exit contract.

        Returns
        -------
        tuple[int, dict[str, Any]]
            The ``--ready`` exit code and decoded status document.

        Examples
        --------
        Check whether the supervisor is ready::

            code, document = harness.readiness_status()
        """
        result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed argv, no shell.
            [str(self.status_command), "--ready", "--json"],
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        return result.returncode, json.loads(result.stdout)

    def drain_fault(self) -> str:
        """Return the current retained-drain fault, if one is reported.

        Returns
        -------
        str
            The reported fault, or an empty string when no fault exists.

        Examples
        --------
        Wait for a retained-work fault to clear::

            if fault := harness.drain_fault():
                print(fault)
        """
        fault = self.status()["drain_fault"]
        return fault if isinstance(fault, str) else ""

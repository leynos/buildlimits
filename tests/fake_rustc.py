#!/usr/bin/env python3
"""Small scripted rustc used to exercise sccache without a real compilation.

Example
-------
Run ``python tests/fake_rustc.py -vV`` to report the fixed toolchain identity
used for sccache's compiler probe.
"""

import hashlib
import os
import sys
import time
from pathlib import Path

_VERSION = "1.80.0"
_HOST = "x86_64-unknown-linux-gnu"
_MISSING_SOURCE_MESSAGE = "a compile request must include a .rs source"


def _option_value(arguments: list[str], option: str) -> str | None:
    """Return one option value in either rustc argument form.

    Returns
    -------
    str | None
        The first separate or equals-form value, if present.
    """
    for index, argument in enumerate(arguments):
        if argument == option and index + 1 < len(arguments):
            return arguments[index + 1]
        if argument.startswith(f"{option}="):
            return argument.partition("=")[2]
    return None


def _emit_kinds(arguments: list[str]) -> frozenset[str]:
    """Return requested emit kinds from rustc arguments.

    Returns
    -------
    frozenset[str]
        The comma-separated emit kinds, or an empty set when absent.
    """
    emit_value = _option_value(arguments, "--emit")
    if emit_value is None:
        return frozenset()
    return frozenset(emit_value.split(","))


def _metadata(arguments: list[str]) -> str | None:
    """Return the ``-C metadata`` value for one test request.

    Returns
    -------
    str | None
        The metadata value, if the request includes one.
    """
    for index, argument in enumerate(arguments):
        if argument == "-C" and index + 1 < len(arguments):
            setting = arguments[index + 1]
            if setting.startswith("metadata="):
                return setting.partition("=")[2]
        if argument.startswith("-Cmetadata="):
            return argument.partition("=")[2]
    return None


def _source_path(arguments: list[str]) -> Path | None:
    """Return the Rust source argument, ignoring common option values.

    Returns
    -------
    Path | None
        The last ``.rs`` argument, if one is present.
    """
    for argument in reversed(arguments):
        if argument.endswith(".rs"):
            return Path(argument)
    return None


def _output_path(arguments: list[str], *, emit_dep_info: bool) -> Path:
    """Choose an explicit or conventional output path for one fake emit.

    Returns
    -------
    Path
        The requested output path or the rustc-style default.
    """
    output = _option_value(arguments, "-o")
    if output is not None:
        return Path(output)

    output_dir = Path(_option_value(arguments, "--out-dir") or ".")
    crate_name = _option_value(arguments, "--crate-name") or "fake"
    suffix = ".d" if emit_dep_info else ".rlib"
    return output_dir / f"lib{crate_name}{suffix}"


def _sleep_for_controlled_request(arguments: list[str], *, emit_dep_info: bool) -> None:
    """Hold one configured request open long enough for process sampling."""
    metadata = _metadata(arguments)
    key_name = (
        "FAKE_RUSTC_SLEEP_DEP_INFO_METADATA"
        if emit_dep_info
        else "FAKE_RUSTC_SLEEP_EMIT_METADATA"
    )
    if metadata != os.environ.get(key_name):
        return

    delay_name = (
        "FAKE_RUSTC_DEP_INFO_SECONDS" if emit_dep_info else "FAKE_RUSTC_EMIT_SECONDS"
    )
    delay = float(os.environ.get(delay_name, "0"))
    if delay > 0:
        time.sleep(delay)


def _write_output(arguments: list[str], *, emit_dep_info: bool) -> None:
    """Write deterministic dep-info or compilation bytes to the requested path.

    Raises
    ------
    ValueError
        If the compiler request contains no Rust source argument.
    """
    source = _source_path(arguments)
    if source is None:
        raise ValueError(_MISSING_SOURCE_MESSAGE)

    output = _output_path(arguments, emit_dep_info=emit_dep_info)
    output.parent.mkdir(parents=True, exist_ok=True)
    if emit_dep_info:
        output.write_text(f"{output.name}: {source}\n", encoding="utf-8")
        return

    digest = hashlib.sha256()
    digest.update(source.read_bytes())
    digest.update((_metadata(arguments) or "").encode())
    digest.update(",".join(sorted(_emit_kinds(arguments))).encode())
    output.write_bytes(b"fake-rustc-output\0" + digest.digest())


def main(arguments: list[str]) -> int:
    """Answer rustc probes or create the requested deterministic artefact.

    Returns
    -------
    int
        A rustc-compatible success status.
    """
    if "-vV" in arguments:
        print(
            f"rustc {_VERSION} (fake build)\n"
            "binary: rustc\n"
            "commit-hash: fake\n"
            "commit-date: 2024-10-01\n"
            f"host: {_HOST}\n"
            f"release: {_VERSION}\n"
            "LLVM version: 18.1.0"
        )
        return 0

    if "--version" in arguments or "-V" in arguments:
        print(f"rustc {_VERSION} (fake build)")
        return 0

    if (
        "--print=sysroot" in arguments
        or _option_value(arguments, "--print") == "sysroot"
    ):
        print(os.environ.get("FAKE_RUSTC_SYSROOT", "/fake-rustc/sysroot"))
        return 0

    emit_kinds = _emit_kinds(arguments)
    emit_dep_info = "dep-info" in emit_kinds and not (
        "link" in emit_kinds or "metadata" in emit_kinds
    )
    _sleep_for_controlled_request(arguments, emit_dep_info=emit_dep_info)
    _write_output(arguments, emit_dep_info=emit_dep_info)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

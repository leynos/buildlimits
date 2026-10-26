"""The adapters that translate between the outside world and the domain.

Each module here owns one boundary. ``fizzbee_output`` is a pure translation of
the checker's text and its error graph; ``fizzbee_process`` owns the scratch
directory, the process group and the timeout; ``configuration`` reads TOML; and
``inventory`` reads ``buildlimits``. Only the last may import the production
package.

``fizzbee_output`` is importable without the others, and without the binary, so
every classification rule can be tested against a recording.
"""

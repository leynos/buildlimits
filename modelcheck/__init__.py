"""Model-check tooling for RFC 0001, a hexagonal package outside the wheel.

The package drives the pinned FizzBee checker over the admission model. It is
split so that every piece of policy can be tested without the 335 MB binary:
the pure domain decides what a configuration expects and what a result means,
the ports name the three collaborators, and the adapters are the only modules
that touch a process, a file or ``buildlimits`` itself.

See ``docs/rfcs/0001-fizzbee-model-of-admission-and-recovery.md`` for the
design and ADR 004 for why this lives beside the package rather than inside
it.
"""

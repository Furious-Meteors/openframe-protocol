"""
Shared fixtures for openframe-protocol-media tests.

No test module in this package is named the same as a module in any
sibling package's `tests/` directory, and none of this monorepo's
`tests/` directories has an `__init__.py` — a plain `from tests.X import Y`
cross-package import would be ambiguous once the whole `packages/` tree
is collected in one pytest session (Python would merge same-named
`tests/` directories into one namespace package and could silently
resolve to the WRONG package's module). See
`openframe-protocol-grpc/tests/conftest.py`'s own docstring for the
original, confirmed occurrence of this exact failure mode. Shared helpers
that another test module in THIS package needs therefore live here, in
conftest.py (pytest loads each conftest.py by its own file path, never
through ambiguous dotted-name resolution), not in a plain test module.
"""
from __future__ import annotations

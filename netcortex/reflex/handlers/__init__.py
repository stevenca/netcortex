"""First-party reflex handlers.

Importing this package registers every handler below with the global
registry in :mod:`netcortex.reflex.registry`. That side-effect is the
point — the runner enumerates the registry, so handlers only need to be
*imported* (not explicitly enumerated) for the runner to find them.

To add a new first-party handler:

1. Create ``netcortex/reflex/handlers/<your_handler>.py`` that calls
   :func:`netcortex.reflex.registry.register_handler` at module scope.
2. Add a line to this file importing the new module.
3. Cover the subject pattern with a test in
   ``tests/reflex/test_handlers.py``.

That is the entire surface — no entry-point discovery, no dynamic
loading. By design (see ``docs/architecture/brain.md`` on plasticity).
"""

from __future__ import annotations

# Import-for-side-effect — each module registers itself on import.
# Keep these imports alphabetical so a diff reviewer can spot additions.
from netcortex.reflex.handlers import bgp_drop  # noqa: F401
from netcortex.reflex.handlers import link_down  # noqa: F401
from netcortex.reflex.handlers import security_webhook  # noqa: F401

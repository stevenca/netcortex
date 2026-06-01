"""Working-memory dedup implementations.

The Protocol lives in :mod:`netcortex.contracts.dedup_store`. This
package contains the concrete implementations the runner can be wired
to.
"""

from netcortex.working.dedup.in_memory import InMemoryDedupStore

__all__ = ["InMemoryDedupStore"]

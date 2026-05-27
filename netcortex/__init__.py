"""NetCortex — The intelligence layer for your network.

Version bump policy (semantic versioning with -dev pre-release counter):

    Between commits, every change increments the ``-devN`` suffix on the
    *current* base version so it's always obvious that work-in-progress
    code is ahead of the last released commit::

        0.4.0           ← last commit (released)
        0.4.0-dev1      ← first change after that commit
        0.4.0-dev2      ← second change …

    When the user asks for a commit, drop the ``-devN`` suffix and bump
    the appropriate slot based on what landed since the last release:
        MAJOR — user-declared (breaking changes or product milestone).
        MINOR — new feature (adapter, view, capability, schema addition).
        PATCH — bug fix only (no new feature; behavior corrected).
    The new commit becomes the new base (e.g. ``0.5.0`` or ``0.4.1``)
    and the next in-flight change starts a fresh ``-dev1`` cycle on top.

The version source of truth is this module. ``pyproject.toml`` and the
``CHANGELOG.md`` MUST be kept in sync whenever ``__version__`` changes.
"""

__version__ = "0.6.0-dev46"

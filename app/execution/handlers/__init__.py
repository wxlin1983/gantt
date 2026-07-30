"""Handler modules.

Custom handlers go in this package; ``load`` imports them so their decorators
run. Registration is a side effect of import, which is why nothing else should
import these modules directly.
"""

from __future__ import annotations

import importlib
import pkgutil

_loaded = False


def load() -> None:
    """Import every module in this package exactly once."""
    global _loaded
    if _loaded:
        return
    for module in pkgutil.iter_modules(__path__):
        if not module.name.startswith("_"):
            importlib.import_module(f"{__name__}.{module.name}")
    _loaded = True

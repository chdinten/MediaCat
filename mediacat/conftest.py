"""Top-level pytest configuration.

Adds the ``src`` layout to ``sys.path`` so ``import mediacat`` works in
both editable-installed and uninstalled checkouts.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

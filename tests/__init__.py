"""Test bootstrap: make ``src/`` importable without an install step.

Run the suite with::

    python -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

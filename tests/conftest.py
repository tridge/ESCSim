from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
DEFAULT_TARGETS = SRC / "escsim" / "resources" / "default-targets.h"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

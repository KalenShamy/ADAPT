"""Put src/ and tools/ on sys.path so tests import the converter and the tools scripts."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

for directory in ("src", "tools"):
    sys.path.insert(0, str(ROOT / directory))

import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(APP_ROOT))

# Test-only explicit model selection; no production fallback.
import os
os.environ.setdefault("ATLAS_CONVERSATION_MODEL", "echo")

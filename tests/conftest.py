import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

# Never touch a real vault or private-knowledge dir from tests.
_TMP = Path(os.environ.get("PYTEST_JARVIS_TMP", "/tmp/jarvis-tests"))
os.environ.setdefault("JARVIS_VAULT", str(_TMP / "vault"))
os.environ.setdefault("JARVIS_PK_DIR", str(_TMP / "pk"))
os.environ.setdefault("JARVIS_SYNC_STATE", str(_TMP / "sync-state.json"))
os.environ.setdefault("JARVIS_BASE_MODEL", "test-model")

import asyncio
import sys
from pathlib import Path


def run_async(coro):
    return asyncio.run(coro)


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


# Make core/services/utils importable when running pytest from the tests folder.
ROOT = project_root()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

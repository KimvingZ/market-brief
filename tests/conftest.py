import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from brief.snapshot import build, load_config  # noqa: E402
from brief.timeutil import UTC  # noqa: E402
from synth import write_prev  # noqa: E402

SMALL = {"BTC", "ETH", "XAU", "NVDA", "SMH"}


@pytest.fixture(scope="session")
def small_cfg():
    cfg = load_config()
    cfg = dict(cfg)
    cfg["assets"] = [a for a in cfg["assets"] if a["id"] in SMALL]
    return cfg


@pytest.fixture(scope="session")
def day1(tmp_path_factory, small_cfg):
    """Synthetic data branch and snapshot at Wed 21 Oct 2026 00:40 UTC (07:40 Phnom Penh)."""
    base = tmp_path_factory.mktemp("day1")
    end = datetime(2026, 10, 21, 0, 40, tzinfo=UTC)
    write_prev(base / "prev", end, cfg=small_cfg)
    snap = build(base / "prev", base / "data", "auto", end, offline=True, cfg=small_cfg)
    return base, snap


def load(p):
    return json.loads(Path(p).read_text())

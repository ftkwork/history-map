"""历史版图 — 数据初始化入口。

从零或增量补全 data/（删除 data/ 后亦可完整重建，需联网）。

会先检查已有数据，仅下载或生成缺失项。

用法:
    python initdata.py              # 首次准备或补全缺失数据
    python initdata.py --force      # 强制重建全部
    python initdata.py --skip-wikidata   # 离线简版（元数据较简略，不推荐）
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("PYTHONUNBUFFERED", "1")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "initdata"))

import run as _initdata_run  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(_initdata_run.main())

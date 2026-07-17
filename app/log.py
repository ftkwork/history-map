"""操作追踪日志，写入 logs/trace.log。"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

_LOG_PATH = Path(__file__).resolve().parents[1] / "logs" / "trace.log"
_ENABLED = True


def trace(tag: str, message: str) -> None:
    if not _ENABLED:
        return
    line = f"{datetime.now().strftime('%H:%M:%S.%f')[:-3]} [{tag}] {message}"
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    print(line, file=sys.stderr, flush=True)


def reset_log() -> None:
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _LOG_PATH.write_text("", encoding="utf-8")
    except OSError:
        pass

"""使用本地大模型丰富补全 data/regimes 中的政权全部信息。

每个政权一次性提交：元数据（译名、年代、族群、君主、来源等）+ 大事记（events）。
模型返回完整 JSON，写回 regimes.json / events.json / names.json。
进度写入 data/regimes/ai_update_progress.json，中断后可继续。

用法:
    python update.py              # 自动：有待创建则 enrich，否则待优化则 refine
    python update.py --status     # 查看进度
    python update.py --limit 3    # 试跑 3 条（配合自动模式）
    python update.py --force Han  # 强制重做指定政权
    python update.py --enrich     # 仅创建/补全（不进入 refine）
    python update.py --refine     # 仅保守审校优化
"""

from __future__ import annotations

import argparse
import json
import re
import signal
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("PYTHONUNBUFFERED", "1")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)

from app.data import clear_data_caches
from app.events_util import is_valid_event_text, merge_event_lists, normalize_events, normalize_event_text
from app.paths import EVENTS_FILE, NAMES_FILE, REGIMES_FILE

# 与 simple_chat.py 相同
SERVER_HOST = "http://192.168.0.44:11434"
MODEL_NAME = "gemma-fast"

PROGRESS_FILE = REGIMES_FILE.parent / "ai_update_progress.json"

_interrupted = False


def _on_interrupt(_sig: int, _frame: object) -> None:
    global _interrupted
    _interrupted = True
    print("\n收到中断信号，将在当前条目完成后保存并退出…", file=sys.stderr)


signal.signal(signal.SIGINT, _on_interrupt)
if hasattr(signal, "SIGBREAK"):
    signal.signal(signal.SIGBREAK, _on_interrupt)


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def load_progress() -> dict[str, Any]:
    if not PROGRESS_FILE.is_file():
        return {
            "version": 1,
            "model": MODEL_NAME,
            "host": SERVER_HOST,
            "started_at": None,
            "updated_at": None,
            "completed": [],
            "failed": {},
        }
    return json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))


def save_progress(progress: dict[str, Any]) -> None:
    progress["updated_at"] = _now_iso()
    PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = PROGRESS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(PROGRESS_FILE)


def load_regimes_payload() -> dict[str, Any]:
    if not REGIMES_FILE.is_file():
        raise FileNotFoundError(f"缺少 {REGIMES_FILE.relative_to(ROOT)}，请先运行 initdata.py")
    return json.loads(REGIMES_FILE.read_text(encoding="utf-8"))


def save_regimes_payload(payload: dict[str, Any]) -> None:
    meta = payload.setdefault("_meta", {})
    regimes = payload.get("regimes", {})
    regime_names = set(regimes)
    payload["events"] = normalize_events(payload.get("events") or {}, regime_names)
    meta["total_regimes"] = len(regimes)
    meta["with_period"] = sum(1 for v in regimes.values() if v.get("period"))
    meta["with_ethnicity"] = sum(1 for v in regimes.values() if v.get("ethnicity"))
    meta["with_rulers"] = sum(1 for v in regimes.values() if v.get("rulers"))
    meta["with_events"] = sum(1 for v in payload["events"].values() if v)
    meta["ai_enriched"] = meta.get("ai_enriched", 0)
    meta["last_ai_update"] = _now_iso()

    tmp = REGIMES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(REGIMES_FILE)


def load_names() -> dict[str, str]:
    if not NAMES_FILE.is_file():
        return {}
    return json.loads(NAMES_FILE.read_text(encoding="utf-8"))


def save_names(names: dict[str, str]) -> None:
    NAMES_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = NAMES_FILE.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(names, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    tmp.replace(NAMES_FILE)


def sync_events_file(payload: dict[str, Any]) -> None:
    """将 regimes.json 中的 events 写入 events.json（应与 save_regimes_payload 后一致）。"""
    events = payload.get("events") or {}
    if not events and not payload.get("regimes"):
        return
    EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = EVENTS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(EVENTS_FILE)


def send_chat(
    prompt: str,
    *,
    host: str,
    model: str,
    timeout: int = 300,
    json_mode: bool = True,
    temperature: float = 0.2,
) -> str | None:
    """调用 Ollama /api/chat（与 simple_chat.py 相同方式）。"""
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "think": False,
        "options": {"num_predict": 4096, "temperature": temperature},
    }
    if json_mode:
        body["format"] = "json"

    try:
        response = requests.post(
            f"{host}/api/chat",
            json=body,
            stream=True,
            timeout=timeout,
        )
        if response.status_code != 200:
            print(f"请求失败: HTTP {response.status_code}", file=sys.stderr)
            return None

        reply = ""
        thinking = ""
        for line in response.iter_lines():
            if not line:
                continue
            chunk = json.loads(line.decode("utf-8"))
            if chunk.get("error"):
                print(f"模型错误: {chunk['error']}", file=sys.stderr)
                return None
            message = chunk.get("message") or {}
            content = message.get("content") or ""
            if content:
                reply += content
            think_part = message.get("thinking") or ""
            if think_part:
                thinking += think_part
            if chunk.get("done"):
                break
        if reply.strip():
            return reply
        if thinking.strip():
            print("  警告: 模型仅返回 thinking，已尝试从中提取。", file=sys.stderr)
            return thinking
        return reply
    except requests.RequestException as exc:
        print(f"连接失败: {exc}", file=sys.stderr)
        return None


def _sanitize_json_text(text: str) -> str:
    """清理模型常在 JSON 中混入的注释、说明文字。"""
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = re.sub(r"```\s*$", "", text)
    text = re.sub(r"\*[^*\n]+\*", "", text)
    text = re.sub(r"<br\s*/?>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^\s*#.+$", "", text, flags=re.MULTILINE)
    text = re.sub(r"//[^\n\"]*", "", text)

    cleaned_lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if re.match(r"^[\u4e00-\u9fff\u0980-\u09ff\u0600-\u06ff].+[。！？]$", stripped):
            continue
        if re.match(r"^(Note|注意|理论上|le:|century:|rag_id:)", stripped, re.I):
            continue
        if re.match(r"^\*[^*]+\*$", stripped):
            continue
        line = re.sub(r",(\s*[}\]])", r"\1", line)
        cleaned_lines.append(line)
    text = "\n".join(cleaned_lines)
    text = re.sub(r",(\s*[}\]])", r"\1", text)
    return text


def _repair_truncated_json(text: str) -> str:
    """尝试闭合被截断的 JSON。"""
    text = text.strip()
    if not text.startswith("{"):
        start = text.find("{")
        if start >= 0:
            text = text[start:]
    in_string = False
    escape = False
    stack: list[str] = []
    for ch in text:
        if escape:
            escape = False
            continue
        if ch == "\\":
            if in_string:
                escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            stack.append("}")
        elif ch == "[":
            stack.append("]")
        elif ch in "}]" and stack and stack[-1] == ch:
            stack.pop()
    if in_string:
        text += '"'
    while stack:
        text += stack.pop()
    return text


def _extract_fields_lenient(text: str) -> dict[str, Any] | None:
    """从损坏/截断的 JSON 文本中尽量提取字段。"""
    result: dict[str, Any] = {}
    for key in ("name_zh", "period", "ethnicity", "changes"):
        m = re.search(rf'"{re.escape(key)}"\s*:\s*("(?:\\.|[^"\\])*"|null)', text, re.DOTALL)
        if m:
            raw = m.group(1)
            result[key] = None if raw == "null" else json.loads(raw)

    rulers_m = re.search(r'"rulers"\s*:\s*\[(.*?)\]', text, re.DOTALL)
    if rulers_m:
        rulers = re.findall(r'"((?:\\.|[^"\\])*)"', rulers_m.group(1))
        if rulers:
            result["rulers"] = rulers

    events: list[dict[str, Any]] = []
    for m in re.finditer(
        r'\{\s*"year"\s*:\s*(-?\d+)\s*,\s*"event"\s*:\s*"((?:\\.|[^"\\])*)"\s*\}',
        text,
    ):
        event = m.group(2).strip()
        if not is_valid_event_text(event):
            continue
        events.append({"year": int(m.group(1)), "event": event})
    if events:
        events.sort(key=lambda e: e["year"])
        result["events"] = events

    if not any(k in result for k in ("name_zh", "period", "ethnicity", "rulers", "events")):
        return None
    return result


def _extract_json(text: str) -> dict[str, Any] | None:
    if not text or not text.strip():
        return None

    text = text.strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence:
        text = fence.group(1).strip()
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1]

    attempts = [
        text,
        _sanitize_json_text(text),
        _repair_truncated_json(_sanitize_json_text(text)),
        _repair_truncated_json(text),
    ]
    seen: set[str] = set()
    for attempt in attempts:
        if attempt in seen:
            continue
        seen.add(attempt)
        try:
            data = json.loads(attempt)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            continue

    return _extract_fields_lenient(text)


def load_events_table(payload: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """合并 regimes.json 与 events.json 中的大事记。"""
    merged: dict[str, list[dict[str, Any]]] = dict(payload.get("events") or {})
    if EVENTS_FILE.is_file():
        from_file = json.loads(EVENTS_FILE.read_text(encoding="utf-8"))
        for name, items in from_file.items():
            if name not in merged or not merged[name]:
                merged[name] = items
    return merged


def get_events_for_regime(
    name_en: str,
    payload: dict[str, Any],
    events_table: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    return list(events_table.get(name_en) or [])


_GARBAGE_FIELD = re.compile(
    r"events_info|substring_error|enrichment_flag|imitationer|<0x|_info_info|thought process:",
    re.I,
)


def _clean_text_field(value: Any, *, max_len: int = 200) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or _GARBAGE_FIELD.search(text):
        return None
    if len(text) > max_len:
        return text[: max_len - 1] + "…"
    return text


def _clean_rulers_list(rulers: Any, *, max_items: int = 8, max_item_len: int = 40) -> list[str]:
    if not isinstance(rulers, list):
        return []
    cleaned: list[str] = []
    for item in rulers:
        text = str(item).strip()
        if not text or _GARBAGE_FIELD.search(text):
            continue
        if len(text) > max_item_len:
            text = text[: max_item_len - 1] + "…"
        cleaned.append(text)
        if len(cleaned) >= max_items:
            break
    return cleaned


def sanitize_entry_for_prompt(entry: dict[str, Any]) -> dict[str, Any]:
    """构建提示前清理异常字段，避免 prompt 过长或含脏数据。"""
    return {
        "name_zh": _clean_text_field(entry.get("name_zh"), max_len=80),
        "period": _clean_text_field(entry.get("period"), max_len=120),
        "ethnicity": _clean_text_field(entry.get("ethnicity"), max_len=120),
        "rulers": _clean_rulers_list(entry.get("rulers")),
    }


def sanitize_regime_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """写回存储前清理元数据字段。"""
    out = dict(entry)
    for key, max_len in (("name_zh", 80), ("period", 120), ("ethnicity", 120)):
        cleaned = _clean_text_field(out.get(key), max_len=max_len)
        if cleaned is None and out.get(key) is not None and str(out.get(key)).strip():
            out.pop(key, None)
        elif cleaned is not None:
            out[key] = cleaned
    rulers = _clean_rulers_list(out.get("rulers"))
    if rulers:
        out["rulers"] = rulers
    elif out.get("rulers"):
        out["rulers"] = []
    return out


def build_prompt(
    name_en: str,
    entry: dict[str, Any],
    events: list[dict[str, Any]],
) -> str:
    """构建提示：包含政权全部现有字段 + 大事记。"""
    safe = sanitize_entry_for_prompt(entry)
    current = {
        "name_en": name_en,
        "name_zh": safe.get("name_zh"),
        "period": safe.get("period"),
        "ethnicity": safe.get("ethnicity"),
        "rulers": safe.get("rulers") or [],
        "events": events[:12],
    }
    has_events = bool(events)
    events_hint = (
        "该政权目前已有大事记，请保留正确条目、纠正错误、补充重要事件。"
        if has_events
        else "该政权目前没有大事记，请根据史实补写大事记（至少 3 条，重要政权可更多）。"
    )
    return f"""你是历史政权百科编辑。请对下列政权的**全部信息**进行丰富、补全与纠错，包括元数据和大事记。

【现有完整数据】
{json.dumps(current, ensure_ascii=False, indent=2)}

【大事记说明】
{events_hint}

【输出要求】
1. 只输出一个 JSON 对象，不要其他文字。
2. 必须包含以下字段：
   - name_zh: 规范中文译名
   - period: 存在年代（中文；不详则 null）
   - ethnicity: 主体族群（不详则 null）
   - rulers: 著名君主/领袖数组（中文，最多 8 个）
   - events: 大事记数组，每项 {{"year": 整数, "event": "中文描述"}}；year 负数表示公元前；最多 8 条
   - changes: 字符串，说明对元数据与大事记做了哪些纠正或补充
3. 元数据与大事记同等重要；events 须按 year 升序排列。
4. 纠正明显史实错误；无把握时保留原值。
5. **禁止**在 JSON 中加入注释、Note、说明文字或 markdown；只输出纯 JSON。
6. 只输出 JSON。"""


def _is_blank_value(value: Any) -> bool:
    if value is None:
        return True
    text = str(value).strip()
    return not text or text in {"待考", "不详", "未知", "暂无记载"}


def build_refine_prompt(
    name_en: str,
    entry: dict[str, Any],
    events: list[dict[str, Any]],
) -> str:
    """保守审阅提示：默认保留现有好数据，只做补全与明确纠错。"""
    safe = sanitize_entry_for_prompt(entry)
    current = {
        "name_en": name_en,
        "name_zh": safe.get("name_zh"),
        "period": safe.get("period"),
        "ethnicity": safe.get("ethnicity"),
        "rulers": safe.get("rulers") or [],
        "events": events[:16],
    }
    return f"""你是历史政权百科的**审校编辑**。下列数据已经过初步整理，整体质量较好。

【审校原则 — 必须遵守】
1. **默认保留原值**：现有非空字段若无明显史实错误，必须原样输出，不要为改而改。
2. **优先补空白**：仅对 null、空字符串、「待考」「不详」等占位内容做补全。
3. **谨慎纠错**：只有当你非常确定存在史实错误时才修改已有内容；须在 changes 中说明理由。
4. **大事记**：已有年份条目原则上保留原文；只可补充缺失的重要年份，不要重写已有条目。
5. **无把握时**：原样保留，changes 写「未发现需修改处」或说明仅补充了哪些空白。

【现有完整数据】
{json.dumps(current, ensure_ascii=False, indent=2)}

【输出要求】
1. 只输出一个 JSON 对象，不要其他文字。
2. 必须包含字段：name_zh, period, ethnicity, rulers, events, changes
3. events: {{"year": 整数, "event": "中文"}} 数组，year 负数表示公元前，按 year 升序
4. changes: 字符串，逐条说明「保留 / 补全 / 纠正」了哪些内容；若无改动写「审校通过，保留原值」
5. 只输出纯 JSON，无注释无 markdown。"""


def merge_enrichment_refine(
    entry: dict[str, Any],
    enriched: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """保守合并元数据：空白补全；已有内容仅在 changes 表明纠错时覆盖。"""
    out = dict(entry)
    changes = str(enriched.get("changes") or "")
    notes: list[str] = []
    has_correction = bool(re.search(r"纠正|修正|更正|改为|修正为", changes))

    for key in ("name_zh", "period", "ethnicity"):
        existing = out.get(key)
        new_val = enriched.get(key)
        if new_val is None or str(new_val).strip() == "":
            continue
        new_text = _clean_text_field(new_val, max_len=120 if key != "name_zh" else 80)
        if not new_text:
            continue
        if _is_blank_value(existing):
            out[key] = new_text
            notes.append(f"补全{key}")
        elif str(existing).strip() != new_text:
            if key == "name_zh" and re.search(r"译名|名称|改名", changes):
                out[key] = new_text
                notes.append(f"纠正{key}")
            elif has_correction:
                out[key] = new_text
                notes.append(f"纠正{key}")

    existing_rulers = out.get("rulers") or []
    new_rulers = enriched.get("rulers")
    if isinstance(new_rulers, list):
        cleaned = _clean_rulers_list(new_rulers)
        if cleaned:
            if not existing_rulers:
                out["rulers"] = cleaned
                notes.append("补全rulers")
            elif has_correction and cleaned != existing_rulers:
                out["rulers"] = cleaned
                notes.append("纠正rulers")

    sources = list(out.get("sources") or [])
    if "ai" not in sources:
        sources.append("ai")
    if "ai_refined" not in sources:
        sources.append("ai_refined")
    out["sources"] = sources
    return sanitize_regime_entry(out), notes


def merge_events_refine(
    name_en: str,
    payload: dict[str, Any],
    enriched: dict[str, Any],
) -> tuple[bool, list[str]]:
    """保守合并大事记：保留已有年份；仅追加新年份（或首次写入）。"""
    if name_en not in payload.get("regimes", {}):
        return False, []
    events = enriched.get("events")
    if not isinstance(events, list):
        return False, []

    incoming: list[dict[str, Any]] = []
    for item in events:
        if not isinstance(item, dict):
            continue
        try:
            year = int(item["year"])
        except (KeyError, TypeError, ValueError):
            continue
        event = normalize_event_text(item.get("event"))
        if not is_valid_event_text(event):
            continue
        incoming.append({"year": year, "event": event})
    if not incoming:
        return False, []

    existing = (payload.get("events") or {}).get(name_en, [])
    if not existing:
        merged = merge_event_lists([], incoming)
        if not merged:
            return False, []
        payload.setdefault("events", {})[name_en] = merged
        return True, [f"新增大事记 {len(merged)} 条"]

    existing_years = {int(item["year"]) for item in existing}
    added = [item for item in incoming if int(item["year"]) not in existing_years]
    if not added:
        return False, []

    merged = merge_event_lists(existing, added)
    payload.setdefault("events", {})[name_en] = merged
    return True, [f"追加大事记 {len(added)} 条（保留已有 {len(existing)} 条）"]


def merge_enrichment(
    entry: dict[str, Any],
    enriched: dict[str, Any],
) -> dict[str, Any]:
    out = dict(entry)
    for key in ("name_zh", "period", "ethnicity"):
        val = _clean_text_field(enriched.get(key))
        if val is not None:
            out[key] = val
    rulers = enriched.get("rulers")
    if isinstance(rulers, list) and rulers:
        cleaned = _clean_rulers_list(rulers)
        if cleaned:
            out["rulers"] = cleaned
    sources = list(out.get("sources") or [])
    if "ai" not in sources:
        sources.append("ai")
    out["sources"] = sources
    return sanitize_regime_entry(out)


def merge_events(
    name_en: str,
    payload: dict[str, Any],
    enriched: dict[str, Any],
) -> bool:
    """写回大事记（仅合法政权键与有效条目）。"""
    if name_en not in payload.get("regimes", {}):
        return False
    events = enriched.get("events")
    if not isinstance(events, list):
        return False
    incoming: list[dict[str, Any]] = []
    for item in events:
        if not isinstance(item, dict):
            continue
        try:
            year = int(item["year"])
        except (KeyError, TypeError, ValueError):
            continue
        event = normalize_event_text(item.get("event"))
        if not is_valid_event_text(event):
            continue
        incoming.append({"year": year, "event": event})
    if not incoming:
        return False
    existing = (payload.get("events") or {}).get(name_en, [])
    merged = merge_event_lists(existing, incoming)
    if not merged:
        return False
    payload.setdefault("events", {})[name_en] = merged
    return True


def pending_refine_names(
    all_names: list[str],
    progress: dict[str, Any],
    *,
    force: set[str],
    refine_all: bool = False,
    retry_failed: bool = False,
) -> list[str]:
    completed = set(progress.get("completed") or [])
    refined = set(progress.get("refined") or [])
    refine_failed = progress.get("refine_failed") or {}
    out: list[str] = []
    for name in all_names:
        if name not in completed and name not in force:
            continue
        if name in force:
            out.append(name)
        elif name in refine_failed and retry_failed:
            out.append(name)
        elif refine_all:
            out.append(name)
        elif name not in refined and name not in refine_failed:
            out.append(name)
    return out


def pending_names(
    all_names: list[str],
    progress: dict[str, Any],
    *,
    force: set[str],
    include_failed: bool = False,
) -> list[str]:
    completed = set(progress.get("completed") or [])
    failed = set(progress.get("failed") or {})
    out: list[str] = []
    for name in all_names:
        if name in force:
            out.append(name)
        elif name in failed and include_failed:
            out.append(name)
        elif name not in completed and name not in failed:
            out.append(name)
    return out


def print_status(progress: dict[str, Any], total: int) -> None:
    completed = progress.get("completed") or []
    refined = progress.get("refined") or []
    failed = progress.get("failed") or {}
    refine_failed = progress.get("refine_failed") or {}
    done = len(completed)
    print(f"进度文件: {PROGRESS_FILE.relative_to(ROOT)}")
    print(f"模型: {progress.get('host', SERVER_HOST)} / {progress.get('model', MODEL_NAME)}")
    print(f"已完成 enrich: {done}/{total}")
    print(f"已完成 refine: {len(refined)}/{done}")
    print(f"enrich 失败: {len(failed)}")
    print(f"refine 失败: {len(refine_failed)}")
    print(f"待 enrich: {max(0, total - done - len(failed))}")
    print(f"待 refine: {max(0, done - len(refined) - len(refine_failed))}")
    if progress.get("updated_at"):
        print(f"上次更新: {progress['updated_at']}")
    if failed:
        print("\nenrich 失败条目（可用 --retry-failed 重试）:")
        for name, info in list(failed.items())[:10]:
            print(f"  - {name}: {info.get('error', '?')}")
        if len(failed) > 10:
            print(f"  … 共 {len(failed)} 条")
    if refine_failed:
        print("\nrefine 失败条目（可用 --refine --retry-failed 重试）:")
        for name, info in list(refine_failed.items())[:10]:
            print(f"  - {name}: {info.get('error', '?')}")
        if len(refine_failed) > 10:
            print(f"  … 共 {len(refine_failed)} 条")


def resolve_auto_mode(
    all_names: list[str],
    progress: dict[str, Any],
    *,
    force: set[str],
    retry_failed: bool = False,
) -> tuple[str | None, list[str], bool]:
    """根据进度决定下一步：enrich → refine → 自动重试失败条目。"""
    enrich_todo = pending_names(
        all_names, progress, force=force, include_failed=retry_failed
    )
    refine_todo = pending_refine_names(
        all_names, progress, force=force, retry_failed=retry_failed
    )
    if enrich_todo:
        return "enrich", enrich_todo, retry_failed
    if refine_todo:
        return "refine", refine_todo, retry_failed

    if not retry_failed:
        enrich_failed = pending_names(all_names, progress, force=force, include_failed=True)
        if enrich_failed:
            return "enrich", enrich_failed, True
        refine_failed_todo = pending_refine_names(
            all_names, progress, force=force, retry_failed=True
        )
        if refine_failed_todo:
            return "refine", refine_failed_todo, True

    return None, [], retry_failed


def recover_failed_from_stored(
    *,
    payload: dict[str, Any],
    progress: dict[str, Any],
    names_table: dict[str, str],
    events_table: dict[str, list[dict[str, Any]]],
    regimes: dict[str, dict[str, Any]],
) -> list[str]:
    """从 progress.failed 里保存的 raw 文本恢复可解析条目，无需再次请求模型。"""
    stored_failed: dict[str, Any] = dict(progress.get("failed") or {})
    if not stored_failed:
        return []

    recovered: list[str] = []
    still_failed: dict[str, Any] = {}
    completed_set = set(progress.get("completed") or [])

    for name_en, info in stored_failed.items():
        raw = info.get("raw") or ""
        enriched = _extract_json(raw) if raw.strip() else None
        if not enriched:
            still_failed[name_en] = info
            continue

        entry = regimes.get(name_en, {})
        regimes[name_en] = merge_enrichment(entry, enriched)
        if merge_events(name_en, payload, enriched):
            events_table[name_en] = payload["events"][name_en]
        name_zh = regimes[name_en].get("name_zh")
        if name_zh:
            names_table[name_en] = name_zh
        completed_set.add(name_en)
        recovered.append(name_en)
        print(f"  [恢复] {name_en} {name_zh or ''}")

    if recovered:
        progress["completed"] = sorted(completed_set)
        progress["failed"] = still_failed
        payload["_meta"]["ai_enriched"] = len(completed_set)
        save_regimes_payload(payload)
        save_names(names_table)
        sync_events_file(payload)
        save_progress(progress)
        clear_data_caches()

    return recovered


def run_update(
    *,
    host: str,
    model: str,
    limit: int | None,
    force: set[str],
    retry_failed: bool,
    delay: float,
    verbose: bool,
    refine: bool = False,
    refine_all: bool = False,
    auto_mode: bool = False,
    auto_retry: bool = False,
) -> int:
    payload = load_regimes_payload()
    regimes: dict[str, dict[str, Any]] = payload.get("regimes", {})
    if not regimes:
        print("regimes.json 中没有政权数据。", file=sys.stderr)
        return 1

    all_names = sorted(regimes)
    progress = load_progress()
    progress.setdefault("refined", [])
    progress.setdefault("refine_failed", {})
    progress["host"] = host
    progress["model"] = model
    if not progress.get("started_at"):
        progress["started_at"] = _now_iso()

    if retry_failed and not refine:
        names_table = load_names()
        events_table = load_events_table(payload)
        old_count = len(progress.get("failed") or {})
        if old_count:
            print(f"尝试从上次失败记录中恢复（共 {old_count} 条）…")
            recovered = recover_failed_from_stored(
                payload=payload,
                progress=progress,
                names_table=names_table,
                events_table=events_table,
                regimes=regimes,
            )
            print(f"已从缓存恢复 {len(recovered)} 条，剩余 {len(progress.get('failed') or {})} 条需重新请求模型\n")

    if refine:
        todo = pending_refine_names(
            all_names,
            progress,
            force=force,
            refine_all=refine_all,
            retry_failed=retry_failed,
        )
    else:
        todo = pending_names(all_names, progress, force=force, include_failed=retry_failed)
    if limit is not None:
        todo = todo[:limit]

    if not todo:
        print_status(progress, len(all_names))
        if refine:
            print("\n没有待审校的政权。")
            if not refine_all:
                print("提示: 使用 --refine-all 可重新审校全部已完成条目。")
        else:
            print("\n没有待 enrich 的政权。")
        return 0

    mode_label = "审校 refine" if refine else "丰富 enrich"
    chat_temperature = 0.1 if refine else 0.2
    if auto_mode:
        phase = "优化 refine" if refine else "创建 enrich"
        suffix = "（含失败重试）" if auto_retry else ""
        print(f"自动模式 → {phase}{suffix}（本次 {len(todo)} 条）")
    print(f"模式: {mode_label}")
    print(f"连接 {host} 模型 {model}")
    print(f"本次处理 {len(todo)} 个政权（共 {len(all_names)} 个）\n")

    names_table = load_names()
    events_table = load_events_table(payload)
    completed_set = set(progress.get("completed") or [])
    refined_set = set(progress.get("refined") or [])
    failed: dict[str, Any] = dict(progress.get("failed") or {})
    refine_failed: dict[str, Any] = dict(progress.get("refine_failed") or {})

    for index, name_en in enumerate(todo, start=1):
        if _interrupted:
            break

        entry = regimes[name_en]
        events = get_events_for_regime(name_en, payload, events_table)
        print(f"[{index}/{len(todo)}] {name_en}  （大事记 {len(events)} 条）")

        prompt = (
            build_refine_prompt(name_en, entry, events)
            if refine
            else build_prompt(name_en, entry, events)
        )
        reply = send_chat(
            prompt,
            host=host,
            model=model,
            temperature=chat_temperature,
        )
        if not reply or not reply.strip():
            reply = send_chat(
                prompt,
                host=host,
                model=model,
                json_mode=False,
                temperature=chat_temperature,
            )
        if not reply or not reply.strip():
            short_prompt = (
                build_refine_prompt(name_en, entry, events[:6])
                if refine
                else build_prompt(name_en, entry, events[:6])
            )
            retry_reply = send_chat(
                short_prompt + "\n\n请输出简短 JSON，events 最多 5 条。",
                host=host,
                model=model,
                json_mode=False,
                temperature=chat_temperature,
            )
            reply = retry_reply or reply

        fail_bucket = refine_failed if refine else failed

        if reply is None or not reply.strip():
            fail_bucket[name_en] = {"error": "模型请求失败", "at": _now_iso()}
            if refine:
                progress["refine_failed"] = refine_failed
            else:
                progress["failed"] = failed
            save_progress(progress)
            print("  -> 失败，已记录\n")
            continue

        if verbose:
            print(f"  模型回复:\n{reply[:500]}{'…' if len(reply) > 500 else ''}\n")

        enriched = _extract_json(reply)
        if not enriched:
            retry_reply = send_chat(
                prompt
                + '\n\n请重新输出。仅纯 JSON 对象，含 name_zh period ethnicity rulers events changes 六个键，无注释无 markdown。',
                host=host,
                model=model,
                temperature=chat_temperature,
            )
            if retry_reply:
                enriched = _extract_json(retry_reply)
                if enriched:
                    reply = retry_reply

        if not enriched and reply:
            stored = fail_bucket.get(name_en, {}).get("raw")
            if stored:
                enriched = _extract_json(stored)

        if not enriched:
            fail_bucket[name_en] = {
                "error": "无法解析 JSON 回复",
                "at": _now_iso(),
                "raw": (reply or "")[:4000],
            }
            if refine:
                progress["refine_failed"] = refine_failed
            else:
                progress["failed"] = failed
            save_progress(progress)
            print("  -> 解析失败，已记录\n")
            continue

        change_notes: list[str] = []
        if refine:
            regimes[name_en], change_notes = merge_enrichment_refine(entry, enriched)
            events_updated, event_notes = merge_events_refine(name_en, payload, enriched)
            change_notes.extend(event_notes)
        else:
            regimes[name_en] = merge_enrichment(entry, enriched)
            events_updated = merge_events(name_en, payload, enriched)
            event_notes = []

        if events_updated:
            events_table[name_en] = payload["events"][name_en]

        name_zh = regimes[name_en].get("name_zh")
        if name_zh:
            names_table[name_en] = name_zh

        if name_en in fail_bucket:
            del fail_bucket[name_en]
        if refine:
            refined_set.add(name_en)
            progress["refined"] = sorted(refined_set)
            progress["refine_failed"] = refine_failed
        else:
            if name_en not in completed_set:
                completed_set.add(name_en)
                progress["completed"] = sorted(completed_set)
            progress["failed"] = failed

        payload["_meta"]["ai_enriched"] = len(completed_set)
        if refine:
            payload["_meta"]["ai_refined"] = len(refined_set)
        save_regimes_payload(payload)
        save_names(names_table)
        sync_events_file(payload)

        progress["last_completed"] = name_en
        save_progress(progress)
        clear_data_caches()

        changes = enriched.get("changes") or ""
        if refine:
            if change_notes:
                print(f"  -> 已更新: {'; '.join(change_notes)}")
            else:
                print("  -> 审校通过，保留原值")
            if changes:
                print(f"     模型说明: {str(changes)[:80]}\n")
            else:
                print()
        else:
            ev_note = f"，大事记 {len(payload['events'].get(name_en, []))} 条" if events_updated else ""
            print(f"  -> 完成 {name_zh or ''}{ev_note} {('— ' + str(changes)[:60]) if changes else ''}\n")

        if delay > 0 and index < len(todo):
            time.sleep(delay)

    print("=" * 48)
    print_status(progress, len(all_names))
    if _interrupted:
        print("\n已中断，下次运行 python update.py 可继续。")
        return 130
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="使用本地大模型丰富 data/regimes 政权数据")
    parser.add_argument("--host", default=SERVER_HOST, help=f"Ollama 地址（默认 {SERVER_HOST}）")
    parser.add_argument("--model", default=MODEL_NAME, help=f"模型名（默认 {MODEL_NAME}）")
    parser.add_argument("--limit", type=int, help="最多处理条数（试跑用）")
    parser.add_argument("--force", nargs="+", metavar="NAME", help="强制重做指定政权（英文名）")
    parser.add_argument("--retry-failed", action="store_true", help="重试失败条目")
    parser.add_argument(
        "--enrich",
        action="store_true",
        help="仅执行 enrich（创建/补全），不自动进入 refine",
    )
    parser.add_argument(
        "--refine",
        action="store_true",
        help="仅执行 refine（保守审校优化）",
    )
    parser.add_argument("--refine-all", action="store_true", help="配合 --refine：重新审校全部已完成条目")
    parser.add_argument("--status", action="store_true", help="仅显示进度")
    parser.add_argument("--reset", action="store_true", help="清空进度记录（不修改 regimes.json）")
    parser.add_argument("--delay", type=float, default=1.0, help="每条之间的间隔秒数（默认 1）")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印模型原始回复")
    args = parser.parse_args()

    if args.reset:
        if PROGRESS_FILE.is_file():
            PROGRESS_FILE.unlink()
        print(f"已清空 {PROGRESS_FILE.relative_to(ROOT)}")
        return 0

    if args.status:
        progress = load_progress()
        progress.setdefault("refined", [])
        progress.setdefault("refine_failed", {})
        try:
            total = len(load_regimes_payload().get("regimes", {}))
        except FileNotFoundError as exc:
            print(exc, file=sys.stderr)
            return 1
        print_status(progress, total)
        return 0

    if args.enrich and args.refine:
        print("不能同时指定 --enrich 与 --refine。", file=sys.stderr)
        return 2

    force_set = set(args.force or [])
    auto_mode = not args.enrich and not args.refine

    try:
        payload = load_regimes_payload()
        total = len(payload.get("regimes", {}))
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1

    progress = load_progress()
    progress.setdefault("refined", [])
    progress.setdefault("refine_failed", {})
    all_names = sorted(payload.get("regimes", {}))

    if auto_mode:
        mode, todo_preview, auto_retry = resolve_auto_mode(
            all_names,
            progress,
            force=force_set,
            retry_failed=args.retry_failed,
        )
        if mode is None:
            print_status(progress, total)
            print("\n全部完成：enrich 与 refine 均已处理。")
            return 0
        refine = mode == "refine"
        refine_all = False
        if auto_retry and not args.retry_failed:
            args.retry_failed = True
    else:
        refine = args.refine
        refine_all = args.refine_all
        auto_retry = args.retry_failed

    return run_update(
        host=args.host.rstrip("/"),
        model=args.model,
        limit=args.limit,
        force=force_set,
        retry_failed=args.retry_failed,
        delay=max(0.0, args.delay),
        verbose=args.verbose,
        refine=refine,
        refine_all=refine_all,
        auto_mode=auto_mode,
        auto_retry=auto_retry if auto_mode else args.retry_failed,
    )


if __name__ == "__main__":
    raise SystemExit(main())

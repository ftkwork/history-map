"""数据准备流水线：从零或增量生成 data/ 全部文件。"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path

_PREP_DIR = Path(__file__).resolve().parent
ROOT = _PREP_DIR.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(_PREP_DIR) not in sys.path:
    sys.path.insert(0, str(_PREP_DIR))

from app.data import clear_data_caches, is_app_data_ready
from app.paths import (
    BUILD_GEOJSON_DIR,
    BUILD_WIKIDATA_FILE,
    CATALOG_FILE,
    EVENTS_FILE,
    MAPS_DIR,
    NAMES_FILE,
    REGIMES_FILE,
    SNAPSHOTS_META_FILE,
    WORLD_COUNTRIES_FILE,
    WORLD_LABELS_FILE,
    WORLD_LAND_FILE,
    year_snapshot_path,
)
from tasks import (
    build_names,
    build_prepared_cache,
    build_regime_periods,
    build_regimes_curated,
    build_regimes_database,
    build_world_labels,
    cleanup_build,
    download_offline_assets,
    enrich_from_wikidata,
    ensure_geojson,
    ensure_source_data,
    needs_offline_assets,
)

ProgressCallback = Callable[[str, int, int, int, int], None]


def _step_header(index: int, total: int, title: str) -> None:
    print(f"\n[{index}/{total}] {title}")
    print("-" * 48)


def _catalog_entries() -> list[dict]:
    if not CATALOG_FILE.is_file():
        return []
    with CATALOG_FILE.open(encoding="utf-8") as f:
        return json.load(f)["years"]


def _has_raw_geojson() -> bool:
    if not BUILD_GEOJSON_DIR.is_dir():
        return False
    entries = _catalog_entries()
    if not entries:
        return False
    return all((BUILD_GEOJSON_DIR / e["filename"]).is_file() for e in entries)


def _missing_geojson_count() -> int:
    if not CATALOG_FILE.is_file():
        return 0
    return sum(
        1
        for e in _catalog_entries()
        if not (BUILD_GEOJSON_DIR / e["filename"]).is_file()
    )


def _missing_map_count() -> int:
    if not CATALOG_FILE.is_file():
        return 0
    return sum(1 for e in _catalog_entries() if not year_snapshot_path(e["year"]).is_file())


def _wikidata_complete() -> bool:
    if not REGIMES_FILE.is_file():
        return False
    meta = json.loads(REGIMES_FILE.read_text(encoding="utf-8")).get("_meta", {})
    return meta.get("with_wikidata", 0) > 0


def _needs_metadata_rebuild(*, force: bool) -> bool:
    if force:
        return True
    return not REGIMES_FILE.is_file() or not NAMES_FILE.is_file()


def _needs_wikidata(*, force: bool, skip_wikidata: bool) -> bool:
    if skip_wikidata:
        return False
    if force:
        return True
    if BUILD_WIKIDATA_FILE.is_file() and _wikidata_complete():
        return False
    return not _wikidata_complete()


def _needs_maps_rebuild(*, force: bool, rebuild_meta: bool) -> bool:
    if force:
        return True
    if not is_app_data_ready():
        return True
    if rebuild_meta:
        return True
    if not SNAPSHOTS_META_FILE.is_file():
        return True
    return _missing_map_count() > 0


def _scan_data_status() -> dict[str, tuple[bool, str]]:
    entries = _catalog_entries() if CATALOG_FILE.is_file() else []
    map_total = len(entries)
    map_done = map_total - _missing_map_count() if map_total else 0
    geo_missing = _missing_geojson_count()

    return {
        "catalog": (CATALOG_FILE.is_file(), str(CATALOG_FILE.relative_to(ROOT))),
        "events": (EVENTS_FILE.is_file(), str(EVENTS_FILE.relative_to(ROOT))),
        "names": (NAMES_FILE.is_file(), str(NAMES_FILE.relative_to(ROOT))),
        "regimes": (REGIMES_FILE.is_file(), str(REGIMES_FILE.relative_to(ROOT))),
        "maps": (
            is_app_data_ready(),
            f"data/maps/ ({map_done}/{map_total})" if map_total else "data/maps/",
        ),
        "basemap": (
            WORLD_LAND_FILE.is_file() and WORLD_COUNTRIES_FILE.is_file(),
            "data/basemap/",
        ),
        "labels": (WORLD_LABELS_FILE.is_file(), str(WORLD_LABELS_FILE.relative_to(ROOT))),
        "assets": (not needs_offline_assets(), "data/static/leaflet/ + basemap"),
        "wikidata": (_wikidata_complete(), "regimes.json _meta.with_wikidata"),
        "geojson_cache": (
            geo_missing == 0 and _has_raw_geojson(),
            f"data/build/geojson/ ({map_total - geo_missing}/{map_total})"
            if map_total
            else "data/build/geojson/",
        ),
    }


def _print_data_scan() -> None:
    labels = {
        "catalog": "年代索引",
        "events": "政权大事记",
        "names": "中文译名",
        "regimes": "政权数据库",
        "maps": "版图快照",
        "basemap": "离线底图",
        "labels": "底图中文标注",
        "assets": "Leaflet/底图资源",
        "wikidata": "Wikidata 元数据",
        "geojson_cache": "原始 GeoJSON 缓存",
    }
    print("数据检查：")
    for key, (ok, detail) in _scan_data_status().items():
        mark = "已有" if ok else "缺失"
        print(f"  [{mark}] {labels[key]}  ({detail})")


def _download_geojson() -> None:
    entries = _catalog_entries()
    total = len(entries)
    print(f"共 {total} 个历史版图快照（已有则跳过）")
    for index, entry in enumerate(entries, start=1):
        filename = entry["filename"]
        year = entry["year"]
        path = BUILD_GEOJSON_DIR / filename
        if path.is_file():
            print(f"  [{index:>2}/{total}] {year:>8}  {filename}  (已有)")
            continue
        path = ensure_geojson(filename)
        size_kb = path.stat().st_size / 1024
        print(f"  [{index:>2}/{total}] {year:>8}  {filename}  ({size_kb:.0f} KB)")


def _build_cache(*, force: bool) -> None:
    entries_total = len(_catalog_entries())

    def on_progress(message: str, current: int, total: int) -> None:
        pct = 0 if total <= 0 else int(current * 100 / total)
        bar = "#" * (pct // 5) + "-" * (20 - pct // 5)
        print(f"\r  [{bar}] {pct:3d}%  {message}", end="", flush=True)

    print(f"  共 {entries_total} 个年代快照（已有则跳过）")
    build_prepared_cache(force=force, on_progress=on_progress)
    print()
    year_count = len(list(MAPS_DIR.glob("*.json"))) if MAPS_DIR.exists() else 0
    print(f"  地图数据: {SNAPSHOTS_META_FILE.relative_to(ROOT)} ({year_count} 个年代文件)")


def _verify_all_outputs() -> list[str]:
    missing: list[str] = []
    for path in (
        CATALOG_FILE,
        EVENTS_FILE,
        NAMES_FILE,
        REGIMES_FILE,
        SNAPSHOTS_META_FILE,
        WORLD_LAND_FILE,
        WORLD_COUNTRIES_FILE,
        WORLD_LABELS_FILE,
    ):
        if not path.is_file():
            missing.append(str(path.relative_to(ROOT)))
    if not is_app_data_ready():
        missing.append("data/maps/*.json（版图快照不完整）")
    return missing


def run_prepare(
    *,
    force: bool,
    skip_download: bool,
    skip_assets: bool,
    skip_wikidata: bool,
) -> int:
    print("历史版图 — 数据初始化")
    print(f"项目目录: {ROOT}")

    try:
        ensure_source_data()
    except Exception as exc:  # noqa: BLE001
        print(f"失败: {exc}", file=sys.stderr)
        return 1

    _print_data_scan()

    steps: list[tuple[str, Callable[[], None]]] = []

    rebuild_meta = _needs_metadata_rebuild(force=force)
    need_wikidata = _needs_wikidata(force=force, skip_wikidata=skip_wikidata)
    need_geojson = (not skip_download) and (
        force or (not is_app_data_ready() and _missing_geojson_count() > 0)
    )

    if rebuild_meta and skip_download and not _has_raw_geojson() and not is_app_data_ready():
        print(
            "缺少历史版图 GeoJSON，无法生成政权元数据。请去掉 --skip-download 后重试。",
            file=sys.stderr,
        )
        return 1

    need_assets = (not skip_assets) and (force or needs_offline_assets())
    need_maps = _needs_maps_rebuild(force=force, rebuild_meta=rebuild_meta or need_wikidata)
    need_labels = force or not WORLD_LABELS_FILE.is_file() or rebuild_meta or need_wikidata

    if need_assets:
        steps.append(("下载离线地图资源（Leaflet + 底图）", download_offline_assets))
    if need_geojson:
        steps.append(("下载历史政权版图 GeoJSON", _download_geojson))
    if need_wikidata:
        steps.append(("从 Wikidata 补全元数据", enrich_from_wikidata))
        rebuild_meta = True
    if rebuild_meta:
        steps.extend(
            [
                ("生成政权精选数据", build_regimes_curated),
                ("推断政权年代范围", build_regime_periods),
                ("合并政权数据库", build_regimes_database),
                ("生成中文译名表", build_names),
            ]
        )
    elif not NAMES_FILE.is_file():
        steps.append(("生成中文译名表", build_names))
    if need_maps:
        steps.append(("构建版图快照", lambda: _build_cache(force=force or rebuild_meta)))
    if need_labels:
        steps.append(("生成底图中文标注", build_world_labels))
    if BUILD_GEOJSON_DIR.exists() or (ROOT / "data" / "build").exists():
        steps.append(("清理构建中间数据", cleanup_build))

    if not steps:
        print("\n全部数据已齐全，无需处理。")
    else:
        print(f"\n待处理 {len(steps)} 项：")
        for title, _ in steps:
            print(f"  · {title}")

    total = len(steps)
    for index, (title, action) in enumerate(steps, start=1):
        _step_header(index, total, title)
        try:
            action()
        except Exception as exc:  # noqa: BLE001
            print(f"\n失败: {exc}", file=sys.stderr)
            return 1

    clear_data_caches()
    missing = _verify_all_outputs()
    print("\n" + "=" * 48)
    if not missing and is_app_data_ready():
        print("全部完成，可以启动应用:")
        print("  python main.py")
        return 0

    if missing:
        print("以下文件缺失或不完整:", file=sys.stderr)
        for item in missing:
            print(f"  - {item}", file=sys.stderr)
    else:
        print("处理结束，但版图快照校验未通过。", file=sys.stderr)
    return 1


def _notify(
    cb: ProgressCallback | None,
    step: str,
    step_i: int,
    step_n: int,
    item_i: int = 0,
    item_n: int = 0,
) -> None:
    if cb:
        cb(step, step_i, step_n, item_i, item_n)


def download_geojson_snapshots(
    *,
    force: bool = False,
    on_progress: ProgressCallback | None = None,
) -> int:
    entries = _catalog_entries()
    total = len(entries)
    for i, entry in enumerate(entries, 1):
        filename = entry["filename"]
        year = entry["year"]
        path = BUILD_GEOJSON_DIR / filename
        if force and path.exists():
            path.unlink()
        ensure_geojson(filename)
        _notify(on_progress, f"下载版图 {year} 年", 0, total, i, total)
    return total


def run_update(
    *,
    include_wikidata: bool = True,
    on_progress: ProgressCallback | None = None,
) -> None:
    """应用内增量更新。"""
    ensure_source_data()
    if _missing_geojson_count() > 0:
        _download_geojson()

    steps: list[tuple[str, str | None]] = []
    if include_wikidata:
        steps.append(("从 Wikidata 补全元数据", "__wikidata__"))
    steps.extend(
        [
            ("更新政权年代范围", "__regime_periods__"),
            ("合并多源数据库", "__regimes_database__"),
            ("更新中文译名", "__names__"),
            ("重建版图快照", "__cache__"),
        ]
    )

    total = len(steps)
    for i, (label, step_id) in enumerate(steps):
        _notify(on_progress, label, i, total, 0, 0)
        if step_id == "__wikidata__":

            def on_batch(current: int, batch_total: int, message: str) -> None:
                _notify(on_progress, message, i, total, current, batch_total)

            enrich_from_wikidata(on_batch_progress=on_batch)
        elif step_id == "__regime_periods__":
            build_regimes_curated()
            build_regime_periods()
        elif step_id == "__regimes_database__":
            build_regimes_database()
        elif step_id == "__names__":
            build_names()
        elif step_id == "__cache__":
            _notify(on_progress, "重建版图快照", i, total, 0, 0)
            build_prepared_cache(force=True)

    _notify(on_progress, "完成", total, total, 0, 0)
    cleanup_build()
    clear_data_caches()


def run_download(
    *,
    force: bool = True,
    include_wikidata: bool = True,
    on_progress: ProgressCallback | None = None,
) -> dict[str, int | bool]:
    geojson_count = download_geojson_snapshots(force=force, on_progress=on_progress)
    run_update(include_wikidata=include_wikidata, on_progress=on_progress)
    return {"geojson_snapshots": geojson_count, "wikidata": include_wikidata}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="检查并补全 data/ 数据（缺失项自动下载/生成，需联网）"
    )
    parser.add_argument("--force", action="store_true", help="强制重建全部数据")
    parser.add_argument("--skip-download", action="store_true", help="跳过版图 GeoJSON 下载")
    parser.add_argument("--skip-assets", action="store_true", help="跳过 Leaflet / 底图下载")
    parser.add_argument(
        "--skip-wikidata",
        action="store_true",
        help="跳过 Wikidata（元数据较简略，不推荐）",
    )
    args = parser.parse_args()
    return run_prepare(
        force=args.force,
        skip_download=args.skip_download,
        skip_assets=args.skip_assets,
        skip_wikidata=args.skip_wikidata,
    )

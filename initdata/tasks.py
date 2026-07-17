"""数据准备任务：下载、构建、验证。"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import defaultdict
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.data import (
    MANUAL_NAMES,
    SnapshotStore,
    _strip_latin,
    _translate_auto,
    format_snapshot_year,
    get_regime_info,
    is_app_data_ready,
    is_political_entity,
    translate,
)
from app.paths import (
    BASEMAP_DIR,
    BUILD,
    BUILD_CURATED_FILE,
    BUILD_GEOJSON_DIR,
    BUILD_MAP_YEARS_FILE,
    BUILD_WIKIDATA_FILE,
    CATALOG_FILE,
    CLIOPATRIA_ZIP,
    DATA,
    LEAFLET_DIR,
    EVENTS_FILE,
    MAPS_DIR,
    NAMES_FILE,
    REGIMES_FILE,
    ROOT,
    SNAPSHOTS_META_FILE,
    WORLD_COUNTRIES_FILE,
    WORLD_LABELS_FILE,
    WORLD_LAND_FILE,
    year_snapshot_path,
)

ProgressCallback = Callable[[str, int, int], None]
BatchProgressCallback = Callable[[int, int, str], None]

DEFAULTS_DIR = Path(__file__).resolve().parent / "defaults"


def ensure_catalog() -> None:
    """确保 data/index/catalog.json 存在（缺失时从 initdata/defaults 恢复）。"""
    if CATALOG_FILE.is_file():
        return
    default = DEFAULTS_DIR / "catalog.json"
    if not default.is_file():
        raise FileNotFoundError(f"缺少内置默认文件 {default.relative_to(ROOT)}")
    CATALOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CATALOG_FILE.write_bytes(default.read_bytes())
    print(f"  已恢复 {CATALOG_FILE.relative_to(ROOT)}")


def ensure_events() -> None:
    """确保 data/regimes/events.json 存在（缺失时从 initdata/defaults 恢复）。"""
    if EVENTS_FILE.is_file():
        return
    default = DEFAULTS_DIR / "events.json"
    if not default.is_file():
        raise FileNotFoundError(f"缺少内置默认文件 {default.relative_to(ROOT)}")
    events = json.loads(default.read_text(encoding="utf-8"))
    if REGIMES_FILE.is_file():
        payload = json.loads(REGIMES_FILE.read_text(encoding="utf-8"))
        regime_names = set(payload.get("regimes", {}))
        if regime_names:
            from app.events_util import normalize_events

            events = normalize_events(events, regime_names)
    EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    EVENTS_FILE.write_text(
        json.dumps(events, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"  已恢复 {EVENTS_FILE.relative_to(ROOT)}")


def ensure_source_data() -> None:
    """补全 catalog、events 等源数据。"""
    ensure_catalog()
    ensure_events()


def _ensure_build_dir() -> None:
    BUILD.mkdir(parents=True, exist_ok=True)


def cleanup_build() -> None:
    """构建完成后删除 data/build/（应用运行时不需要）。"""
    import shutil

    if not BUILD.exists():
        return
    shutil.rmtree(BUILD)
    print(f"已清理 {BUILD.relative_to(ROOT)}（构建中间数据，应用不需要）")

# ============================================================================
# 下载离线地图资源
# ============================================================================
"""下载离线地图资源（Leaflet + 世界底图）。"""


ASSETS = {
    "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css": LEAFLET_DIR / "leaflet.css",
    "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js": LEAFLET_DIR / "leaflet.js",
    "https://unpkg.com/leaflet@1.9.4/dist/images/marker-icon.png": LEAFLET_DIR / "images" / "marker-icon.png",
    "https://unpkg.com/leaflet@1.9.4/dist/images/marker-icon-2x.png": LEAFLET_DIR / "images" / "marker-icon-2x.png",
    "https://unpkg.com/leaflet@1.9.4/dist/images/marker-shadow.png": LEAFLET_DIR / "images" / "marker-shadow.png",
    "https://unpkg.com/leaflet@1.9.4/dist/images/layers.png": LEAFLET_DIR / "images" / "layers.png",
    "https://unpkg.com/leaflet@1.9.4/dist/images/layers-2x.png": LEAFLET_DIR / "images" / "layers-2x.png",
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_110m_land.geojson": WORLD_LAND_FILE,
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_110m_admin_0_countries.geojson": WORLD_COUNTRIES_FILE,
}


def needs_offline_assets() -> bool:
    """检查 Leaflet 与底图文件是否齐全。"""
    return any(not path.is_file() for path in ASSETS.values())


def _http_with_retry(
    req: urllib.request.Request,
    *,
    timeout: int = 120,
    max_retries: int = 5,
) -> bytes:
    delay = 2.0
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 503) and attempt < max_retries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            raise
        except (urllib.error.URLError, OSError):
            if attempt < max_retries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            raise
    raise RuntimeError("unreachable")


def download_offline_assets() -> None:
    BASEMAP_DIR.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": "HistoryMap/2.1"}

    for url, path in ASSETS.items():
        if path.is_file() and path.stat().st_size > 0:
            print(f"已存在 {path.relative_to(ROOT)} ({path.stat().st_size // 1024} KB)")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        request = urllib.request.Request(url, headers=headers)
        path.write_bytes(_http_with_retry(request))
        print(f"已下载 {path.relative_to(ROOT)} ({path.stat().st_size // 1024} KB)")

    print("离线地图资源下载完成。")

# ============================================================================
# 下载 historical-basemaps 版图快照
# ============================================================================
"""下载 historical-basemaps 全部版图快照到本地。"""


def download_geojson() -> None:
    with CATALOG_FILE.open(encoding="utf-8") as f:
        entries = json.load(f)["years"]

    total = len(entries)
    print(f"准备下载 {total} 个历史版图快照…")

    for i, entry in enumerate(entries, 1):
        filename = entry["filename"]
        year = entry["year"]
        path = ensure_geojson(filename)
        size_kb = path.stat().st_size / 1024
        print(f"[{i}/{total}] {year} 年  {filename}  ({size_kb:.0f} KB)")

    print("全部下载完成。")

# ============================================================================
# 下载 Cliopatria 数据集
# ============================================================================
"""下载 Cliopatria 学术历史政区数据集（Seshat，CC BY 4.0）。

Cliopatria 覆盖公元前 3400 年至 2024 年，含 1800+ 政体，
可作为 historical-basemaps 的补充与交叉验证来源。

运行：python scripts/download_cliopatria.py
"""


URL = (
    "https://raw.githubusercontent.com/Seshat-Global-History-Databank/"
    "cliopatria/main/cliopatria.geojson.zip"
)

def download_cliopatria() -> None:
    if CLIOPATRIA_ZIP.exists():
        print(f"已存在，跳过：{CLIOPATRIA_ZIP} ({CLIOPATRIA_ZIP.stat().st_size // 1024 // 1024} MB)")
        return

    CLIOPATRIA_ZIP.parent.mkdir(parents=True, exist_ok=True)
    print(f"正在下载 Cliopatria…\n  {URL}")
    req = urllib.request.Request(URL, headers={"User-Agent": "HistoryMap/2.0"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        CLIOPATRIA_ZIP.write_bytes(resp.read())
    print(f"已保存 {CLIOPATRIA_ZIP.relative_to(ROOT)} ({CLIOPATRIA_ZIP.stat().st_size // 1024 // 1024} MB)")

# ============================================================================
# 构建版图快照
# ============================================================================
ProgressCallback = Callable[[str, int, int], None]

GEOJSON_BASE_URL = (
    "https://raw.githubusercontent.com/aourednik/historical-basemaps/master/geojson/"
)
GEOJSON_CDN_URL = (
    "https://cdn.jsdelivr.net/gh/aourednik/historical-basemaps@master/geojson/"
)


@lru_cache(maxsize=1)
def _load_index() -> list[dict[str, Any]]:
    with CATALOG_FILE.open(encoding="utf-8") as f:
        return json.load(f)["years"]


def _download_file(url: str, path: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "HistoryMap/2.0"})
    path.write_bytes(_http_with_retry(request))


def ensure_geojson(filename: str) -> Path:
    """确保本地存在 GeoJSON 文件，必要时从 GitHub 下载到 data/build/geojson/。"""
    BUILD_GEOJSON_DIR.mkdir(parents=True, exist_ok=True)
    path = BUILD_GEOJSON_DIR / filename
    if path.exists():
        return path

    errors: list[str] = []
    for base_url in (GEOJSON_BASE_URL, GEOJSON_CDN_URL):
        try:
            _download_file(base_url + filename, path)
            return path
        except OSError as exc:
            errors.append(str(exc))

    raise FileNotFoundError(f"无法下载版图数据 {filename}：{'；'.join(errors)}")


def _feature_id(name: str, index: int) -> str:
    slug = "".join(c if c.isalnum() else "_" for c in name.lower()).strip("_")
    return f"{slug}_{index}"


def _normalize_feature(
    feature: dict[str, Any], snapshot_year: int, index: int
) -> dict[str, Any] | None:
    props = feature.get("properties") or {}
    name = (props.get("NAME") or "").strip()
    if not name or name == " ":
        return None
    if not is_political_entity(name):
        return None

    name_zh = translate(name)
    feature_id = _feature_id(name, index)
    part_of = (props.get("PARTOF") or "").strip()
    subject = (props.get("SUBJECTO") or "").strip()
    part_of_zh = translate(part_of) if part_of else ""
    subject_zh = translate(subject) if subject else ""

    info = get_regime_info(name, subject)

    return {
        "type": "Feature",
        "properties": {
            "id": feature_id,
            "name_en": name,
            "name_zh": name_zh,
            "snapshot_year": snapshot_year,
            "part_of_zh": part_of_zh,
            "subject_zh": subject_zh,
            "period_zh": info["period_zh"],
            "ethnicity_zh": info["ethnicity_zh"],
            "rulers_zh": info["rulers_zh"],
        },
        "geometry": feature["geometry"],
    }


def _compute_snapshot_collection(snapshot_year: int, filename: str) -> dict[str, Any]:
    path = ensure_geojson(filename)
    with path.open(encoding="utf-8") as f:
        raw = json.load(f)

    features: list[dict[str, Any]] = []
    for index, feature in enumerate(raw.get("features", [])):
        normalized = _normalize_feature(feature, snapshot_year, index)
        if normalized is not None:
            features.append(normalized)

    features.sort(key=lambda f: f["properties"]["name_zh"])

    from app.regime_colors import assign_regime_colors

    colors = assign_regime_colors(features)
    for feature in features:
        name_en = feature["properties"]["name_en"]
        if name_en in colors:
            feature["properties"]["color"] = colors[name_en]

    return {
        "type": "FeatureCollection",
        "snapshot_year": snapshot_year,
        "requested_year": snapshot_year,
        "features": features,
    }


def build_prepared_cache(
    *,
    force: bool = False,
    on_progress: ProgressCallback | None = None,
) -> None:
    """从 data/build/geojson/ 构建 data/maps/ 下的最终快照。"""
    MAPS_DIR.mkdir(parents=True, exist_ok=True)

    entries = _load_index()
    total = len(entries)
    manifest_years: list[dict[str, Any]] = []

    for index, entry in enumerate(entries, start=1):
        year = entry["year"]
        filename = entry["filename"]
        if on_progress:
            on_progress(
                f"处理版图快照 {format_snapshot_year(year)}",
                index,
                total,
            )
        out = year_snapshot_path(year)
        if not force and out.exists():
            collection = json.loads(out.read_text(encoding="utf-8"))
        else:
            collection = _compute_snapshot_collection(year, filename)
            out.write_text(
                json.dumps(collection, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
        manifest_years.append(
            {
                "year": year,
                "file": out.name,
                "feature_count": len(collection.get("features", [])),
            }
        )

    SNAPSHOTS_META_FILE.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOTS_META_FILE.write_text(
        json.dumps({"version": 1, "years": manifest_years}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

# ============================================================================
# 生成中文译名表
# ============================================================================
"""生成全部政体中文译名表。"""


def _auto_translate(name: str) -> str:
    return _translate_auto(name)


def build_names() -> None:
    with CATALOG_FILE.open(encoding="utf-8") as f:
        index = json.load(f)

    all_names: set[str] = set()
    for entry in index["years"]:
        for country in entry["countries"]:
            country = country.strip()
            if country and country != " ":
                all_names.add(country)

    wikidata: dict[str, dict] = {}
    if BUILD_WIKIDATA_FILE.exists():
        wikidata = json.loads(BUILD_WIKIDATA_FILE.read_text(encoding="utf-8"))

    result: dict[str, str] = {}
    for name in sorted(all_names):
        if name in MANUAL_NAMES:
            result[name] = MANUAL_NAMES[name]
        elif (wd := wikidata.get(name, {})).get("name_zh"):
            result[name] = wd["name_zh"]
        else:
            result[name] = _strip_latin(_auto_translate(name))

    NAMES_FILE.parent.mkdir(parents=True, exist_ok=True)
    NAMES_FILE.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    manual = sum(1 for n in all_names if n in MANUAL_NAMES)
    wd_count = sum(1 for n in all_names if n not in MANUAL_NAMES and wikidata.get(n, {}).get("name_zh"))
    print(
        f"已写入 {len(result)} 条译名"
        f"（手工 {manual}，Wikidata {wd_count}，规则 {len(result) - manual - wd_count}）"
        f" -> {NAMES_FILE}"
    )

# ============================================================================
# 扫描版图数据生成政权年代范围
# ============================================================================
"""扫描版图数据，生成各政权在地图数据中的出现年代范围。"""


def parse_year(filename: str) -> int:
    stem = filename.replace("world_", "").replace(".geojson", "")
    if stem.startswith("bc"):
        return -int(stem[2:])
    return int(stem)


def build_regime_periods() -> None:
    _ensure_build_dir()
    years_by_name: dict[str, set[int]] = defaultdict(set)
    geojson_paths = (
        list(BUILD_GEOJSON_DIR.glob("world_*.geojson")) if BUILD_GEOJSON_DIR.is_dir() else []
    )
    if geojson_paths:
        for path in geojson_paths:
            year = parse_year(path.name)
            data = json.loads(path.read_text(encoding="utf-8"))
            for feature in data.get("features", []):
                name = ((feature.get("properties") or {}).get("NAME") or "").strip()
                if name and is_political_entity(name):
                    years_by_name[name].add(year)
    elif MAPS_DIR.is_dir():
        for path in MAPS_DIR.glob("*.json"):
            year = int(path.stem)
            data = json.loads(path.read_text(encoding="utf-8"))
            for feature in data.get("features", []):
                props = feature.get("properties") or {}
                name = (props.get("name_en") or "").strip()
                if name and is_political_entity(name):
                    years_by_name[name].add(year)

    result = {
        name: {"start_year": min(years), "end_year": max(years)}
        for name, years in sorted(years_by_name.items())
    }
    BUILD_MAP_YEARS_FILE.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {len(result)} entries to {BUILD_MAP_YEARS_FILE}")

# ============================================================================
# 生成政权精选百科数据
# ============================================================================
"""生成政权精选百科数据 regimes_curated.json。"""


# 精选政权：历史存在年代、主体族群、著名君主
CURATED: dict[str, dict] = {
    "Han": {
        "period": "公元前206年至公元220年",
        "ethnicity": "汉族（华夏）",
        "rulers": ["汉高祖刘邦", "汉武帝刘彻", "光武帝刘秀"],
    },
    "Roman Empire": {
        "period": "公元前27年至公元476年（西罗马）",
        "ethnicity": "罗马人",
        "rulers": ["奥古斯都", "图拉真", "马可·奥勒留", "君士坦丁大帝"],
    },
    "Roman Republic": {
        "period": "公元前509年至公元前27年",
        "ethnicity": "罗马人",
        "rulers": ["凯撒", "庞培", "苏拉"],
    },
    "Parthian Empire": {
        "period": "公元前247年至公元224年",
        "ethnicity": "帕提亚人",
        "rulers": ["阿萨息斯一世", "米特拉达梯一世", "阿尔达班四世"],
    },
    "Sasanian Empire": {
        "period": "公元224年至651年",
        "ethnicity": "波斯人",
        "rulers": ["阿尔达希尔一世", "沙普尔一世", "库斯老一世"],
    },
    "Kushan Empire": {
        "period": "约公元1世纪至375年",
        "ethnicity": "月氏人（贵霜）",
        "rulers": ["丘就却", "阎膏珍", "迦腻色伽大帝"],
    },
    "Achaemenid Empire": {
        "period": "公元前550年至公元前330年",
        "ethnicity": "波斯人",
        "rulers": ["居鲁士大帝", "大流士一世", "薛西斯一世"],
    },
    "Seleucid Empire": {
        "period": "公元前312年至公元前63年",
        "ethnicity": "希腊化马其顿统治者",
        "rulers": ["塞琉古一世", "安条克三世"],
    },
    "Macedonian Empire": {
        "period": "公元前336年至公元前323年",
        "ethnicity": "马其顿希腊人",
        "rulers": ["亚历山大大帝"],
    },
    "Byzantine Empire": {
        "period": "公元330年至1453年",
        "ethnicity": "希腊化罗马人",
        "rulers": ["查士丁尼一世", "巴西尔二世", "君士坦丁十一世"],
    },
    "Ottoman Empire": {
        "period": "公元1299年至1922年",
        "ethnicity": "突厥人",
        "rulers": ["奥斯曼一世", "苏莱曼大帝", "穆罕默德二世"],
    },
    "Abbasid Caliphate": {
        "period": "公元750年至1258年",
        "ethnicity": "阿拉伯人",
        "rulers": ["阿布·阿拔斯", "哈伦·拉希德", "马蒙"],
    },
    "Umayyad Caliphate": {
        "period": "公元661年至750年",
        "ethnicity": "阿拉伯人",
        "rulers": ["穆阿维叶一世", "阿卜杜勒·马利克", "瓦利德一世"],
    },
    "Rashidun Caliphate": {
        "period": "公元632年至661年",
        "ethnicity": "阿拉伯人",
        "rulers": ["阿布·伯克尔", "欧麦尔", "奥斯曼", "阿里"],
    },
    "Tang Empire": {
        "period": "公元618年至907年",
        "ethnicity": "汉族（华夏）",
        "rulers": ["唐太宗李世民", "武则天", "唐玄宗李隆基"],
    },
    "Song Empire": {
        "period": "公元960年至1279年",
        "ethnicity": "汉族（华夏）",
        "rulers": ["宋太祖赵匡胤", "宋仁宗", "宋徽宗"],
    },
    "Ming Empire": {
        "period": "公元1368年至1644年",
        "ethnicity": "汉族（华夏）",
        "rulers": ["明太祖朱元璋", "明成祖朱棣", "崇祯帝"],
    },
    "Ming Chinese Empire": {
        "period": "公元1368年至1644年",
        "ethnicity": "汉族（华夏）",
        "rulers": ["明太祖朱元璋", "明成祖朱棣", "崇祯帝"],
    },
    "Qing Empire": {
        "period": "公元1636年至1912年",
        "ethnicity": "满族",
        "rulers": ["康熙帝", "乾隆帝", "慈禧太后"],
    },
    "Yuan Empire": {
        "period": "公元1271年至1368年",
        "ethnicity": "蒙古人",
        "rulers": ["元世祖忽必烈", "元成宗"],
    },
    "Mongol Empire": {
        "period": "公元1206年至1368年",
        "ethnicity": "蒙古人",
        "rulers": ["成吉思汗", "窝阔台", "忽必烈"],
    },
    "Jin": {
        "period": "公元1115年至1234年",
        "ethnicity": "女真人",
        "rulers": ["金太祖完颜阿骨打", "金世宗"],
    },
    "Liao": {
        "period": "公元916年至1125年",
        "ethnicity": "契丹人",
        "rulers": ["辽太祖耶律阿保机", "辽圣宗"],
    },
    "Xia": {
        "period": "公元1038年至1227年",
        "ethnicity": "党项人",
        "rulers": ["李元昊"],
    },
    "Sui Empire": {
        "period": "公元581年至618年",
        "ethnicity": "汉族（华夏）",
        "rulers": ["隋文帝杨坚", "隋炀帝杨广"],
    },
    "Qin": {
        "period": "公元前221年至公元前206年",
        "ethnicity": "华夏族",
        "rulers": ["秦始皇嬴政", "秦二世胡亥"],
    },
    "Zhou": {
        "period": "约公元前1046年至公元前256年",
        "ethnicity": "华夏族",
        "rulers": ["周武王姬发", "周宣王", "周幽王"],
    },
    "Shang": {
        "period": "约公元前1600年至公元前1046年",
        "ethnicity": "华夏族",
        "rulers": ["商汤", "武丁", "商纣王"],
    },
    "Warring States": {
        "period": "公元前475年至公元前221年",
        "ethnicity": "华夏诸族",
        "rulers": ["秦孝公", "赵武灵王", "齐威王"],
    },
    "Three Kingdoms": {
        "period": "公元220年至280年",
        "ethnicity": "汉族（华夏）",
        "rulers": ["曹操", "刘备", "孙权"],
    },
    "Shu Han": {
        "period": "公元221年至263年",
        "ethnicity": "汉族（华夏）",
        "rulers": ["刘备", "诸葛亮", "刘禅"],
    },
    "Wu": {
        "period": "公元222年至280年",
        "ethnicity": "汉族（华夏）",
        "rulers": ["孙权", "孙皓"],
    },
    "Wei": {
        "period": "公元220年至266年",
        "ethnicity": "汉族（华夏）",
        "rulers": ["曹丕", "司马懿", "司马炎"],
    },
    "Armenia": {
        "period": "约公元前6世纪至公元428年",
        "ethnicity": "亚美尼亚人",
        "rulers": ["提格兰大帝", "梯里达底三世"],
    },
    "Axum": {
        "period": "约公元1世纪至10世纪",
        "ethnicity": "阿克苏姆人（埃塞俄比亚）",
        "rulers": ["埃扎纳", "卡列布"],
    },
    "Meroe": {
        "period": "约公元前8世纪至公元4世纪",
        "ethnicity": "努比亚人",
        "rulers": ["皮安基", "塔哈尔卡"],
    },
    "Egypt": {
        "period": "约公元前3100年至公元前30年",
        "ethnicity": "古埃及人",
        "rulers": ["拉美西斯二世", "图坦卡蒙", "克娄巴特拉七世"],
    },
    "Ptolemaic Kingdom": {
        "period": "公元前305年至公元前30年",
        "ethnicity": "希腊化统治者",
        "rulers": ["托勒密一世", "克娄巴特拉七世"],
    },
    "Seleucid kingdom": {
        "period": "公元前312年至公元前63年",
        "ethnicity": "希腊化马其顿统治者",
        "rulers": ["塞琉古一世", "安条克三世"],
    },
    "Babylonia": {
        "period": "约公元前1894年至公元前539年",
        "ethnicity": "巴比伦人",
        "rulers": ["汉谟拉比", "尼布甲尼撒二世"],
    },
    "Assyria": {
        "period": "约公元前2500年至公元前609年",
        "ethnicity": "亚述人",
        "rulers": ["亚述纳西帕尔二世", "萨尔贡二世", "阿淑尔巴尼拔"],
    },
    "Hittite Empire": {
        "period": "约公元前1600年至公元前1178年",
        "ethnicity": "赫梯人",
        "rulers": ["苏皮卢利乌玛一世", "穆瓦塔里二世"],
    },
    "Phoenicia": {
        "period": "约公元前1500年至公元前332年",
        "ethnicity": "腓尼基人",
        "rulers": ["希拉姆一世"],
    },
    "Carthage": {
        "period": "约公元前814年至公元前146年",
        "ethnicity": "腓尼基-布匿人",
        "rulers": ["汉尼拔", "哈斯德鲁巴"],
    },
    "Greek city-states": {
        "period": "约公元前8世纪至公元前146年",
        "ethnicity": "希腊人",
        "rulers": ["伯里克利", "亚历山大大帝", "列奥尼达"],
    },
    "Sparta": {
        "period": "约公元前11世纪至公元前192年",
        "ethnicity": "多里安希腊人",
        "rulers": ["莱奥尼达斯", "阿吉斯四世"],
    },
    "Athens": {
        "period": "约公元前8世纪至公元前146年",
        "ethnicity": "爱奥尼亚希腊人",
        "rulers": ["伯里克利", "地米斯托克利"],
    },
    "Macedon": {
        "period": "约公元前808年至公元前168年",
        "ethnicity": "马其顿希腊人",
        "rulers": ["腓力二世", "亚历山大大帝"],
    },
    "Silla": {
        "period": "公元前57年至公元935年",
        "ethnicity": "新罗人（朝鲜族）",
        "rulers": ["朴赫居世", "真德女王", "弓裔"],
    },
    "Koguryo": {
        "period": "公元前37年至公元668年",
        "ethnicity": "高句丽人",
        "rulers": ["朱蒙", "广开土王", "婴阳王"],
    },
    "Baekje": {
        "period": "公元前18年至公元660年",
        "ethnicity": "百济人",
        "rulers": ["温祚王", "武宁王", "义慈王"],
    },
    "Gaya": {
        "period": "公元42年至562年",
        "ethnicity": "伽倻人",
        "rulers": ["首露王"],
    },
    "Goguryeo": {
        "period": "公元前37年至公元668年",
        "ethnicity": "高句丽人",
        "rulers": ["朱蒙", "广开土王"],
    },
    "Japan": {
        "period": "约公元前660年至今（天皇世系）",
        "ethnicity": "大和民族",
        "rulers": ["圣德太子", "织田信长", "德川家康"],
    },
    "Yamato": {
        "period": "约公元250年至710年",
        "ethnicity": "大和民族",
        "rulers": ["推古天皇", "圣德太子"],
    },
    "Vietnam": {
        "period": "公元938年至今（独立王朝）",
        "ethnicity": "京族（越族）",
        "rulers": ["李太祖", "黎圣宗", "阮惠"],
    },
    "Annam": {
        "period": "公元679年至公元10世纪",
        "ethnicity": "越族",
        "rulers": ["梅叔鸾"],
    },
    "Champa": {
        "period": "公元192年至1832年",
        "ethnicity": "占族人",
        "rulers": ["因陀罗跋摩"],
    },
    "Khmer Empire": {
        "period": "公元802年至1431年",
        "ethnicity": "高棉人",
        "rulers": ["阇耶跋摩二世", "苏利耶跋摩二世"],
    },
    "Srivijaya": {
        "period": "约公元7世纪至13世纪",
        "ethnicity": "马来人",
        "rulers": ["三佛齐诸王"],
    },
    "Majapahit": {
        "period": "公元1293年至1527年",
        "ethnicity": "爪哇人",
        "rulers": ["拉查萨纳加拉", "哈扬·乌鲁克"],
    },
    "Delhi Sultanate": {
        "period": "公元1206年至1526年",
        "ethnicity": "突厥-阿富汗穆斯林统治者",
        "rulers": ["伊勒图特米什", "阿拉乌丁·哈勒吉", "巴布尔"],
    },
    "Mughal Empire": {
        "period": "公元1526年至1857年",
        "ethnicity": "突厥-蒙古穆斯林统治者",
        "rulers": ["巴布尔", "阿克巴", "奥朗则布"],
    },
    "Maratha Empire": {
        "period": "公元1674年至1818年",
        "ethnicity": "马拉地人",
        "rulers": ["希瓦吉", "巴吉拉奥一世"],
    },
    "Maurya Empire": {
        "period": "公元前322年至公元前185年",
        "ethnicity": "印度人",
        "rulers": ["旃陀罗笈多", "阿育王"],
    },
    "Gupta Empire": {
        "period": "约公元320年至550年",
        "ethnicity": "印度人",
        "rulers": ["旃陀罗笈多一世", "沙摩陀罗笈多", "旃陀罗笈多二世"],
    },
    "Hindu kingdoms": {
        "period": "公元前后至约公元700年",
        "ethnicity": "印度诸民族",
        "rulers": ["各地印度教诸王"],
    },
    "Harsha's Empire": {
        "period": "公元606年至647年",
        "ethnicity": "印度人",
        "rulers": ["戒日王"],
    },
    "Chola Empire": {
        "period": "约公元848年至1279年",
        "ethnicity": "泰米尔人",
        "rulers": ["拉惹拉贾一世", "拉惹恩德拉一世"],
    },
    "Nabatean Kingdom": {
        "period": "公元前4世纪至公元106年",
        "ethnicity": "纳巴泰阿拉伯人",
        "rulers": ["阿雷特四世"],
    },
    "Himyarite Kingdom": {
        "period": "约公元前110年至公元525年",
        "ethnicity": "南阿拉伯人",
        "rulers": ["沙玛尔·尤哈里什"],
    },
    "Hadramaut": {
        "period": "约公元前8世纪至公元3世纪",
        "ethnicity": "南阿拉伯人",
        "rulers": ["哈德拉毛诸王"],
    },
    "Saba": {
        "period": "约公元前1200年至公元275年",
        "ethnicity": "示巴人（南阿拉伯）",
        "rulers": ["示巴女王"],
    },
    "Palmyrene Empire": {
        "period": "公元270年至273年",
        "ethnicity": "帕尔米拉人",
        "rulers": ["芝诺比娅"],
    },
    "Dacia": {
        "period": "约公元前82年至公元106年",
        "ethnicity": "达契亚人",
        "rulers": ["布雷比斯塔", "德凯巴鲁斯"],
    },
    "Bosporian Kingdom": {
        "period": "公元前438年至公元370年",
        "ethnicity": "希腊-斯基泰混合",
        "rulers": ["阿斯波耳戈斯", "索罗马科斯"],
    },
    "Bosporan Kingdom": {
        "period": "公元前438年至公元370年",
        "ethnicity": "希腊-斯基泰混合",
        "rulers": ["阿斯波耳戈斯"],
    },
    "Alans": {
        "period": "约公元1世纪至13世纪",
        "ethnicity": "阿兰人（萨尔马提亚系）",
        "rulers": ["阿兰诸部首领"],
    },
    "Blemmyes": {
        "period": "约公元前5世纪至公元8世纪",
        "ethnicity": "努比亚部族",
        "rulers": ["暂无确切记载"],
    },
    "Saka Kingdom": {
        "period": "约公元前2世纪至公元4世纪",
        "ethnicity": "塞种人",
        "rulers": ["塞种诸王"],
    },
    "Huns": {
        "period": "约公元370年至469年",
        "ethnicity": "匈人",
        "rulers": ["阿提拉"],
    },
    "Frankish Empire": {
        "period": "公元481年至843年",
        "ethnicity": "法兰克人",
        "rulers": ["克洛维一世", "查理大帝"],
    },
    "Carolingian Empire": {
        "period": "公元800年至888年",
        "ethnicity": "法兰克人",
        "rulers": ["查理大帝", "路易一世"],
    },
    "Holy Roman Empire": {
        "period": "公元962年至1806年",
        "ethnicity": "德意志人",
        "rulers": ["奥托一世", "查理五世", "腓特烈二世"],
    },
    "German Empire": {
        "period": "公元1871年至1918年",
        "ethnicity": "德意志人",
        "rulers": ["威廉一世", "俾斯麦辅政"],
    },
    "Austrian Empire": {
        "period": "公元1804年至1867年",
        "ethnicity": "德意志人",
        "rulers": ["弗朗茨·约瑟夫一世"],
    },
    "Austro-Hungarian Empire": {
        "period": "公元1867年至1918年",
        "ethnicity": "多民族帝国",
        "rulers": ["弗朗茨·约瑟夫一世"],
    },
    "Kingdom of France": {
        "period": "公元843年至1792年",
        "ethnicity": "法兰西人",
        "rulers": ["路易十四", "拿破仑一世"],
    },
    "France": {
        "period": "公元843年至今",
        "ethnicity": "法兰西人",
        "rulers": ["路易十四", "拿破仑一世", "戴高乐"],
    },
    "British Empire": {
        "period": "公元1603年至1997年",
        "ethnicity": "不列颠人",
        "rulers": ["伊丽莎白一世", "维多利亚女王", "丘吉尔"],
    },
    "United Kingdom": {
        "period": "公元1707年至今",
        "ethnicity": "不列颠人",
        "rulers": ["维多利亚女王", "丘吉尔", "伊丽莎白二世"],
    },
    "Spain": {
        "period": "公元1479年至今",
        "ethnicity": "卡斯蒂利亚人",
        "rulers": ["费迪南德与伊莎贝拉", "查理五世", "腓力二世"],
    },
    "Portugal": {
        "period": "公元1139年至1910年",
        "ethnicity": "葡萄牙人",
        "rulers": ["亨利航海王子", "曼努埃尔一世"],
    },
    "Russian Empire": {
        "period": "公元1721年至1917年",
        "ethnicity": "俄罗斯人",
        "rulers": ["彼得大帝", "叶卡捷琳娜二世", "尼古拉二世"],
    },
    "Soviet Union": {
        "period": "公元1922年至1991年",
        "ethnicity": "多民族联盟",
        "rulers": ["列宁", "斯大林", "戈尔巴乔夫"],
    },
    "Poland": {
        "period": "公元966年至今",
        "ethnicity": "波兰人",
        "rulers": ["波列斯瓦夫一世", "卡齐米日三世"],
    },
    "Aztec Empire": {
        "period": "公元1428年至1521年",
        "ethnicity": "纳瓦人",
        "rulers": ["蒙特祖马二世"],
    },
    "Inca Empire": {
        "period": "公元1438年至1533年",
        "ethnicity": "克丘亚人",
        "rulers": ["帕查库蒂", "阿塔瓦尔帕"],
    },
    "Maya city-states": {
        "period": "约公元前2000年至公元1697年",
        "ethnicity": "玛雅人",
        "rulers": ["帕卡尔大帝", "夸克·斯卡利"],
    },
    "United States": {
        "period": "公元1776年至今",
        "ethnicity": "欧洲移民后裔为主的多民族国家",
        "rulers": ["华盛顿", "林肯", "罗斯福"],
    },
    "Suren Kingdom": {
        "period": "约公元前1世纪至公元3世纪",
        "ethnicity": "苏伦人（波斯贵族）",
        "rulers": ["苏伦家族诸王"],
    },
    "Kalinga": {
        "period": "约公元前4世纪至公元4世纪",
        "ethnicity": "羯陵伽人",
        "rulers": ["羯陵伽诸王"],
    },
    "Hainan": {
        "period": "约公元1世纪至15世纪",
        "ethnicity": "黎族等海南岛原住民",
        "rulers": ["儋耳、珠崖诸县首领"],
    },
    "Almohad Caliphate": {
        "period": "公元1121年至1269年",
        "ethnicity": "柏柏尔人",
        "rulers": ["阿卜杜勒·穆明"],
    },
    "Almoravid dynasty": {
        "period": "公元1040年至1147年",
        "ethnicity": "柏柏尔人",
        "rulers": ["优素福·本·塔什芬"],
    },
    "Fatimid Caliphate": {
        "period": "公元909年至1171年",
        "ethnicity": "阿拉伯人",
        "rulers": ["马赫迪", "哈基姆"],
    },
    "Mamluk Sultanate": {
        "period": "公元1250年至1517年",
        "ethnicity": "马穆鲁克（突厥-切尔克斯武士）",
        "rulers": ["拜伯尔斯", "萨利赫·阿尤布"],
    },
    "Seljuk Empire": {
        "period": "公元1037年至1194年",
        "ethnicity": "突厥人",
        "rulers": ["图格鲁尔贝格", "马利克沙"],
    },
    "Timurid Empire": {
        "period": "公元1370年至1507年",
        "ethnicity": "突厥-蒙古人",
        "rulers": ["帖木儿", "沙哈鲁"],
    },
    "Safavid Empire": {
        "period": "公元1501年至1736年",
        "ethnicity": "波斯人",
        "rulers": ["伊斯玛仪一世", "阿巴斯大帝"],
    },
    "Afghanistan": {
        "period": "公元1747年至今",
        "ethnicity": "普什图人等",
        "rulers": ["艾哈迈德沙·杜兰尼"],
    },
    "Tibet": {
        "period": "公元7世纪至今",
        "ethnicity": "藏族",
        "rulers": ["松赞干布", "达赖喇嘛"],
    },
    "Goryeo": {
        "period": "公元918年至1392年",
        "ethnicity": "高丽族",
        "rulers": ["王建", "恭愍王"],
    },
    "Joseon": {
        "period": "公元1392年至1897年",
        "ethnicity": "朝鲜族",
        "rulers": ["李成桂", "世宗大王", "李祘"],
    },
}


def build_regimes_curated() -> None:
    _ensure_build_dir()
    BUILD_CURATED_FILE.write_text(
        json.dumps(CURATED, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Wrote {len(CURATED)} curated regimes to {BUILD_CURATED_FILE}")

# ============================================================================
# 合并多源政权数据库
# ============================================================================
"""合并多源政权数据，生成 regimes_database.json。

优先级（高 → 低）：
  1. regimes_curated.json（人工精选）
  2. wikidata_regimes.json（Wikidata API）
  3. regime_map_years.json（版图快照推断年代）
  4. Cliopatria 元数据（若已下载）

运行：
  python scripts/enrich_from_wikidata.py
  python scripts/build_regimes_database.py
"""


def _format_period(start: int | None, end: int | None, *, map_only: bool = False) -> str | None:
    if start is None and end is None:
        return None
    if start is not None and end is not None:
        text = f"{format_snapshot_year(start)}至{format_snapshot_year(end)}"
    elif start is not None:
        text = f"{format_snapshot_year(start)}年起"
    else:
        text = f"至{format_snapshot_year(end)}"
    if map_only:
        text += "（版图记载）"
    return text


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _load_cliopatria_years() -> dict[str, dict[str, int]]:
    """从 Cliopatria 提取各政权名称的年代范围（若本地已下载）。"""
    if not CLIOPATRIA_ZIP.exists():
        return {}

    years: dict[str, set[int]] = {}
    try:
        with zipfile.ZipFile(CLIOPATRIA_ZIP) as zf:
            geojson_name = next(n for n in zf.namelist() if n.endswith(".geojson"))
            with zf.open(geojson_name) as f:
                data = json.load(f)
    except (OSError, StopIteration, json.JSONDecodeError):
        return {}

    for feature in data.get("features", []):
        props = feature.get("properties") or {}
        name = (props.get("Name") or props.get("NAME") or "").strip()
        if not name:
            continue
        try:
            start = int(props.get("FromYear", 0))
            end = int(props.get("ToYear", 0))
        except (TypeError, ValueError):
            continue
        bucket = years.setdefault(name, set())
        bucket.add(start)
        bucket.add(end)

    return {
        name: {"start_year": min(vals), "end_year": max(vals)}
        for name, vals in years.items()
    }


def merge_regime(name_en: str, *, curated: dict, wikidata: dict, map_years: dict, clio: dict) -> dict:
    entry: dict = {"name_en": name_en, "sources": []}

    c = curated.get(name_en, {})
    w = wikidata.get(name_en, {})
    m = map_years.get(name_en)
    cl = clio.get(name_en)

    if c:
        entry["sources"].append("curated")
        if c.get("period"):
            entry["period"] = c["period"]
        if c.get("ethnicity"):
            entry["ethnicity"] = c["ethnicity"]
        if c.get("rulers"):
            entry["rulers"] = c["rulers"]

    if w.get("wikidata_id"):
        entry["sources"].append("wikidata")
        entry["wikidata_id"] = w["wikidata_id"]
        if not entry.get("period") and w.get("period"):
            entry["period"] = w["period"]
        if not entry.get("ethnicity") and w.get("ethnicity"):
            entry["ethnicity"] = w["ethnicity"]
        if not entry.get("rulers") and w.get("rulers"):
            entry["rulers"] = w["rulers"]
        if w.get("name_zh"):
            entry["name_zh"] = w["name_zh"]

    if cl and not entry.get("period"):
        entry["sources"].append("cliopatria")
        entry["period"] = _format_period(cl["start_year"], cl["end_year"])

    if m and not entry.get("period"):
        entry["sources"].append("historical-basemaps")
        entry["period"] = _format_period(m["start_year"], m["end_year"], map_only=True)

    if not entry.get("period"):
        entry["period"] = None
    if not entry.get("ethnicity"):
        entry["ethnicity"] = None
    if not entry.get("rulers"):
        entry["rulers"] = []

    return entry


def build_regimes_database() -> None:
    curated = _load_json(BUILD_CURATED_FILE)
    wikidata = _load_json(BUILD_WIKIDATA_FILE)
    map_years = _load_json(BUILD_MAP_YEARS_FILE)
    clio = _load_cliopatria_years()

    all_names: set[str] = set(curated) | set(wikidata) | set(map_years) | set(clio)
    database = {
        name: merge_regime(
            name,
            curated=curated,
            wikidata=wikidata,
            map_years=map_years,
            clio=clio,
        )
        for name in sorted(all_names)
    }

    meta = {
        "sources": [
            "regimes_curated.json (manual)",
            "wikidata_regimes.json (Wikidata CC0)",
            "regime_map_years.json (historical-basemaps inference)",
            "cliopatria.geojson (Seshat, if downloaded)",
        ],
        "total_regimes": len(database),
        "with_period": sum(1 for v in database.values() if v.get("period")),
        "with_ethnicity": sum(1 for v in database.values() if v.get("ethnicity")),
        "with_rulers": sum(1 for v in database.values() if v.get("rulers")),
        "with_wikidata": sum(1 for v in database.values() if "wikidata" in v.get("sources", [])),
    }

    existing_events: dict[str, Any] = {}
    if REGIMES_FILE.exists():
        existing_events = json.loads(REGIMES_FILE.read_text(encoding="utf-8")).get("events", {})
    if EVENTS_FILE.is_file():
        from_file = json.loads(EVENTS_FILE.read_text(encoding="utf-8"))
        for key, items in from_file.items():
            if key not in existing_events or not existing_events[key]:
                existing_events[key] = items

    from app.events_util import normalize_events

    regime_names = set(database.keys())
    existing_events = normalize_events(existing_events, regime_names)
    meta["with_events"] = sum(1 for v in existing_events.values() if v)

    payload = {"_meta": meta, "regimes": database, "events": existing_events}
    REGIMES_FILE.parent.mkdir(parents=True, exist_ok=True)
    REGIMES_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    EVENTS_FILE.write_text(
        json.dumps(existing_events, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"已写入 {REGIMES_FILE}")
    print(json.dumps(meta, ensure_ascii=False, indent=2))

# ============================================================================
# 生成离线底图中文地名标注
# ============================================================================
"""生成离线底图用的中文地名标注（含 LOD 层级）。"""


# tier 1: 全球视图（zoom >= 2）
TIER1 = {
    "China", "Russia", "United States of America", "Brazil", "Australia",
    "India", "Canada", "Argentina", "Kazakhstan", "Algeria",
    "Saudi Arabia", "Mexico", "Indonesia", "Sudan", "Libya", "Iran",
    "Mongolia", "South Africa", "Egypt", "Nigeria", "France", "Germany",
    "Japan", "United Kingdom", "Turkey", "Spain", "Italy", "Poland",
    "Ukraine", "Greenland",
}

# tier 2: 大陆级（zoom >= 3）
TIER2 = {
    "Democratic Republic of the Congo", "Peru", "Chad", "Niger", "Angola",
    "Mali", "Tanzania", "Venezuela", "Pakistan", "Namibia", "Mozambique",
    "Chile", "Zambia", "Myanmar", "Afghanistan", "Somalia", "Madagascar",
    "Botswana", "Kenya", "Yemen", "Thailand", "Turkmenistan", "Cameroon",
    "Papua New Guinea", "Sweden", "Uzbekistan", "Morocco", "Iraq",
    "Paraguay", "Zimbabwe", "Philippines", "Finland", "Malaysia", "Vietnam",
    "Norway", "New Zealand", "Ecuador", "Romania", "Belarus", "Greece",
    "Syria", "Cambodia", "Uruguay", "Tunisia", "Bangladesh", "Nepal",
    "South Korea", "North Korea", "Ethiopia", "Colombia", "Bolivia",
    "Mauritania",
}


def _zh(name: str) -> str:
    return MANUAL_NAMES.get(name, name)


def _centroid(coords) -> list[float] | None:
    if not coords:
        return None
    ring = coords[0] if isinstance(coords[0][0][0], (int, float)) else coords[0][0]
    if not ring:
        return None
    lng = sum(p[0] for p in ring) / len(ring)
    lat = sum(p[1] for p in ring) / len(ring)
    return [lng, lat]


def _tier(name: str) -> int:
    if name in TIER1:
        return 1
    if name in TIER2:
        return 2
    return 3


def build_world_labels() -> None:
    if not WORLD_COUNTRIES_FILE.is_file():
        raise FileNotFoundError(
            f"缺少 {WORLD_COUNTRIES_FILE.relative_to(ROOT)}，请先完成离线地图资源下载"
        )

    with WORLD_COUNTRIES_FILE.open(encoding="utf-8") as f:
        countries = json.load(f)

    features = []
    for feature in countries["features"]:
        name = feature["properties"].get("NAME") or ""
        tier = _tier(name)
        if tier > 3:
            continue

        geom = feature["geometry"]
        coords = geom.get("coordinates")
        if not coords:
            continue
        if geom["type"] == "Polygon":
            center = _centroid(coords)
        elif geom["type"] == "MultiPolygon":
            center = _centroid(coords[0])
        else:
            continue
        if center is None:
            continue

        # tier 1→zoom2, tier 2→zoom3, tier 3→zoom4
        min_zoom = tier + 1
        features.append({
            "type": "Feature",
            "properties": {
                "name_zh": _zh(name),
                "tier": tier,
                "min_zoom": min_zoom,
                "orig_lng": center[0],
            },
            "geometry": {"type": "Point", "coordinates": center},
        })

    WORLD_LABELS_FILE.parent.mkdir(parents=True, exist_ok=True)
    WORLD_LABELS_FILE.write_text(
        json.dumps({"type": "FeatureCollection", "features": features}, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"已写入 {len(features)} 个中文地名标注 -> {WORLD_LABELS_FILE}")

# ============================================================================
# 从 Wikidata 补全政权元数据
# ============================================================================
"""从 Wikidata 批量补全政权元数据（中文名、年代、族群）。

使用 SPARQL 批量查询，比逐个 API 搜索更稳定。
运行：python scripts/enrich_from_wikidata.py
"""


USER_AGENT = "HistoryMap/2.0 (historical map desktop app; academic use)"
SPARQL_URL = "https://query.wikidata.org/sparql"


def _sparql(query: str) -> list[dict]:
    body = urllib.parse.urlencode({"query": query}).encode()
    req = urllib.request.Request(
        SPARQL_URL,
        data=body,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/sparql-results+json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    payload = json.loads(_http_with_retry(req).decode("utf-8"))
    return payload.get("results", {}).get("bindings", [])


def _parse_wikidata_year(time_str: str | None) -> int | None:
    if not time_str:
        return None
    m = re.match(r"([+-])(\d+)", time_str)
    if not m:
        return None
    year = int(m.group(2))
    return -year if m.group(1) == "-" else year


def collect_regime_names() -> list[str]:
    names: set[str] = set()
    geojson_paths = list(BUILD_GEOJSON_DIR.glob("world_*.geojson")) if BUILD_GEOJSON_DIR.is_dir() else []
    if geojson_paths:
        for path in geojson_paths:
            data = json.loads(path.read_text(encoding="utf-8"))
            for feature in data.get("features", []):
                props = feature.get("properties") or {}
                name = (props.get("NAME") or "").strip()
                if name and is_political_entity(name):
                    names.add(name)
    elif MAPS_DIR.is_dir():
        for path in MAPS_DIR.glob("*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            for feature in data.get("features", []):
                props = feature.get("properties") or {}
                name = (props.get("name_en") or "").strip()
                if name and is_political_entity(name):
                    names.add(name)
    return sorted(names)


def _sparql_values(names: list[str]) -> str:
    escaped = []
    for n in names:
        s = n.replace("\\", "\\\\").replace('"', '\\"')
        escaped.append(f'"{s}"@en')
    return " ".join(escaped)


_PREFERRED_TYPES = {
    "Q3024240",  # historical country
    "Q3624078",  # sovereign state
    "Q6256",     # country
    "Q4830453",  # empire
    "Q82794",    # geographic region (fallback)
}


def _fetch_types(qids: list[str]) -> dict[str, set[str]]:
    if not qids:
        return {}
    values = " ".join(f"wd:{qid}" for qid in qids)
    query = f"""
SELECT ?item ?type WHERE {{
  VALUES ?item {{ {values} }}
  ?item wdt:P31 ?type .
}}
"""
    rows = _sparql(query)
    out: dict[str, set[str]] = {}
    for row in rows:
        item = row["item"]["value"].rsplit("/", 1)[-1]
        typ = row["type"]["value"].rsplit("/", 1)[-1]
        out.setdefault(item, set()).add(typ)
    return out


def _pick_best_match(candidates: list[dict], types: dict[str, set[str]]) -> dict:
    def score(entry: dict) -> int:
        qid = entry["wikidata_id"]
        t = types.get(qid, set())
        if t & _PREFERRED_TYPES:
            return 10
        if qid.startswith("Q"):
            return 1
        return 0

    return max(candidates, key=score)


def fetch_batch(names: list[str]) -> dict[str, dict]:
    """SPARQL 批量查询政权年代与中文名。"""
    if not names:
        return {}

    query = f"""
SELECT ?label ?item ?nameZh ?inception ?dissolved ?ethnicityLabel WHERE {{
  VALUES ?label {{ {_sparql_values(names)} }}
  ?item rdfs:label ?label .
  OPTIONAL {{ ?item wdt:P571 ?inception . }}
  OPTIONAL {{ ?item wdt:P576 ?dissolved . }}
  OPTIONAL {{
    ?item rdfs:label ?nameZh .
    FILTER(LANG(?nameZh) = "zh")
  }}
  OPTIONAL {{
    ?item wdt:P172 ?ethnicity .
    ?ethnicity rdfs:label ?ethnicityLabel .
    FILTER(LANG(?ethnicityLabel) = "zh" || LANG(?ethnicityLabel) = "en")
  }}
}}
"""
    rows = _sparql(query)
    grouped: dict[str, list[dict]] = {}

    for row in rows:
        name = row["label"]["value"]
        qid = row["item"]["value"].rsplit("/", 1)[-1]
        entry = {
            "name_en": name,
            "wikidata_id": qid,
            "name_zh": row.get("nameZh", {}).get("value"),
            "start_year": None,
            "end_year": None,
            "ethnicity": None,
            "rulers": [],
            "source": "wikidata",
        }
        for key, field in (("inception", "start_year"), ("dissolved", "end_year")):
            if key in row:
                y = _parse_wikidata_year(row[key]["value"])
                if y is not None:
                    cur = entry[field]
                    if cur is None:
                        entry[field] = y
                    elif field == "start_year":
                        entry[field] = min(cur, y)
                    else:
                        entry[field] = max(cur, y)
        if "ethnicityLabel" in row and not entry["ethnicity"]:
            label = row["ethnicityLabel"]["value"]
            if row["ethnicityLabel"].get("xml:lang") == "zh":
                entry["ethnicity"] = label
        grouped.setdefault(name, []).append(entry)

    all_qids = [e["wikidata_id"] for cands in grouped.values() for e in cands]
    types = _fetch_types(all_qids)

    by_name: dict[str, dict] = {}
    for name, candidates in grouped.items():
        best = _pick_best_match(candidates, types)
        best["period"] = _format_period(best.get("start_year"), best.get("end_year"))
        by_name[name] = best

    return by_name


def enrich_from_wikidata(*, on_batch_progress: Callable[[int, int, str], None] | None = None) -> None:
    names = collect_regime_names()
    result: dict[str, dict] = {}
    batch_size = 30
    batch_total = (len(names) + batch_size - 1) // batch_size

    for i in range(0, len(names), batch_size):
        batch = names[i : i + batch_size]
        batch_no = i // batch_size + 1
        msg = f"Wikidata 查询 {batch_no}/{batch_total}"
        if on_batch_progress:
            on_batch_progress(batch_no, batch_total, msg)
        else:
            print(f"  批次 {batch_no}/{batch_total}: {len(batch)} 条")
        try:
            result.update(fetch_batch(batch))
        except (urllib.error.URLError, urllib.error.HTTPError) as exc:
            if on_batch_progress:
                on_batch_progress(batch_no, batch_total, f"批次 {batch_no} 失败，跳过")
            else:
                print(f"  警告：批次失败 ({exc})，跳过")
        time.sleep(3)

    for name in names:
        if name not in result:
            result[name] = {"name_en": name, "source": "wikidata", "error": "not_found"}

    _ensure_build_dir()
    BUILD_WIKIDATA_FILE.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if not on_batch_progress:
        found = sum(1 for v in result.values() if v.get("wikidata_id"))
        with_period = sum(1 for v in result.values() if v.get("period"))
        with_zh = sum(1 for v in result.values() if v.get("name_zh"))
        print(f"已写入 {BUILD_WIKIDATA_FILE}")
        print(f"  匹配 Wikidata: {found}/{len(names)}")
        print(f"  含年代: {with_period}")
        print(f"  含中文名: {with_zh}")

# ============================================================================
# 验证主窗口地图显示
# ============================================================================
"""验证主窗口地图能显示政权边界。"""


def verify_app() -> int:
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    try:
        from app.ui import MainWindow
    except ImportError:
        from app.main_window import MainWindow

    if not is_app_data_ready():
        print("SKIP: run python initdata.py first")
        return 1

    app = QApplication(sys.argv)
    store = SnapshotStore()
    window = MainWindow(store)
    window.show()

    result = {"layers": 0}

    def finish(layers: int) -> None:
        result["layers"] = layers
        app.quit()

    def read_layers() -> None:
        window._map.page().runJavaScript(
            "territoryLayer ? territoryLayer.getLayers().length : 0",
            lambda value: finish(int(value or 0)),
        )

    def poll(attempt: int = 0) -> None:
        if window._map_ready:
            read_layers()
            return
        if attempt >= 150:
            finish(0)
            return
        QTimer.singleShot(200, lambda: poll(attempt + 1))

    QTimer.singleShot(500, poll)
    app.exec()

    layers = result["layers"]
    if layers > 0:
        print(f"PASS: main window shows {layers} territory layers")
        return 0

    print(f"FAIL: only {layers} territory layers")
    return 1

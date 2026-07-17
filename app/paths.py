"""项目路径：data/ 为应用数据，data/build/ 为 initdata 构建缓存。"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

# 应用运行时数据
INDEX_DIR = DATA / "index"
DICT_DIR = DATA / "dict"
REGIMES_DIR = DATA / "regimes"
MAPS_DIR = DATA / "maps"
BASEMAP_DIR = DATA / "basemap"

CATALOG_FILE = INDEX_DIR / "catalog.json"
SNAPSHOTS_META_FILE = INDEX_DIR / "snapshots.json"
NAMES_FILE = DICT_DIR / "names.json"
REGIMES_FILE = REGIMES_DIR / "regimes.json"
EVENTS_FILE = REGIMES_DIR / "events.json"

WORLD_LAND_FILE = BASEMAP_DIR / "world_land.geojson"
WORLD_COUNTRIES_FILE = BASEMAP_DIR / "world_countries.geojson"
WORLD_LABELS_FILE = BASEMAP_DIR / "world_labels.zh.json"

# 离线前端资源（initdata 下载，应用运行时加载）
STATIC_DIR = DATA / "static"
LEAFLET_DIR = STATIC_DIR / "leaflet"

# initdata 构建缓存（完成后删除，应用不读取）
BUILD = DATA / "build"
BUILD_GEOJSON_DIR = BUILD / "geojson"
BUILD_CURATED_FILE = BUILD / "regimes_curated.json"
BUILD_MAP_YEARS_FILE = BUILD / "regime_map_years.json"
BUILD_WIKIDATA_FILE = BUILD / "wikidata.json"
CLIOPATRIA_ZIP = BUILD / "cliopatria.geojson.zip"


def year_snapshot_path(year: int) -> Path:
    return MAPS_DIR / f"{year}.json"

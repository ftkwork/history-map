# 历史版图

桌面窗口应用，用于浏览历史上各政权的不同时期疆域变化。

## 快速开始

```powershell
pip install PySide6 PySide6-Addons
python initdata.py    # 首次运行，需联网（含 Wikidata）
python main.py
```

删除整个 `data/` 文件夹后，运行 `python initdata.py` 即可完整重建（内置默认 catalog/events 会自动恢复），**无需**在应用内点「更新数据」。已有文件会跳过，只补缺失项。

## 项目结构

```
main.py / initdata.py    入口
data/                    应用数据（initdata 自动生成）
  index/                   年代索引、快照清单
  dict/                    中文译名
  regimes/                 政权元数据 + 大事记
  maps/                    各年代版图快照
  basemap/                 离线底图
  static/leaflet/          离线 Leaflet 库
initdata/
  run.py / tasks.py        数据初始化实现
  defaults/                内置默认（catalog、events，删 data 后用于恢复）
app/                       应用代码
```

`initdata.py` 完成后会删除 `data/build/`（构建缓存）。

## initdata.py 会生成什么

| 输出 | 说明 |
|------|------|
| `data/index/catalog.json` | 年代索引（缺失时从 initdata/defaults 恢复） |
| `data/index/snapshots.json` | 53 个年代快照清单 |
| `data/dict/names.json` | 手工译名 + Wikidata + 规则 |
| `data/regimes/events.json` | 政权大事记（缺失时从 initdata/defaults 恢复） |
| `data/regimes/regimes.json` | 精选 + Wikidata + 版图推断 + 大事记 |
| `data/maps/*.json` | 53 个年代版图快照 |
| `data/basemap/` | 陆地、国界、中文地名 |

## 可选参数

```powershell
python initdata.py --force           # 强制全部重建
python initdata.py --skip-wikidata   # 跳过 Wikidata（离线简版，不推荐）
python initdata.py --skip-download   # 不重新下载 GeoJSON
python initdata.py --skip-assets     # 不下载 Leaflet/底图
```

## 技术栈

Python · PySide6 · Leaflet · Natural Earth

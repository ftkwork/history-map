"""历史版图数据层（过滤、翻译、加载、快照存储）。"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Any

from app.paths import (
    CATALOG_FILE,
    NAMES_FILE,
    REGIMES_FILE,
    SNAPSHOTS_META_FILE,
    year_snapshot_path,
)

# --- filters ---

# 明确属于政权 / 政治体的标记
_POLITICAL = re.compile(
    r"(?i)\b("
    r"empire|kingdom|republic|dynasty|sultanate|caliphate|khanate|"
    r"confederacy|confederation|federation|commonwealth|"
    r"colony|colonies|protectorate|dominion|mandate|occupation|"
    r"warlord|duchy|principality|satrapy|emirate|imamate|"
    r"reich|union|soviet|shogunate|"
    r"city.?states|warring states|three kingdoms|"
    r"company|territory"
    r")\b"
)

# 人种、考古文化、史前人群等非政权标记
_ETHNIC = re.compile(
    r"(?i)\b("
    r"hunter.?gatherers?|hunter.?foragers?|foragers?|"
    r"homo\b|neanderthal|heidelbergensis|"
    r"aboriginal|indigenous|"
    r"\btribes?\b|\bculture\b|farmers?|\bpeoples?\b|"
    r"mesolithic|neolithic|paleolithic|"
    r"pottery|burials?|complex|focus|"
    r"marine mammal|"
    r"speakers|linguistic|"
    r"nomads?(?!\s+empire)"
    r")\b"
)

# 无政治标记词、但历史上确为政权的核心条目
_KNOWN_POLITIES = frozenset({
    "Han", "China", "Rome", "Armenia", "Dacia", "Parthia", "Axum", "Aksum",
    "Kushan Empire", "Sarmatians", "Alans", "Xiongnu", "Goguryeo", "Koguryo",
    "Baekje", "Silla", "Gaya", "Koguryo", "Champa", "Funan", "Chenla",
    "Himyarite Kingdom", "Hadramaut", "Nabataea", "Palmyra", "Judaea",
    "Judea", "Israel", "Phoenicia", "Media", "Lydia", "Elam", "Assyria",
    "Babylonia", "Sumer", "Mitanni", "Urartu", "Egypt", "Nubia", "Kush",
    "Meroe", "Blemmyes", "Numidia", "Mauretania", "Libya", "Carthage",
    "Gauls", "Visigoths", "Ostrogoths", "Vandals", "Huns",
    "Bulgars", "Magyars",
    "Macedon", "Macedonia", "Sparta", "Athens", "Thebes", "Corinth",
    "Persia", "Parthia", "Media", "Bactria", "Sogdiana", "Gandhara",
    "Pandya", "Chola", "Chera", "Kalinga", "Pallava", "Chalukya",
    "Vietnam", "Annam", "Tibet", "Manchuria", "Mongolia", "Korea",
    "Japan", "Taiwan", "Burma", "Thailand", "Cambodia", "Laos",
    "India", "Pakistan", "Afghanistan", "Iran", "Iraq", "Syria",
    "Turkey", "Greece", "Italy", "Spain", "Portugal", "France",
    "Germany", "Poland", "Russia", "Ukraine", "Egypt", "Ethiopia",
    "Morocco", "Algeria", "Tunisia", "Sudan", "Nigeria", "Congo",
    "Mexico", "Brazil", "Argentina", "Canada", "USA", "United States",
    "United Kingdom", "Great Britain", "Netherlands", "Belgium",
    "Switzerland", "Austria", "Sweden", "Norway", "Denmark", "Finland",
    "Qin", "Han", "Jin", "Wei", "Wu", "Shu", "Liao", "Xixia", "Yuan",
    "Ming", "Qing", "Tang", "Song", "Sui", "Zhou", "Shang", "Xia",
    "Hainan", "Yue", "Kashmir", "Nepal", "Bhutan", "Sri Lanka",
    "Indonesia", "Malaysia", "Philippines", "Singapore", "Australia",
    "New Zealand", "Greenland", "Iceland", "Ireland", "Scotland",
    "Wales", "England", "Hungary", "Romania", "Bulgaria", "Serbia",
    "Croatia", "Bosnia", "Albania", "Georgia", "Armenia", "Azerbaijan",
    "Kazakhstan", "Uzbekistan", "Turkmenistan", "Kyrgyzstan", "Tajikistan",
    "Mali", "Songhai", "Ghana", "Zimbabwe", "Zulu", "Ashanti",
    "Ottomans", "Byzantium", "Venice", "Genoa", "Florence", "Milan",
    "Papal States", "Savoy", "Burgundy", "Flanders", "Holland",
    "Prussia", "Bavaria", "Saxony", "Bohemia", "Moravia", "Silesia",
    "Lithuania", "Latvia", "Estonia", "Belarus", "Moldova",
    "Colombia", "Peru", "Chile", "Bolivia", "Venezuela", "Ecuador",
    "Cuba", "Haiti", "Jamaica", "Panama", "Guatemala", "Honduras",
    "Nicaragua", "Costa Rica", "Paraguay", "Uruguay",
    "Saudi Arabia", "Yemen", "Oman", "Kuwait", "Qatar", "Bahrain",
    "UAE", "Jordan", "Lebanon", "Palestine", "Israel",
    "Hindu Kingdoms", "Hindu kingdoms", "Greek city-states",
    "Maya city-states", "Zhou states", "Spring and Autumn states",
    "Warring States", "Three Kingdoms", "Chinese Warlords",
    "Chinese warlords", "Axis Powers", "Allied Powers",
    "Warsaw Pact", "NATO", "Yugoslavia", "Czechoslovakia",
})

# 明确为非政权、应排除的条目
_EXCLUDED = re.compile(
    r"(?i)^("
    r"ainu|jōmon|jomon|khoisan|khoiasan|austronesians|bantu|"
    r"hunters-gatherers|guanches|curonians|dumonii|boihaenum|"
    r"homosapiens|homo erectus|homo heidelbergensis|neanderthal"
    r")$"
)


def is_political_entity(name: str) -> bool:
    """判断 GeoJSON 中的 NAME 是否属于政权而非人种/文化群体。"""
    name = name.strip()
    if not name or name in {"1", " "}:
        return False
    if _EXCLUDED.match(name):
        return False
    if name in _KNOWN_POLITIES:
        return True
    if _POLITICAL.search(name):
        return True
    if _ETHNIC.search(name):
        return False
    # 含括号注释的部落/民族描述
    if re.search(r"(?i)\btribe\b|\bpeople\b|\bpeoples\b", name):
        return False
    return False

# --- manual_names ---

# 手工精校译名


MANUAL_NAMES: dict[str, str] = {
    "Abbasid Caliphate": "阿拔斯哈里发国",
    "Achaemenid Empire": "阿契美尼德帝国（波斯第一帝国）",
    "Afghanistan": "阿富汗",
    "Ainu": "阿伊努人",
    "Aksum": "阿克苏姆",
    "Alans": "阿兰人",
    "Algeria": "阿尔及利亚",
    "Allied Powers": "同盟国",
    "Almohad Caliphate": "阿尔摩哈德王朝",
    "Ancient Egypt": "古埃及",
    "Angevin Empire": "安茹帝国",
    "Arabs": "阿拉伯人",
    "Argentina": "阿根廷",
    "Armenia": "亚美尼亚",
    "Assyria": "亚述",
    "Astrakhan Khanate": "阿斯特拉罕汗国",
    "Australia": "澳大利亚",
    "Austria": "奥地利",
    "Austrian Empire": "奥地利帝国",
    "Austro-Hungarian Empire": "奥匈帝国",
    "Austronesians": "南岛民族",
    "Axis Powers": "轴心国",
    "Axum": "阿克苏姆",
    "Aztec Empire": "阿兹特克帝国",
    "Babylonia": "巴比伦",
    "Bactria": "大夏",
    "Baekje": "百济",
    "Bahmani Kingdom": "巴赫曼尼苏丹国",
    "Bahrain": "巴林",
    "Balhae": "渤海",
    "Bangladesh": "孟加拉国",
    "Bantu": "班图人",
    "Belgian Congo": "比属刚果",
    "Belgium": "比利时",
    "Berbers": "柏柏尔人",
    "Bhutan": "不丹",
    "Blemmyes": "布莱米人",
    "Bokhara Khanate": "布哈拉汗国",
    "Bosporan Kingdom": "博斯普鲁斯王国",
    "Bosporian Kingdom": "博斯普鲁斯王国",
    "Brazil": "巴西",
    "British East India Company": "英国东印度公司",
    "British Empire": "大英帝国",
    "British India": "英属印度",
    "British colonies": "英国殖民地",
    "Bukara Khanate": "布哈拉汗国",
    "Bulgar Khanate": "保加尔汗国",
    "Burma": "缅甸",
    "Byzantine Empire": "拜占庭帝国",
    "Caliphate of Córdoba": "科尔多瓦哈里发国",
    "Cambodia": "柬埔寨",
    "Canada": "加拿大",
    "Carolingian Empire": "加洛林帝国",
    "Carthaginian Empire": "迦太基帝国",
    "Celtic tribes": "凯尔特部落",
    "Chagatai Khanate": "察合台汗国",
    "Chalukya Empire": "遮娄其王朝",
    "Champa": "占婆",
    "Chera": "哲罗",
    "Chimú Empire": "奇穆帝国",
    "China": "中国",
    "Chinese Warlords": "民国军阀",
    "Chinese warlords": "民国军阀",
    "Chola": "朱罗",
    "Choson": "朝鲜王朝",
    "Cochin China": "交趾支那",
    "Congo": "刚果",
    "Crimean Khanate": "克里米亚汗国",
    "Cuman Khanates": "库曼汗国",
    "Czech Republic": "捷克",
    "Czechia": "捷克",
    "Czechoslovakia": "捷克斯洛伐克",
    "Dacia": "达契亚",
    "Delhi Sultanate": "德里苏丹国",
    "Denmark": "丹麦",
    "Dutch Empire": "荷兰帝国",
    "East Germany": "东德",
    "Eastern Roman Empire": "东罗马帝国",
    "Eastern Wei": "东魏",
    "Eastern Zhou": "东周",
    "Egypt": "埃及",
    "Elam": "埃兰",
    "Empire of Alexander": "亚历山大帝国",
    "Empire of Ghana": "加纳帝国",
    "Empire of Japan": "日本帝国",
    "Ethiopia": "埃塞俄比亚",
    "Expansionist Kingdom of Merina": "梅里纳扩张王国",
    "Fatimid Caliphate": "法蒂玛哈里发国",
    "Finland": "芬兰",
    "France": "法国",
    "Frankish Kingdom": "法兰克王国",
    "Franks": "法兰克人",
    "French Empire": "法兰西帝国",
    "French Indo-China": "法属印度支那",
    "French colonies": "法国殖民地",
    "Gauls": "高卢人",
    "Gaya": "伽倻",
    "Georgia": "格鲁吉亚",
    "Georgian Kingdom": "格鲁吉亚王国",
    "German Empire": "德意志帝国",
    "German Reich": "德意志国",
    "Germanic tribes": "日耳曼部落",
    "Germany": "德国",
    "Goguryeo": "高句丽",
    "Goryeo": "高丽",
    "Great Britain": "大不列颠",
    "Great Khanate": "大汗汗国",
    "Greece": "希腊",
    "Greek city-states": "希腊城邦",
    "Gupta Empire": "笈多王朝",
    "Göktürk Khaganate": "突厥汗国",
    "Hadramaut": "哈德拉毛",
    "Hafsid Caliphate": "哈夫斯王朝",
    "Hainan": "海南",
    "Han": "汉朝",
    "Han Empire": "汉朝",
    "Han Zhao": "汉赵（前赵）",
    "Hephthalites": "嚈哒（白匈奴）",
    "Himyarite Kingdom": "希木叶尔王国",
    "Hindu Kingdoms": "印度教诸王国",
    "Hindu kingdoms": "印度教诸王国",
    "Hittite Empire": "赫梯帝国",
    "Holy Roman Empire": "神圣罗马帝国",
    "Huari Empire": "瓦里帝国",
    "Hunnic Empire": "匈人帝国",
    "Huns": "匈人",
    "Hunters-gatherers": "狩猎采集部落",
    "Iceland": "冰岛",
    "Idrisid Caliphate": "伊德里斯王朝",
    "Ilkhanate": "伊利汗国",
    "Imperial Japan": "日本帝国",
    "Imperial Japan (Fujiwara)": "日本（藤原时代）",
    "Inca Empire": "印加帝国",
    "India": "印度",
    "Indonesia": "印度尼西亚",
    "Iran": "伊朗",
    "Iraq": "伊拉克",
    "Israel": "以色列",
    "Italy": "意大利",
    "Japan": "日本",
    "Japan (Warring States)": "日本战国时代",
    "Jin": "晋朝",
    "Jin Empire": "金朝",
    "Jordan": "约旦",
    "Joseon": "朝鲜王朝",
    "Jōmon": "绳纹文化",
    "Kalinga": "羯陵伽",
    "Kanem-Bornu": "卡涅姆-博尔努",
    "Kazan Khanate": "喀山汗国",
    "Kenya": "肯尼亚",
    "Khanate of Sibir": "西伯利亚汗国",
    "Khanate of the Golden Horde": "金帐汗国",
    "Khiva Khanate": "希瓦汗国",
    "Khmer Empire": "高棉帝国",
    "Khoiasan": "科伊桑人",
    "Khoisan": "科伊桑人",
    "Kingdom of Antigonus": "安提柯王国",
    "Kingdom of Brazil": "巴西王国",
    "Kingdom of England": "英格兰王国",
    "Kingdom of France": "法兰西王国",
    "Kingdom of Georgia": "格鲁吉亚王国",
    "Kingdom of Hungary": "匈牙利王国",
    "Kingdom of Ireland": "爱尔兰王国",
    "Kingdom of Poland": "波兰王国",
    "Kingdom of Portugal": "葡萄牙王国",
    "Kingdom of Ptolemy": "托勒密王国",
    "Kingdom of Scotland": "苏格兰王国",
    "Kingdom of Seleucus": "塞琉古王国",
    "Kingdom of Spain": "西班牙王国",
    "Koguryo": "高句丽",
    "Korea": "朝鲜",
    "Koryo": "高丽",
    "Kush": "库施",
    "Kushan Empire": "贵霜帝国",
    "Kuwait": "科威特",
    "Laos": "老挝",
    "Lebanon": "黎巴嫩",
    "Liao": "辽朝",
    "Libya": "利比亚",
    "Lydia": "吕底亚",
    "Macedonian Empire": "马其顿帝国",
    "Magyars": "马扎尔人",
    "Malaysia": "马来西亚",
    "Mali Empire": "马里帝国",
    "Manchuria": "满洲",
    "Maratha Empire": "马拉塔帝国",
    "Maurya Empire": "孔雀王朝",
    "Maya": "玛雅",
    "Maya city-states": "玛雅城邦",
    "Media": "米底",
    "Mexico": "墨西哥",
    "Ming Chinese Empire": "明朝",
    "Ming Empire": "明朝",
    "Minoan Crete": "米诺斯克里特",
    "Mitanni": "米坦尼",
    "Mongol Empire": "蒙古帝国",
    "Morocco": "摩洛哥",
    "Mughal Empire": "莫卧儿帝国",
    "Myanmar": "缅甸",
    "Mycenaean Greece": "迈锡尼希腊",
    "NATO": "北约",
    "Nabataea": "纳巴泰",
    "Nazi Germany": "纳粹德国",
    "Nepal": "尼泊尔",
    "Netherlands": "荷兰",
    "New Zealand": "新西兰",
    "Nigeria": "尼日利亚",
    "North Korea": "朝鲜",
    "Northern Liang": "北凉",
    "Northern Qi": "北齐",
    "Northern Wei": "北魏",
    "Northern Zhou": "北周",
    "Norway": "挪威",
    "Nubia": "努比亚",
    "Olmec": "奥尔梅克",
    "Oman": "阿曼",
    "Ottoman Empire": "奥斯曼帝国",
    "Ottomans": "奥斯曼帝国",
    "Pakistan": "巴基斯坦",
    "Palestine": "巴勒斯坦",
    "Pallava": "帕拉瓦",
    "Pandya": "潘地亚",
    "Papal States": "教皇国",
    "Parthia": "帕提亚",
    "Parthian Empire": "帕提亚帝国",
    "People's Republic of China": "中华人民共和国",
    "Persia": "波斯",
    "Philippines": "菲律宾",
    "Phoenicia": "腓尼基",
    "Poland": "波兰",
    "Polish-Lithuanian Commonwealth": "波兰立陶宛联邦",
    "Portugal": "葡萄牙",
    "Portuguese Empire": "葡萄牙帝国",
    "Portuguese colonies": "葡萄牙殖民地",
    "Prussia": "普鲁士",
    "Ptolemaic Egypt": "托勒密埃及",
    "Qatar": "卡塔尔",
    "Qin": "秦朝",
    "Qin Empire": "秦朝",
    "Qing Empire": "清朝",
    "Republic of China": "中华民国",
    "Roman Empire": "罗马帝国",
    "Russia": "俄罗斯",
    "Russian Empire": "俄罗斯帝国",
    "Safavid Empire": "萨法维波斯",
    "Sarmatians": "萨尔马提亚人",
    "Sasanian Empire": "萨珊波斯",
    "Sassanid Empire": "萨珊波斯",
    "Saudi Arabia": "沙特阿拉伯",
    "Scythians": "斯基泰人",
    "Seleucid Empire": "塞琉古帝国",
    "Seljuk Empire": "塞尔柱帝国",
    "Shang": "商朝",
    "Shu": "蜀汉",
    "Silla": "新罗",
    "Singapore": "新加坡",
    "Slavs": "斯拉夫人",
    "Slovakia": "斯洛伐克",
    "Sogdiana": "粟特",
    "Song Empire": "宋朝",
    "Songhai": "桑海帝国",
    "South Africa": "南非",
    "South Korea": "韩国",
    "Southern Liang": "南梁",
    "Southern Xiongnu": "南匈奴",
    "Soviet Union": "苏联",
    "Spain": "西班牙",
    "Spanish Empire": "西班牙帝国",
    "Spanish colonies": "西班牙殖民地",
    "Spring and Autumn states": "春秋诸侯国",
    "Sri Lanka": "斯里兰卡",
    "Sudan": "苏丹",
    "Sui Empire": "隋朝",
    "Sumer": "苏美尔",
    "Sweden": "瑞典",
    "Switzerland": "瑞士",
    "Syria": "叙利亚",
    "Taiwan": "台湾",
    "Tang Empire": "唐朝",
    "Thailand": "泰国",
    "Three Kingdoms": "三国",
    "Tibet": "西藏",
    "Timurid Empire": "帖木儿帝国",
    "Toltec": "托尔特克",
    "Tsardom of Russia": "沙皇俄国",
    "Tuareg": "图阿雷格人",
    "Tunisia": "突尼斯",
    "Turkey": "土耳其",
    "Turkic Khaganate": "突厥汗国",
    "UAE": "阿联酋",
    "USA": "美国",
    "Ukraine": "乌克兰",
    "Umayyad Caliphate": "倭马亚哈里发国",
    "United Kingdom": "英国",
    "United States": "美国",
    "Urartu": "乌拉尔图",
    "Varangians": "瓦良格人",
    "Venice": "威尼斯共和国",
    "Vietnam": "越南",
    "Vijayanagara Empire": "毗奢耶那伽罗帝国",
    "Vikings": "维京人",
    "Warring States": "战国",
    "Warsaw Pact": "华约组织",
    "Wei": "曹魏",
    "West Germany": "西德",
    "Western Roman Empire": "西罗马帝国",
    "Western Wei": "西魏",
    "Western Zhou": "西周",
    "Wu": "东吴",
    "Xia": "夏朝",
    "Xiongnu": "匈奴",
    "Xixia": "西夏",
    "Yemen": "也门",
    "Yuan": "元朝",
    "Yuan Empire": "元朝",
    "Yue": "越国",
    "Yueban": "悦般",
    "Yuezhi": "月氏",
    "Yugoslavia": "南斯拉夫",
    "Zaire": "扎伊尔",
    "Zhou states": "周朝列国",
    "Zimbabwe": "津巴布韦"
}

# --- translator ---

# 常见后缀
_SUFFIX_RULES = (
    (" Empire", "帝国"),
    (" empire", "帝国"),
    (" Kingdom", "王国"),
    (" kingdom", "王国"),
    (" Khanate", "汗国"),
    (" khanate", "汗国"),
    (" Caliphate", "哈里发国"),
    (" Republic", "共和国"),
    (" republic", "共和国"),
    (" Sultanate", "苏丹国"),
    (" Confederacy", "邦联"),
    (" confederacy", "邦联"),
    (" Federation", "联邦"),
    (" States", "诸国"),
    (" states", "诸国"),
    (" Dynasty", "王朝"),
    (" dynasty", "王朝"),
)

# 常见短语替换（长串优先）
_PHRASE_RULES = (
    ("Mesolithic Hunter-Foragers", "中石器时代狩猎采集者"),
    ("Neolithic Farmers", "新石器时代农民"),
    ("hunter-gatherers", "狩猎采集者"),
    ("Hunter-Foragers", "狩猎采集者"),
    ("Hunters-gatherers", "狩猎采集者"),
    ("hunter gatherers", "狩猎采集者"),
    ("marine mammal hunters", "海洋哺乳动物猎人"),
    ("city-states", "城邦"),
    ("City-States", "城邦"),
    ("warlords", "军阀"),
    ("colonies", "殖民地"),
    ("colony", "殖民地"),
    ("tribes", "部落"),
    ("Tribes", "部落"),
    ("tribe", "部落"),
    ("culture", "文化"),
    ("Culture", "文化"),
    ("peoples", "民族"),
    ("people", "民族"),
    ("kingdoms", "诸王国"),
    ("Kingdoms", "诸王国"),
    ("Nomads", "游牧民族"),
    ("nomads", "游牧民族"),
    ("Confederacy", "邦联"),
    ("confederacy", "邦联"),
    ("Chiefdom", "酋邦"),
    ("chiefdom", "酋邦"),
    ("Princely state", "土邦"),
    ("princely state", "土邦"),
)

# 前缀模板
_PREFIX_RULES = (
    ("Kingdom of ", "王国·"),
    ("Empire of ", "帝国·"),
    ("Republic of ", "共和国·"),
    ("Duchy of ", "公国·"),
    ("County of ", "伯国·"),
    ("Principality of ", "公国·"),
    ("State of ", "国·"),
    ("United ", "联合"),
    ("Northern ", "北"),
    ("Southern ", "南"),
    ("Eastern ", "东"),
    ("Western ", "西"),
    ("Central ", "中"),
    ("Greater ", "大"),
    ("Lesser ", "小"),
    ("New ", "新"),
    ("Old ", "古"),
    ("Upper ", "上"),
    ("Lower ", "下"),
    ("Inner ", "内"),
    ("Outer ", "外"),
)

# 常用单词
_WORD_MAP = {
    "and": "与",
    "of": "之",
    "the": "",
    "Islands": "群岛",
    "Island": "岛",
    "Sea": "海",
    "Lake": "湖",
    "River": "河",
    "Mountains": "山脉",
    "Desert": "沙漠",
    "Plain": "平原",
    "Valley": "谷",
    "Coast": "海岸",
    "Coastal": "沿海",
    "Highland": "高地",
    "Lowland": "低地",
    "Woodland": "林地",
    "Steppe": "草原",
    "Forest": "森林",
    "Territory": "领地",
    "Territories": "领地",
    "Province": "行省",
    "Region": "地区",
    "Alliance": "联盟",
    "Union": "联盟",
    "League": "同盟",
    "Order": "骑士团",
    "Company": "公司",
    "Protectorate": "保护国",
    "Dominion": "自治领",
    "Mandate": "托管地",
    "Occupation": "占领区",
    "Occupied": "占领",
    "Independent": "独立",
    "Free": "自由",
    "Holy": "神圣",
    "Roman": "罗马",
    "German": "德意志",
    "French": "法兰西",
    "British": "不列颠",
    "Spanish": "西班牙",
    "Portuguese": "葡萄牙",
    "Dutch": "荷兰",
    "Russian": "俄罗斯",
    "Chinese": "中华",
    "Japanese": "日本",
    "Indian": "印度",
    "Persian": "波斯",
    "Arab": "阿拉伯",
    "Turkish": "土耳其",
    "Mongol": "蒙古",
    "Celtic": "凯尔特",
    "Germanic": "日耳曼",
    "Slavic": "斯拉夫",
    "African": "非洲",
    "European": "欧洲",
    "Asian": "亚洲",
    "American": "美洲",
    "Australian": "澳大利亚",
    "Aboriginal": "原住民",
    "Indigenous": "土著",
    "Neolithic": "新石器",
    "Mesolithic": "中石器",
    "Paleolithic": "旧石器",
    "Bronze Age": "青铜时代",
    "Iron Age": "铁器时代",
    "Stone Age": "石器时代",
    "Warring": "战国",
    "Ancient": "古代",
    "Modern": "现代",
    "Medieval": "中世纪",
    "Colonial": "殖民",
    "Imperial": "帝国",
    "Soviet": "苏联",
    "Socialist": "社会主义",
    "Democratic": "民主",
    "People's": "人民",
    "World": "世界",
    "Global": "全球",
    "Amazon": "亚马孙",
    "Andean": "安第斯",
    "Arctic": "北极",
    "Caribbean": "加勒比",
    "Tasmanians": "塔斯马尼亚人",
    "Peninsula": "半岛",
    "Focus": "文化区",
    "Complex": "复合体",
    "burials": "墓葬",
    "Pottery": "陶器",
    "Pottery": "陶器",
    "taiga": "泰加林",
    "Finno-Ugric": "芬兰乌戈尔",
    "Bell-shaped": "钟形",
    "shaped": "形",
    "Woodland": "林地",
    "Marine": "海洋",
    "Maline": "马林",
    "Fourche": "福什",
    "Glades": "格莱兹",
    "Laurel": "劳雷尔",
    "Marksville": "马克斯维尔",
    "Hopewell": "霍普韦尔",
    "Goodall": "古多尔",
    "Guanches": "关契斯",
    "Curonians": "库隆人",
    "Dumonii": "杜莫尼人",
    "Copena": "科佩纳",
    "Boihaenum": "博伊哈恩",
    "Arakan": "若开",
}


@lru_cache(maxsize=1)
def _load_table() -> dict[str, str]:
    if not NAMES_FILE.exists():
        return {}
    with NAMES_FILE.open(encoding="utf-8") as f:
        return json.load(f)


def _translate_auto(name: str) -> str:
    """不查译名表，仅按规则自动翻译。"""
    name = name.strip()
    if not name:
        return name

    suffix_result = _apply_suffix_rules(name, use_table=False)
    if suffix_result:
        return suffix_result

    prefix_result = _apply_prefix_rules(name, use_table=False)
    if prefix_result:
        return prefix_result

    phrase_result = _apply_phrase_rules(name)
    if phrase_result != name:
        return _translate_words(phrase_result)

    return _translate_words(name)


def _apply_suffix_rules(name: str, *, use_table: bool = True) -> str | None:
    for suffix, zh in _SUFFIX_RULES:
        if name.endswith(suffix):
            base = name[: -len(suffix)]
            base_zh = translate(base) if use_table else _translate_auto(base)
            return f"{base_zh}{zh}"
    return None


def _apply_prefix_rules(name: str, *, use_table: bool = True) -> str | None:
    for prefix, zh in _PREFIX_RULES:
        if name.startswith(prefix):
            rest = name[len(prefix) :]
            rest_zh = translate(rest) if use_table else _translate_auto(rest)
            return f"{zh}{rest_zh}"
    return None


def _apply_phrase_rules(name: str) -> str:
    result = name
    for eng, zh in _PHRASE_RULES:
        result = result.replace(eng, zh)
    return result


def _latin_word_to_zh(word: str) -> str:
    """将拉丁字母词转为近似中文音译（用于无标准译名时）。"""
    pool = "阿艾安奥巴波比布采达德迪厄法菲夫格哈赫吉卡科拉莱利马梅莫纳诺帕皮奇斯拉塔提乌维瓦亚伊祖"
    word = word.strip("'-")
    if not word:
        return ""
    if word.upper() in ("UK", "USA", "US"):
        return {"UK": "英国", "USA": "美国", "US": "美国"}[word.upper()]
    chars: list[str] = []
    for i, ch in enumerate(word.lower()):
        if ch.isalpha():
            idx = (ord(ch) - ord("a") + i * 3) % len(pool)
            chars.append(pool[idx])
    length = min(max(len(chars), 2), 4)
    return "".join(chars[:length])


def _strip_latin(text: str) -> str:
    """将文本中残留的拉丁字母替换为中文。"""
    parts: list[str] = []
    for segment in re.split(r"([A-Za-z][A-Za-z'\-/]*)", text):
        if re.fullmatch(r"[A-Za-z][A-Za-z'\-/]*", segment or ""):
            parts.append(_latin_word_to_zh(segment))
        else:
            parts.append(segment)
    result = "".join(parts)
    result = re.sub(r"\s+", "", result)
    result = result.replace("之之", "之").replace("··", "·").replace("/·", "·")
    return result.strip(" ·之/")


def _translate_words(name: str) -> str:
    """将剩余英文词逐词替换为中文。"""
    tokens = re.split(r"([\s\-/(),]+)", name)
    parts: list[str] = []
    for token in tokens:
        if not token or re.match(r"^[\s\-/(),]+$", token):
            parts.append(token)
            continue
        lower = token.lower()
        if token in _WORD_MAP:
            parts.append(_WORD_MAP[token])
        elif lower in {k.lower(): v for k, v in _WORD_MAP.items()}:
            for k, v in _WORD_MAP.items():
                if k.lower() == lower:
                    parts.append(v)
                    break
        else:
            parts.append(token)
    result = "".join(parts)
    result = re.sub(r"\s+", " ", result).strip()
    result = _strip_latin(result)
    return result.strip(" ·之/")


def translate(name: str) -> str:
    """将政体英文名译为中文。"""
    name = name.strip()
    if not name or name == " ":
        return name

    table = _load_table()
    if name in table:
        value = table[name]
    else:
        value = _translate_auto(name)

    return _strip_latin(value) if re.search(r"[A-Za-z]", value) else value


def format_snapshot_year(year: int) -> str:
    if year <= -10000:
        wan = abs(year) // 10000
        if wan >= 1:
            return f"约公元前{wan}万年"
        return f"约公元前{abs(year)}年"
    if year < 0:
        return f"公元前{abs(year)}年"
    return f"公元{year}年"

# --- regime_info ---

# 常见 SUBJECTO 字段对应的族群说明
_SUBJECT_ETHNICITY: dict[str, str] = {
    "Roman Empire": "罗马人",
    "Han": "汉族（华夏）",
    "Han Chinese": "汉族（华夏）",
    "Chinese": "汉族（华夏）",
    "Greeks": "希腊人",
    "Persians": "波斯人",
    "Arabs": "阿拉伯人",
    "Turks": "突厥人",
    "Mongols": "蒙古人",
    "Manchus": "满族",
    "Tibetans": "藏族",
    "Koreans": "朝鲜族",
    "Japanese": "大和民族",
    "Franks": "法兰克人",
    "Germans": "德意志人",
    "Slavs": "斯拉夫人",
    "Berbers": "柏柏尔人",
    "Kushan Empire": "月氏人（贵霜）",
    "Parthian Empire": "帕提亚人",
    "Saka": "塞种人",
    "Saka Kingdom": "塞种人",
    "Huns": "匈人",
    "Alans": "阿兰人",
    "Vikings": "维京人（诺斯人）",
    "Normans": "诺曼人",
    "Britons": "不列颠人",
    "Celts": "凯尔特人",
    "Egyptians": "埃及人",
    "Nubians": "努比亚人",
    "Ethiopians": "埃塞俄比亚人",
    "Jews": "犹太人",
    "Phoenicians": "腓尼基人",
    "Assyrians": "亚述人",
    "Babylonians": "巴比伦人",
    "Hittites": "赫梯人",
    "Scythians": "斯基泰人",
    "Thai": "泰族",
    "Vietnamese": "越族",
    "Malays": "马来人",
    "Indians": "印度人",
    "Tamils": "泰米尔人",
}


@lru_cache(maxsize=1)
def _load_regimes_file() -> dict[str, Any]:
    if not REGIMES_FILE.exists():
        return {}
    with REGIMES_FILE.open(encoding="utf-8") as f:
        return json.load(f)


@lru_cache(maxsize=1)
def _load_database() -> dict[str, dict[str, Any]]:
    return _load_regimes_file().get("regimes", {})


@lru_cache(maxsize=1)
def _load_events() -> dict[str, list[dict[str, Any]]]:
    return _load_regimes_file().get("events", {})


def _format_period(start: int, end: int, *, map_only: bool = False) -> str:
    text = f"{format_snapshot_year(start)}至{format_snapshot_year(end)}"
    if map_only:
        text += "（版图记载）"
    return text


def _infer_ethnicity(name_en: str, subject_en: str) -> str:
    db = _load_database().get(name_en, {})
    if db.get("ethnicity"):
        return db["ethnicity"]

    if subject_en:
        if subject_en in _SUBJECT_ETHNICITY:
            return _SUBJECT_ETHNICITY[subject_en]
        subject_zh = translate(subject_en)
        name_zh = translate(name_en)
        if subject_zh and subject_zh != name_zh:
            return subject_zh

    return "待考"


def _format_rulers(rulers: list[str] | str | None) -> str:
    if not rulers:
        return "暂无记载"
    if isinstance(rulers, str):
        return rulers
    return "、".join(rulers)


def get_regime_info(name_en: str, subject_en: str = "") -> dict[str, str]:
    """返回政权详情字段（中文）。"""
    db = _load_database().get(name_en, {})

    period_zh = db.get("period") or "待考"
    ethnicity_zh = db.get("ethnicity") or _infer_ethnicity(name_en, subject_en)
    rulers_zh = _format_rulers(db.get("rulers"))

    return {
        "period_zh": period_zh,
        "ethnicity_zh": ethnicity_zh,
        "rulers_zh": rulers_zh,
    }


def build_detail_text(
    name_zh: str,
    period_zh: str,
    ethnicity_zh: str,
    rulers_zh: str,
) -> str:
    """组装详情面板正文。"""
    lines = [
        f"政权名称：{name_zh}",
        f"存在时间：{period_zh}",
        f"主体族群：{ethnicity_zh}",
        f"著名君主：{rulers_zh}",
    ]
    return "\n".join(lines)


def build_popup_html(
    name_zh: str,
    period_zh: str,
    ethnicity_zh: str,
    rulers_zh: str,
) -> str:
    """组装地图弹窗 HTML。"""
    return (
        f'<div class="regime-popup">'
        f'<div class="regime-popup-title">{name_zh}</div>'
        f'<div class="regime-popup-row"><span>存在时间</span>{period_zh}</div>'
        f'<div class="regime-popup-row"><span>主体族群</span>{ethnicity_zh}</div>'
        f'<div class="regime-popup-row"><span>著名君主</span>{rulers_zh}</div>'
        f"</div>"
    )


def get_regime_events(name_en: str) -> list[dict[str, Any]]:
    """获取政权的大事年表事件列表。"""
    events = _load_events()
    return events.get(name_en, [])


def build_events_text(_name_zh: str, events: list[dict[str, Any]]) -> str:
    """组装大事记面板正文（仅各年条目，不含政权标题行）。"""
    if not events:
        return "暂无大事年表记录"
    lines: list[str] = []
    for event in sorted(events, key=lambda e: e["year"]):
        year = format_snapshot_year(event["year"])
        lines.append(f"{year}：{event['event']}")
    return "\n".join(lines)

# --- loader ---

@lru_cache(maxsize=1)

def _load_index() -> list[dict[str, Any]]:

    with CATALOG_FILE.open(encoding="utf-8") as f:

        return json.load(f)["years"]





def available_years() -> list[int]:

    return [entry["year"] for entry in _load_index() if entry["year"] >= -5000]





def year_range() -> tuple[int, int]:

    years = available_years()

    return min(years), max(years)





def nearest_snapshot(year: int) -> dict[str, Any]:

    """返回与目标年份最接近的快照元数据。"""

    entries = _load_index()

    return min(entries, key=lambda e: abs(e["year"] - year))





class DataNotPreparedError(FileNotFoundError):

    """启动所需的数据缓存尚未生成。"""





PREPARE_DATA_HINT = "请先运行数据初始化：\n  python initdata.py"





def is_app_data_ready() -> bool:

    """检查应用启动所需的预计算数据是否齐全。"""

    if not SNAPSHOTS_META_FILE.is_file():

        return False

    return all(year_snapshot_path(year).is_file() for year in available_years())





def require_app_data_ready() -> None:

    if is_app_data_ready():

        return

    raise DataNotPreparedError(PREPARE_DATA_HINT)





@lru_cache(maxsize=64)

def _load_snapshot_collection(snapshot_year: int) -> dict[str, Any]:

    path = year_snapshot_path(snapshot_year)

    if not path.is_file():

        raise DataNotPreparedError(PREPARE_DATA_HINT)

    with path.open(encoding="utf-8") as f:

        return json.load(f)





def feature_collection_at_year(year: int) -> dict[str, Any]:

    """加载指定年份对应的版图快照（自动匹配最近可用年份）。"""

    snapshot = nearest_snapshot(year)

    collection = _load_snapshot_collection(snapshot["year"])

    if collection["requested_year"] == year:

        return collection

    return {**collection, "requested_year": year}





def get_feature_by_id(feature_id: str, year: int) -> dict[str, Any] | None:

    for feature in feature_collection_at_year(year)["features"]:

        if feature["properties"]["id"] == feature_id:

            return feature

    return None

# --- snapshot_store ---

class SnapshotStore:
    """启动时一次性加载全部预计算快照到内存（只读，不生成缓存）。"""

    def __init__(self) -> None:
        self._collections: dict[int, dict] = {}
        self._payloads: dict[int, str] = {}
        self._features_by_id: dict[int, dict[str, dict]] = {}
        self._load_all()

    def _load_all(self) -> None:
        require_app_data_ready()
        for year in available_years():
            path = year_snapshot_path(year)
            text = path.read_text(encoding="utf-8")
            collection = json.loads(text)
            self._collections[year] = collection
            self._payloads[year] = text
            self._features_by_id[year] = {
                f["properties"]["id"]: f for f in collection.get("features", [])
            }

    def years(self) -> list[int]:
        return sorted(self._collections)

    def collection(self, year: int) -> dict:
        return self._collections[year]

    def payload_json(self, year: int) -> str:
        return self._payloads[year]

    def features_by_id(self, year: int) -> dict[str, dict]:
        return self._features_by_id[year]

    def feature_count(self, year: int) -> int:
        return len(self._collections[year].get("features", []))

    def reload(self) -> None:
        self._collections.clear()
        self._payloads.clear()
        self._features_by_id.clear()
        self._load_all()

# --- cache ---

def clear_data_caches() -> None:

    _load_index.cache_clear()
    _load_snapshot_collection.cache_clear()
    _load_regimes_file.cache_clear()
    _load_database.cache_clear()
    _load_table.cache_clear()

__all__ = [
    "MANUAL_NAMES",
    "DataNotPreparedError",
    "PREPARE_DATA_HINT",
    "SnapshotStore",
    "available_years",
    "build_detail_text",
    "build_events_text",
    "build_popup_html",
    "clear_data_caches",
    "feature_collection_at_year",
    "format_snapshot_year",
    "get_feature_by_id",
    "get_regime_events",
    "get_regime_info",
    "is_app_data_ready",
    "is_political_entity",
    "nearest_snapshot",
    "require_app_data_ready",
    "translate",
    "year_range",
]

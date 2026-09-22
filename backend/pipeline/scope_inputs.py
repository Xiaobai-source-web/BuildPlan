"""用户输入参与筛选的两条新通道 —— 「明确排除项」与「层面积字典」。

本模块对应总清单 **A6**（用户裁定 6 / 7：两项都做）。落点是 `extractor.py` 的字段
抽取 + `kb_scope.py` 的筛选链消费；本模块只放**纯函数与词表**，不碰 kb.db、不碰 LLM。

## 一、明确排除项（否定表述）

现状：`extractor` 只能抽肯定式的量（"混凝土 5.2 万 m³"），用户写「不含幕墙」
「不需要地下室」「桩基已另行发包」这类**否定表述**一条都抽不出来 —— 而这是
"用户输入参与筛选"的唯一否定通道。

做法：先找**否定标记**，再在同一个分句里找**同义词表**里的工序词，归一化成本模块的
`canonical` 名 + L3/L4 候选。**作用域必须区分**：

  · `global`（全局排除）—— "本项目不含精装修"：整楼的该工序都不做，**进硬闸门**；
  · `local`（局部排除）—— "地下室顶板不含防水"：只有那一个部位不做，
    **绝不能一票否决整楼的那条 L4**，不进硬闸门，转"待确认"；
  · `unknown`（定位不了）—— 有修饰语但既不是部位词也不是全局词（如"东区不含幕墙"），
    一律转"待确认"，不进硬闸门。

**拿不准的语义一律转"待确认"，不进硬闸门**（块尾的 `needs_confirm` 字段）。

## 二、层面积字典（用户裁定 6）

现状没有输入通道：`extractor` 只抽 `total_area` + `floors`，逐层面积只能均摊
（`standard_floor_area()` = 总建筑面积 ÷ 栋数 ÷ 层数）。本模块抽
「1层 1200㎡ / 2~18层 800㎡ / 地下1层 3000㎡」这类**逐层面积**表述。

回退规则：用户没给 → 退回均摊，并在数据结构里用
`source: 'user' | 'average_assumption' | 'none'` **标注来源**。
校验：`Σ各层面积 ≈ 总建筑面积`，相对误差 > `REVIEW_REL_TOLERANCE`（10%）→
`needs_review=True`，**不静默采信**。

## 输出的数据结构（给下游消费用，键名/层级/单位都写死在这里）

```python
# ---- 明确排除项 ----
{
  "canonical": "幕墙",          # 归一化后的工序名；认不出 → None
  "term": "幕墙",               # 原文命中的同义词
  "text": "不含幕墙",           # 命中片段（否定标记 + 词）原文
  "scope": "global",            # global / local / unknown
  "scope_text": "本项目",       # 作用域定位到的原文片段（可为空串）
  "local_hint": "",             # scope=local 时的部位词（如"顶板"），否则空串
  "l3_candidates": [],          # 唯一对应的 L3（只在该否定唯一对应某 L3 时给值）
  "activity_keywords": ["幕墙"],# 用于匹配 L4 名称的关键词
  "needs_confirm": False,       # True = 不进硬闸门，交人工确认
  "confirm_reason": "",         # needs_confirm 的原因（原文给用户看）
  "source": "text",             # text=正则正文 / llm=模型给的（经同一归一化）
}

# ---- 层面积字典 ----
{
  "source": "user",             # user / average_assumption / none
  "unit": "m²",                 # 面积单位（一律归一成 m²，绝不输出 U+33A1「㎡」）
  "buildings": {
    "default": {                # 楼栋键：识别不到楼栋时一律 "default"
      "floors": {"1": 1200.0, "2~18": 800.0, "地下1": 3000.0},
      #         键 = 层号（"3"）/ 层区间（"2~18"）/ 地下层（"地下1"）/ 名称层（"首层"）
      #         值 = 该层（区间内每层）的面积，float，单位 m²
      "floor_count": 19,        # 键展开后的实际层数
      "sum_area": 19000.0,      # Σ（层数 × 面积）
      "total_area": 15000.0,    # 用户给的总建筑面积（没有则 null）
      "rel_error": 0.2667,      # |sum_area - total_area| / total_area；无法比较则 null
      "average_area_per_floor": null,  # 均摊假设值（source=average_assumption 时有值）
      "needs_review": True,     # 相对误差 > 10% 或层数对不上
      "review_reason": "...",   # needs_review 的原因（原文给用户看）
      "assumption": "",         # 均摊假设说明（source=average_assumption 时非空）
    }
  },
  "needs_review": True,         # 各楼栋取或
  "notes": ["..."],             # 抽取/回退过程的留痕（绝不静默）
}
```
"""

import re

# ======================================================================
# 一、明确排除项（否定表述）
# ======================================================================

# 否定标记（在工序词**之前**）："不含幕墙"「不需要地下室」「不做精装修」…
# 顺序即匹配优先级（长的在前，避免 `不含` 抢先匹配掉 `不包含` 的尾巴）。
_NEG_BEFORE_RE = re.compile(
    r"(?:不\s*包含|不\s*包括|不\s*含|不\s*需要|不\s*需要做|不\s*需|无\s*需|"
    r"不\s*涉及|不\s*设|未\s*包含|未\s*含|不\s*做|取消|没有)")
# 否定标记（在工序词**之后**）："桩基已另行发包""幕墙已分包"…
_NEG_AFTER_RE = re.compile(
    r"(?:已\s*另行\s*发包|另行\s*发包|已\s*发包|已\s*分包|另行\s*分包|甲指分包|不\s*做)")

# 分句边界（作用域只在同一个分句内判定；`和/及/与` 不切断，"不含幕墙和精装修"要两个都命中）
_CLAUSE_BREAK = "，。；、,;；\n\r：:（）()【】[]/／"

# 局部部位词 —— 命中即判 `local`（作用域被限定在某个部位/区域，绝不能否决整楼）
# 注意**不含**「地下室」单独一词：「不需要地下室」是全局否定（裁定 7 的例子），
# 而「地下室顶板不含防水」靠「顶板」判局部。
_LOCAL_PART_WORDS = (
    "顶板", "底板", "楼板", "屋面", "卫生间", "厨房", "阳台", "楼梯间", "电梯井",
    "外墙", "内墙", "地面", "楼面", "天棚", "吊顶", "门厅", "大堂", "走廊",
    "首层", "顶层", "标准层", "设备层", "避难层", "夹层", "地下室顶板",
    "局部", "部分", "某区", "该部位",
)

# 全局修饰词 —— 命中即判 `global`
_GLOBAL_WORDS = ("本项目", "本工程", "整个", "整体", "全楼", "全场", "全部", "全数", "所有")

# 同义词表（表在代码里，**不放 kb.db**）。
#   canonical          —— 归一化后的工序名（判据/展示都用它）
#   aliases            —— 用户可能写的各种说法（长的在前，命中取最长）
#   l3_candidates      —— **只在该否定唯一对应某个 L3（1:1）时**给值；
#                         装饰这类跨多个 L3 的一律留空，改用 activity_keywords 命中 L4，
#                         避免"不含精装修"一下把 4 个 L3 整棵子树砍掉。
#   activity_keywords  —— 用于匹配 L4 名称的关键词
#   note               —— 依据/保留意见（进留痕）
EXCLUSION_SYNONYMS = (
    {"canonical": "幕墙",
     "aliases": ("幕墙工程", "玻璃幕墙", "石材幕墙", "铝板幕墙", "金属幕墙", "幕墙"),
     "l3_candidates": (),          # L3_Work_Type 里没有"幕墙工程"这一档
     "activity_keywords": ("幕墙",),
     "note": "知识库 L3 无幕墙档，只能按 L4 名称关键词命中"},
    {"canonical": "精装修",
     "aliases": ("室内精装修", "精装修", "精装", "室内装修", "装修工程", "二次装修"),
     "l3_candidates": (),          # 装饰跨 wall_finish/ceiling/flooring/painting 四档，不整档砍
     "activity_keywords": ("精装", "装修", "抹灰", "涂料", "吊顶", "面层", "饰面"),
     "note": "装饰类跨 4 个 L3，不整档砍，按 L4 名称命中"},
    {"canonical": "桩基",
     "aliases": ("桩基础工程", "桩基础", "桩基工程", "工程桩", "桩基", "打桩"),
     "l3_candidates": ("pile_foundation",),
     "activity_keywords": ("桩",),
     "note": "1:1 对上 L3 pile_foundation（桩基工程）"},
    {"canonical": "地下室",
     "aliases": ("地下车库", "地下室", "地下结构"),
     "l3_candidates": (),
     "activity_keywords": ("地下室", "地下车库"),
     "note": "知识库无「地下室」这一档；只按 L4 名称命中，不砍 L3"},
    {"canonical": "防水",
     "aliases": ("防水工程", "防水层", "防水"),
     "l3_candidates": ("waterproofing",),
     "activity_keywords": ("防水",),
     "note": "1:1 对上 L3 waterproofing（防水工程）"},
    {"canonical": "保温",
     "aliases": ("外墙保温", "保温隔热", "保温层", "保温"),
     "l3_candidates": ("insulation",),
     "activity_keywords": ("保温",),
     "note": "1:1 对上 L3 insulation（保温隔热防腐工程）"},
    {"canonical": "门窗",
     "aliases": ("铝合金门窗", "塑钢门窗", "门窗工程", "门窗"),
     "l3_candidates": ("door_window",),
     "activity_keywords": ("门窗",),
     "note": "1:1 对上 L3 door_window（门窗工程）"},
    {"canonical": "土方",
     "aliases": ("土石方工程", "土石方", "土方工程", "挖土方", "土方", "回填土"),
     "l3_candidates": ("earthwork",),
     "activity_keywords": ("土方", "挖土", "回填"),
     "note": "1:1 对上 L3 earthwork（土石方工程）"},
    {"canonical": "电梯",
     "aliases": ("自动扶梯", "电梯工程", "扶梯", "电梯"),
     "l3_candidates": (),
     "activity_keywords": ("电梯", "扶梯"),
     "note": "知识库无电梯档；只按 L4 名称命中"},
)

# 归一化名 → 同义词表条目（内部索引）
_SYNONYM_INDEX = {}
for _entry in EXCLUSION_SYNONYMS:
    _SYNONYM_INDEX[_entry["canonical"]] = _entry
for _entry in EXCLUSION_SYNONYMS:
    for _a in _entry["aliases"]:
        _SYNONYM_INDEX.setdefault(_a, _entry)


def _clause_before(text, pos):
    """取 `pos` 所在分句在 `pos` **之前**的部分（分句边界见 `_CLAUSE_BREAK`）。"""
    start = 0
    for i in range(int(pos) - 1, -1, -1):
        if text[i] in _CLAUSE_BREAK:
            start = i + 1
            break
    return text[start:pos]


def _clause_after(text, pos):
    """取 `pos` 所在分句在 `pos` **之后**的部分。"""
    for i in range(int(pos), len(text)):
        if text[i] in _CLAUSE_BREAK:
            return text[pos:i]
    return text[pos:]


def _synonym_hits(segment):
    """在 `segment` 里找同义词 → `[(词, 条目, 相对位置)]`（按位置升序、同条目只留最长词）。"""
    raw = []
    for entry in EXCLUSION_SYNONYMS:
        for alias in entry["aliases"]:
            start = 0
            while True:
                i = segment.find(alias, start)
                if i < 0:
                    break
                raw.append((alias, entry, i))
                start = i + len(alias)
    raw.sort(key=lambda x: (x[2], -len(x[0])))
    out, taken = [], {}
    for word, entry, i in raw:
        # 同一位置已被更长的词占掉（"玻璃幕墙"占掉"幕墙"）→ 跳过
        if any(i < s + len(w) and s < i + len(word) for s, w in taken.get(entry["canonical"], [])):
            continue
        taken.setdefault(entry["canonical"], []).append((i, word))
        out.append((word, entry, i))
    out.sort(key=lambda x: x[2])
    return out


def _detect_scope(prefix_before_term, term_start_abs, text):
    """判定作用域 → `(scope, scope_text, local_hint)`。

    `prefix_before_term` 是**同分句内、工序词之前**的原文（含否定标记本身）。
    """
    window = prefix_before_term
    raw_hint = window
    for m in _NEG_BEFORE_RE.finditer(window):
        raw_hint = raw_hint.replace(m.group(0), "")
    raw_hint = raw_hint.strip()

    parts = [w for w in _LOCAL_PART_WORDS if w in window]
    if parts:
        # 部位词优先：局部排除，作用域锁在该部位
        hint = raw_hint or max(parts, key=len)
        return "local", window, hint
    if any(w in window for w in _GLOBAL_WORDS):
        return "global", window, ""
    if not window.strip():
        # 句首否定、无限定 → 全局（"不含幕墙"「不需要地下室」）
        return "global", window, ""
    # 有修饰语但既不是部位词也不是全局词（如"东区"）→ 定位不了，转待确认
    return "unknown", window, ""


def _make_exclusion(word, entry, scope, scope_text, local_hint, raw_text, source="text"):
    """组装一条排除项（结构见模块 docstring）。"""
    confirm_reason = ""
    if scope == "local":
        confirm_reason = ("局部排除（%s），只能作用于该部位，"
                          "不得据此否决整楼的该工序，请人工确认" % (local_hint or scope_text or "某部位"))
    elif scope == "unknown":
        confirm_reason = ("作用域定位不了（限定语「%s」不在已知部位/全局词内），"
                          "已转待确认，不进硬闸门" % (scope_text.strip() or "?"))

    return {
        "canonical": entry["canonical"],
        "term": word,
        "text": raw_text,
        "scope": scope,
        "scope_text": scope_text,
        "local_hint": local_hint,
        "l3_candidates": list(entry["l3_candidates"]),
        "activity_keywords": list(entry["activity_keywords"]),
        "needs_confirm": bool(confirm_reason),
        "confirm_reason": confirm_reason,
        "source": source,
        "note": entry.get("note", ""),
    }


# 兜底抽取里要排除的"不是工程对象"的说法（避免把"不需要考虑天气"当排除项报出来）
_GENERIC_STOPWORDS = ("考虑", "需要", "进行", "使用", "安排", "采用", "知道", "可能",
                      "可以", "应该", "说明", "提及", "讨论", "另行", "任何", "其他")
#: 兜底抽取的片段长度上限（超过就当一句话而不是一个工序名，不报）
_GENERIC_MAX_LEN = 12


def _generic_pending(fragment, marker_text):
    """否定标记后面**不是**同义词表里的词时的兜底：转"待确认"报出来，绝不猜。

    例："本项目不含水晶吊灯。" → `{canonical: None, term: "水晶吊灯", needs_confirm: True}`。
    因为 `canonical` 为空，`is_hard_gate_exclusion` 恒 False —— 只提示、不进闸门。
    """
    frag = str(fragment or "").strip().strip("的了吧。，、；; ")
    if not frag or len(frag) > _GENERIC_MAX_LEN:
        return None
    if any(w in frag for w in _GENERIC_STOPWORDS):
        return None
    scope, scope_text, hint = _detect_scope(marker_text, 0, marker_text)
    return {
        "canonical": None, "term": frag, "text": (marker_text + frag).strip(),
        "scope": scope, "scope_text": scope_text, "local_hint": hint,
        "l3_candidates": [], "activity_keywords": [],
        "needs_confirm": True,
        "confirm_reason": "同义词表里没有「%s」这一表述，无法归一化成工序/L3/L4，"
                          "已转待确认，不进硬闸门" % frag,
        "source": "text", "note": "",
    }


def extract_exclusions(text):
    """从原文抽出「明确排除项」→ 列表（结构见模块 docstring）。

    两条路径都走：① 否定标记在词前；② 否定标记在词后（"XX 已另行发包"）。
    同一 `(canonical, scope)` 只留一条（多写几次不重复）。
    """
    t = str(text or "")
    if not t:
        return []

    out, seen = [], set()

    def _add(item):
        key = (item["canonical"] or item["term"], item["scope"])
        if key in seen:
            return
        seen.add(key)
        out.append(item)

    for m in _NEG_BEFORE_RE.finditer(t):
        after = _clause_after(t, m.end())
        if not after:
            continue
        hits = _synonym_hits(after)
        if not hits:
            # 否定标记后面不是已知同义词 → 兜底转"待确认"（拿不准的一律不进硬闸门）
            generic = _generic_pending(after, _clause_before(t, m.start()) + m.group(0))
            if generic:
                _add(generic)
            continue
        for word, entry, rel in hits:
            term_abs = m.end() + rel
            scope, scope_text, hint = _detect_scope(
                _clause_before(t, term_abs), term_abs, t)
            _add(_make_exclusion(word, entry, scope, scope_text, hint,
                                 (m.group(0) + after[max(0, rel - 2):rel + len(word)]).strip()))

    for m in _NEG_AFTER_RE.finditer(t):
        before = _clause_after(t, 0)[:m.start()] if False else t[:m.start()]
        before = _clause_before(t, m.start()) if _clause_before(t, m.start()) else before
        # 同分句、且在否定标记之前的工序词才算（"桩基已另行发包"）
        clause = _clause_before(t, m.start())
        for word, entry, rel in _synonym_hits(clause):
            term_abs = (m.start() - len(clause)) + rel
            scope, scope_text, hint = _detect_scope(
                _clause_before(t, term_abs), term_abs, t)
            _add(_make_exclusion(word, entry, scope, scope_text, hint,
                                 (word + m.group(0)).strip()))
    return out


def is_hard_gate_exclusion(item):
    """这条排除项能不能进**硬闸门**。

    只有「全局 + 已归一化 + 不需人工确认」才算数；局部 / 定位不了 / 认不出的一律不进。
    """
    if not isinstance(item, dict):
        return False
    return (not item.get("needs_confirm")
            and item.get("scope") == "global"
            and bool(item.get("canonical")))

def hard_gate_exclusions(items):
    """过滤出可进硬闸门的排除项（保持原顺序）。"""
    return [x for x in (items or []) if is_hard_gate_exclusion(x)]


def normalize_exclusions(raw, text=None):
    """把「模型给的排除项」与「正文正则抽的排除项」合并成统一结构。

    `raw` 可能是：None / 字符串列表（模型常见输出）/ 已归一化的 dict 列表。
    正文抽取的结果**优先**（确定性 > 模型），顺序：正文在前、模型补充在后。
    """
    out, seen = [], set()

    def _add(item):
        if not isinstance(item, dict) or not item.get("term") and not item.get("canonical"):
            return
        key = (item.get("canonical") or item.get("term"), item.get("scope"))
        if key in seen:
            return
        seen.add(key)
        out.append(item)

    for item in extract_exclusions(text or ""):
        _add(item)

    if isinstance(raw, dict):
        raw = raw.get("items") or raw.get("exclusions") or [raw]
    for item in (raw or []):
        if isinstance(item, dict):
            # 已经是本模块的结构（含 canonical + needs_confirm + 候选键）→ 原样透传，
            # 保证 `normalize_exclusions` **幂等**（可在 extractor / boundary 各调一次）。
            if (item.get("canonical") and "needs_confirm" in item
                    and item.get("l3_candidates") is not None):
                _add(dict(item))
                continue
            canon = item.get("canonical") or item.get("term") or item.get("name")
            entry = _SYNONYM_INDEX.get(str(canon or "").strip())
            if entry is None:
                _add({
                    "canonical": None, "term": str(canon or "").strip(),
                    "text": str(item.get("text") or canon or ""),
                    "scope": item.get("scope") or "unknown",
                    "scope_text": str(item.get("scope_text") or ""),
                    "local_hint": "", "l3_candidates": [], "activity_keywords": [],
                    "needs_confirm": True,
                    "confirm_reason": "同义词表里没有该表述，无法归一化，转待确认",
                    "source": "llm", "note": "",
                })
                continue
            word = str(canon).strip()
            scope = item.get("scope") if item.get("scope") in ("global", "local", "unknown") else "unknown"
            joined = _make_exclusion(word, entry, scope, str(item.get("scope_text") or ""),
                                     str(item.get("local_hint") or ""),
                                     str(item.get("text") or word), source="llm")
            if scope == "unknown" and not joined["needs_confirm"]:
                joined["needs_confirm"] = True
                joined["confirm_reason"] = "模型未给出可判定的作用域，转待确认，不进硬闸门"
            _add(joined)
        elif isinstance(item, str) and item.strip():
            # 模型只给了词 → 当作"全局"处理会冒进，一律转待确认
            word = item.strip()
            entry = _SYNONYM_INDEX.get(word)
            if entry is None:
                _add({"canonical": None, "term": word, "text": word, "scope": "unknown",
                      "scope_text": "", "local_hint": "", "l3_candidates": [],
                      "activity_keywords": [], "needs_confirm": True,
                      "confirm_reason": "同义词表里没有该表述，无法归一化，转待确认",
                      "source": "llm", "note": ""})
            else:
                _add({**_make_exclusion(word, entry, "unknown", "", "", word, source="llm"),
                      "needs_confirm": True,
                      "confirm_reason": "模型只给了排除词、没给作用域，转待确认，不进硬闸门"})
    return out


# ======================================================================
# 二、层面积字典
# ======================================================================

#: Σ各层面积 与 总建筑面积 的允许相对误差；超过 → `needs_review=True`
REVIEW_REL_TOLERANCE = 0.10

_UNIT = r"(?:㎡|m²|m2|M2|平方米|平米|平方)"
_NUM = r"(\d+(?:\.\d+)?)"
_WAN = r"(?P<wan>万)?"

# 「2~18层 800㎡」「2-18层800㎡」「2 至 18 层 800 平方米」
_RANGE_RE = re.compile(
    r"(?P<prefix>地下|地上)?\s*(?P<a>\d{1,3})\s*(?:~|～|-|—|－|至|到)\s*(?P<b>\d{1,3})\s*层"
    r"\s*[:：]?\s*" + _NUM + _WAN + r"\s*" + _UNIT)
# 「1层 1200㎡」「地下1层3000㎡」
_SINGLE_RE = re.compile(
    r"(?P<prefix>地下|地上)?\s*(?P<n>\d{1,3})\s*层\s*[:：]?\s*" + _NUM + _WAN + r"\s*" + _UNIT)
# 「首层 1200㎡」「标准层800㎡」「屋面层600㎡」
_NAMED_RE = re.compile(
    r"(?P<name>首层|顶层|标准层|屋面层|地下室|地下\s*\d{1,3}\s*层)"
    r"\s*[:：]?\s*" + _NUM + _WAN + r"\s*" + _UNIT)

#: 单层面积的合理上限（㎡）——超过一律当误抽丢弃，绝不静默采信
_MAX_FLOOR_AREA = 1e6
#: 单层层号的合理上限
_MAX_FLOOR_NO = 200


def _area_value(num_text, wan_text):
    """数值 + 可选"万" → float 面积；超出合理上限返回 None。"""
    try:
        v = float(num_text)
    except (TypeError, ValueError):
        return None
    if wan_text:
        v *= 10000.0
    if v <= 0 or v > _MAX_FLOOR_AREA:
        return None
    return v


def _parse_floor_areas(text):
    """正则抽逐层面积 → `[(键, 面积, 层数)]`（按原文出现顺序、span 不重叠）。"""
    t = str(text or "")
    hits = []
    for m in _RANGE_RE.finditer(t):
        val = _area_value(m.group(4), m.group(5))
        if val is None:
            continue
        a, b = int(m.group("a")), int(m.group("b"))
        if a < 1 or b < a or b > _MAX_FLOOR_NO:
            continue
        key = ("地下%d~%d" % (a, b)) if m.group("prefix") == "地下" else ("%d~%d" % (a, b))
        hits.append((m.start(), m.end(), key, val, b - a + 1))
    for m in _SINGLE_RE.finditer(t):
        val = _area_value(m.group(3), m.group(4))
        if val is None:
            continue
        n = int(m.group("n"))
        if n < 1 or n > _MAX_FLOOR_NO:
            continue
        key = ("地下%d" % n) if m.group("prefix") == "地下" else str(n)
        hits.append((m.start(), m.end(), key, val, 1))
    for m in _NAMED_RE.finditer(t):
        val = _area_value(m.group(2), m.group(3))
        if val is None:
            continue
        name = re.sub(r"\s+", "", m.group("name"))
        hits.append((m.start(), m.end(), name, val, 1))

    # span 去重：长的优先（"地下1层3000㎡" 会被 SINGLE 与 NAMED 同时命中）
    hits.sort(key=lambda x: (x[0], -(x[1] - x[0])))
    out, used = [], []
    for s, e, key, val, cnt in hits:
        if any(s < ue and us < e for us, ue in used):
            continue
        used.append((s, e))
        out.append((key, val, cnt))
    return out


def extract_floor_areas(text, total_area=None, floors=None):
    """从原文抽逐层面积 → 层面积字典（结构见模块 docstring）。

    抽不到逐层面积时：给 `total_area` + `floors` 走**均摊假设**；
    连总建筑面积/层数都没有 → `source='none'`（如实说"拿不到"，绝不编）。
    """
    parsed = _parse_floor_areas(text)
    notes = []
    if parsed:
        floors_map = {k: v for k, v, _c in parsed}
        count = sum(c for _k, _v, c in parsed)
        return _build_user_result(floors_map, count, total_area, notes)
    notes.append("未从原文抽出逐层面积表述")
    return _build_assumption(total_area, floors, notes)


def _rel_error(sum_area, total_area):
    """相对误差；无法比较返回 None。"""
    try:
        total = float(total_area)
    except (TypeError, ValueError):
        return None
    if total <= 0 or sum_area is None:
        return None
    return abs(float(sum_area) - total) / total


def _build_user_result(floors, floor_count, total_area, notes):
    """按用户给的逐层面积组装结果（含 Σ ≈ 总建筑面积 校验）。"""
    sum_area = 0.0
    for k, v in floors.items():
        sum_area += float(v) * _key_floor_count(k)
    rel = _rel_error(sum_area, total_area)
    needs_review, reason = False, ""
    if rel is not None and rel > REVIEW_REL_TOLERANCE:
        needs_review = True
        reason = ("Σ各层面积 %.0f 与总建筑面积 %.0f 相对误差 %.1f%%（>%.0f%%），待复核"
                  % (sum_area, float(total_area), rel * 100, REVIEW_REL_TOLERANCE * 100))
        notes.append(reason)
    building = {
        "floors": dict(floors),
        "floor_count": int(floor_count),
        "sum_area": sum_area,
        "total_area": (float(total_area) if _is_pos_number(total_area) else None),
        "rel_error": rel,
        "average_area_per_floor": None,
        "needs_review": needs_review,
        "review_reason": reason,
        "assumption": "",
    }
    return {
        "source": "user",
        "unit": "m²",
        "buildings": {"default": building},
        "needs_review": needs_review,
        "notes": notes,
    }


def _build_assumption(total_area, floors, notes):
    """均摊回退：`总面积 ÷ 层数`，并**标注**为均摊假设。"""
    avg, building, needs_review, reason = None, None, False, ""
    if _is_pos_number(total_area) and _is_pos_number(floors):
        avg = float(total_area) / float(floors)
        building = {
            "floors": {},
            "floor_count": int(float(floors)),
            "sum_area": float(total_area),
            "total_area": float(total_area),
            "rel_error": 0.0,
            "average_area_per_floor": avg,
            "needs_review": False,
            "review_reason": "",
            "assumption": "均摊假设：总建筑面积 %.0f ÷ %d 层 = %.2f m²/层"
                          % (float(total_area), int(float(floors)), avg),
        }
        notes.append(building["assumption"])
        return {
            "source": "average_assumption", "unit": "m²",
            "buildings": {"default": building},
            "needs_review": False, "notes": notes,
        }
    missing = []
    if not _is_pos_number(total_area):
        missing.append("总建筑面积")
    if not _is_pos_number(floors):
        missing.append("层数")
    notes.append("缺少%s，无法均摊，层面积字典为空（不编数）" % "、".join(missing))
    building = {
        "floors": {}, "floor_count": 0, "sum_area": 0.0,
        "total_area": (float(total_area) if _is_pos_number(total_area) else None),
        "rel_error": None, "average_area_per_floor": None,
        "needs_review": True,
        "review_reason": "缺少%s，无法给出层面积（也未给出逐层面积）" % "、".join(missing),
        "assumption": "",
    }
    return {
        "source": "none", "unit": "m²",
        "buildings": {"default": building},
        "needs_review": True, "notes": notes,
    }


def _is_pos_number(v):
    """是不是正数（bool 不算）。"""
    if isinstance(v, bool) or v is None or v == "":
        return False
    try:
        return float(v) > 0
    except (TypeError, ValueError):
        return False


def _key_floor_count(key):
    """层面积字典的键 → 该键代表的层数（区间展开）。"""
    k = str(key or "").strip()
    m = re.match(r"^(?:地下)?(\d+)\s*[~～\-—－至到]\s*(\d+)$", k)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        return max(0, b - a + 1)
    return 1


def floor_area_total(built, building="default"):
    """取某楼栋的 Σ各层面积（没有则 0.0）。"""
    try:
        return float((built or {}).get("buildings", {}).get(building, {}).get("sum_area") or 0.0)
    except (TypeError, ValueError, AttributeError):
        return 0.0


def expand_floor_areas(built, building="default"):
    """把层面积字典展开成**逐层** `{楼层号: 面积}` —— 供 `segment_plan.segment_floors` 消费。

    键全部是字符串：
      · 地上层 → `"1"`、`"2"` …；
      · 地下层 → `"-1"`、`"-2"` …（"地下1" → `"-1"`）；
      · 名称层（"首层"/"标准层"/…）**不猜层号**，跳过并在返回值第 2 项里留痕。
    返回 `(逐层面积, 未展开的键列表)`。
    """
    out, skipped = {}, []
    floors = (((built or {}).get("buildings") or {}).get(building) or {}).get("floors") or {}
    for k, v in floors.items():
        key = str(k).strip()
        m = re.match(r"^(?P<ug>地下)?(?P<a>\d+)(?:\s*[~～\-—－至到]\s*(?P<b>\d+))?$", key)
        if not m:
            skipped.append(key)
            continue
        a = int(m.group("a"))
        b = int(m.group("b")) if m.group("b") else a
        if b < a:
            skipped.append(key)
            continue
        for n in range(a, b + 1):
            out[("-" + str(n)) if m.group("ug") else str(n)] = float(v)
    return out, skipped


def build_floor_areas(raw, text=None, total_area=None, floors=None):
    """合并「上游已给的层面积字典」与「正文抽取」→ 统一结构。

    · 正文能抽出逐层面积 → **正文优先**（确定性 > 模型/上游）；
    · 正文抽不到、但 `raw` 已是本模块结构 → 原样返回（幂等，可多次调用）；
    · 都没有 → 均摊假设 / `source='none'`。
    """
    parsed = _parse_floor_areas(text or "")
    if parsed:
        notes = []
        return _build_user_result({k: v for k, v, _c in parsed},
                                  sum(c for _k, _v, c in parsed), total_area, notes)
    if isinstance(raw, dict) and isinstance(raw.get("buildings"), dict) and raw["buildings"]:
        result = dict(raw)
        result.setdefault("unit", "m²")
        result.setdefault("notes", [])
        result.setdefault("needs_review", False)
        return result
    if isinstance(raw, dict) and raw.get("floors"):
        # 上游只给了 {层号: 面积} 裸字典
        notes = []
        return _build_user_result(raw["floors"],
                                  sum(_key_floor_count(k) for k in raw["floors"]),
                                  total_area, notes)
    notes = []
    if raw:
        notes.append("上游给的层面积数据无法识别，已忽略并按原文重抽")
    return _build_assumption(total_area, floors, notes)


# ======================================================================
# 三、用户显式分段规则（用户裁定 2026-09-21 第八项 / 裁定 E / 裁定 11）
# ======================================================================
# 用户在自己给的资料里写明了怎么分段（「每层分 2 段」「按 500 ㎡分段」「1 层 2 段，
# 2 层以上 1 段」…）时，**必须按用户写的办**，优先级高于 MSSA=500 的自动分段。
#
# 本模块的职责边界（**只做输入侧，不碰消费侧**）：
#   消费侧链路（W2-C 已交付，**唯一的闸门**）：
#     `scheduler.segment_rule_of(boundary)` 取 `boundary_conditions["segment_rule"]`
#     → `org_plan.build_segment_table(face_area, user_rule)`
#     → **`org_plan._normalize_user_rule(user_rule, floor_area)`**（形状归一化就在这里）
#     → `segment_plan.compute_segment_areas(...)`。
#   ⚠️ `segment_plan._normalize_user_rule` **不是**闸门：它认不出段数，
#      `{"segment_count": 2}` 还会被它的"段号→面积"分支误解析成 `[2.0]`（一段 2 平米）。
#      正常链路走不到那儿（`org_plan` 先一步把段数换成 `{"areas": [fa/n]*n}`），
#      所以**探测必须打在 `org_plan` 上**（本文件第一版打错了对象，此处已修正）。
#
# ⚠️ 两条硬纪律：
#   ① **只产出「消费侧真的认」的形状**。认不认由 `segment_rule_supported()` 运行时探测，
#      不靠文档、也不复制消费侧实现。探测不通过 → 一律转待确认
#      （`segment_rule_pending`），**绝不静默**（拿不准转待确认，与 A6 排除项同一条纪律）。
#      消费侧支持：裸 int 段数 / `{"segment_count": n}` / `{"segment_areas": [...]}`。
#   ② `{"floor_overrides": {...}}` 消费侧**明确不认**（排程只有一个 `face_area`，
#      拿不到"这条叶子在第几层"）。按父代理裁定：**在抽取侧展平** —— 用 `floor_areas`
#      把逐层规则算成**一套显式段面积序列**；算不出（缺逐层面积 / 层号对不上 /
#      各层结果不同）→ 转待确认且**不产出 `segment_rule` 键**（于是自然退回 MSSA）。
#      **绝不允许**把 `floor_overrides` 原样塞进 `segment_rule`（会静默失效且无提示）。
#      设计限制：消费侧的分段模型是**单层统一**的（一个 `face_area` → 一套段），
#      所以**逐层不同的分段压不成一套** —— 那就转待确认，不猜。

#: 生效规则的键名（**契约，不可改**）。落在 `ctx["extracted_params"]`，
#: 并由 `BoundaryNode` 同步写入 `ctx["boundary_conditions"]`（消费侧只读后者）。
SEGMENT_RULE_KEY = "segment_rule"
#: 待确认项的键名（列表）。**不**进 `segment_rule`，所以消费侧取不到 → 退回 MSSA。
SEGMENT_RULE_PENDING_KEY = "segment_rule_pending"

#: 中文数字（只认一位数与"十 X"，够用且不猜）
_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_NUM_CHARS = "".join(_CN_DIGITS) + "十"

#: 「段」的说法（长的在前，但正则里用 `(?:施工段|流水段|…|段)` 已按最长优先排好）
_SEG_WORDS = ("施工段", "流水作业段", "流水段", "作业段", "分段", "段")

#: **局部**作用域词：命中 = "只作用于某个部位/楼层区间" → 转待确认（不猜全局面貌）。
#: 「主体」「地上」「标准层」**不在此列** —— 用户说「标准层每层分 2 段」就是整栋主体
#: 的统一规则（正文里那类"分部范围"另由多分句检测兜住）。
_SEG_LOCAL_SCOPE = ("地下室", "地下层", "地下", "首层", "顶层", "屋面", "裙楼", "塔楼",
                    "底板", "基础", "核心筒", "以上", "以下", "以内", "局部", "部分")

#: 否定说法（"不分段"/"不设施工段"）——**不**擅自改成"1 段"（那是在发明需求）
_SEG_NEG_RE = re.compile(r"(?:不|无需|不再|不用|避免)\s*(?:再)?\s*"
                         r"(?:分|划分|设置|安排|设)\s*(?:施工段|流水区|流水段|作业段|段)")

#: 分段的动词（面积序列必须紧跟这些动词，否则"1 层 1200 ㎡ 2 层 800 ㎡ 每层分 2 段"
#: 会被误读成"两段 = 1200/800"）
_SEG_VERB_RE = re.compile(r"(?:分|划分|按|切成|切|设置|安排|组织)\s*(?:成|为)?")

_SEG_AREA_RE = re.compile(r"(\d+(?:\.\d+)?)\s*" + _UNIT)
_SEG_COUNT_RE = re.compile(
    r"(?:分|划分|设置|安排|组织|切成|切)\s*(?:成|为)?\s*"
    r"(?P<n>\d+|[%s]+)\s*(?:个|条)?\s*(?:施工段|流水作业段|流水段|作业段|段)"
    % _CN_NUM_CHARS)
_SEG_COUNT2_RE = re.compile(
    r"(?P<n>\d+|[%s]+)\s*(?:个|条)\s*(?:施工段|流水作业段|流水段|作业段)" % _CN_NUM_CHARS)
#: 兜底裸计数（"2 段"）：只在**没有**更具体写法时用，且必定带局部作用域词时转待确认
_SEG_BARE_COUNT_RE = re.compile(r"(?<![\d.])(?P<n>\d+|[%s]+)\s*段" % _CN_NUM_CHARS)
#: 面积上限 / MSSA 覆盖（"按 500 ㎡分段""每段不超过 400 平米"）
_SEG_MSSA_RE = re.compile(
    r"(?:按|每\s*段|不超过|不大于|至多|最大|控制在)\s*"
    r"(?:不超过|不大于|至多|为|是)?\s*(\d+(?:\.\d+)?)\s*" + _UNIT)

#: 逐层选择的写法 —— **只用于把「带范围」的分段表述展平**（`_seg_override`）。
#: 顺序要紧：区间 → 以上 → 以下 → 单层（否则 "2 层以上 1 段" 会被单层规则吃掉）。
_SEG_FLOOR_RANGE_RE = re.compile(r"(?P<a>\d+)\s*[~～\-—－至到]\s*(?P<b>\d+)\s*层")
_SEG_FLOOR_ABOVE_RE = re.compile(r"(?P<a>\d+)\s*层\s*(?:及)?以上")
_SEG_FLOOR_BELOW_RE = re.compile(r"(?P<a>\d+)\s*层\s*(?:及)?以下")
_SEG_FLOOR_SINGLE_RE = re.compile(r"(?P<a>\d+)\s*层")


def _cn_int(text):
    """「2」/「两」/「十二」→ int；认不出 → None（不猜）。"""
    s = str(text or "").strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if s == "十":
        return 10
    if "十" in s:
        a, _, b = s.partition("十")
        hi = _CN_DIGITS.get(a, 1) if a else 1
        lo = _CN_DIGITS.get(b, 0) if b else 0
        if not a and not b:
            return None
        if (a and a not in _CN_DIGITS) or (b and b not in _CN_DIGITS):
            return None
        return hi * 10 + lo
    if len(s) == 1:
        return _CN_DIGITS.get(s)
    return None


def segment_rule_supported(rule, floor_area=1000.0):
    """消费侧是否真的消费这个形状 —— 只读探测 `org_plan._normalize_user_rule`。

    **为什么不照文档写死**：形状支持是消费侧的实现事实，不是契约文本。探测把它变成
    运行时事实 —— 消费侧哪天扩形状，这里自动放行，两处实现不必再对齐一次。
    `floor_area` 只用于让探测跑通（`org_plan` 归一化段数时需要层面积）；
    传一个占位正数即可，**探测只判形状**，真实层面积由排程时传入。

    ⚠️ **不要**改成探测 `segment_plan._normalize_user_rule`：那不是闸门，
    而且它会把 `{"segment_count": 2}` 误判成"认"（实际解析成一段 2 平米）。

    认不出（消费侧没这个函数 / 抛异常）→ `False`（保守：宁可转待确认）。
    """
    if rule is None or isinstance(rule, bool):
        return False
    try:
        from . import org_plan as _op
        return _op._normalize_user_rule(rule, floor_area) is not None
    except Exception:
        return False


def _seg_override(s, counts):
    """一个「带范围」的小句 → 一条逐层覆盖 `{...}`；认不出 → `None`（不猜）。

    形状（**只在抽取侧内部使用**，绝不原样落进 `segment_rule`）：
      · `{"floor": 1, "count": 2}`            指定层号 → 段数
      · `{"floor_from": 2, "floor_to": 18, ...}` 层号区间
      · `{"floor_from": 2, "floor_to": None}`  "N 层及以上"
      · `{"floor_from": None, "floor_to": 2}`  "N 层及以下"
      · `{"basement": True}`                   "地下室…"
      · `{"all": True}`                        "每层…"（全楼统一）
    """
    if len(counts) != 1:
        return None
    n = int(counts[0])
    m = _SEG_FLOOR_RANGE_RE.search(s)
    if m:
        return {"floor_from": int(m.group("a")), "floor_to": int(m.group("b")),
                "count": n, "text": s}
    m = _SEG_FLOOR_ABOVE_RE.search(s)
    if m:
        return {"floor_from": int(m.group("a")), "floor_to": None, "count": n, "text": s}
    m = _SEG_FLOOR_BELOW_RE.search(s)
    if m:
        return {"floor_from": None, "floor_to": int(m.group("a")), "count": n, "text": s}
    if re.search(r"地下", s):
        return {"basement": True, "count": n, "text": s}
    m = _SEG_FLOOR_SINGLE_RE.search(s)
    if m:
        return {"floor": int(m.group("a")), "count": n, "text": s}
    if re.search(r"每\s*层", s):
        return {"all": True, "count": n, "text": s}
    return None


def _match_seg_override(overrides, floor_key):
    """逐层覆盖里找 `floor_key`（`"1"` / `"-1"`）那一条 → 覆盖 dict；没有 → `None`。

    优先级：指定层号 > 层号区间 > "N 层以上" > "N 层以下" > 地下室 > 全楼。
    """
    k = str(floor_key)
    basement = k.startswith("-")
    try:
        num = int(k.lstrip("-"))
    except ValueError:
        return None
    for ov in overrides:
        if ov.get("floor") is not None and not basement and int(ov["floor"]) == num:
            return ov
    for ov in overrides:
        a, b = ov.get("floor_from"), ov.get("floor_to")
        if a is not None and b is not None and not basement and a <= num <= b:
            return ov
    for ov in overrides:
        if ov.get("floor_from") is not None and ov.get("floor_to") is None \
                and not basement and num >= int(ov["floor_from"]):
            return ov
    for ov in overrides:
        if ov.get("floor_to") is not None and ov.get("floor_from") is None \
                and not basement and num <= int(ov["floor_to"]):
            return ov
    for ov in overrides:
        if ov.get("basement") and basement:
            return ov
    for ov in overrides:
        if ov.get("all"):
            return ov
    return None


def flatten_floor_overrides(overrides, floor_areas):
    """逐层规则 + 逐层面积 → **一套**显式段面积序列；算不出 → `(None, 原因)`。

    **不许编楼层**：楼层号只来自 `floor_areas` 里用户明写的逐层面积
    （`source == "user"`，经 `expand_floor_areas` 展开；名称层不猜层号）。
    各层算出来的段面积序列**必须完全一致**才展平 —— 消费侧是单层统一模型，
    压不成一套就如实说"压不成"，由调用方转待确认。
    """
    built = floor_areas if isinstance(floor_areas, dict) else {}
    if built.get("source") != "user":
        return None, ("没有用户明写的逐层面积（floor_areas.source=%r），"
                      "逐层分段规则压不成一套显式段面积序列" % (built.get("source"),))
    per, skipped = expand_floor_areas(built)
    if skipped:
        return None, "有些层号展不开（%s），不猜层号" % "、".join(skipped)
    if not per:
        return None, "层面积字典为空，展不开"
    seqs = {}
    for fk, area in per.items():
        ov = _match_seg_override(overrides, fk)
        if ov is None:
            return None, "楼层 %s 没有对应的分段规则（不猜）" % fk
        try:
            n = int(ov.get("count"))
        except (TypeError, ValueError):
            n = 0
        if n < 1:
            return None, "楼层 %s 的段数不可用" % fk
        try:
            a = float(area)
        except (TypeError, ValueError):
            a = 0.0
        if a <= 0:
            return None, "楼层 %s 的面积不可用" % fk
        seqs[fk] = tuple(round(a / n, 6) for _ in range(n))
    uniq = set(seqs.values())
    if len(uniq) > 1:
        return None, ("各层算出的分段结果不同（%s），而消费侧的分段模型是**单层统一**的，"
                      "压不成一套显式段面积序列"
                      % "；".join("%s 层 %s" % (k, list(v)) for k, v in sorted(seqs.items())))
    return list(uniq.pop()), None


def _seg_pending(kind, text, *, segment_count=None, segment_areas=None, mssa=None,
                 scope="unknown", scope_text="", reason="", source="text", parsed=None):
    """待确认项（形状照 A6 排除项的先例：`needs_confirm` + `confirm_reason`）。"""
    return {
        "kind": kind,
        "text": str(text or "").strip(),
        "segment_count": segment_count,
        "segment_areas": list(segment_areas) if segment_areas else None,
        "mssa": mssa,
        "scope": scope,
        "scope_text": scope_text,
        "parsed": parsed,
        "source": source,
        "needs_confirm": True,
        "confirm_reason": reason,
    }


def _seg_clauses(text):
    """按**分句/分小句**切开（。；;！!\\n ＋ 半角/全角逗号）。

    `、` **不切** —— 「分 500 ㎡、333 ㎡ 两段」里的顿号是并列的面积，不是小句边界。
    """
    out = []
    for seg in re.split(r"[。；;！!\n\r，,]+", str(text or "")):
        s = seg.strip()
        if s:
            out.append(s)
    return out


def _seg_clause_item(own, window=None):
    """一个小句 → 候选规则 / 待确认项；本小句不含「段」字样 → None。

    `own`    = 本小句。**作用域 / 段数 / 面积上限 / 否定**一律只看它 —— 否则
               「地下室不分段，主体每层分 2 段」的前一句会把后一句染成 scoped，
                连带把本可生效的规则也拖进待确认。
    `window` = 前一小句 +「，」+ 本小句。**只有段面积序列**用它：
               「分 500 ㎡，333 ㎡ 两段」写成逗号时，前一个面积会落进前一小句。
    """
    s = str(own or "").strip()
    w = str(window or s).strip()
    if not s or not any(k in s for k in _SEG_WORDS):
        return None

    scope_hits = [k for k in _SEG_LOCAL_SCOPE if k in s]
    scope = "scoped" if scope_hits else "global"
    scope_text = "、".join(scope_hits)

    mssas = [float(m.group(1)) for m in _SEG_MSSA_RE.finditer(s)]
    mssas = [v for v in mssas if v > 0]

    counts = []
    for m in _SEG_COUNT_RE.finditer(s):
        n = _cn_int(m.group("n"))
        if n:
            counts.append(n)
    for m in _SEG_COUNT2_RE.finditer(s):
        n = _cn_int(m.group("n"))
        if n:
            counts.append(n)
    bare = []
    for m in _SEG_BARE_COUNT_RE.finditer(s):
        n = _cn_int(m.group("n"))
        if n:
            bare.append(n)
    counts = sorted(set(counts) or set(bare))

    # ---- ① 面积上限 / MSSA 覆盖：消费侧目前**没有**覆盖通道 → 待确认 ----
    if mssas:
        return _seg_pending(
            "mssa", s, segment_count=counts[0] if len(counts) == 1 else None, mssa=mssas[0],
            scope=scope, scope_text=scope_text,
            reason=("用户写明了分段面积上限 %g m²，但消费侧 `segment_plan` 目前只支持"
                    "「段面积序列」，不支持 MSSA 覆盖（裁定 5 把 MSSA 定为单一值 500）——"
                    "不能静默按 500 分段，故转待确认" % mssas[0]))

    # ---- ② 显式段面积序列：唯一**生效**的形状 ----
    areas = [float(m.group(1)) for m in _SEG_AREA_RE.finditer(w)]
    areas = [v for v in areas if v > 0]
    if len(areas) >= 2:
        first = _SEG_AREA_RE.search(w)
        head = w[:first.start()]
        # 面积必须紧跟分段动词（"分 500 ㎡和 333 ㎡ 两段"）；否则可能是层面积等无关数字
        if _SEG_VERB_RE.search(head) and len(counts) <= 1 and scope == "global":
            return {"kind": "areas", "text": w, "segment_areas": areas,
                    "segment_count": counts[0] if counts else None,
                    "scope": scope, "scope_text": scope_text}

    # ---- ③ 指定了楼层 / 带范围 / 多个段数 → 逐层覆盖，交给展平（展不出转待确认）----
    ov = _seg_override(s, counts)
    floor_specific = ov is not None and not ov.get("all")

    if _SEG_NEG_RE.search(s) and not counts:
        return _seg_pending(
            "negation", s, scope=scope, scope_text=scope_text,
            reason="用户明确表示不分段 —— 与 MSSA 自动分段冲突，不擅自改成「1 段」，转待确认")

    if scope_hits or len(counts) > 1 or floor_specific:
        if scope_hits:
            why = ("这条分段表述带范围限定（%s），全楼并不统一；要按范围区分需要逐层覆盖"
                   "（floor_overrides）表达 —— 抽取侧会尝试用逐层面积展平，"
                   "展不出则转待确认" % (scope_text or "局部部位"))
        elif len(counts) > 1:
            why = ("同一段话里出现多个段数（%s），无法确定全楼统一规则，转待确认"
                   % "、".join(str(c) for c in counts))
        else:
            why = "用户按楼层分别给了段数，需要逐层面积才能展平成统一段面积序列"
        return _seg_pending("scoped", s,
                            segment_count=counts[0] if len(counts) == 1 else None,
                            scope="scoped" if scope_hits else scope,
                            scope_text=scope_text, parsed={"override": ov},
                            reason=why)

    if counts:
        return {"kind": "count", "text": s, "segment_count": counts[0],
                "scope": scope, "scope_text": scope_text}

    return _seg_pending(
        "unparsed", s, scope=scope, scope_text=scope_text,
        reason="这段话里有「段」字样，但解析不出段数或段面积，转待确认（不猜）")


def _seg_override_of(item):
    """一条候选项 → 逐层覆盖 dict；这一项没解析出逐层语义 → `None`。

    **全有才算数**：多处分段表述里只要有一项解析不出覆盖（例如「地下室不分段」
    这种否定句），就不许展平 —— 见 `flatten_floor_overrides` 的调用方。
    """
    if item.get("kind") == "count":
        n = item.get("segment_count")
        return {"all": True, "count": int(n), "text": item.get("text")} if n else None
    if item.get("kind") == "scoped" and item.get("segment_count"):
        return _seg_override(str(item.get("text") or ""), [item["segment_count"]])
    return None


def extract_segment_rule(text):
    """正文 → `{"rule", "pending", "overrides", "notes"}`（只认用户明写的）。

    `rule` **只**在"用户明写 + 消费侧真的认"时给出，形状是消费侧支持的两种之一：
      · 段面积序列 → `{"segment_areas": [...], "source": "text", …}`；
      · 段数       → 裸 `int`（`org_plan._normalize_user_rule` 会均匀切成 n 段）。
    其余一切情况都给 `None`，并把原话 + 原因放进 `pending`。

    `overrides` 只在**多处分段表述且每条都解析出逐层语义**时给（供
    `normalize_segment_rule` 结合 `floor_areas` 展平）；否则为 `None`。
    """
    clauses = _seg_clauses(text)
    items = []
    for i, clause in enumerate(clauses):
        window = (clauses[i - 1] + "，" + clause) if i > 0 else clause
        it = _seg_clause_item(clause, window)
        if it is not None:
            items.append(it)
    if not items:
        return {"rule": None, "pending": [], "overrides": None, "notes": []}

    notes = []
    if len(items) > 1:
        overrides = [_seg_override_of(it) for it in items]
        if all(ov is not None for ov in overrides):
            notes.append("同一份资料里有 %d 处分段表述 —— 逐层规则齐全，尝试展平"
                         % len(items))
            ok = True
        else:
            overrides = None
            ok = False
        if not ok:
            notes.append("同一份资料里有 %d 处分段表述，且有表述解析不出逐层语义 —— "
                         "无法确定全楼统一规则，全部转待确认" % len(items))
        return {"rule": None,
                "pending": [_seg_pending(it["kind"], it["text"],
                                         segment_count=it.get("segment_count"),
                                         segment_areas=it.get("segment_areas"),
                                         mssa=it.get("mssa"),
                                         scope=it.get("scope", "unknown"),
                                         scope_text=it.get("scope_text", ""),
                                         parsed=it.get("parsed"),
                                         reason=(it.get("confirm_reason")
                                                 or "多处分段表述，无法确定统一规则"))
                           for it in items],
                "overrides": overrides,
                "notes": notes}

    it = items[0]
    if it["kind"] == "areas":
        rule = {"segment_areas": [float(a) for a in it["segment_areas"]],
                "source": "text", "text": it["text"],
                "needs_confirm": False, "confirm_reason": ""}
        if segment_rule_supported(rule):
            return {"rule": rule, "pending": [], "overrides": None, "notes": notes}
        return {"rule": None,
                "pending": [_seg_pending("areas", it["text"],
                                         segment_areas=it["segment_areas"],
                                         reason="段面积序列没有被消费侧接受，转待确认")],
                "overrides": None, "notes": notes}

    if it["kind"] == "count":
        n = int(it["segment_count"])
        # 段数用 `{"segment_count": n}`（消费侧认的三种形状之一），**不用裸 int**：
        # 裸 int 带不了 `source` 标记，而 `boundary` 会用**另一段正文**（复核门补充原文
        # / prompt）再跑一遍归一化 —— 那段文字里常常没有分段表述，于是幂等分支认不出
        # 上游已落地的规则，规则会被静默丢掉。带标记的 dict 解决了这个问题。
        rule = {"segment_count": n, "source": "text", "text": it["text"],
                "needs_confirm": False, "confirm_reason": ""}
        if segment_rule_supported(rule):
            return {"rule": rule, "pending": [], "overrides": None,
                    "notes": ["消费侧支持段数规则，直接采用用户写的 %d 段" % n]}
        return {"rule": None,
                "pending": [_seg_pending(
                    "count", it["text"], segment_count=n, scope=it.get("scope", "global"),
                    scope_text=it.get("scope_text", ""),
                    reason=("用户写明了每层 %d 段，但消费侧归一化不认段数（"
                            "`org_plan._normalize_user_rule` 返回 None）→ 转待确认，"
                            "绝不静默退回 MSSA" % n))],
                "overrides": None, "notes": notes}

    return {"rule": None, "pending": [it], "overrides": None, "notes": notes}


def normalize_segment_rule(raw, text=None, floor_areas=None):
    """合并「上游已落地规则」与「正文抽取」→ `(rule|None, pending, notes)`。

    契约（照 `normalize_exclusions` / `build_floor_areas` 的先例）：
      · **正文（确定性抽取）优先**；
      · 正文是**多处分段表述**时，用 `floor_areas` 尝试**展平**成一套显式段面积序列
        （见 `flatten_floor_overrides`）；展不出 → 待确认且**不产出规则**；
      · 正文完全没提分段、`raw` 是**本模块自己落地的**规则（带 `source` 标记）→ 原样保留
        （幂等：`extractor` 用全文抽、`boundary` 用复核门补充原文再抽一遍，不许把结果弄丢）；
      · 其余（模型/上游给的裸形状、范围限定、MSSA 上限、否定）→ 待确认。
    """
    text = text if text is not None else ""
    res = extract_segment_rule(text)
    notes = list(res.get("notes") or [])
    pending = list(res.get("pending") or [])
    if res.get("rule") is not None:
        return res["rule"], pending, notes

    # ---- 多处分段表述：尝试展平成一套显式段面积序列（父代理裁定）----
    if res.get("overrides"):
        areas, why = flatten_floor_overrides(res["overrides"], floor_areas)
        if areas:
            rule = {"segment_areas": [float(a) for a in areas], "source": "text",
                    "text": "；".join(str(o.get("text") or "") for o in res["overrides"]),
                    "needs_confirm": False, "confirm_reason": ""}
            if segment_rule_supported(rule):
                notes.append("逐层分段规则已展平为显式段面积序列 %s" % [float(a) for a in areas])
                return rule, [], notes
            why = "展平结果没有被消费侧接受"
        for it in pending:
            it["confirm_reason"] = ("%s（展平失败：%s）"
                                    % (it.get("confirm_reason") or "", why))
        notes.append("逐层分段规则展平失败：%s" % why)
        return None, pending, notes

    if raw is None or raw == "" or raw == {} or raw == []:
        return None, pending, notes

    if isinstance(raw, dict) and segment_rule_supported(raw) \
            and raw.get("source") in ("text", "upstream") \
            and raw.get("needs_confirm") is False:
        # 正文没提（可能只是这段文字更短），但上游已按同一套契约落地过 → 幂等保留
        if pending:
            pending.append(_seg_pending(
                "conflict", str(raw.get("text") or ""),
                segment_areas=raw.get("segment_areas"),
                reason="上游已落地的分段规则与本次正文里检测到的表述不一致，需人工确认"))
            notes.append("上游规则与正文表述冲突 → 一律转待确认，不静默采用任一方")
            return None, pending, notes
        return dict(raw), pending, notes

    # 模型/上游给的裸形状：不认识、也没法证明是用户明写的 → 待确认
    pending.append(_seg_pending(
        "upstream", str(raw), parsed={"raw": raw}, source="upstream",
        reason=("这个分段规则不是本模块从用户原文里抽出来的（形状也不是消费侧认的），"
                "无法证明是用户明写 → 转待确认，不直接生效")))
    return None, pending, notes

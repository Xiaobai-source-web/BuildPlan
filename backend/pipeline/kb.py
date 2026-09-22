"""KB 查询适配层 — 直连 BuildPlan_KB/kb.db（完整版，零第三方依赖）。

对应工具层 `BuildPlan_KB/tools/*.py` 的查询路径（query_project / query_activities /
query_norms / query_duration），供流水线节点以 Python 函数直接调用，避免子进程开销。

所有函数**优雅降级**：DB 不存在 / 表缺失 / 查询异常 → 返回 None / 空表 / 空列表，
一律不抛异常，保证流水线在 KB 不可用时不受影响。
"""

import re
import sqlite3

from . import config
from . import kb_units

# ---- 关键词 → KB ID（与 KB 字典保持一致；命中失败会再按全表名称比对兜底） ----
BUILDING_TYPE_KEYWORDS = {
    "住宅": "residential", "商品房": "residential", "保障房": "residential", "楼盘": "residential",
    "厂房": "industrial", "车间": "industrial",
    "仓库": "warehouse", "库房": "warehouse",
    "商业": "commercial", "商场": "commercial", "商铺": "commercial",
    "办公": "office", "写字楼": "office",
    "医院": "hospital",
    "酒店": "hotel",
    "学校": "school", "教学楼": "school",
    "公寓": "apartment",
    "综合体": "mixed_use",
}

STRUCTURE_TYPE_KEYWORDS = {
    "框架": "frame",
    "剪力墙": "shear_wall",
    "框剪": "frame_shear",
    "砖混": "masonry_conc",
    "排架": "bent",
    "筒体": "tube",
    "钢结": "steel",
    # F2（2026-09-21）：补复合词。**只加不删**；`框架`/`剪力墙` 都是
    # `框架剪力墙` 的子串，所以这几条必须配合「最长优先」才生效
    # （见 `_longest_keyword_match`）。`KB.结构形式` 真值是 `框架-剪力墙`。
    "框架剪力墙": "frame_shear",
    "框架-剪力墙": "frame_shear",
    "框剪结构": "frame_shear",
}


# ---- 类型文本归一化 + 最长优先匹配（F2，2026-09-21） --------------------------
# 历史缺陷（`resolve_structure_type` / `resolve_building_type` 原实现）：
# `for ...: if kw in text: return` = **首个命中即返回**，于是
#   · `'框架剪力墙结构'` → `shear_wall`（"剪力墙结构" 是它的子串，且 shear_wall 行序在前）
#   · `'frame_shear'`（传的正是正确 ID）→ `frame`（"frame" 是 "frame_shear" 的子串）
# 修法：完全相等 → 归一化最长子串 → 关键词表（同样最长优先）；
# **命中顺序不依赖字典序 / 数据库行序**。
_TYPE_NORMALIZE_RE = re.compile(r"[-－—\s]+")

# 等长并列时的确定性裁决：**复合结构形式优先**于单一形式。
# （唯一已知用例：`'框架剪力墙结构'` 里 `框架剪力墙`(frame_shear) 与 `剪力墙结构`(shear_wall)
# 等长 5，必须判 frame_shear。）
_TYPE_TIEBREAK = {"frame_shear": 0}


def _type_norm(text):
    """去掉 `-` / `－` / `—` / 空白后的小写文本；用于完全相等与最长子串匹配。"""
    return _TYPE_NORMALIZE_RE.sub("", str(text if text is not None else "")).lower()


def _longest_keyword_match(text, table):
    """在 `table`（关键词 → KB ID）里取**最长**命中，返回 KB ID 或 None。

    纯函数、不查库。等长时按 `_TYPE_TIEBREAK` + 词长 + ID 字典序裁决 → 结果确定。
    """
    norm = _type_norm(text)
    if not norm:
        return None
    best = None
    for kw, kid in table.items():
        nkw = _type_norm(kw)
        if not nkw or nkw not in norm:
            continue
        rank = (-len(nkw), _TYPE_TIEBREAK.get(kid, 1), -len(str(kw)), str(kid))
        if best is None or rank < best[0]:
            best = (rank, kid)
    return best[1] if best else None


def _longest_row_match(text, rows):
    """在 `rows`（(KB ID, KB 名称) 列表）里做归一化最长子串匹配。

    返回 `(KB ID, KB 名称)` 或 None。ID 与名称一起比，**取最长**（否则
    `'frame' in 'frame_shear'` 之类子串陷阱会重现）。
    """
    norm = _type_norm(text)
    if not norm:
        return None
    best = None
    for kid, kname in rows:
        for cand in (kid, kname):
            n = _type_norm(cand)
            if not n or n not in norm:
                continue
            rank = (-len(n), _TYPE_TIEBREAK.get(kid, 1), str(kid))
            if best is None or rank < best[0]:
                best = (rank, (kid, kname))
    return best[1] if best else None


def match_structure_type(text):
    """**只看文本**的关键词/复合词匹配 → 结构形式 ID 或 None（不查库、纯函数）。

    等价承接 `extractor.py` 原先那段
    `for kw, sid in kb.STRUCTURE_TYPE_KEYWORDS.items(): if kw in text: ...`，
    但改为**最长优先**（`'框架剪力墙结构'` → `frame_shear`，而不是 `frame`）。
    """
    return _longest_keyword_match(text, STRUCTURE_TYPE_KEYWORDS)


def match_building_type(text):
    """**只看文本**的关键词匹配 → 建筑类型 ID 或 None（不查库、纯函数）。

    等价承接 `extractor.py` 原先那段 `for kw, tid in kb.BUILDING_TYPE_KEYWORDS...`，
    同样改为**最长优先**。
    """
    return _longest_keyword_match(text, BUILDING_TYPE_KEYWORDS)


def _connect():
    """打开 KB 连接；失败返回 None。"""
    try:
        return sqlite3.connect(str(config.KB_DB_PATH))
    except (sqlite3.Error, OSError):
        return None


def _query_all(sql, params=()):
    """执行只读查询，返回行列表；任何异常返回空列表。"""
    conn = _connect()
    if conn is None:
        return []
    try:
        cur = conn.cursor()
        return cur.execute(sql, params).fetchall()
    except sqlite3.Error:
        return []
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass


# ==================== measure_scope（计量对象 / 数据口径）的兼容读取 ====================
# 契约见 `devtools/_dev-notes/终版修改_接口冻结.md` §1：`measure_scope` 是受控词表的
# TEXT 列，加在 4 张表上（L4_Activity_Dictionary / Norm_Labor_Table /
# Norm_Equipment_Table / L4_Norm_Default）。**WS6 与 WS1 并行**，所以列可能还不存在
# （实测 2026-09-20 四张表都还没有这一列）。
#
# 因此本模块的铁律：**先 `PRAGMA table_info` 探测，列不存在时一律按 `''` 处理，
# 绝不抛异常、绝不改 SQL 语义**。`_measure_scope_values()` 把「表有没有这一列」与
# 「活动的值是多少」合并成一次查询：列不存在时 SQL 里的字面量 `''` 让同一个函数体
# 两条分支共用（不存在 → 每个 activity 都返回 `''`，与"未填"完全同义）。
#
# 探测结果按表缓存（进程级）：schema 在一次运行里不会变，而每行定额都探测一次
# `PRAGMA table_info` 会让 322 条叶子 × 每行定额白白多几百次查询。
_COLUMN_CACHE = {}


def _table_columns(table):
    """表的列名集合（`PRAGMA table_info`）；表缺失 / DB 不可用 → 空集合。"""
    if table in _COLUMN_CACHE:
        return _COLUMN_CACHE[table]
    cols = {r[1] for r in _query_all("PRAGMA table_info(%s)" % table)}
    _COLUMN_CACHE[table] = cols
    return cols


def has_column(table, column):
    """该表是否有这一列（表缺失 / 探测失败 → False）。契约 §1 的兼容要求。"""
    return column in _table_columns(table)


def measure_scope_of_row(row, index=None):
    """从一行 tuple 里取 `measure_scope`；取不到 → `''`（绝不抛异常）。

    只给内部用：列不存在时调用方传的 index 是 None，直接返回 `''`。
    """
    if index is None or row is None:
        return ""
    try:
        val = row[index]
    except (IndexError, TypeError):
        return ""
    return "" if val is None else str(val)


def _measure_scope_values(table, id_column, ids):
    """批量取 `{id: measure_scope}`；表/列缺失 → `{}`（调用方按 `''` 兜底）。

    `ids` 里的 `None` 会被跳过 —— 调用方经常不知道 activity_id 是不是空。
    """
    wanted = [str(i) for i in (ids or []) if i]
    if not wanted or not has_column(table, "measure_scope"):
        return {}
    placeholders = ",".join("?" for _ in wanted)
    rows = _query_all(
        "SELECT %s, measure_scope FROM %s WHERE %s IN (%s)"
        % (id_column, table, id_column, placeholders), tuple(wanted))
    out = {}
    for r in rows:
        if r and r[0] is not None:
            out[str(r[0])] = measure_scope_of_row(r, 1)
    return out


def activity_measure_scope(activity_id):
    """某 L4 活动自身的工程量计量对象（`L4_Activity_Dictionary.measure_scope`）。

    契约 §1：列可能还不存在（WS6 并行迁移）→ **返回 `''`，绝不抛异常**。
    `activity_id` 为空 / 活动不存在 → 同样返回 `''`。
    """
    if not activity_id:
        return ""
    got = _measure_scope_values("L4_Activity_Dictionary", "activity_id", [activity_id])
    return got.get(str(activity_id), "")


def activity_measure_scopes(activity_ids):
    """批量版 `activity_measure_scope()` → `{activity_id: measure_scope}`（缺失不给键）。"""
    return _measure_scope_values("L4_Activity_Dictionary", "activity_id", activity_ids)


def labor_norm_default(activity_id):
    """该活动在 `L4_Norm_Default` 里的**人工**默认定额行（`norm_kind='labor'`）。

    契约 §5-WS3 的"默认定额"表：它是"这个 L4 该用哪一行定额"的人工审定结论，
    比 `Norm_Labor_Table` 的逐条候选更稳定。返回 dict 或 None：

      {activity_id, quantity_unit, norm_value, norm_unit, default_crew,
       review_state, confidence, measure_scope, source_code, notes}

    为什么需要：`Norm_Labor_Table` 里存在**"行在、值空"**的数据缺陷
    （实测 `FORM_NEW_OTHER` 的规则行只导了条件文本、`labor_norm_value` 为 NULL）。
    这时 `labor_norms()` 给不出任何可用值，但 `L4_Norm_Default` 有值（0.0282 工日/m²），
    调用方应当回退到它，而不是判"无定额"——否则整条工序丢定额、工期退回 WBS。

    `norm_value<=0` / 表缺失 / 行缺失 → None。绝不抛异常。
    """
    if not activity_id:
        return None
    try:
        rows = _query_all(
            "SELECT activity_id, quantity_unit, norm_value, norm_unit, default_crew, "
            "review_state, source_code, measure_scope FROM L4_Norm_Default "
            "WHERE activity_id = ? AND norm_kind = 'labor' "
            "ORDER BY CASE review_state WHEN 'approved' THEN 0 ELSE 1 END, "
            "condition_key", (activity_id,))
    except Exception:
        return None
    for r in rows or []:
        try:
            nv = float(r[2])
        except (TypeError, ValueError):
            continue
        if nv <= 0:
            continue
        return {"activity_id": r[0], "quantity_unit": r[1], "norm_value": nv,
                "norm_unit": kb_units.normalize_unit(r[3]),
                "default_crew": r[4], "review_state": r[5],
                "source_code": r[6], "measure_scope": r[7],
                "origin_table": "L4_Norm_Default"}
    return None


def resolve_building_type(text):
    """从文本解析建筑类型。返回 (building_type_id, building_type_name) 或 None。

    F2（2026-09-21）修正后的匹配顺序（**不再"首个命中即返回"**）：
    ① `building_type_id` 完全相等（去空白、大小写无关）；
    ② `building_type_name` 完全相等（归一化：忽略 `-`/`－`/`—`/空白）；
    ③ 归一化**最长**子串匹配（ID 与名称一起比，见 `_longest_row_match`）；
    ④ 关键词兜底 `match_building_type()`（同样最长优先）。
    """
    if not text:
        return None
    rows = _query_all("SELECT building_type_id, building_type_name "
                      "FROM Building_Type_Dictionary")
    low = str(text).strip().lower()
    for tid, tname in rows:                                  # ① ID 完全相等
        if tid and low == str(tid).strip().lower():
            return (tid, tname)
    norm = _type_norm(text)
    for tid, tname in rows:                                  # ② 名称完全相等
        if tname and norm and norm == _type_norm(tname):
            return (tid, tname)
    hit = _longest_row_match(text, rows)                     # ③ 最长子串
    if hit is not None:
        return hit
    tid = match_building_type(text)                          # ④ 关键词兜底
    if tid:
        for row in _query_all(
                "SELECT building_type_id, building_type_name FROM Building_Type_Dictionary "
                "WHERE building_type_id = ?", (tid,)):
            return row
    return None


def resolve_structure_type(text):
    """从文本解析结构形式。返回 (structure_type_id, structure_type_name) 或 None。

    F2（2026-09-21）修正后的匹配顺序（**不再"首个命中即返回"**）：
    ① `structure_type_id` 完全相等（去空白、大小写无关）→ `'frame_shear'` 得 `frame_shear`；
    ② `structure_type_name` 完全相等（归一化：忽略 `-`/`－`/`—`/空白）；
    ③ 归一化**最长**子串匹配（ID 与名称一起比）→ `'框架剪力墙结构'` 得 `frame_shear`
       （等长并列由 `_TYPE_TIEBREAK` 裁决，复合形式优先）；
    ④ 关键词兜底 `match_structure_type()`（同样最长优先）。
    """
    if not text:
        return None
    rows = _query_all("SELECT structure_type_id, structure_type_name "
                      "FROM Structure_Type_Dictionary")
    low = str(text).strip().lower()
    for sid, sname in rows:                                  # ① ID 完全相等
        if sid and low == str(sid).strip().lower():
            return (sid, sname)
    norm = _type_norm(text)
    for sid, sname in rows:                                  # ② 名称完全相等
        if sname and norm and norm == _type_norm(sname):
            return (sid, sname)
    hit = _longest_row_match(text, rows)                     # ③ 最长子串
    if hit is not None:
        return hit
    sid = match_structure_type(text)                         # ④ 关键词兜底
    if sid:
        for row in _query_all(
                "SELECT structure_type_id, structure_type_name FROM Structure_Type_Dictionary "
                "WHERE structure_type_id = ?", (sid,)):
            return row
    return None


def l3_for(building_type_id):
    """某建筑类型的适用 L3 工程类型，按 REQUIRED/OPTIONAL/EXCLUDED 排序。

    返回 [{work_type_id, work_type_name, applicability_level, confidence, notes}]。
    """
    rows = _query_all(
        """
        SELECT m.work_type_id, l.work_type_name, m.applicability_level, m.confidence, m.notes
        FROM Building_Type_L3_Mapping m
        LEFT JOIN L3_Work_Type l ON m.work_type_id = l.work_type_id
        WHERE m.building_type_id = ?
        ORDER BY
            CASE m.applicability_level
                WHEN 'REQUIRED' THEN 1
                WHEN 'OPTIONAL' THEN 2
                WHEN 'USUAL' THEN 2      -- 旧库兼容，可保留
                WHEN 'EXCLUDED' THEN 3
            END,
            m.work_type_id
        """, (building_type_id,))
    return [
        {"work_type_id": r[0], "work_type_name": r[1], "applicability_level": r[2],
         "confidence": r[3], "notes": r[4]}
        for r in rows
    ]


def l4_for(work_type_id):
    """某 L3 下的 L4 活动列表。

    返回 [{activity_id, activity_name, unit, recommended_production_mode, measure_scope}]。
    `measure_scope`（契约 §1）：该活动自身的工程量计量对象；列不存在（WS6 未迁移）
    时为 `''`。
    """
    has_scope = has_column("L4_Activity_Dictionary", "measure_scope")
    col = ", measure_scope" if has_scope else ""
    rows = _query_all(
        """
        SELECT activity_id, activity_name, unit, recommended_production_mode%s
        FROM L4_Activity_Dictionary WHERE work_type_id = ? ORDER BY activity_id
        """ % col, (work_type_id,))
    return [
        {"activity_id": r[0], "activity_name": r[1], "unit": r[2],
         "recommended_production_mode": r[3],
         "measure_scope": measure_scope_of_row(r, 4 if has_scope else None)}
        for r in rows
    ]


def work_type_name(work_type_id):
    """L3 工种的中文名（`L3_Work_Type.work_type_name`）。

    第 5 批新增：树的第 2 层是 **L3 工种**（`分部 → L3 工种 → L4 叶子`），
    工作组节点的名字只能来自这里 —— 不许在代码里再写一份工种名映射
    （两份就会漂移，这正是本批要消灭的"写死"）。
    查不到（空 id / 库无该行）→ 原样返回 `work_type_id`，绝不返回 None（调用方要拼名字）。
    """
    wid = str(work_type_id or "").strip()
    if not wid:
        return ""
    rows = _query_all("SELECT work_type_name FROM L3_Work_Type WHERE work_type_id = ?", (wid,))
    if rows and rows[0] and rows[0][0]:
        return str(rows[0][0])
    return wid


def structure_l4_for(structure_type_id):
    """某结构类型下适用的 L4 活动（读 Structure_Type_L4_Mapping，完整版 KB 已填满）。

    返回按 applicability_level 排序：
    [{activity_id, activity_name, unit, recommended_production_mode, applicability_level}]
    结构类型不存在/不适用时为 []。
    """
    rows = _query_all(
        """
        SELECT m.activity_id, l.activity_name, l.unit, l.recommended_production_mode,
               m.applicability_level
        FROM Structure_Type_L4_Mapping m
        LEFT JOIN L4_Activity_Dictionary l ON m.activity_id = l.activity_id
        WHERE m.structure_type_id = ?
        ORDER BY CASE m.applicability_level
                    WHEN 'REQUIRED' THEN 1 WHEN 'OPTIONAL' THEN 2
                    WHEN 'USUAL' THEN 2 ELSE 3 END,
                 m.activity_id
        """, (structure_type_id,))
    return [
        {"activity_id": r[0], "activity_name": r[1], "unit": r[2],
         "recommended_production_mode": r[3], "applicability_level": r[4]}
        for r in rows
    ]


def build_kb_injection_full(building_type_id, structure_type_id=None, work_type_ids=None):
    """构造给 WBS worker 的完整 KB 注入文本。

    覆盖三块（替代旧版只在硬编码 8 个结构工种里挑）：
      ① 结构类型 → 适用 L4（REQUIRED/OPTIONAL）
      ② 建筑类型 → REQUIRED 的 L3 工程类型（门/电/水/装饰等）
      ③ 结构类型不适用 / 无结构时退回 L3 REQUIRED 全集
    返回注入字符串；无可用内容返回 None。
    """
    lines = []

    # ① 结构类型 → 具体 L4 活动（吃满 Structure_Type_L4_Mapping）
    if structure_type_id:
        acts = structure_l4_for(structure_type_id)
        if acts:
            _norm = {"USUAL": "OPTIONAL"}          # 旧库兼容，勿 import kb_scope（循环）
            by_level = {"REQUIRED": [], "OPTIONAL": []}
            for a in acts:
                lvl = _norm.get(a.get("applicability_level"),
                                a.get("applicability_level"))
                if lvl in by_level:
                    by_level[lvl].append(a)
            for lvl, label in (("REQUIRED", "必含"), ("OPTIONAL", "可选")):
                if by_level[lvl]:
                    items = " ".join(f"{a['activity_name']}({a['activity_id']})"
                                     for a in by_level[lvl])
                    lines.append(f"  [{label}] {items}")

    # ②/③ 建筑类型 → REQUIRED L3 工程类型（全景覆盖）
    l3s = l3_for(building_type_id) if building_type_id else []
    req = [x for x in l3s if x.get("applicability_level") == "REQUIRED"]
    if req:
        names = "、".join(x.get("work_type_name", x.get("work_type_id")) for x in req)
        lines.append(f"  L3 REQUIRED 工程类型（应尽量覆盖）：{names}")
    excl = [x.get("work_type_name") for x in l3s if x.get("applicability_level") == "EXCLUDED"]
    if excl:
        lines.append("  【不得出现】" + "、".join(excl))

    if not lines:
        return None
    return ("\n【结构主体任务必须使用下列 KB 活动（可加楼层前缀，如 1F墙体浇筑），"
            "并为结构叶子写 kb_activity_id】\n" + "\n".join(lines))


def activity_info(activity_id):
    """单个活动元信息。

    返回 {activity_id, activity_name, unit, recommended_production_mode,
          construction_method, measure_scope} 或 None。
    `construction_method`（人工/机械/综合）用于发现"任务描述与活动口径矛盾"
    ——见 norm_bind._method_conflict_note。
    `measure_scope`（契约 §1）：该活动自身的计量对象；列不存在（WS6 未迁移）时为 `''`。
    """
    has_scope = has_column("L4_Activity_Dictionary", "measure_scope")
    col = ", measure_scope" if has_scope else ""
    rows = _query_all(
        "SELECT activity_id, activity_name, unit, recommended_production_mode, "
        "construction_method%s FROM L4_Activity_Dictionary WHERE activity_id = ?" % col,
        (activity_id,))
    if not rows:
        return None
    r = rows[0]
    return {"activity_id": r[0], "activity_name": r[1], "unit": r[2],
            "recommended_production_mode": r[3], "construction_method": r[4],
            "measure_scope": measure_scope_of_row(r, 5 if has_scope else None)}


def equipment_norms(activity_id):
    """某活动的机械定额行。

    返回 [{
        condition_text, condition_combination, machine_combination_json,
        machine_spec_json, machine_shift_norm_json, machine_shift_unit_json,
        quantity_basis, quantity_unit, source_code, measure_scope,
    }]

    `condition_combination`：该台班定额适用的条件（如 `{"岩类别": "坚硬岩"}`）。
    机械侧条件精筛（D2 裁定-3）需要它；**只加列，不改任何既有列语义/返回顺序**。

    `measure_scope`（契约 §1）：该台班定额的分母计量对象。**列可能还不存在**
    （WS6 并行迁移）→ 探测不到时一律 `''`（"未填"），绝不置空整个结果集。
    """
    has_scope = has_column("Norm_Equipment_Table", "measure_scope")
    col = ", measure_scope" if has_scope else ""
    rows = _query_all(
        """
        SELECT condition_text, machine_combination_json, machine_spec_json,
               machine_shift_norm_json, machine_shift_unit_json,
               quantity_basis, quantity_unit, source_code,
               condition_combination%s
        FROM Norm_Equipment_Table WHERE activity_id = ? ORDER BY condition_text
        """ % col, (activity_id,))
    return [
        {"condition_text": r[0], "machine_combination_json": r[1], "machine_spec_json": r[2],
         "machine_shift_norm_json": r[3], "machine_shift_unit_json": r[4],
         "quantity_basis": r[5], "quantity_unit": r[6], "source_code": r[7],
         "condition_combination": r[8],
         "measure_scope": measure_scope_of_row(r, 9 if has_scope else None)}
        for r in rows
    ]


# ==================== v2.1：人工定额 / 工作面容量 / 主控机械 / 配员 / 工人种类 ====================
# 全部沿用"优雅降级"约定：DB 不可用时返回 None 或空列表，绝不抛异常。

def l3_of_activity(activity_id):
    """某 L4 属于哪个 L3（工种大类）。返回 work_type_id 或 None。"""
    rows = _query_all("SELECT work_type_id FROM L4_Activity_Dictionary WHERE activity_id = ?",
                      (activity_id,))
    return rows[0][0] if rows else None


def _labor_basis_column():
    """Norm_Labor_Table 里"原始基数"列名：第 37 轮后为 raw_quantity_basis。

    迁移未执行时退回旧列名 quantity_basis（读到的仍是同一个数）。
    """
    cols = {r[1] for r in _query_all("PRAGMA table_info(Norm_Labor_Table)")}
    for name in ("raw_quantity_basis", "quantity_basis"):
        if name in cols:
            return name
    return "_missing_basis_column_"


def _basis_deprecation_note(basis_col):
    """（保留给文档/测试引用）quantity_basis 的废弃口径说明。"""
    if basis_col == "quantity_basis":
        return ("quantity_basis（deprecated）：与 raw_quantity_basis 同值。"
                "第 37 轮前下游把它当乘数用（产能 = 基数/定额），"
                "正确口径是 productivity_value = 1/labor_norm_value，基数仅溯源")
    return ("quantity_basis（deprecated，别名 = raw_quantity_basis）："
            "原始书页口径的批量基数（1 / 10 / 100 / 1000），仅用于溯源，"
            "绝不参与产能计算；产能 = 1/labor_norm_value")


def labor_norms(activity_id):
    """某 L4 的全部人工定额行。

    返回 [{norm_id, condition_text, condition_combination, norm_value, norm_unit,
           raw_quantity_basis, quantity_basis(deprecated), quantity_unit,
           productivity_value, productivity_unit, source_code, source_item_code, status,
           measure_scope}]

    - norm_value          ：工日 / **1 个** quantity_unit（已归一）。书页上的
      「工日 / 10m²」这类批量分母已折进 norm_value，分母见到 10 就是重复乘。
    - raw_quantity_basis  ：原始书页基数（1/10/100/1000），**只作溯源**；
      `raw_value / raw_quantity_basis == norm_value` 是留档不变式。
    - quantity_basis      ：已废弃别名（= raw_quantity_basis 同值）。保留它只是为了让
      仓库里那条写死 `quantity_basis` 的旧测试（`tests/test_norm_basis.py`）还能执行到
      断言。**禁止当乘数用** —— 产能只有一条口径：`1 / norm_value`。
    - productivity_value  ：单位/工日，即"每人每天产量"，= 1/norm_value。
      库里为 NULL/<=0 时用 kb_units.productivity_of(norm_value) 现场兜底（不改库）。
    - norm_unit           ：归一后的「工日/<规范单位>」（normalize_unit 处理 m3→m³ 等）。
    - measure_scope       ：该人工定额的**分母计量对象**（契约 §1）。列可能还不存在
      （WS6 并行迁移）→ 探测不到时一律 `''`（"未填"），绝不抛异常。
    """
    basis_col = _labor_basis_column()
    has_scope = has_column("Norm_Labor_Table", "measure_scope")
    scope_col = ", measure_scope" if has_scope else ""
    rows = _query_all(
        """
        SELECT norm_id, condition_text, condition_combination, labor_norm_value,
               labor_norm_unit, %s, quantity_unit, productivity_value,
               productivity_unit, source_code, source_item_code, status%s
        FROM Norm_Labor_Table WHERE activity_id = ?
        ORDER BY condition_text
        """ % (basis_col, scope_col), (activity_id,))
    out = []
    for r in rows:
        norm_value = r[3]
        pv = r[7]
        try:
            need_fallback = pv is None or float(pv) <= 0
        except (TypeError, ValueError):
            need_fallback = True
        if need_fallback:
            pv = kb_units.productivity_of(norm_value)
        out.append({
            "norm_id": r[0], "condition_text": r[1], "condition_combination": r[2],
            "norm_value": norm_value, "norm_unit": kb_units.normalize_unit(r[4]),
            "raw_quantity_basis": r[5], "quantity_basis": r[5],
            "quantity_unit": r[6], "productivity_value": pv, "productivity_unit": r[8],
            "source_code": r[9], "source_item_code": r[10], "status": r[11],
            "measure_scope": measure_scope_of_row(r, 12 if has_scope else None),
        })
    return out


def labor_norm_match(activity_id, keywords):
    """按关键字 AND 匹配人工定额条件（对齐 query_duration.py --match 的口径）。

    keywords: 关键字列表，例如 ["框架梁", "≤25"]。
    命中判定同时看 condition_text 与 condition_combination（构件类型等维度
    常常只存在后者里，例如 REBAR_NEW_BEAM 的"框架梁"）。
    返回命中的行列表（可能为空 / 可能多行——调用方自行判断是否唯一）。
    """
    kws = [str(k).strip() for k in (keywords or []) if str(k).strip()]
    rows = labor_norms(activity_id)
    if not kws:
        return rows
    out = []
    for r in rows:
        hay = f"{r.get('condition_text') or ''} {r.get('condition_combination') or ''}"
        if all(k in hay for k in kws):
            out.append(r)
    return out


def typical_labor_norm(activity_id):
    """取"典型"人工定额（用户未提供条件时的兜底）。

    策略：单行直接用；多行时取 productivity_value 的**中位数所在行**，即"典型值"。
    返回 {"norm": 行, "candidates": 候选行数, "match_type": "exact"|"default"}；无定额返回 None。
    """
    rows = [r for r in labor_norms(activity_id) if (r.get("productivity_value") or 0) > 0]
    if not rows:
        # 退一步：只要有定额值就用它
        rows = [r for r in labor_norms(activity_id) if (r.get("norm_value") or 0) > 0]
        if not rows:
            return None
        if len(rows) == 1:
            return {"norm": rows[0], "candidates": 1, "match_type": "exact"}
        return {"norm": rows[len(rows) // 2], "candidates": len(rows), "match_type": "default"}
    if len(rows) == 1:
        return {"norm": rows[0], "candidates": 1, "match_type": "exact"}
    srt = sorted(rows, key=lambda r: r["productivity_value"])
    return {"norm": srt[len(srt) // 2], "candidates": len(rows), "match_type": "default"}


_CAPACITY_V2_KEYS = (
    "quantity_unit", "q_ref", "crew_base", "crew_step_q", "crew_step_n",
    "crew_min", "crew_max", "segments_factor",
    "machine_q_ref", "machine_base", "machine_step_q", "machine_step_n",
    "machine_min", "machine_max",
)

# ⚠️ 域 1.6（第 6 批）：`_WORKFACE_COLUMNS`（合表后的列顺序）与
# `_CAPACITY_COMPAT_FROM_COLUMN`（`max_labor ← legacy_max_labor` 的兼容键映射）
# 随 `_workface_row()` / `workface_capacity()` 一并退役 —— 它们只服务于那张已删的表。


# ⚠️ 域 1.6（第 6 批）：原先这里另有 `_workface_row()` / `workface_capacity()` 一对函数，
# 从 `Workface_Capacity_Rule` 取「每活动每施工段最多几人/几台」。该表已删除
# （kb.db 21 → 20 表），两者随之退役：
#   · 它们查的是**已被删除的表**，外面只包一层 `except Exception: return None`，
#     于是恒定返回 None 而**调用方完全看不出"数据源已不存在"**（静默降级）；
#   · `Resource_Workface_Index`（MWI，67 行）**无法**顶上：它的主键是**资源名**
#     （电焊工/瓦工…），列是 `mwi`（一个工人所需最小工位面积，如 12 m²/人），
#     回答的是"一人要多大工位"，而旧表回答"一条活动每班最多几人"——主键、量纲、
#     语义全不同，不存在等价迁移。
# 容量主口径改为 `segment_capacity.segment_capacity`（段面积 ÷ MWI），见域 7.1/7.11。
# 叶子仍可自带 `workface_capacity` 键（用户/上游显式给的上限仍然说话），
# `scheduler.workface_limits_from_rule` 继续从叶子读它 —— 但**不再从 KB 补齐**。


def production_method_baseline(work_type_l3):
    """某 L3 工程类型的生产方式基线（`Production_Method_Baseline`）。

    返回 {default_mode, qty_threshold_high, qty_threshold_low,
          machine_activity_hint, notes, source_type, confidence} 或 None
    （表缺失 / 该 L3 未标定）。

    - default_mode          ：'machine' | 'labor'；
    - qty_threshold_high/low：任务量级阈值（同 quantity_unit；未标定为 None）；
    - machine_activity_hint ：该 L3 里可直接改绑的机械活动 activity_id（可能为 None）。
    数据来源已写进 source_type（contract_rule / kb_dict_majority / ai_estimate），
    调用方必须把 source_type / confidence 一起标出来。
    """
    if not work_type_l3:
        return None
    rows = _query_all(
        """SELECT default_mode, qty_threshold_high, qty_threshold_low,
                  machine_activity_hint, notes, source_type, confidence
           FROM Production_Method_Baseline WHERE work_type_l3 = ?""", (work_type_l3,))
    if not rows:
        return None
    r = rows[0]
    return {"default_mode": r[0], "qty_threshold_high": r[1], "qty_threshold_low": r[2],
            "machine_activity_hint": r[3], "notes": r[4], "source_type": r[5],
            "confidence": r[6]}


def main_machine(activity_id, condition_text=None):
    """某 L4 的主控机械。给出 condition_text 时优先精确匹配，否则返回全部候选。

    返回 [{condition_text, machine_name, machine_spec, source_type, confidence}]
    """
    rows = _query_all(
        """SELECT condition_text, machine_name, machine_spec, source_type, confidence
           FROM Activity_Main_Machine WHERE activity_id = ?""", (activity_id,))
    out = [{"condition_text": r[0], "machine_name": r[1], "machine_spec": r[2],
            "source_type": r[3], "confidence": r[4]} for r in rows]
    if condition_text and out:
        exact = [r for r in out if r["condition_text"] == condition_text]
        if exact:
            return exact
    return out


def crew_for_machine(machine_name, machine_spec=None):
    """机械配员（Equipment_Crew_Mapping）。

    返回 {machine_name, machine_spec, crew_size, composition, source_type, confidence} 或 None。
    注意：配员表只覆盖 20 种机械，其余返回 None（调用方须标注"无配员数据"）。
    """
    rows = _query_all(
        """SELECT machine_name, machine_spec, default_crew_size, crew_composition,
                  source_type, confidence
           FROM Equipment_Crew_Mapping WHERE machine_name = ?""", (machine_name,))
    if not rows and machine_name:
        # 退一步：模糊匹配（KB 机械名常带规格后缀）
        rows = _query_all(
            """SELECT machine_name, machine_spec, default_crew_size, crew_composition,
                      source_type, confidence
               FROM Equipment_Crew_Mapping WHERE ? LIKE '%' || machine_name || '%'""",
            (machine_name,))
    if not rows:
        return None
    r = rows[0]
    return {"machine_name": r[0], "machine_spec": r[1], "crew_size": r[2],
            "composition": r[3], "source_type": r[4], "confidence": r[5]}


def l3_labor_type(work_type_id):
    """L3 → 主要工人种类。返回 {labor_type, source_type, confidence, notes} 或 None。

    labor_type 为空串表示"管理/验收类，不派工种"。
    """
    rows = _query_all(
        """SELECT labor_type, source_type, confidence, notes
           FROM L3_Labor_Type WHERE work_type_id = ?""", (work_type_id,))
    if not rows:
        return None
    return {"labor_type": rows[0][0] or "", "source_type": rows[0][1],
            "confidence": rows[0][2], "notes": rows[0][3]}


def labor_type_for_activity(activity_id):
    """一条 L4 该派什么工种：按 L3 规则判定。

    H6（2026-09-21）：`L4_Labor_Type_Override` 表已删除（0 行空表），
    原「先查 L4 例外表、再退回 L3」的例外分支整体移除 —— 该分支恒返回 None，
    删除后行为不变。

    返回 {labor_types: [...], source: "L3规则", ref: work_type_id}
    """
    l3 = l3_of_activity(activity_id)
    if not l3:
        return {"labor_types": [], "source": "无", "ref": "", "confidence": ""}
    t = l3_labor_type(l3)
    if t is None:
        return {"labor_types": [], "source": "无", "ref": l3, "confidence": ""}
    lt = t.get("labor_type") or ""
    return {"labor_types": [lt] if lt else [],
            "source": "L3规则", "ref": l3, "confidence": t.get("confidence", "")}


def structure_l4_filtered(work_type_id, structure_type_id):
    """某 L3 下、在给定结构形式下"可用"的 L4 清单（剔除 EXCLUDED）。

    返回 ([可用 L4 行], mapping_absent)：
      - mapping_absent=True 表示该 L3 在该结构下**没有任何映射数据**，
        此时返回该 L3 的全部 L4（不因缺数据而误删），由调用方标注"无结构约束"。
    """
    acts = l4_for(work_type_id)
    if not structure_type_id:
        return acts, True
    rows = _query_all(
        """SELECT activity_id, applicability_level FROM Structure_Type_L4_Mapping
           WHERE structure_type_id = ?""", (structure_type_id,))
    if not rows:
        return acts, True
    lvl = {r[0]: r[1] for r in rows}
    touched = [a for a in acts if a["activity_id"] in lvl]
    if not touched:
        # 该 L3 一条映射都没有 → 不做过滤（避免"没数据就误删"）
        return acts, True
    keep = []
    for a in acts:
        v = lvl.get(a["activity_id"])
        if v == "EXCLUDED":
            continue
        keep.append(dict(a, applicability_level=v))
    return keep, False


# ==================== v2.2：进程内查询缓存 ====================
# 为什么需要：竖向改成"一层一段"后叶子数量增长 3.6~7 倍（38 层主体 = 380 条），
# 而同一个 activity_id 会被每个施工段反复查询——实测 738 次查询只涉及 16 个不同活动，
# 白花 1.17 秒。这层缓存同时解决"测试变慢导致偶发超时"和"生产端性能"。
#
# 只缓存"按 activity_id 纯查字典/规则表"且结果很小的函数；返回值做深拷贝，
# 避免调用方修改共享对象。定额明细（labor_norms / equipment_norms）行数多、
# 且规范要求可被调用方按条件筛选，故**不缓存**。

import copy as _copy

_KB_CACHE = {}
_KB_CACHE_MAX = 50000
_MISS = object()


def _memoize(fn):
    """按全部入参做进程内记忆化；命中时返回深拷贝。"""

    def wrapper(*args):
        key = (fn.__name__,) + tuple(args)
        hit = _KB_CACHE.get(key, _MISS)
        if hit is not _MISS:
            return _copy.deepcopy(hit)
        val = fn(*args)
        if len(_KB_CACHE) < _KB_CACHE_MAX:
            _KB_CACHE[key] = val
        # 冷路径也必须返回副本：否则调用方一改，等于改进了缓存本身
        return _copy.deepcopy(val)

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


def clear_cache():
    """清空查询缓存（知识库更新后、或测试之间需要隔离时调用）。

    同时清空**列探测缓存**：`measure_scope` 那类列是 WS6 用 DDL 后加的，
    `PRAGMA table_info` 的结果在进程内被缓存（每行定额探测一次太贵）。
    迁移脚本执行完、或测试给临时库加了列之后必须调本函数，否则缓存里还留着
    "没有这一列"的旧结论。
    """
    _KB_CACHE.clear()
    _COLUMN_CACHE.clear()


def cache_stats():
    """缓存条目数（诊断用）。"""
    return {"entries": len(_KB_CACHE), "max": _KB_CACHE_MAX}


for _name in ("l3_of_activity", "l3_labor_type",
              "labor_type_for_activity", "activity_info",
              "production_method_baseline", "main_machine", "l4_for", "l3_for",
              "structure_l4_filtered"):
    if _name in globals():
        globals()[_name] = _memoize(globals()[_name])



# -*- coding: utf-8 -*-
"""工作面容量**合表**迁移：两张同源表 → 一张 `Workface_Capacity_Rule`（v3 结构）。

## 为什么要有这一步

`BuildPlan_KB/kb.db` 里曾有两张**同源**表（都是 478 行 / 478 个活动，
`source_type='ai_estimate'` / `confidence='LOW'`）：

| 表 | 形态 | 问题 |
| --- | --- | --- |
| `Workface_Capacity_Rule`（v1，9 列） | 纯常数：`max_labor` / `max_machine` | "一个东西两个名字"的根源 |
| `Workface_Capacity_Rule_v2`（v2，23 列） | 公式版：`crew_base/step_q/step_n/min/max` | 与 v1 并存 → 读哪张靠调用方约定 |

后果：`kb.workface_capacity()` 要返回"v2 键 ∪ 旧键"的并集，冲突时旧键优先；
排程/资源/修订三条路各自判断回退；代码与 AI 都要记两套列名。
本脚本把它们合成**一张** v3 结构的 `Workface_Capacity_Rule`，旧两表**改名归档**
（`*_legacy_v1` / `*_legacy_v2`，只读保留供审计，**不得被运行时读取**）。

## 铁律：数值一个都不许变

v3 的每一列都是**原样搬运**，不重新标定、不美化 confidence：

* `crew_base/crew_step_q/crew_step_n/crew_min/crew_max/q_ref/quantity_unit/`
  `work_type_l3/model_version/q_ref_source` ← v2 原值；
* `machine_q_ref/machine_base/machine_step_q/machine_step_n/machine_min/machine_max`
  ← v2 原值（v2 的这六列本就是"只认 v1.max_machine（70/478 行）"生成的）；
* `segments_factor` ← v2 原值（全表 1，语义是 **0/1 闸门**不是折减系数）；
* `source_type/confidence` ← **v1 优先**（旧 `kb.workface_capacity()` 的"旧键优先"
  语义；实测两表同为 `ai_estimate`/`LOW`，逐行相同）；
* `notes` ← v1.notes + "；" + v2.notes（**逐字复刻旧 `kb.workface_capacity()` 的
  运行时拼接结果**：v1 原文在前，v2 标定说明在后；v2 的说明已含 v1 时不重复）。
  这样合表后 `kb.workface_capacity()["notes"]` 与合表前**逐字节相同**。
* `created_at` ← v2.created_at（`2026-09-19 13:39:41`，标定生成时刻，全表同一秒）。

只有三列是**新增**、且**一律取保守默认值**：

* `crew_preferred` ← **= crew_base**（无节拍要求时的默认配置，暂等于基准人数）；
* `min_workface_qty` / `max_workface_qty` ← **NULL**（作业面划分依据待标定；
  宁可 NULL 也不许编）；
* `review_state` ← `'pending'`（新增列，默认值）；`evidence_ref` ← NULL（依据留在 notes）。

## 幂等

* 第 2 次运行：检测到 `Workface_Capacity_Rule` 已是 v3 结构（有 `crew_preferred` 列）
  → **什么都不做**，绝不重建、绝不覆盖已归档表。
* 归档表已存在时：**保留原表**，不 drop 不重命名（只读审计件）。
* 默认 dry-run；`--apply` 才写库，写前自动备份 `BuildPlan_KB/kb.db.bak_<YYYYmmdd_HHMMSS>`。

用法：
    python devtools/migrate_v3_workface.py            # dry-run：只打印将改什么
    python devtools/migrate_v3_workface.py --apply    # 真正写库（先自动备份）
    python devtools/migrate_v3_workface.py --db <path> --apply
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "BuildPlan_KB", "kb.db")

LIVE = "Workface_Capacity_Rule"          # 合表后的唯一运行时表（v3 结构）
#: ⚠️ 这两个名字是本脚本在**迁移 pre-v3 库时创建**的归档表名（`ALTER TABLE … RENAME TO`），
#: **不是**本脚本期望已存在的表。已迁移完成的库里它们已被 H2/H3（2026-09-21）删除 →
#: 所有引用处一律"存在就用、不存在就跳过"（`table_exists`/`count_rows` 均容错），
#: 运行路径里不主动列举这两个已删表名。
ARCHIVE_V1 = "Workface_Capacity_Rule_legacy_v1"
ARCHIVE_V2 = "Workface_Capacity_Rule_legacy_v2"
V2_LIVE = "Workface_Capacity_Rule_v2"

# v3 结构里"新增且保守"的默认值
V3_REVIEW_STATE = "pending"
V3_UNIT_BASIS_DEFAULT = "每施工段"

# 新增列的判据：只有 v3 表才有 crew_preferred
V3_MARKER_COLUMN = "crew_preferred"

DDL_V3 = """
CREATE TABLE Workface_Capacity_Rule (
  rule_id       TEXT PRIMARY KEY,
  activity_id   TEXT NOT NULL,
  work_type_l3  TEXT,
  quantity_unit TEXT,
  unit_basis    TEXT NOT NULL DEFAULT '每施工段',   -- 容量的分母：一个作业面
  -- 人工侧：一个作业面上的"合理同时作业人数"
  crew_base      INTEGER,   -- 参考工程量(q_ref)下的基准人数
  crew_preferred INTEGER,   -- 【新增】无节拍要求时的默认配置；现在 = crew_base
  crew_min       INTEGER,
  crew_max       INTEGER,   -- 公式上限：一个作业面最多站几人（q_ref/step 放大后的夹紧上限）
  legacy_max_labor   INTEGER, -- 兼容列：= 旧 v1 表的 max_labor。**不要删**（见文件头注释）
  q_ref REAL, crew_step_q REAL, crew_step_n INTEGER,
  -- 【新增】作业面划分依据；现在**一律 NULL**，等标定（宁可 NULL 也不许编）
  min_workface_qty REAL,    -- 低于此量不值得单独开一个面
  max_workface_qty REAL,    -- 高于此量必须再开一个面
  -- 机械侧
  machine_base INTEGER, machine_min INTEGER, machine_max INTEGER,
  legacy_max_machine INTEGER, -- 兼容列：= 旧 v1 表的 max_machine
  machine_q_ref REAL, machine_step_q REAL, machine_step_n INTEGER,
  -- 闸门 + 证据 + 版本
  segments_factor INTEGER DEFAULT 1,
  source_type TEXT,
  confidence  TEXT
      CHECK (confidence IS NULL OR confidence IN ('HIGH','MEDIUM','LOW','UNKNOWN')),
  review_state TEXT DEFAULT 'pending',   -- approved / pending / rejected（新增，默认 pending）
  evidence_ref TEXT,                      -- 规范/定额/审定出处；拿不到就 NULL 并把依据留在 notes
  notes TEXT, model_version TEXT, q_ref_source TEXT,
  created_at TEXT, updated_at TEXT
)
"""

# ---------------------------------------------------------------------------
# 为什么表里还留着 `legacy_max_labor` / `legacy_max_machine` 两列
# ---------------------------------------------------------------------------
# 合表前 `kb.workface_capacity()` 返回的是"v2 公式键 ∪ v1 旧键并集"，其中
#   max_labor   = v1.max_labor   （**不等于** crew_max！）
#   max_machine = v1.max_machine （= machine_max，实测 487/487 相同）
# 实测 `v1.max_labor != v2.crew_max` 的有 **387/487 行**（例：REBAR_NEW_FOUND
# v1.max_labor=14，而 v2 同族 crew_max=15/16）。下游 `crew_bind._workface_payload()`
# 把这个兼容键原样写进 `leaf["workface_capacity"]["max_labor"]` 并落进计划 JSON，
# 现有用例（test_kb_integrity / test_crew_bind / test_plan_store 都断言 ==14）
# 与交付物口径都依赖它。
#
# 所以"兼容键 = crew_max"是**错的**（会把 14 悄悄变成 16）。两条路：
#   ① 兼容键直接取 crew_max —— 数值漂移，交付物变了 ⇒ 违反"逐位不变"铁律 ✗
#   ② 把 v1 的两个旧键作为**兼容列原样存进唯一的一张表** —— 数值零漂移 ✓
# 选 ②：缺点是多两列，优点是把"兼容键从哪来"这件事写成数据而不是代码约定，
# 而且归档表被删也不影响保真。这两列不参与任何公式计算，只是保真出口。


# 插入语句（32 列，顺序与 build_rows 返回的元组**逐位一致**）
INSERT_COLUMNS = (
    "rule_id", "activity_id", "work_type_l3", "quantity_unit", "unit_basis",
    "crew_base", "crew_preferred", "crew_min", "crew_max", "legacy_max_labor",
    "q_ref", "crew_step_q", "crew_step_n",
    "min_workface_qty", "max_workface_qty",
    "machine_base", "machine_min", "machine_max", "legacy_max_machine",
    "machine_q_ref", "machine_step_q", "machine_step_n",
    "segments_factor", "source_type", "confidence", "review_state", "evidence_ref",
    "notes", "model_version", "q_ref_source", "created_at", "updated_at",
)

# v3 表的**目标列集合**（判"是否已迁移完成"用；改 DDL 必须同步改这里）
TARGET_COLUMNS = frozenset(INSERT_COLUMNS)


# ---------------------------------------------------------------------------
# 基础设施
# ---------------------------------------------------------------------------

def table_exists(cur, table):
    return bool(cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def table_columns(cur, table):
    if not table_exists(cur, table):
        return []
    return [r[1] for r in cur.execute("PRAGMA table_info(%s)" % table)]


def count_rows(cur, table):
    if not table_exists(cur, table):
        return None
    return cur.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]


def _assert_ddl_matches_insert():
    """门禁：`DDL_V3` 建出来的列必须**恰好**等于 `INSERT_COLUMNS`（顺序无关）。

    开发时踩过：加了兼容列后忘了在 DDL 里补 `q_ref`，直到 `--apply` 往真库写
    才炸 `no column named q_ref`。在内存库里建一次表、比一次列集合就当场暴露，
    不必等到写真库。
    """
    probe = sqlite3.connect(":memory:")
    try:
        probe.execute(DDL_V3)
        ddl_cols = [r[1] for r in probe.execute("PRAGMA table_info(%s)" % LIVE)]
    finally:
        probe.close()
    missing = TARGET_COLUMNS - set(ddl_cols)
    extra = set(ddl_cols) - TARGET_COLUMNS
    if missing or extra:
        raise SystemExit(
            "DDL_V3 与 INSERT_COLUMNS 不一致：DDL 缺 %s；DDL 多 %s"
            % (sorted(missing) or "无", sorted(extra) or "无"))


def is_v3(cur):
    """`Workface_Capacity_Rule` 是否已是 **完整且列齐** 的 v3 单表。

    判据必须同时满足三条，缺一条就重做：
    * 结构：有 `crew_preferred` 列（只有 v3 才有）；
    * 列齐：列集合 ⊇ `TARGET_COLUMNS`（防止"上一版脚本建的 v3 少列"，
      比如少了 `legacy_max_labor` 的那版）；
    * 数据：行数 > 0。

    只判"表里有 crew_preferred"是不够的 —— 本脚本开发时踩过两次：
    ① 一次中途失败的写库留下"v3 **空表** + 两张归档表"；
    ② 加了兼容列之后，旧版脚本建的表"列不齐"。
    这两种状态都必须被识别为"待重建"，否则再跑 `--apply` 会直接跳过。
    """
    if V3_MARKER_COLUMN not in table_columns(cur, LIVE):
        return False
    if not TARGET_COLUMNS.issubset(set(table_columns(cur, LIVE))):
        return False
    return bool(count_rows(cur, LIVE))


V1_MARKER_COLUMN = "max_labor"           # 只有 v1 结构才有
V2_MARKER_COLUMN = "crew_base"           # 只有 v2/v3 结构才有


def _has_marker(cur, table, marker):
    """表存在且有该标志列时才认它。"""
    return table_exists(cur, table) and marker in table_columns(cur, table)


def _source_v2_table(cur):
    """v2 数据的来源表名：优先活动表，其次归档表；都没有 → None。

    ⚠️ 必须**按列名判结构**，不能按表名判存在：一次中途失败的 `--apply` 可能留下
    "v3 空表占着 `Workface_Capacity_Rule` 这个名字 + 真数据在归档表"的状态。
    只看表名会把 v3 表当成 v1/v2 源，SELECT 直接 `no such column`（实测踩过）。
    """
    if _has_marker(cur, V2_LIVE, V2_MARKER_COLUMN):
        return V2_LIVE
    if _has_marker(cur, ARCHIVE_V2, V2_MARKER_COLUMN):
        return ARCHIVE_V2
    return None


def _source_v1_table(cur):
    """v1 数据的来源表名：优先活动表，其次归档表；都没有 → None（理由同 v2）。"""
    if _has_marker(cur, LIVE, V1_MARKER_COLUMN):
        return LIVE
    if _has_marker(cur, ARCHIVE_V1, V1_MARKER_COLUMN):
        return ARCHIVE_V1
    return None


# ---------------------------------------------------------------------------
# 建 v3 行（纯搬运 + 复刻旧并集语义）
# ---------------------------------------------------------------------------

def _merge_notes(old_note, new_note):
    """逐字复刻旧 `kb.workface_capacity()` 的 notes 拼接：v1 原文在前，v2 说明在后。

    旧代码：
        old_note = (legacy[5] or "").strip(); new_note = (v2[18] or "").strip()
        if old_note and new_note and new_note not in old_note:
            notes = old_note + "；" + new_note
        else:
            notes = old_note or new_note
    """
    old_note = (old_note or "").strip()
    new_note = (new_note or "").strip()
    if old_note and new_note and new_note not in old_note:
        return old_note + "；" + new_note
    return old_note or new_note


def _pick(primary, fallback):
    """旧键优先：`primary` 为 None 时才用 `fallback`。"""
    return primary if primary is not None else fallback


def build_rows(cur, stats, now):
    """构造 v3 的 478 行（从 v1 + v2 原样搬运）。

    `stats` 会被填入搬运覆盖情况的计数；`now` 是 `updated_at` / 兜底 `created_at`。
    """
    v1_name = _source_v1_table(cur)
    v2_name = _source_v2_table(cur)
    if v1_name is None and v2_name is None:
        raise SystemExit("两张工作面容量表都不存在（%s / %s 或它们的归档表）"
                         % (LIVE, V2_LIVE))

    # ---- v1：unit_basis / max_labor / max_machine / source_type / confidence / notes
    v1 = {}
    if v1_name:
        for r in cur.execute(
                "SELECT activity_id, unit_basis, max_labor, max_machine, source_type, "
                "confidence, notes FROM %s" % v1_name):
            v1[r[0]] = {"unit_basis": r[1], "max_labor": r[2], "max_machine": r[3],
                        "source_type": r[4], "confidence": r[5], "notes": r[6]}
    stats["v1_table"] = v1_name
    stats["v1_rows"] = len(v1)

    # ---- v2：公式标定 18 列 + 机械六列
    v2 = {}
    order = []
    if v2_name:
        for r in cur.execute(
                "SELECT activity_id, rule_id, work_type_l3, quantity_unit, q_ref, "
                "crew_base, crew_step_q, crew_step_n, crew_min, crew_max, segments_factor, "
                "machine_q_ref, machine_base, machine_step_q, machine_step_n, machine_min, "
                "machine_max, model_version, q_ref_source, source_type, confidence, notes, "
                "created_at FROM %s ORDER BY activity_id" % v2_name):
            v2[r[0]] = r
            order.append(r[0])
    stats["v2_table"] = v2_name
    stats["v2_rows"] = len(v2)

    # 行的主序：v2 有则按 v2 的 activity_id 排序（保留其 rule_id 编号），
    # 否则退化为按 v1 排序（无标定行时）
    keys = order if order else sorted(v1)
    rows_out = []
    for aid in keys:
        b = v2.get(aid)
        a = v1.get(aid, {})
        if b is None:
            raise SystemExit("活动 %s 只有 v1 没有 v2：无法构造 v3（不编造公式列）" % aid)
        (r_aid, rule_id, l3, unit, q_ref, crew_base, step_q, step_n, crew_min, crew_max,
         seg_factor, m_q_ref, m_base, m_step_q, m_step_n, m_min, m_max, model_version,
         q_ref_source, v2_st, v2_cf, v2_notes, v2_created) = b
        old_note = a.get("notes")
        # 旧键优先（复刻旧并集语义；实测两表同为 ai_estimate / LOW）
        src_type = _pick(a.get("source_type"), v2_st)
        conf = _pick(a.get("confidence"), v2_cf)
        if a and a.get("unit_basis") is None:
            unit_basis = V3_UNIT_BASIS_DEFAULT
        else:
            unit_basis = a.get("unit_basis") or V3_UNIT_BASIS_DEFAULT
        rows_out.append((
            rule_id, r_aid, l3, unit, unit_basis,
            crew_base, crew_base, crew_min, crew_max,            # crew_base, crew_preferred, crew_min, crew_max
            a.get("max_labor"),                                   # legacy_max_labor（兼容列，原样）
            q_ref, step_q, step_n,
            None, None,                                          # min/max_workface_qty：待标定
            m_base, m_min, m_max,                                # machine_base, machine_min, machine_max
            a.get("max_machine"),                                 # legacy_max_machine（兼容列，原样）
            m_q_ref, m_step_q, m_step_n,
            seg_factor if seg_factor is not None else 1,
            src_type, conf, V3_REVIEW_STATE, None,               # review_state / evidence_ref
            _merge_notes(old_note, v2_notes), model_version, q_ref_source,
            v2_created or now, now,                              # created_at / updated_at
        ))

    stats["v3_rows"] = len(rows_out)
    stats["v3_crew_preferred_eq_base"] = sum(
        1 for r in rows_out if r[5] is not None and r[5] == r[6])
    stats["v3_machine_rows"] = sum(1 for r in rows_out if r[16] is not None)
    stats["v3_notes_merged"] = sum(
        1 for r in rows_out if "；" in (r[27] or ""))
    # 兼容列保真度：v1 有值的行里，legacy 列是否逐行相同
    stats["v3_legacy_labor_rows"] = sum(1 for r in rows_out if r[9] is not None)
    stats["v3_legacy_machine_rows"] = sum(1 for r in rows_out if r[18] is not None)
    stats["v3_legacy_labor_ne_crew_max"] = sum(
        1 for r in rows_out if r[9] is not None and r[9] != r[8])
    return rows_out


# ---------------------------------------------------------------------------
# 落库后自动对拍：v3 的每一列必须**原样**来自归档表
# ---------------------------------------------------------------------------

# (v3 列名, 归档表 'v1'/'v2', 归档列序号)
_DRIFT_CHECKS = (
    ("rule_id", "v2", 1), ("work_type_l3", "v2", 2), ("quantity_unit", "v2", 3),
    ("q_ref", "v2", 4), ("crew_base", "v2", 5), ("crew_step_q", "v2", 6),
    ("crew_step_n", "v2", 7), ("crew_min", "v2", 8), ("crew_max", "v2", 9),
    ("segments_factor", "v2", 10), ("machine_q_ref", "v2", 11),
    ("machine_base", "v2", 12), ("machine_step_q", "v2", 13),
    ("machine_step_n", "v2", 14), ("machine_min", "v2", 15), ("machine_max", "v2", 16),
    ("model_version", "v2", 17), ("q_ref_source", "v2", 18), ("created_at", "v2", 22),
    ("legacy_max_labor", "v1", 2), ("legacy_max_machine", "v1", 3),
)
_V2_SELECT = ("activity_id, rule_id, work_type_l3, quantity_unit, q_ref, crew_base, "
              "crew_step_q, crew_step_n, crew_min, crew_max, segments_factor, "
              "machine_q_ref, machine_base, machine_step_q, machine_step_n, machine_min, "
              "machine_max, model_version, q_ref_source, source_type, confidence, notes, "
              "created_at")


def verify_against_archives(cur):
    """把 v3 表逐列对拍归档表，返回不一致项 [(activity_id, 说明), ...]。

    这是"逐位不变"铁律的**自动证据**：写完库立刻跑一遍，不一致就报出来。
    归档表读的是 `_source_*` 认可的那张（活动表或归档表），所以迁移前后都能跑。
    """
    v1_name = _source_v1_table(cur)
    v2_name = _source_v2_table(cur)
    if not (v1_name and v2_name):
        return [("-", "归档表缺失，无法对拍")]
    v1 = {r[0]: r for r in cur.execute(
        "SELECT activity_id, unit_basis, max_labor, max_machine, source_type, "
        "confidence, notes FROM %s" % v1_name)}
    v2 = {r[0]: r for r in cur.execute(
        "SELECT %s FROM %s" % (_V2_SELECT, v2_name))}

    bad = []
    cols = table_columns(cur, LIVE)
    for row in cur.execute("SELECT * FROM %s ORDER BY activity_id" % LIVE):
        d = dict(zip(cols, row))
        aid = d["activity_id"]
        a, b = v1.get(aid), v2.get(aid)
        if a is None or b is None:
            bad.append((aid, "归档表无此行"))
            continue
        for v3c, which, idx in _DRIFT_CHECKS:
            src = a if which == "v1" else b
            if d[v3c] != src[idx]:
                bad.append((aid, "%s: v3=%r 归档=%r" % (v3c, d[v3c], src[idx])))
        # 兼容列：旧键优先时的来源标注
        if d["source_type"] != (a[4] if a[4] is not None else b[19]):
            bad.append((aid, "source_type 漂移"))
        if d["confidence"] != (a[5] if a[5] is not None else b[20]):
            bad.append((aid, "confidence 漂移"))
        if d["notes"] != _merge_notes(a[6], b[21]):
            bad.append((aid, "notes 拼接漂移"))
        if d["unit_basis"] != (a[1] or V3_UNIT_BASIS_DEFAULT):
            bad.append((aid, "unit_basis 漂移"))
        # 新增列的保守默认
        if d["crew_preferred"] != d["crew_base"]:
            bad.append((aid, "crew_preferred != crew_base"))
        if d["min_workface_qty"] is not None or d["max_workface_qty"] is not None:
            bad.append((aid, "min/max_workface_qty 不是 NULL"))
        if d["review_state"] != V3_REVIEW_STATE:
            bad.append((aid, "review_state != %s" % V3_REVIEW_STATE))
        if d["evidence_ref"] is not None:
            bad.append((aid, "evidence_ref 不是 NULL"))
        if d["updated_at"] is None:
            bad.append((aid, "updated_at 为空"))
    return bad


# ---------------------------------------------------------------------------
# 迁移动作
# ---------------------------------------------------------------------------
def migrate(cur, apply_changes, stats, now):
    """执行（或预览）合表。返回一段可打印的说明文本。"""
    if is_v3(cur):
        n = count_rows(cur, LIVE)
        # 已合表：什么都不做（幂等）。归档表动态发现（H2/H3 已删 → 通常为空）。
        kept = [r[0] for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name LIKE 'Workface_Capacity_Rule_legacy%' ORDER BY name")]
        stats["mode"] = "already_v3"
        stats["v3_rows_existing"] = n
        stats["archives_present"] = kept
        return ("已是 v3 单表（%d 行）：不做任何修改；归档表 %s"
                % (n, "、".join(kept) if kept else "（未发现，可能从未迁移过）"))

    rows = build_rows(cur, stats, now)
    v1_name = stats["v1_table"]
    v2_name = stats["v2_table"]

    # 归档表已存在 → 不覆盖（只读审计件）
    v1_already = table_exists(cur, ARCHIVE_V1)
    v2_already = table_exists(cur, ARCHIVE_V2)
    stats["archive_v1_pre_existing"] = v1_already
    stats["archive_v2_pre_existing"] = v2_already

    if not apply_changes:
        stats["mode"] = "dry_run"
        repopulate = table_exists(cur, LIVE)
        return ("将重建 %s 为 v3 结构并灌 %d 行%s；\n"
                "        %s → %s %s；\n"
                "        %s → %s %s"
                % (LIVE, len(rows),
                   "（现表是空的/列不齐的旧版表，将 DROP 后重建）" if repopulate else "",
                   v1_name, ARCHIVE_V1,
                   "（归档表已存在，保留不覆盖）" if v1_already else "",
                   v2_name, ARCHIVE_V2,
                   "（归档表已存在，保留不覆盖）" if v2_already else ""))

    # ---- 写库 ----
    # ① 先把旧 v1 活动表改名归档（顺序很重要：LIVE 这个名字要腾给 v3）
    if v1_name == LIVE:
        if v1_already:
            cur.execute("DROP TABLE %s" % LIVE)     # 归档件已在，活动表冗余 → 丢弃
        else:
            cur.execute("ALTER TABLE %s RENAME TO %s" % (LIVE, ARCHIVE_V1))
    # ② 把旧 v2 活动表改名归档
    if v2_name == V2_LIVE:
        if v2_already:
            cur.execute("DROP TABLE %s" % V2_LIVE)
        else:
            cur.execute("ALTER TABLE %s RENAME TO %s" % (V2_LIVE, ARCHIVE_V2))
    # ③ 建 v3 并灌数据（`DROP IF EXISTS` 同时清掉"v3 结构的空半成品"）
    cur.execute("DROP TABLE IF EXISTS %s" % LIVE)
    cur.execute(DDL_V3)
    placeholders = ", ".join("?" * len(INSERT_COLUMNS))
    cur.executemany(
        "INSERT INTO %s (%s) VALUES (%s)"
        % (LIVE, ", ".join(INSERT_COLUMNS), placeholders), rows)
    stats["mode"] = "applied"
    return ("已重建 %s（v3，%d 行）；归档 %s + %s（只读保留，仅审计用）"
            % (LIVE, len(rows), ARCHIVE_V1, ARCHIVE_V2))


def write_quality_log(cur, stats, now):
    """往 data_quality_log 写一条合表记录（表/列缺失则静默跳过）。

    ⚠️ `data_quality_log.issue_type` / `severity` 都有 CHECK 枚举，写错值会抛
    `IntegrityError`（本脚本开发时实测踩过一次）。这里的取值是**实测枚举内的**
    安全值：`issue_type='other'`、`severity='low'`（与 `migrate_v2_kb.py` 里
    "旧表全表 ai_estimate/LOW" 那条记录同一种写法）。
    调用方必须把本函数的异常吞掉：迁移已经落库，审计日志失败不该回滚数据。
    """
    try:
        cols = table_columns(cur, "data_quality_log")
        if not cols or "table_name" not in cols:
            return False
        want = {
            "table_name": LIVE,
            "record_id": "workface-capacity-merge",
            "issue_type": "other",
            "severity": "low",
            "description": ("工作面容量两张同源表（Workface_Capacity_Rule v1 9 列 / "
                            "Workface_Capacity_Rule_v2 23 列，各 %s 行）合并为一张 v3 结构表；"
                            "数值原样搬运，新增列 crew_preferred=crew_base、"
                            "min/max_workface_qty=NULL、review_state='pending'"
                            % stats.get("v1_rows")),
            "resolution": ("旧两表改名为 %s / %s 只读归档；运行时只读 %s"
                           % (ARCHIVE_V1, ARCHIVE_V2, LIVE)),
            "resolved_by": "devtools/migrate_v3_workface.py",
            "resolved_at": now, "created_at": now,
        }
        use = {k: v for k, v in want.items() if k in cols}
        names = list(use.keys())
        cur.execute("INSERT INTO data_quality_log (%s) VALUES (%s)"
                    % (", ".join(names), ", ".join("?" for _ in names)),
                    [use[n] for n in names])
        return True
    except sqlite3.Error as exc:
        print("   （提示：data_quality_log 审计记录未写入：%s）" % exc)
        return False


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="工作面容量合表迁移（v1+v2 → 单张 v3 结构表；默认 dry-run）")
    ap.add_argument("--apply", action="store_true", help="真正写库（先自动备份）")
    ap.add_argument("--dry-run", action="store_true",
                    help="显式声明只预览（默认行为，仅为可读性保留）")
    ap.add_argument("--db", default=DB_PATH, help="kb.db 路径（默认仓库内的 BuildPlan_KB/kb.db）")
    args = ap.parse_args(argv)

    apply_changes = bool(args.apply)
    db_path = args.db
    if not os.path.exists(db_path):
        sys.exit("找不到 KB：%s" % db_path)

    stats = {}
    print("=" * 78)
    print("工作面容量合表（v1 + v2 → 一张 v3 结构表）   模式 = %s"
          % ("APPLY（写库）" if apply_changes else "DRY-RUN（不写库）"))
    print("DB = %s" % db_path)
    print("sqlite3 = %s，python = %s" % (sqlite3.sqlite_version, sys.version.split()[0]))
    print("=" * 78)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    _assert_ddl_matches_insert()
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    try:
        # ---- 先判定有没有活要干，再决定要不要备份 ----
        # 无事可做时**不备份**：否则每次 --apply 都往 BuildPlan_KB/ 扔一个
        # 60MB 级的 kb.db.bak_* 副本，几天就把目录塞满。
        need_work = not is_v3(cur)
        bak_name = "-"
        if apply_changes and need_work:
            bak_name = "kb.db.bak_%s" % datetime.now().strftime("%Y%m%d_%H%M%S")
            shutil.copy2(db_path, os.path.join(os.path.dirname(db_path), bak_name))
            print("[1] 备份 -> %s" % bak_name)
        elif not apply_changes:
            print("[1] 备份：dry-run 不备份（--apply 时执行 "
                  "shutil.copy2 → kb.db.bak_<YYYYmmdd_HHMMSS>）")
        else:
            print("[1] 备份：无事可做（已是 v3 单表），不产生备份副本")
        stats["backup"] = bak_name

        print("[2] 迁移前各表行数：")
        for t in (LIVE, V2_LIVE, ARCHIVE_V1, ARCHIVE_V2):
            print("      %-34s %s" % (t, count_rows(cur, t)))

        desc = migrate(cur, apply_changes, stats, now)
        print("[3] %s" % desc)

        if apply_changes and stats.get("mode") == "applied":
            # ⚠️ 顺序铁律：**先提交数据迁移，再写审计日志**。
            # 反过来的话，审计日志里一个 CHECK 约束失败就会把已经写好的 v3 表
            # 回滚成空表（本脚本开发时实测踩过：留下"v3 空表 + 两张归档表"）。
            con.commit()
            stats["quality_log"] = write_quality_log(cur, stats, now)
            if stats["quality_log"]:
                con.commit()
            else:
                con.rollback()
            print("[4] 迁移后各表行数：")
            for t in (LIVE, V2_LIVE, ARCHIVE_V1, ARCHIVE_V2):
                print("      %-34s %s" % (t, count_rows(cur, t)))
            print("[5] 校验：v3 行的 crew_preferred IS crew_base = %d/%d；"
                  "机械侧有值 %d 行；notes 含『；』拼接 %d 行"
                  % (stats["v3_crew_preferred_eq_base"], stats["v3_rows"],
                     stats["v3_machine_rows"], stats["v3_notes_merged"]))
            print("    兼容列保真：legacy_max_labor 有值 %d 行（其中 != crew_max 的 %d 行 "
                  "← 这就是不能用 crew_max 顶替的证据）；legacy_max_machine 有值 %d 行"
                  % (stats["v3_legacy_labor_rows"], stats["v3_legacy_labor_ne_crew_max"],
                     stats["v3_legacy_machine_rows"]))
            drift = verify_against_archives(cur)
            stats["verify_drift"] = len(drift)
            print("[5b] 逐列对拍归档表：不一致 %d 项%s"
                  % (len(drift), "" if not drift else " → " + "; ".join(
                      "%s/%s" % (a, b) for a, b in drift[:5])))
            print("[6] data_quality_log 审计记录：%s"
                  % ("已写入" if stats["quality_log"] else "未写入（见上方提示）"))
        elif apply_changes:
            print("[4] 已合表（v3 单表 %s 行），无写库动作"
                  % count_rows(cur, LIVE))
        else:
            print("[4] dry-run：未写库；加 --apply 执行")
        print("备份文件：%s" % bak_name)
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

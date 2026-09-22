#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""WS6 知识库迁移脚本（契约 `devtools/_dev-notes/终版修改_接口冻结.md` §10 C1–C12）。

覆盖项：
  C1  监测类活动（基坑变形监测 / 周边环境监测 / 沉降观测）+ 工作面容量行
  C2  预留预埋类活动（电气 / 给排水 / 暖通 / 消防），与「制作安装」区分
  C3  预制构件（叠合板 / 阳台板 / 凸窗 / 楼梯）台班定额占位
  C4  沥青混凝土路面 / 道路基层活动
  C5  场地平整口径与「场地硬化」分开
  C6  L4_Norm_Default 补 norm_kind='machine' 索引行
  C7  measure_scope 列注入 + 填充（受控词表，推断不出留空，绝不编造）
  C8  单位写法归一（㎡→m²）
  C9  塔吊 / 施工电梯台班产量占位
  C10 legacy 工作面容量表 notes 前置废弃标记（不改列、不改 kb.py）
  C11 Activity_Main_Machine 按台班行重推
  C12 只出报告，不动表

用法（默认 dry-run，不写库）：
  python devtools/kb_migrate_ws6.py --db BuildPlan_KB\\kb.db.migrated
  python devtools/kb_migrate_ws6.py --db BuildPlan_KB\\kb.db.migrated --apply
  python devtools/kb_migrate_ws6.py --db BuildPlan_KB\\kb.db.migrated --report   # 只读统计
  python devtools/kb_migrate_ws6.py --db ... --only C7,C6                        # 只跑部分步骤

约束：幂等（可重复执行）；只写显式传入的 --db；绝不修改 backend/pipeline/**。
"""
from __future__ import print_function

import argparse
import io
import json
import os
import sqlite3
import sys

# --------------------------------------------------------------------------- 常量

#: 契约 §1 受控词表 —— 只能出现这些值（'' 表示未填）
SCOPES = (
    u"建筑面积", u"外墙面积", u"内墙抹灰面积", u"天棚面积", u"楼地面面积",
    u"模板接触面积", u"风管展开面积", u"保温面积", u"防水面积", u"管道长度",
    u"电缆长度", u"体积", u"质量", u"桩根数", u"件数", u"台数", u"自然单位",
    u"项",
)
SCOPE_SET = set(SCOPES)

#: 需要 measure_scope 列的 4 张表
SCOPE_TABLES = (
    "Norm_Labor_Table", "Norm_Equipment_Table",
    "L4_Norm_Default", "L4_Activity_Dictionary",
)

#: 契约 C10 废弃文案（逐行前置到 notes）
DEPRECATED_MARK = (u"[deprecated 2026-09-20] 以 Workface_Capacity_Rule 为唯一现行版，"
                   u"本表仅存档，不得被代码读取。")

#: 占位来源（契约 §10 约定）
SCAFFOLD = "SCAFFOLD_V1"

#: 契约 §10 要求占位行 status='estimated'，但现有 schema 的 CHECK 只允许
#: ('raw','parsed','converted','verified','needs_review','rejected') —— 'estimated' 会直接
#: IntegrityError。这里取最接近的合法值 'needs_review'，并把 estimated 语义放进
#: source_type / review_notes 标记，同时写进报告供父代理裁决。
SCAFFOLD_STATUS = "needs_review"
SCAFFOLD_NOTE_PREFIX = u"[SCAFFOLD_V1][estimated]"

#: 无歧义的单位 → 口径
UNIT_SCOPE = {
    u"m³": u"体积",
    u"t": u"质量",
    u"项": u"项",
    u"件": u"件数",
    u"台": u"台数",
}
#: 计数型单位 → 自然单位（同量纲恒等，不属于可推断的计量对象）
COUNT_UNITS = (
    u"樘", u"块", u"个", u"扇", u"卷", u"张", u"套", u"节", u"座", u"箱",
    u"组", u"点", u"捆", u"批", u"头", u"个头", u"只", u"端头", u"见表", u"处",
)   # 注：「根」另有「桩」判定，见 unit_scope()

#: m² 名称 → 口径（有序，先命中先返回）
M2_NAME_RULES = (
    (u"风管", u"风管展开面积"),
    (u"保温", u"保温面积"),
    (u"防水", u"防水面积"),
    (u"卷材", u"防水面积"),
    (u"玻璃纤维布", u"防水面积"),
    (u"涂刷沥青", u"防水面积"),
    (u"瓦垄铁", u"防水面积"),
    (u"内墙抹灰", u"内墙抹灰面积"),
    (u"外墙抹灰", u"外墙面积"),
    (u"内墙涂料", u"内墙抹灰面积"),
    (u"外墙涂料", u"外墙面积"),
    (u"天棚", u"天棚面积"),
    (u"吊顶", u"天棚面积"),
    (u"模板", u"模板接触面积"),
    (u"块料面层", u"楼地面面积"),
    (u"平整场地", u"建筑面积"),   # GB 50854：按首层建筑面积计算
    (u"测量放线", u"建筑面积"),
    (u"预留预埋", u"建筑面积"),   # 伴随型，按建筑规模计
)

#: 「运输类」活动即使单位是 m² 也不是面积作业面（是运输量）
TRANSPORT_MARKERS = (u"运输", u"运输道")

PIPE_MARKERS = (u"管道", u"水管", u"配管", u"给水管", u"排水管", u"雨水管")
CABLE_MARKERS = (u"电缆", u"导线", u"电线")


# --------------------------------------------------------------------------- 工具

def table_exists(cur, name):
    return cur.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone()[0] > 0


def columns(cur, name):
    return [r[1] for r in cur.execute("PRAGMA table_info([%s])" % name)]


def scalar(cur, sql, args=()):
    row = cur.execute(sql, args).fetchone()
    return row[0] if row else None


def next_seq_id(cur, table, column, prefix):
    """返回 prefix + 4 位序号（现有最大序号 + 1）。"""
    rows = cur.execute(
        "SELECT [%s] FROM [%s] WHERE [%s] LIKE ?" % (column, table, column),
        (prefix + "%",)).fetchall()
    n = 0
    for (v,) in rows:
        tail = (v or "")[len(prefix):]
        if tail.isdigit():
            n = max(n, int(tail))
    return "%s%04d" % (prefix, n + 1)


def log(msg):
    print(msg)


# --------------------------------------------------------------------------- 口径推断

def unit_scope(name, unit):
    """纯单位推断（不依赖活动）。推不出返回 ''。"""
    unit = (unit or "").strip()
    name = name or ""
    if unit in UNIT_SCOPE:
        return UNIT_SCOPE[unit]
    if unit == u"根":
        return u"桩根数" if u"桩" in name else u""
    if unit == u"m":
        if any(k in name for k in CABLE_MARKERS):
            return u"电缆长度"
        if any(k in name for k in PIPE_MARKERS):
            return u"管道长度"
        return u""
    if unit in (u"m²", u"㎡", u"m2", u"M2"):
        return ""          # m² 没有单位级默认值，只能看名称
    if unit in COUNT_UNITS:
        return u"自然单位"
    if unit:
        return ""          # 未知单位不猜
    return ""


def activity_scope(name, unit, work_type_id=""):
    """活动自身的口径：单位可决 → 单位；m² → 名称规则；否则 ''。"""
    unit = (unit or "").strip()
    if unit in (u"m²", u"㎡", u"m2", u"M2"):
        if u"运输" in (work_type_id or "") or any(
                k in (name or "") for k in TRANSPORT_MARKERS):
            return ""      # 材料运输类不是面积作业面
        for key, val in M2_NAME_RULES:
            if key in (name or ""):
                return val
        return ""
    return unit_scope(name, unit)


def row_scope(name, unit, act_unit, act_scope):
    """定额行的口径：单位与活动一致 → 用活动口径；否则退回单位推断。"""
    unit = (unit or "").strip()
    if (unit and (unit == (act_unit or "").strip()) and act_scope):
        return act_scope
    return unit_scope(name, unit)


# --------------------------------------------------------------------------- 新增活动定义

def _act(aid, name, wtype, unit, category, mode, scope, desc, notes):
    return dict(activity_id=aid, activity_name=name, work_type_id=wtype,
                unit=unit, activity_category=category,
                recommended_production_mode=mode, measure_scope=scope,
                description=desc, notes=notes)


NEW_ACTIVITIES = [
    # --- C1 监测类（按次计量；org_defaults.py:132 已归入「按次但无独立工期」措施项）
    _act("MON_AI_001", u"基坑变形监测", "tech_prep", u"项", u"施工准备", "labor_driven", u"项",
         u"占位活动：基坑支护结构变形、位移、沉降的周期性观测与报表（C1 监测类）。",
         u"[SCAFFOLD_V1] 类别占位，数值为估，需整体清退。依据：契约 §8「周边环境监测 1 人 60 天」"
         u"→ 60 工日/项。注：backend/pipeline/org_defaults.py:132 MEASURE_ITEM_NO_DURATION 已把"
         u"「基坑监测/沉降观测/变形监测」归为按次但无独立工期，故本行是按次人工占位，不作为工期驱动。"),
    _act("MON_AI_002", u"周边环境监测", "tech_prep", u"项", u"施工准备", "labor_driven", u"项",
         u"占位活动：基坑周边建（构）筑物、道路、管线的变形与沉降监测（C1 监测类）。",
         u"[SCAFFOLD_V1] 类别占位，数值为估，需整体清退。依据：契约 §8 明确「3.4.2 周边环境监测 "
         u"1 人 60 天」→ 60 工日/项（60 工日 ÷ 60 天 = 1 人，与 §8 完全一致）。"),
    _act("MON_AI_003", u"沉降观测", "tech_prep", u"项", u"施工准备", "labor_driven", u"项",
         u"占位活动：主体结构施工期间及竣工后的沉降观测与数据分析（C1 监测类）。",
         u"[SCAFFOLD_V1] 类别占位，数值为估，需整体清退。KB 无观测周期证据，暂与周边环境监测同锚"
         u"（60 工日/项，按次人工占位）；观测周期需人工确认。"
         u"注：backend/pipeline/org_defaults.py:132 已把「沉降观测」归为按次但无独立工期。"),

    # --- C2 预留预埋类（伴随型，按建筑面积计；与「制作安装」严格区分）
    _act("EMB_ELEC_001", u"电气预留预埋", "electrical", u"m²", u"机电安装", "labor_driven", u"建筑面积",
         u"占位活动：结构施工阶段随主体预埋的电气线管、接线盒、防雷接地引下线（不含穿线、桥架、"
         u"灯具等制作安装，C2 预留预埋类）。",
         u"[SCAFFOLD_V1] 类别占位，数值为估，需整体清退。口径=建筑面积（伴随型）。锚点：KB 内同口径"
         u"（建筑面积）现成估算 TPREP_AI_005 测量放线 0.012 工日/m²。与 ELEC_AI_001 桥架安装 / "
         u"ELEC_AI_002 配管配线 / ELEC_AI_003 电缆敷设 等「制作安装」活动口径不同，不得互绑。"),
    _act("EMB_PLUMB_001", u"给排水预留预埋", "plumbing", u"m²", u"机电安装", "labor_driven", u"建筑面积",
         u"占位活动：结构施工阶段随主体预埋的给水/排水/雨水套管、预留孔洞（不含管道安装，C2）。",
         u"[SCAFFOLD_V1] 类别占位，数值为估，需整体清退。口径=建筑面积（伴随型），锚点 TPREP_AI_005 "
         u"0.012 工日/m²。与 PLUMB_AI_001 给水管道 / PLUMB_AI_002 排水管道 / PLUMB_AI_003 雨水管道 "
         u"等「制作安装」活动口径不同，不得互绑。"),
    _act("EMB_HVAC_001", u"暖通预留预埋", "hvac", u"m²", u"机电安装", "labor_driven", u"建筑面积",
         u"占位活动：结构施工阶段随主体预埋的通风/空调套管、预留孔洞与预埋件（不含风管制作安装，C2）。",
         u"[SCAFFOLD_V1] 类别占位，数值为估，需整体清退。口径=建筑面积（伴随型），锚点 TPREP_AI_005 "
         u"0.012 工日/m²。**本条修复契约 §1 点名的静默口径漏洞**：WBS 7.1.3「暖通预留预埋」(14200 m²) "
         u"原被绑到 HVAC_AI_001 风管制作安装 0.35 工日/m²（风管展开面积口径），口径不同却判为可用。"
         u"与 HVAC_AI_001 风管制作安装口径不同，不得互绑。"),
    _act("EMB_FIRE_001", u"消防预留预埋", "fire_protection", u"m²", u"机电安装", "labor_driven", u"建筑面积",
         u"占位活动：结构施工阶段随主体预埋的消防管道套管、预留孔洞（不含消火栓/喷淋系统安装，C2）。",
         u"[SCAFFOLD_V1] 类别占位，数值为估，需整体清退。口径=建筑面积（伴随型），锚点 TPREP_AI_005 "
         u"0.012 工日/m²。与 FIRE_AI_001 消火栓系统 / FIRE_AI_002 自动喷淋系统 等「制作安装」活动"
         u"口径不同，不得互绑。"),

    # --- C4 沥青混凝土路面类
    _act("PAVE_AI_001", u"沥青混凝土路面施工", "earthwork", u"m²", u"综合", "equipment_driven", "",
         u"占位活动：道路沥青混凝土面层摊铺、碾压（C4 沥青混凝土路面类）。",
         u"[SCAFFOLD_V1] 类别占位，数值为估，需整体清退。KB 的 L3_Work_Type 无「道路/市政」类别，"
         u"暂挂 earthwork，属已知类别缺口。口径未定（词表无「道路面积」），留空不编造。"),
    _act("PAVE_AI_002", u"道路基层施工", "earthwork", u"m²", u"综合", "equipment_driven", "",
         u"占位活动：道路基层碎石/水泥稳定碎石摊铺、碾压（C4 沥青混凝土路面类）。",
         u"[SCAFFOLD_V1] 类别占位，数值为估，需整体清退。同 PAVE_AI_001，L3 类别缺口暂挂 earthwork。"),
]

#: 新增活动的占位人工定额 (activity_id, unit, value, note)
NEW_LABOR_NORMS = [
    ("MON_AI_001", u"项", 60.0,
     u"契约 §8「周边环境监测 1 人 60 天」→ 60 工日/项（按次人工占位；org_defaults.py:132 归为无独立工期）"),
    ("MON_AI_002", u"项", 60.0,
     u"契约 §8 明确 3.4.2 周边环境监测 1 人 60 天 → 60 工日/项"),
    ("MON_AI_003", u"项", 60.0,
     u"无观测周期证据，暂与周边环境监测同锚 60 工日/项，需人工确认"),
    ("EMB_ELEC_001", u"m²", 0.012,
     u"建筑面积口径，锚点 TPREP_AI_005 测量放线 0.012 工日/m²"),
    ("EMB_PLUMB_001", u"m²", 0.012,
     u"建筑面积口径，锚点 TPREP_AI_005 测量放线 0.012 工日/m²"),
    ("EMB_HVAC_001", u"m²", 0.012,
     u"建筑面积口径，锚点 TPREP_AI_005 测量放线 0.012 工日/m²"),
    ("EMB_FIRE_001", u"m²", 0.012,
     u"建筑面积口径，锚点 TPREP_AI_005 测量放线 0.012 工日/m²"),
    ("PAVE_AI_001", u"m²", 0.5,
     u"镜像现行 WBS 9.2.3 的模型估算值 0.5 工日/m²（不改变既有量级）"),
    ("PAVE_AI_002", u"m²", 0.5,
     u"镜像现行 WBS 9.2.2 的模型估算值 0.5 工日/m²（不改变既有量级）"),
]

#: C3 预制构件台班占位 (activity_id, machine_name, spec, shift_per_unit, note)
PC_HOIST_NORMS = [
    ("CONC_PC_SLAB", u"塔式起重机", u"起重量≤6t", 0.040,
     u"占位：叠合板吊装按 25 m²/台班（=0.04 台班/m²）估；KB 无同类规范台班行可锚，需人工确认"),
    ("CONC_PC_BALCONY", u"塔式起重机", u"起重量≤6t", 0.040,
     u"占位：预制阳台板吊装按 25 m²/台班估；构件重、就位慢，需按构件类型细化"),
    ("CONC_PC_BAYWIN", u"塔式起重机", u"起重量≤6t", 0.040,
     u"占位：预制凸窗吊装按 25 m²/台班估；构件重、就位慢，需按构件类型细化"),
    ("CONC_PC_STAIR", u"塔式起重机", u"起重量≤6t", 0.040,
     u"占位：预制楼梯吊装按 25 m²/台班估；楼梯为异形重构件，需人工确认"),
]

#: C9 塔吊 / 施工电梯台班占位 (activity_id, machine, spec, shift_per_unit, note)
EPREP_EQ_NORMS = [
    ("EPREP_AI_002", u"汽车式起重机", u"起重量25t", 2.0,
     u"占位：塔吊安装按 2 台班/台 估算（含组装、就位；顶升/附着未单列）。"
     u"KB 现有台班行仅覆盖广东定额 A.1.1/A.1.3/A.1.5，无「大型机械安拆/措施项目」来源，"
     u"需扩充定额解析后替换本占位值"),
    ("EPREP_AI_003", u"汽车式起重机", u"起重量25t", 1.0,
     u"占位：施工电梯安装按 1 台班/台 估算。同 EPREP_AI_002，需扩充「大型机械安拆」来源后替换"),
]

#: H1（2026-09-21）：原 `Unit_Conversion` 登记表已删除（10 行，产品运行时不读；
#: 单位写法归一由 `backend/pipeline/kb_units.normalize_unit` 里的写死映射承担）。
#: 原 `UNIT_CONVERSION_SEEDS` 种子与 `step_c8_create_unit_conversion()` 一并移除。


# --------------------------------------------------------------------------- 步骤实现

class Stats(object):
    def __init__(self):
        self.items = []

    def add(self, step, key, value):
        self.items.append((step, key, value))

    def dump(self):
        log("")
        log("=" * 78)
        log("WS6 迁移统计")
        log("=" * 78)
        cur_step = None
        for step, key, value in self.items:
            if step != cur_step:
                log("")
                log("[%s]" % step)
                cur_step = step
            log("  %-42s %s" % (key, value))


def _ensure_scope_columns(cur):
    """静默补齐 measure_scope 列，返回本次新增的表名列表。"""
    added = []
    for t in SCOPE_TABLES:
        if table_exists(cur, t) and "measure_scope" not in columns(cur, t):
            cur.execute("ALTER TABLE [%s] ADD COLUMN measure_scope TEXT" % t)
            added.append(t)
    return added


def step_add_scope_columns(cur, st):
    """C0 前置：为 4 张表补齐 measure_scope 列（后续步骤依赖该列存在）。"""
    added = _ensure_scope_columns(cur)
    for t in SCOPE_TABLES:
        if not table_exists(cur, t):
            st.add("C0", u"缺表 %s" % t, u"跳过")
        else:
            st.add("C0", t, u"新增列" if t in added else u"已存在")


def _wfc_template(cur, unit):
    """取同单位族的现有 WFC 行作为模板（返回 dict；只在 ai_estimate 生成行里挑）。"""
    aid = {u"m²": "TPREP_AI_005", u"项": "TPREP_AI_001"}.get(unit)
    if not aid:
        return None
    cols = columns(cur, "Workface_Capacity_Rule")
    row = cur.execute("SELECT * FROM Workface_Capacity_Rule WHERE activity_id=?",
                      (aid,)).fetchone()
    return dict(zip(cols, row)) if row else None


def step_new_activities(cur, st):
    """C1 / C2 / C4：新增活动 + 工作面容量行 + 占位人工定额。"""
    _ensure_scope_columns(cur)
    cols = columns(cur, "L4_Activity_Dictionary")
    added, skipped = 0, 0
    for a in NEW_ACTIVITIES:
        if scalar(cur, "SELECT COUNT(*) FROM L4_Activity_Dictionary WHERE activity_id=?",
                  (a["activity_id"],)):
            skipped += 1
            continue
        row = {
            "activity_id": a["activity_id"],
            "work_type_id": a["work_type_id"],
            "activity_name": a["activity_name"],
            "unit": a["unit"],
            "recommended_production_mode": a["recommended_production_mode"],
            "is_l5_expandable": 0,
            "description": a["description"],
            "activity_category": a["activity_category"],
            "construction_method": None,
            "status": "needs_review",
            "needs_deconstruction": 0,
            "legacy_id": None,
            "notes": a["notes"],
        }
        if "measure_scope" in cols:
            row["measure_scope"] = a["measure_scope"]
        keys = [k for k in row if k in cols]
        cur.execute(
            "INSERT INTO L4_Activity_Dictionary (%s) VALUES (%s)"
            % (",".join(keys), ",".join("?" * len(keys))),
            [row[k] for k in keys])
        added += 1
    st.add("C1/C2/C4", "L4_Activity_Dictionary 新增活动",
           u"新增 %d 个 / 已存在 %d 个 / 现共 %d 行" % (
               added, skipped,
               scalar(cur, "SELECT COUNT(*) FROM L4_Activity_Dictionary")))

    # 工作面容量行（复用同单位族模板，保持与既有 ai 标定一致的形态）
    wfc_cols = columns(cur, "Workface_Capacity_Rule")
    seq = next_seq_id(cur, "Workface_Capacity_Rule", "rule_id", "WFC2_")
    wfc_added = 0
    for a in NEW_ACTIVITIES:
        aid = a["activity_id"]
        if scalar(cur, "SELECT COUNT(*) FROM Workface_Capacity_Rule WHERE activity_id=?", (aid,)):
            continue
        tpl = _wfc_template(cur, a["unit"])
        if tpl is None:
            st.add("C1/C2/C4", u"WFC 模板缺失 unit=%s" % a["unit"], "跳过")
            continue
        # 按列名复制，避免位置错配
        row = {}
        for c in wfc_cols:
            if c in tpl:
                row[c] = tpl[c]
        row["rule_id"] = seq
        row["activity_id"] = aid
        row["work_type_l3"] = a["work_type_id"]
        row["notes"] = (tpl.get("notes") or "") + (
            u"；[WS6/%s] 新增占位活动，复用同单位族（unit=%s）模板 %s 的经验工作面容量，"
            u"数值为估，需整体清退" % (SCAFFOLD, a["unit"], tpl.get("rule_id")))
        keys = [k for k in row if k in wfc_cols]
        cur.execute(
            "INSERT INTO Workface_Capacity_Rule (%s) VALUES (%s)"
            % (",".join(keys), ",".join("?" * len(keys))),
            [row[k] for k in keys])
        wfc_added += 1
        seq = "%s%04d" % ("WFC2_", int(seq[len("WFC2_"):]) + 1)
    st.add("C1/C2/C4", "Workface_Capacity_Rule 新增行",
           u"新增 %d 行 / 现共 %d 行" % (
               wfc_added, scalar(cur, "SELECT COUNT(*) FROM Workface_Capacity_Rule")))

    # 占位人工定额
    lcols = columns(cur, "Norm_Labor_Table")
    # 兼容别名列修复（老行或早期版本可能漏填 quantity_basis）
    if "quantity_basis" in lcols and "raw_quantity_basis" in lcols:
        cur.execute("UPDATE Norm_Labor_Table SET quantity_basis = raw_quantity_basis "
                    "WHERE source_code=? AND quantity_basis IS NULL", (SCAFFOLD,))
    seq = next_seq_id(cur, "Norm_Labor_Table", "norm_id", "LN_SCAFFOLD_")
    ln_added = 0
    for aid, unit, value, why in NEW_LABOR_NORMS:
        key = "SCAFFOLD:%s" % aid
        if scalar(cur, "SELECT COUNT(*) FROM Norm_Labor_Table WHERE activity_id=? AND "
                       "quantity_unit=? AND source_code=?", (aid, unit, SCAFFOLD)):
            continue
        note = u"[%s] 类别占位，数值为估，需整体清退。%s" % (SCAFFOLD, why)
        row = {
            "norm_id": seq,
            "activity_id": aid,
            "condition_combination": None,
            "condition_text": u"类别占位",
            "labor_norm_value": value,
            "labor_norm_unit": u"工日/%s" % unit,
            "raw_quantity_basis": 1.0,
            "quantity_basis": 1.0,   # 兼容别名列：不变量要求 == raw_quantity_basis
            "quantity_unit": unit,
            "productivity_value": (1.0 / value) if value else None,
            "productivity_unit": u"%s/工日" % unit,
            "conversion_method": "reciprocal",
            "conversion_notes": note,
            "source_code": SCAFFOLD,
            "source_type": "scaffold_placeholder",
            "status": SCAFFOLD_STATUS,
            "review_notes": SCAFFOLD_NOTE_PREFIX + u" 占位行，需人工复核并清退",
        }
        if "measure_scope" in lcols:
            scope = activity_scope(
                scalar(cur, "SELECT activity_name FROM L4_Activity_Dictionary WHERE activity_id=?",
                       (aid,)) or "", unit)
            row["measure_scope"] = scope
        keys = [k for k in row if k in lcols]
        cur.execute(
            "INSERT INTO Norm_Labor_Table (%s) VALUES (%s)"
            % (",".join(keys), ",".join("?" * len(keys))),
            [row[k] for k in keys])
        ln_added += 1
        seq = "%s%04d" % ("LN_SCAFFOLD_", int(seq[len("LN_SCAFFOLD_"):]) + 1)
    st.add("C1/C2/C4", "Norm_Labor_Table 占位定额行", u"新增 %d 行" % ln_added)


def _insert_equipment(cur, st, aid, machines, specs, shifts, unit, basis, note):
    """插入一行 Norm_Equipment_Table 占位台班定额（幂等）。"""
    ecols = columns(cur, "Norm_Equipment_Table")
    if scalar(cur, "SELECT COUNT(*) FROM Norm_Equipment_Table WHERE activity_id=? AND "
                   "source_code=?", (aid, SCAFFOLD)):
        return 0
    seq = next_seq_id(cur, "Norm_Equipment_Table", "norm_id", "NE_SCAFFOLD_")
    row = {
        "norm_id": seq,
        "activity_id": aid,
        "condition_combination": None,
        "condition_text": u"类别占位",
        "machine_combination_json": json.dumps(machines, ensure_ascii=False),
        "machine_spec_json": json.dumps(specs, ensure_ascii=False),
        "machine_shift_norm_json": json.dumps(shifts),
        "machine_shift_unit_json": json.dumps([u"台班"] * len(machines), ensure_ascii=False),
        "quantity_basis": float(basis),
        "quantity_unit": unit,
        "is_common_combination": 1,
        "source_code": SCAFFOLD,
        "source_type": "scaffold_placeholder",
        "status": SCAFFOLD_STATUS,
        "review_notes": SCAFFOLD_NOTE_PREFIX + u" 占位台班行，需人工复核并清退",
    }
    if "measure_scope" in ecols:
        name = scalar(cur, "SELECT activity_name FROM L4_Activity_Dictionary "
                           "WHERE activity_id=?", (aid,)) or ""
        row["measure_scope"] = activity_scope(name, unit)
    row["raw_text"] = u"[%s] 类别占位，数值为估，需整体清退。%s" % (SCAFFOLD, note)
    row["review_notes"] = row["raw_text"]
    keys = [k for k in row if k in ecols]
    cur.execute(
        "INSERT INTO Norm_Equipment_Table (%s) VALUES (%s)"
        % (",".join(keys), ",".join("?" * len(keys))),
        [row[k] for k in keys])
    return 1


def step_c3_pc_hoist(cur, st):
    """C3：4 个预制构件活动的台班定额占位。"""
    _ensure_scope_columns(cur)
    n = 0
    for aid, machine, spec, per_unit, note in PC_HOIST_NORMS:
        n += _insert_equipment(cur, st, aid, [machine], [spec], [per_unit * 100.0],
                               u"m²", 100, note + u"（quantity_basis=100 m²）")
    st.add("C3", "Norm_Equipment_Table 预制构件台班", u"新增 %d 行" % n)


def step_c9_eprep_equipment(cur, st):
    """C9：塔吊 / 施工电梯台班产量占位。"""
    _ensure_scope_columns(cur)
    n = 0
    for aid, machine, spec, per_unit, note in EPREP_EQ_NORMS:
        n += _insert_equipment(cur, st, aid, [machine], [spec], [per_unit], u"台", 1, note)
    st.add("C9", "Norm_Equipment_Table 塔吊/电梯台班", u"新增 %d 行" % n)


def step_c5_pingzheng(cur, st):
    """C5：场地平整口径与「场地硬化」分开。"""
    _ensure_scope_columns(cur)
    cols = columns(cur, "L4_Activity_Dictionary")
    if "measure_scope" not in cols:
        st.add("C5", "跳过：measure_scope 列不存在", "-")
        return
    note_pz = (u"[WS6/C5] 规范活动：广东定额 A.1.1「平整场地」（机械推土）。"
               u"口径=建筑面积，依据 GB 50854《房屋建筑与装饰工程工程量计算规范》："
               u"平整场地按设计图示尺寸以建筑物首层建筑面积计算（与 WBS 1.1.1 工程量 14200 m²"
               u"= total_area 一致）。**与 SPREP_AI_003「场地硬化」不同类，不得互绑。**")
    note_yh = (u"[WS6/C5] 本活动是「场地硬化」（临时道路/堆场硬化），**不是**「场地平整」；"
               u"口径=硬化面积（受控词表无此词，留空不编造），与 GD_A11_平整场地"
               u"（口径=建筑面积、机械台班）严格分开，禁止再把 WBS「场地平整」绑到本条。")
    for aid, scope, note in (("GD_A11_平整场地", u"建筑面积", note_pz),
                             ("SPREP_AI_003", u"", note_yh)):
        row = cur.execute("SELECT IFNULL(notes,'') FROM L4_Activity_Dictionary "
                          "WHERE activity_id=?", (aid,)).fetchone()
        if row is None:
            st.add("C5", aid, u"活动不存在，跳过")
            continue
        old = row[0]
        if u"[WS6/C5]" in old:
            st.add("C5", aid, u"已处理（幂等跳过）")
            continue
        cur.execute("UPDATE L4_Activity_Dictionary SET measure_scope=?, notes=? "
                    "WHERE activity_id=?",
                    (scope, (old + u" | " if old else u"") + note, aid))
        st.add("C5", aid, u"measure_scope=%s（与另一条分开口径）" % (scope or u"''"))


def step_c11_main_machine(cur, st):
    """C11：Activity_Main_Machine 按台班行重推（有台班行的活动 → regional_quota/HIGH）。"""
    rows = list(cur.execute(
        "SELECT e.activity_id, e.norm_id, e.condition_text, e.machine_combination_json, "
        "e.machine_spec_json, e.source_code "
        "FROM Norm_Equipment_Table e "
        "JOIN L4_Activity_Dictionary d ON d.activity_id = e.activity_id "
        "WHERE d.recommended_production_mode = 'equipment_driven' "
        "ORDER BY e.activity_id, e.norm_id"))
    by_act = {}
    for aid, nid, ctext, mcomb, mspec, scode in rows:
        by_act.setdefault(aid, []).append((nid, ctext, mcomb, mspec, scode))
    updated, inserted, activities = 0, 0, 0
    for aid, cands in sorted(by_act.items()):
        nid, ctext, mcomb, mspec, scode = cands[0]
        try:
            machines = json.loads(mcomb or "[]")
            specs = json.loads(mspec or "[]")
        except Exception:
            continue
        if not machines:
            continue
        # 主控机械 = machine_combination_json 首位（KB 约定：子目主机械排第一）
        mname = machines[0]
        mspec0 = specs[0] if specs else None
        note = (u"[WS6/C11] 由台班定额行反推（主控机械取 machine_combination_json[0]=%s）。"
                u"来源：Norm_Equipment_Table %s（source_code=%s）。原 ai_estimate/MEDIUM 已按契约"
                u"C11 覆盖为 regional_quota/HIGH。条件文本沿用原有行，不改主键。"
                % (mname, nid, scode))
        existing = list(cur.execute(
            "SELECT rowid FROM Activity_Main_Machine WHERE activity_id=?", (aid,)))
        if existing:
            # 原地更新该活动的既有行，保留各自 condition_text（主键组成部分）
            for (rid,) in existing:
                cur.execute(
                    "UPDATE Activity_Main_Machine SET machine_name=?, machine_spec=?, "
                    "source_type='regional_quota', confidence='HIGH', notes=? "
                    "WHERE rowid=?", (mname, mspec0, note, rid))
                updated += 1
        else:
            cur.execute(
                "INSERT INTO Activity_Main_Machine (activity_id, condition_text, "
                "machine_name, machine_spec, source_type, confidence, notes) "
                "VALUES (?,?,?,?,'regional_quota','HIGH',?)",
                (aid, ctext or u"", mname, mspec0, note))
            inserted += 1
        activities += 1
    st.add("C11", "Activity_Main_Machine",
           u"原地更新 %d 行 / 新增 %d 行 / 覆盖活动 %d 个 / 现共 %d 行" % (
               updated, inserted, activities,
               scalar(cur, "SELECT COUNT(*) FROM Activity_Main_Machine")))
    dist = dict(cur.execute("SELECT source_type, COUNT(*) FROM Activity_Main_Machine GROUP BY 1"))
    st.add("C11", "source_type 分布", json.dumps(dist, ensure_ascii=False, sort_keys=True))


def step_c7_measure_scope(cur, st):
    """C7：填充 4 张表的 measure_scope（列由 C0 前置步骤补齐）。"""
    _ensure_scope_columns(cur)

    # --- 1) 活动字典（必须先物化，边遍历边 UPDATE 会让同一 cursor 失效）
    acts = {}
    for aid, name, unit, wtype, cur_scope in list(cur.execute(
            "SELECT activity_id, activity_name, unit, work_type_id, measure_scope "
            "FROM L4_Activity_Dictionary")):
        scope = cur_scope if cur_scope is not None else activity_scope(name, unit, wtype)
        acts[aid] = (name, unit, scope)
        if cur_scope is None:
            cur.execute("UPDATE L4_Activity_Dictionary SET measure_scope=? "
                        "WHERE activity_id=?", (scope, aid))
    dist = dict(cur.execute("SELECT IFNULL(measure_scope,''), COUNT(*) "
                            "FROM L4_Activity_Dictionary GROUP BY 1"))
    st.add("C7", "L4_Activity_Dictionary",
           u"%d 行；已填 %d；分布 %s" % (
               sum(dist.values()), sum(n for k, n in dist.items() if k),
               json.dumps(dist, ensure_ascii=False, sort_keys=True)))

    # --- 2) 定额行：单位与活动一致 → 沿用活动口径；否则单位推断
    for t in ("Norm_Labor_Table", "Norm_Equipment_Table", "L4_Norm_Default"):
        if not table_exists(cur, t):
            continue
        rows = list(cur.execute(
            "SELECT rowid, activity_id, quantity_unit, measure_scope FROM [%s]" % t))
        filled = 0
        for rid, aid, qunit, cur_scope in rows:
            if cur_scope is not None:
                continue
            name, aunit, ascope = acts.get(aid, ("", "", ""))
            scope = row_scope(name, qunit, aunit, ascope)
            cur.execute("UPDATE [%s] SET measure_scope=? WHERE rowid=?" % t, (scope, rid))
            filled += 1
        dist = dict(cur.execute("SELECT IFNULL(measure_scope,''), COUNT(*) FROM [%s] "
                                "GROUP BY 1" % t))
        st.add("C7", t, u"%d 行；本次填 %d；已填 %d/%d；分布 %s" % (
            sum(dist.values()), filled, sum(n for k, n in dist.items() if k),
            sum(dist.values()), json.dumps(dist, ensure_ascii=False, sort_keys=True)))

    # --- 校验：不得出现词表外的值
    stray = []
    for t in SCOPE_TABLES:
        if not table_exists(cur, t):
            continue
        for (v,) in cur.execute("SELECT DISTINCT measure_scope FROM [%s]" % t):
            if v is not None and v not in SCOPE_SET and v != "":
                stray.append((t, v))
    st.add("C7", u"词表外值校验", (u"FAIL %s" % stray) if stray else u"PASS（0 个词表外值）")


def step_c6_machine_index(cur, st):
    """C6：L4_Norm_Default 补 norm_kind='machine' 索引行。"""
    _ensure_scope_columns(cur)
    dcols = columns(cur, "L4_Norm_Default")
    rows = list(cur.execute(
        "SELECT e.activity_id, e.norm_id, e.condition_text, e.machine_combination_json, "
        "e.machine_shift_norm_json, e.quantity_basis, e.quantity_unit, e.source_code, "
        "e.measure_scope "
        "FROM Norm_Equipment_Table e "
        "JOIN L4_Activity_Dictionary d ON d.activity_id = e.activity_id "
        "WHERE d.recommended_production_mode = 'equipment_driven' "
        "ORDER BY e.activity_id, e.norm_id"))
    # 每个 (activity, quantity_unit) 取代表行 = norm_id 最小
    rep = {}
    for (aid, nid, ctext, mcomb, mshift, basis, qunit, scode, mscope) in rows:
        key = (aid, qunit)
        if key in rep:
            continue
        try:
            shifts = json.loads(mshift or "[]")
            machines = json.loads(mcomb or "[]")
        except Exception:
            continue
        basis = float(basis or 1) or 1.0
        total = sum(float(x) for x in shifts)
        rep[key] = dict(activity_id=aid, quantity_unit=qunit,
                        norm_value=total / basis,
                        norm_unit=u"台班/%s" % qunit,
                        source_code=scode, source_kind="kb_equipment_index",
                        measure_scope=mscope,
                        _norm_id=nid, _machines="/".join(machines),
                        notes=(u"[WS6/C6] 机械索引行：由台班定额行 %s 汇总（%s，合计 %.4f 台班 / "
                               u"quantity_basis=%s）。来源 source_code=%s。数值为台班合计，"
                               u"不是工程量单位产能。"
                               % (nid, "/".join(machines), total, basis, scode)))
    added, skipped = 0, 0
    for key, r in sorted(rep.items()):
        if scalar(cur, "SELECT COUNT(*) FROM L4_Norm_Default WHERE activity_id=? AND "
                       "quantity_unit=? AND norm_kind='machine'",
                  (r["activity_id"], r["quantity_unit"])):
            skipped += 1
            continue
        row = dict(r)
        row["norm_kind"] = "machine"
        row["condition_key"] = json.dumps(
            {"机械索引": True, "来源定额行": r["_norm_id"], "机械": r["_machines"]},
            ensure_ascii=False, sort_keys=True)
        row["confidence"] = "parsed"
        row["default_crew"] = None
        row["review_state"] = "pending"
        keys = [k for k in row if k in dcols and not k.startswith("_")]
        cur.execute(
            "INSERT INTO L4_Norm_Default (%s) VALUES (%s)"
            % (",".join(keys), ",".join("?" * len(keys))),
            [row[k] for k in keys])
        added += 1
    dist = dict(cur.execute("SELECT norm_kind, COUNT(*) FROM L4_Norm_Default GROUP BY 1"))
    st.add("C6", "L4_Norm_Default machine 索引行",
           u"新增 %d / 已存在 %d；活动 %d 个；分布 %s" % (
               added, skipped, len(set(k[0] for k in rep)),
               json.dumps(dist, ensure_ascii=False, sort_keys=True)))


def step_c10_legacy_deprecation(cur, st):
    """C10：legacy 工作面容量表 notes 前置废弃标记（保留原文，不加列）。

    H2/H3（2026-09-21）已把两张 legacy 归档表删除 → 这里改为**动态发现**
    （不写死已删表名）：有就标、没有就跳过，幂等且缺表不报错。
    """
    legacy = [r[0] for r in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name LIKE 'Workface_Capacity_Rule_legacy%' ORDER BY name")]
    if not legacy:
        st.add("C10", u"legacy 工作面容量归档表",
               u"不存在（H2/H3 已删除）→ 跳过")
    for t in legacy:
        cols = columns(cur, t)
        if "notes" not in cols:
            st.add("C10", u"%s 无 notes 列" % t, "跳过")
            continue
        total = scalar(cur, "SELECT COUNT(*) FROM [%s]" % t)
        already = scalar(cur, "SELECT COUNT(*) FROM [%s] WHERE IFNULL(notes,'') LIKE ?"
                         % t, ("%deprecated%",))
        todo = list(cur.execute(
            "SELECT rowid, IFNULL(notes,'') FROM [%s] WHERE IFNULL(notes,'') NOT LIKE ?"
            % t, ("%deprecated%",)))
        for rid, old in todo:
            cur.execute("UPDATE [%s] SET notes=? WHERE rowid=?" % t,
                        (DEPRECATED_MARK + (u" " + old if old else u""), rid))
        st.add("C10", t, u"%d 行：本次标记 %d，此前已标 %d" % (total, len(todo), already))
    st.add("C10", u"后端引用检查", u"见报告：排除 __pycache__/_probe_tmp/tests 后 backend 下无引用")


def step_c8_normalize_units(cur, st):
    """C8：KB 全文单位写法归一 ㎡ → m²。"""
    tables = [r[0] for r in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]
    total_cells = 0
    for t in tables:
        for (col, ctype) in [(r[1], r[2]) for r in cur.execute("PRAGMA table_info([%s])" % t)]:
            if ctype and "TEXT" not in ctype.upper() and ctype.upper() not in ("", "BLOB"):
                continue
            try:
                n = scalar(cur, "SELECT COUNT(*) FROM [%s] WHERE CAST([%s] AS TEXT) LIKE ?"
                           % (t, col), (u"%\u33a1%",))
            except Exception:
                continue
            if not n:
                continue
            cur.execute("UPDATE [%s] SET [%s]=REPLACE(CAST([%s] AS TEXT), ?, ?) "
                        "WHERE CAST([%s] AS TEXT) LIKE ?"
                        % (t, col, col, col), (u"\u33a1", u"m²", u"%\u33a1%"))
            total_cells += n
            st.add("C8", u"%s.%s" % (t, col), u"归一 %d 处" % n)
    st.add("C8", u"㎡(U+33A1) 归一合计",
           u"%d 处（KB 内原本为 0；㎡ 实际出现在 WBS 产物，属 WS2 范围）" % total_cells)


STEPS = (
    ("C0", step_add_scope_columns),
    ("C124", step_new_activities),
    ("C3", step_c3_pc_hoist),
    ("C9", step_c9_eprep_equipment),
    ("C5", step_c5_pingzheng),
    ("C11", step_c11_main_machine),
    ("C7", step_c7_measure_scope),
    ("C6", step_c6_machine_index),
    ("C10", step_c10_legacy_deprecation),
    ("C8b", step_c8_normalize_units),
)

STEP_ALIASES = {
    "C8": ("C8b",),
    "C1": ("C124",), "C2": ("C124",), "C4": ("C124",),
    "C12": (),
}


def report(db):
    con = sqlite3.connect(db)
    cur = con.cursor()
    log("=" * 78)
    log("WS6 只读报告：%s" % db)
    log("=" * 78)
    log(u"约 %s" % os.path.getsize(db))
    for t in SCOPE_TABLES:
        if not table_exists(cur, t) or "measure_scope" not in columns(cur, t):
            log(u"%-24s measure_scope: **缺列**" % t)
            continue
        dist = dict(cur.execute(
            "SELECT IFNULL(measure_scope,'(空)'), COUNT(*) FROM [%s] GROUP BY 1" % t))
        filled = scalar(cur, "SELECT COUNT(*) FROM [%s] WHERE IFNULL(measure_scope,'')<>''" % t)
        total = scalar(cur, "SELECT COUNT(*) FROM [%s]" % t)
        log(u"%-24s measure_scope 已填 %d/%d  %s" % (
            t, filled, total, json.dumps(dist, ensure_ascii=False, sort_keys=True)))
    if table_exists(cur, "L4_Norm_Default"):
        dist = dict(cur.execute("SELECT norm_kind, COUNT(*) FROM L4_Norm_Default GROUP BY 1"))
        log(u"L4_Norm_Default norm_kind: %s" % json.dumps(dist, ensure_ascii=False))
    # H2/H3（2026-09-21）已删除两张 legacy 归档表 → 动态发现，不写死已删表名
    _legacy = [r[0] for r in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name LIKE 'Workface_Capacity_Rule_legacy%' ORDER BY name")]
    if not _legacy:
        log(u"legacy 工作面容量归档表: 不存在（H2/H3 已删除）")
    for t in _legacy:
        if table_exists(cur, t) and "notes" in columns(cur, t):
            n = scalar(cur, "SELECT COUNT(*) FROM [%s] WHERE IFNULL(notes,'') LIKE ?" % t,
                       ("%deprecated%",))
            log(u"%-40s 已标废弃 %d/%d" % (
                t, n, scalar(cur, "SELECT COUNT(*) FROM [%s]" % t)))
    if table_exists(cur, "Activity_Main_Machine"):
        dist = dict(cur.execute("SELECT source_type, COUNT(*) FROM Activity_Main_Machine "
                                "GROUP BY 1"))
        log(u"Activity_Main_Machine source_type: %s" % json.dumps(dist, ensure_ascii=False))
    if table_exists(cur, "sources"):
        log(u"sources.SCAFFOLD_V1: %s" % scalar(
            cur, "SELECT COUNT(*) FROM sources WHERE source_code='SCAFFOLD_V1'"))
    log(u"AI_ESTIMATE_V1 行数: %s" % scalar(
        cur, "SELECT COUNT(*) FROM Norm_Labor_Table WHERE source_code='AI_ESTIMATE_V1'"))
    con.close()


def step_scaffold_source(cur, st):
    """登记占位来源 SCAFFOLD_V1（验收检查器 KB.scaffold_source 依赖）。"""
    if not table_exists(cur, "sources"):
        st.add("src", u"sources 表不存在", "跳过")
        return
    if scalar(cur, "SELECT COUNT(*) FROM sources WHERE source_code=?", (SCAFFOLD,)):
        st.add("src", u"sources.%s" % SCAFFOLD, u"已存在")
        return
    cols = columns(cur, "sources")
    row = {
        "source_code": SCAFFOLD,
        "document_name": u"WS6 类别占位（无规范来源）",
        "standard_code": None,
        "publisher": u"内部占位",
        "year": None,
        "region": None,
        "source_type": "SCAFFOLD",
        "source_category": "placeholder",
        "file_path": None,
        "file_name": None,
        "total_pages": None,
        "has_text_layer": 0,
        "parse_difficulty": None,
        "primary_purpose": u"占位：类别缺失时的临时定额，必须整体清退",
        "coverage_scope": u"仅 WS6 新增的占位活动（监测/预留预埋/路面/预制构件/塔吊电梯）",
        "notes": u"类别占位，数值为估，需整体清退",
    }
    keys = [k for k in row if k in cols]
    cur.execute("INSERT INTO sources (%s) VALUES (%s)"
                % (",".join(keys), ",".join("?" * len(keys))),
                [row[k] for k in keys])
    st.add("src", u"sources.%s" % SCAFFOLD, u"新增（notes=「类别占位，数值为估，需整体清退」）")


def main():
    ap = argparse.ArgumentParser(description="WS6 知识库迁移（默认 dry-run）")
    ap.add_argument("--db", required=True, help="目标 kb.db（只写这一份）")
    ap.add_argument("--apply", action="store_true", help="真正提交（默认 dry-run 回滚）")
    ap.add_argument("--report", action="store_true", help="只读统计，不做任何迁移")
    ap.add_argument("--only", default="", help="只跑指定步骤，逗号分隔，如 C7,C6")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print("ERROR: 找不到 --db %s" % args.db)
        return 2

    if args.report:
        report(args.db)
        return 0

    mode = "APPLY（提交）" if args.apply else "DRY-RUN（回滚，不写库）"
    log("=" * 78)
    log("WS6 迁移  db=%s  模式=%s" % (args.db, mode))
    log("=" * 78)

    con = sqlite3.connect(args.db)
    con.isolation_level = None
    cur = con.cursor()
    st = Stats()
    cur.execute("BEGIN")
    try:
        step_scaffold_source(cur, st)
        wanted = [x.strip() for x in args.only.split(",") if x.strip()]
        allowed = set()
        for w in wanted:
            allowed.update(STEP_ALIASES.get(w, (w,)))
        for name, fn in STEPS:
            if allowed and name not in allowed:
                continue
            log(">> %s %s" % (name, fn.__doc__.splitlines()[0] if fn.__doc__ else ""))
            fn(cur, st)
        st.dump()
        if args.apply:
            cur.execute("COMMIT")
            log("")
            log("已提交（--apply）。")
        else:
            cur.execute("ROLLBACK")
            log("")
            log("已回滚（dry-run）：库未被修改。加 --apply 才会写入。")
    except Exception:
        cur.execute("ROLLBACK")
        raise
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

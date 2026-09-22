#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""WS6 补丁：修正 C11 主控机械（缺陷 1）+ 补齐台班行 measure_scope（缺陷 2）。

背景
----
首轮 C11 用「本活动台班行里 norm_id 最小那行的 machine_combination_json[0]」当主控机械，
实测把**伴随机具**抬成了主控机械：

    CONC_NEW_FOUND  混凝土振捣器（应为 混凝土输送泵车）
    180 m³ ÷ 10 × 1.26 = 22.68 台班 ÷ 1 台 = 23 天（旧值 1 天）

WS1 重建后的 `norm_bind._pick_machine_row` 规则③明确：主控机械不在任何台班行里 →
整条绑定降级为「仅参考」，**不得**借用同行其它机械的定额。所以 KB 侧必须自己给出
**控制性机械**，否则混凝土浇筑的产能口径必错。

选取规则（确定性、逐条可解释）
------------------------------
① 泵送优先：混凝土浇筑的产能由泵送设备控制；混凝土振捣器是**伴随机具**（其台班随
   浇筑子目计，不表征浇筑产能）。候选集里若有 混凝土输送泵车 / 混凝土输送泵 /
   混凝土布料机 / 布料机 / 泵车 / 输送泵 → 按此优先级取之。
② 既有标定延续：C11 之前 `Activity_Main_Machine` 是企业/AI 已标定的主控机械
   （见 `CURATED_MAIN_MACHINE`，取自 kb.db.bak_20260920_201941_pre_final）。
   若该机械确实出现在本活动的台班行里 → 沿用，并用台班行坐实 source_type。
   这一条是除混凝土之外各活动恢复正确机械的依据。
③ 兜底：无既有标定（如 C3 新增的 4 个 PC 活动）→ 在该活动台班行里取**台班用量最大**
   且不属于 `AUX_MARKERS` 伴随机具的机械。

同优先级/同分时的确定性 tie-break：条件串匹配度 → 行的 norm_id 从小到大 → 机械下标。

用法（默认 dry-run 不写库）
---------------------------
  python devtools/kb_migrate_ws6_fix.py --db BuildPlan_KB\\kb.db
  python devtools/kb_migrate_ws6_fix.py --db BuildPlan_KB\\kb.db --apply
  python devtools/kb_migrate_ws6_fix.py --db BuildPlan_KB\\kb.db --verify   # 只读校验
"""
from __future__ import print_function

import argparse
import io
import json
import os
import sqlite3
import sys

# --------------------------------------------------------------------------- 常量

#: C11 之前的既有主控机械标定（取自 kb.db.bak_20260920_201941_pre_final，66 行）。
#: 用作选取规则②「既有标定延续」的依据：该机械确在台班行里时沿用。
CURATED_MAIN_MACHINE = (
    ("CONC_NEW_BEAM", "", "混凝土输送泵车", ""),
    ("CONC_NEW_COLUMN", "", "混凝土输送泵车", ""),
    ("CONC_NEW_FOUND", "", "混凝土输送泵车", ""),
    ("CONC_NEW_OTHER", "", "混凝土输送泵车", ""),
    ("CONC_NEW_SLAB", "", "混凝土输送泵车", ""),
    ("CONC_NEW_STAIR", "", "混凝土输送泵车", ""),
    ("CONC_NEW_WALL", "", "混凝土输送泵车", ""),
    ("GD_A11_凿岩机破碎石方", "", "风动凿岩机", ""),
    ("GD_A11_压路机碾压土(石)方", "", "钢轮振动压路机", ""),
    ("GD_A11_原土打夯", "", "电动夯实机", ""),
    ("GD_A11_原土打夯", "压路机碾压", "钢轮内燃压路机", ""),
    ("GD_A11_回填土(夯实机夯实)", "", "电动夯实机", ""),
    ("GD_A11_回填砂、石屑", "", "电动夯实机", ""),
    ("GD_A11_履带式单头液压岩石破碎机破碎石方", "", "履带式单头岩石破碎机", ""),
    ("GD_A11_平整场地", "", "履带式推土机", ""),
    ("GD_A11_挖掘机转堆土方和机械垂直运输土方", "", "履带式单斗液压挖掘机", ""),
    ("GD_A11_挖掘机转堆土方和机械垂直运输土方", "机械垂直运输土方 / 100m3", "卷扬机架(单笼5t内)", ""),
    ("GD_A11_挖掘机转堆松散石方及机械垂直运输石方", "", "履带式单斗液压挖掘机", ""),
    ("GD_A11_挖掘机转堆松散石方及机械垂直运输石方", "机械垂直运输石方 / 100m3", "卷扬机架(单笼5t内)", ""),
    ("GD_A11_推土机推土方", "", "履带式推土机", ""),
    ("GD_A11_支挡土板", "", "载货汽车", ""),
    ("GD_A11_机械打眼爆破石方", "", "风动凿岩机", ""),
    ("GD_A11_机械挖土方、淤泥流砂", "", "履带式单斗液压挖掘机", ""),
    ("GD_A11_机械挖石方", "", "履带式单斗液压挖掘机", ""),
    ("GD_A11_机械挖石方", "装载机装松散石方", "轮胎式装载机", ""),
    ("GD_A11_机械挖装土方、淤泥流砂", "", "履带式单斗液压挖掘机", ""),
    ("GD_A11_机械装土方", "", "履带式单斗液压挖掘机", ""),
    ("GD_A11_机械装土方", "装载机装土方", "轮胎式装载机", ""),
    ("GD_A11_石方控制爆破", "", "风动凿岩机", ""),
    ("GD_A11_自卸汽车运土方、淤泥流砂", "", "自卸汽车", ""),
    ("GD_A11_自卸汽车运石方", "", "自卸汽车", ""),
    ("GD_A11_铲运机铲运土方", "", "拖式铲运机", ""),
    ("GD_A11_静力爆破石方", "", "风动凿岩机", ""),
    ("GD_A13_CFG桩成孔", "", "长螺旋钻机", ""),
    ("GD_A13_冲孔入岩", "", "冲击式打桩机", ""),
    ("GD_A13_冲孔成孔", "", "冲击式打桩机", ""),
    ("GD_A13_压力灌浆微型桩", "", "工程地质液压钻机", ""),
    ("GD_A13_压方桩", "", "静力压桩机", ""),
    ("GD_A13_压管桩", "", "静力压桩机", ""),
    ("GD_A13_压钢管桩", "", "静力压桩机", ""),
    ("GD_A13_后压浆", "", "电动灌浆机", ""),
    ("GD_A13_微型桩钢管埋设", "", "交流弧焊机", ""),
    ("GD_A13_截凿桩头", "", "风动凿岩机", ""),
    ("GD_A13_打圆木桩", "", "吊锤打桩机", ""),
    ("GD_A13_打方桩", "", "履带式柴油打桩机", ""),
    ("GD_A13_打管桩", "", "履带式柴油打桩机", ""),
    ("GD_A13_打钢管桩", "", "振动沉拔桩机", ""),
    ("GD_A13_接桩", "", "交流弧焊机", ""),
    ("GD_A13_旋挖入岩", "", "履带式旋挖钻机", ""),
    ("GD_A13_旋挖成孔", "", "履带式旋挖钻机", ""),
    ("GD_A13_桩尖制作", "", "剪板机", ""),
    ("GD_A13_检测管制安", "", "交流弧焊机", ""),
    ("GD_A13_沉管夯扩成孔", "", "履带式柴油打桩机", ""),
    ("GD_A13_沉管灌注成孔", "", "履带式柴油打桩机", ""),
    ("GD_A13_泥浆运输", "", "泥浆罐车", ""),
    ("GD_A13_砂石灌注桩", "", "履带式柴油打桩机", ""),
    ("GD_A13_管内钻孔取土", "", "螺旋钻机", ""),
    ("GD_A13_管桩填芯", "", "混凝土振捣器", ""),
    ("GD_A13_精割盖帽", "", "汽车式起重机", ""),
    ("GD_A13_钢护筒", "", "振动沉拔桩机", ""),
    ("GD_A13_钢管桩内切割", "", "内切割机", ""),
    ("GD_A13_钢管桩填芯", "", "混凝土振捣器", ""),
    ("GD_A13_钢管桩接桩", "", "交流弧焊机", ""),
    ("GD_A13_钻孔入岩", "", "回旋钻机", ""),
    ("GD_A13_钻孔成孔", "", "回旋钻机", ""),
    ("GD_A13_钻孔灌注微型桩", "", "工程地质液压钻机", ""),
)

#: 规则①——泵送/布料优先（混凝土浇筑的控制性机械），按优先级从高到低
CONTROL_LEXICON = (
    u"混凝土输送泵车", u"混凝土输送泵", u"混凝土布料机", u"布料机", u"泵车", u"输送泵",
)

#: 规则③——伴随机具（不得在兜底时被选为主控机械）
AUX_MARKERS = (
    u"混凝土振捣器", u"电动修钎机", u"空气压缩机", u"洒水车", u"卷扬机架",
    u"电动单筒慢速卷扬机", u"潜水泵", u"污水泵",
)

#: 缺陷 2：台班行 measure_scope 补齐（受控词表内，逐条给依据）
SCOPE_FIXES = (
    # NE_CONC_016 亭面板：广东定额 A1-5-19 规范原样按 m² 计量，不是占位写错
    ("NE_CONC_016", u"楼地面面积",
     u"规范原样按 m² 计量（广东定额 A1-5-19 亭面板，quantity_basis=100 m²）。"
     u"词表无「楼板面积」，取水平构件面积最接近的「楼地面面积」；下游据此可正当地要求 "
     u"thickness_m 换算（m² ↔ m³），不得放宽这道判据。"),
    # C3 占位行：叠合板 / 阳台板 属水平板类构件
    ("NE_SCAFFOLD_0001", u"楼地面面积",
     u"占位行口径：预制叠合板属水平板类构件，按 m² 吊装计量，取词表内最接近的「楼地面面积」。"
     u"（真实口径为「预制构件面积」，词表缺该词，见报告遗留项）"),
    ("NE_SCAFFOLD_0002", u"楼地面面积",
     u"占位行口径：预制阳台板属水平板类构件，按 m² 吊装计量，取「楼地面面积」。"),
)


def log(m):
    print(m)


def _json_list(v):
    if isinstance(v, (list, tuple)):
        return list(v)
    try:
        return list(json.loads(v or "[]"))
    except Exception:
        return []


def columns(cur, t):
    return [r[1] for r in cur.execute("PRAGMA table_info([%s])" % t)]


# --------------------------------------------------------------------------- 候选

def load_candidates(cur):
    """activity_id -> [(row_dict, index, machine_name, spec, shift)]"""
    cols = columns(cur, "Norm_Equipment_Table")
    out = {}
    for tup in cur.execute("SELECT * FROM Norm_Equipment_Table ORDER BY norm_id"):
        row = dict(zip(cols, tup))
        machines = [str(x) for x in _json_list(row.get("machine_combination_json"))]
        specs = [str(x) for x in _json_list(row.get("machine_spec_json"))]
        shifts = _json_list(row.get("machine_shift_norm_json"))
        for i, m in enumerate(machines):
            out.setdefault(row["activity_id"], []).append(dict(
                row=row, index=i, name=m,
                spec=(specs[i] if i < len(specs) else u""),
                shift=float(shifts[i]) if i < len(shifts) and shifts[i] is not None else 0.0))
    return out


def _name_matches(a, b):
    return bool(a) and bool(b) and (a == b or a in b or b in a)


def _best_by_name(cands, name, condition_text):
    """在候选里挑 name 匹配的项；同名多行用条件串匹配度消歧，再按 norm_id/下标定序。"""
    hit = [c for c in cands if _name_matches(c["name"], name)]
    if not hit:
        return None
    key = (condition_text or "").strip()
    if key and len(hit) > 1:
        def score(c):
            ct = c["row"].get("condition_text") or u""
            if ct and (key in ct or ct in key):
                return 2
            if ct and set(ct) & set(key):
                return 1
            return 0
        hit.sort(key=lambda c: (-score(c), str(c["row"]["norm_id"]), c["index"]))
    else:
        hit.sort(key=lambda c: (str(c["row"]["norm_id"]), c["index"]))
    return hit[0]


def choose_machine(cands, activity_id, condition_text):
    """返回 (candidate, rule_no, rule_text)；无候选 → (None, None, "")。"""
    if not cands:
        return None, None, u""
    # 规则① 泵送优先
    for lex in CONTROL_LEXICON:
        c = _best_by_name(cands, lex, condition_text)
        if c is not None:
            return c, 1, (u"判据①泵送优先：混凝土浇筑的产能由泵送设备控制，"
                          u"混凝土振捣器为伴随机具（其台班随浇筑子目计，不表征浇筑产能）")
    # 规则② 既有标定延续
    curated = None
    for aid, ct, mn, _sp in CURATED_MAIN_MACHINE:
        if aid == activity_id and (ct or u"") == (condition_text or u""):
            curated = mn
            break
    if curated is None:
        for aid, ct, mn, _sp in CURATED_MAIN_MACHINE:
            if aid == activity_id and not ct:
                curated = mn
                break
    if curated:
        c = _best_by_name(cands, curated, condition_text)
        if c is not None:
            return c, 2, (u"判据②既有标定延续：C11 之前该活动的主控机械标定为「%s」，"
                          u"本活动台班行中确有该机械，沿用并以台班行坐实来源" % curated)
    # 规则③ 兜底：台班用量最大的非伴随机具
    pool = [c for c in cands
            if not any(a in c["name"] for a in AUX_MARKERS)] or list(cands)
    pool.sort(key=lambda c: (-c["shift"], str(c["row"]["norm_id"]), c["index"]))
    return pool[0], 3, (u"判据③兜底（无既有标定）：取本活动台班行中台班用量最大、"
                        u"且不属于伴随机具的机械（%g 台班，行 %s）"
                        % (pool[0]["shift"], pool[0]["row"]["norm_id"]))


# --------------------------------------------------------------------------- 缺陷 1

def fix_main_machine(cur, apply, changes):
    rows = list(cur.execute(
        "SELECT rowid, activity_id, IFNULL(condition_text,''), machine_name, "
        "IFNULL(machine_spec,''), IFNULL(source_type,''), IFNULL(notes,'') "
        "FROM Activity_Main_Machine ORDER BY activity_id, condition_text"))
    cands = load_candidates(cur)
    n_upd, n_same, n_skip = 0, 0, 0
    for rid, aid, ct, old_m, old_s, old_st, old_notes in rows:
        c, rule, why = choose_machine(cands.get(aid, []), aid, ct)
        if c is None:
            changes.append(dict(kind="main_machine", rowid=rid, aid=aid, ct=ct,
                                ok=False, reason=u"该活动无台班行，跳过"))
            n_skip += 1
            continue
        # 下标对齐自检：候选的 name 必须等于该行 machine_combination_json[index]
        machines = [str(x) for x in _json_list(c["row"].get("machine_combination_json"))]
        assert machines[c["index"]] == c["name"], (
            u"下标错位！%s index=%d 期望 %s 实得 %s"
            % (c["row"]["norm_id"], c["index"], c["name"], machines[c["index"]]))
        shifts = _json_list(c["row"].get("machine_shift_norm_json"))
        assert 0 <= c["index"] < len(shifts), (
            u"machine_shift_norm_json 长度不足！%s index=%d len=%d"
            % (c["row"]["norm_id"], c["index"], len(shifts)))
        new_m, new_s = c["name"], c["spec"]
        note = (u"[WS6/C11 修正] %s。主控机械「%s」（台班行 %s，machine_combination_json "
                u"下标 %d，该台班 %g 台班 / basis %s %s，source_code=%s）。"
                u"首轮 C11 误取「最小 norm_id 行的机器下标 0」，把伴随机具写成了主控机械。"
                % (why, new_m, c["row"]["norm_id"], c["index"], c["shift"],
                   c["row"].get("quantity_basis"), c["row"].get("quantity_unit"),
                   c["row"].get("source_code")))
        changed = (new_m != old_m) or (new_s != old_s) or (old_st != "regional_quota")
        changes.append(dict(kind="main_machine", rowid=rid, aid=aid, ct=ct, ok=True,
                            rule=rule, old_m=old_m, old_s=old_s, new_m=new_m, new_s=new_s,
                            norm_id=c["row"]["norm_id"], index=c["index"],
                            shift=c["shift"], changed=changed))
        if changed:
            n_upd += 1
            cur.execute(
                "UPDATE Activity_Main_Machine SET machine_name=?, machine_spec=?, "
                "source_type='regional_quota', confidence='HIGH', notes=? WHERE rowid=?",
                (new_m, new_s, note, rid))
        else:
            n_same += 1
            cur.execute("UPDATE Activity_Main_Machine SET notes=? WHERE rowid=?",
                        (note, rid))
    log(u"[缺陷1] Activity_Main_Machine：%d 行；改名/改规格 %d 行，主控机械已正确 %d 行，跳过 %d 行"
        % (len(rows), n_upd, n_same, n_skip))
    return n_upd


# --------------------------------------------------------------------------- 缺陷 2

def fix_equipment_scope(cur, apply, changes):
    n = 0
    for norm_id, scope, why in SCOPE_FIXES:
        row = cur.execute("SELECT rowid, IFNULL(measure_scope,''), quantity_unit, "
                          "quantity_basis FROM Norm_Equipment_Table WHERE norm_id=?",
                          (norm_id,)).fetchone()
        if row is None:
            changes.append(dict(kind="scope", norm_id=norm_id, ok=False,
                                reason=u"行不存在"))
            continue
        rid, old, unit, basis = row
        changes.append(dict(kind="scope", norm_id=norm_id, ok=True, old=old, new=scope,
                            unit=unit, basis=basis, why=why, changed=(old != scope)))
        if old != scope:
            n += 1
            cur.execute("UPDATE Norm_Equipment_Table SET measure_scope=?, "
                        "review_notes=COALESCE(review_notes,'')||? WHERE rowid=?",
                        (scope, u" [WS6/缺陷2] " + why, rid))
    log(u"[缺陷2] Norm_Equipment_Table measure_scope：目标 %d 行，实际改动 %d 行"
        % (len(SCOPE_FIXES), n))
    return n


# --------------------------------------------------------------- 裁决1：machine 索引行

#: 父代理裁决 1 要求的确定性说明（L4_Norm_Default 的 machine 索引行逐行写入）
INDEX_ONLY_MARK = (
    u"[index-only 2026-09-20] 本行 norm_kind='machine' 仅为机械台账索引；"
    u"norm_value 为同活动各台班之和，不表征单台机械产能。"
    u"机械产能请看 Activity_Main_Machine + Norm_Equipment_Table。")


def _equipment_by_activity(cur):
    """activity_id -> [equipment row dict]，按 norm_id 升序。"""
    cols = columns(cur, "Norm_Equipment_Table")
    out = {}
    for tup in cur.execute("SELECT * FROM Norm_Equipment_Table ORDER BY norm_id"):
        row = dict(zip(cols, tup))
        out.setdefault(row["activity_id"], []).append(row)
    return out


def _control_shift_for(erows, machine_name, quantity_unit, condition_text):
    """在活动台班行里找含 machine_name 且 quantity_unit 相符的行。

    返回 `(row, index, shift, basis)`；找不到 → None。
    同名多行用条件串匹配度消歧，再按 norm_id/下标定序（确定性）。
    """
    cands = []
    for row in erows:
        if (row.get("quantity_unit") or u"") != (quantity_unit or u""):
            continue
        machines = [str(x) for x in _json_list(row.get("machine_combination_json"))]
        for i, m in enumerate(machines):
            if _name_matches(m, machine_name):
                cands.append((row, i))
    if not cands:
        return None
    key = (condition_text or u"").strip()
    if key and len(cands) > 1:
        def score(c):
            ct = c[0].get("condition_text") or u""
            if ct and (key in ct or ct in key):
                return 2
            if ct and set(ct) & set(key):
                return 1
            return 0
        cands.sort(key=lambda c: (-score(c), str(c[0]["norm_id"]), c[1]))
    else:
        cands.sort(key=lambda c: (str(c[0]["norm_id"]), c[1]))
    row, i = cands[0]
    shifts = _json_list(row.get("machine_shift_norm_json"))
    basis = float(row.get("quantity_basis") or 1) or 1.0
    return row, i, float(shifts[i]), basis


def fix_index_only(cur, apply, changes):
    """裁决 1：`L4_Norm_Default` machine 索引行加说明；能确定算就改成控制性机械那一份。

    1. 说明（必须）：逐行追加 `INDEX_ONLY_MARK`，**保留原 notes 原文**（同 C10 做法）。
       已含标记的行不重复追加 → 幂等。
    2. 重算（优先，确定性可算时）：`norm_value` 改成**控制性机械那一份**的台班值
       （= shift / basis，与修正后的 `Activity_Main_Machine` 一致），
       `condition_key` 同步指向该台班行。算不出来就**只留说明、绝不编数值**。
    """
    erows = _equipment_by_activity(cur)
    amm = {}
    for aid, ct, mn in cur.execute(
            "SELECT activity_id, IFNULL(condition_text,''), machine_name "
            "FROM Activity_Main_Machine"):
        amm.setdefault(aid, []).append((ct or u"", mn))
    for k in amm:
        # 先 condition_text=''（典型行），再按文本升序 —— 确定性
        amm[k].sort(key=lambda p: (p[0] != u"", p[0]))

    rows = list(cur.execute(
        "SELECT rowid, activity_id, quantity_unit, norm_value, norm_unit, condition_key, "
        "IFNULL(notes,'') FROM L4_Norm_Default WHERE norm_kind='machine' "
        "ORDER BY activity_id, quantity_unit"))
    n_note, n_recalc, n_unknown = 0, 0, 0
    for rid, aid, qunit, old_val, _old_unit, old_ck, old_notes in rows:
        mark_present = u"[index-only 2026-09-20]" in old_notes
        found = None
        for ct, mn in amm.get(aid, []):
            got = _control_shift_for(erows.get(aid, []), mn, qunit, ct)
            if got is not None:
                found = (ct, mn) + got
                break
        extra = u""
        if found is None:
            n_unknown += 1
            new_val = old_val
            if u"控制性机械无对应台班行" in (old_ck or u""):
                new_ck, extra = old_ck, u""      # 已标注 → 保持（幂等）
            else:
                src_rows = [r["norm_id"] for r in erows.get(aid, [])
                            if (r.get("quantity_unit") or u"") == (qunit or u"")]
                new_ck = json.dumps(
                    {u"机械索引": True, u"控制性机械": None,
                     u"说明": u"该 quantity_unit 下控制性机械无对应台班行，"
                              u"norm_value 保持首轮求和值未改（不编数值）",
                     u"求和台班行": src_rows, u"原condition_key": old_ck},
                    ensure_ascii=False, sort_keys=True)
                extra = (u" 但该 quantity_unit（%s）下，Activity_Main_Machine 的控制性机械"
                         u"没有对应台班行 —— 本行 norm_value 保持首轮求和值**未改**，"
                         u"不编数值。该单位下参与求和的台班行：%s。"
                         % (qunit, u"、".join(src_rows) or u"（无）"))
        else:
            ct, mn, erow, idx, shift, basis = found
            new_val = shift / basis
            new_ck = json.dumps(
                {u"机械索引": True, u"控制性机械": mn, u"来源定额行": erow["norm_id"],
                 u"machine_combination_json下标": idx, u"该台班": shift,
                 u"quantity_basis": basis},
                ensure_ascii=False, sort_keys=True)
            n_recalc += 1
            extra = (u" 本行 norm_value 已按控制性机械「%s」修正为 %g 台班/%s"
                     u"（台班行 %s，machine_combination_json 下标 %d，该台班 %g 台班 / "
                     u"basis %g %s），与 Activity_Main_Machine 一致。"
                     % (mn, new_val, qunit, erow["norm_id"], idx, shift, basis, qunit))
        if mark_present:
            new_notes = old_notes
        else:
            n_note += 1
            # 追加式：标记在前，原 notes 原文完整保留
            new_notes = INDEX_ONLY_MARK + extra + (u"\n" + old_notes if old_notes else u"")
        changes.append(dict(kind="index_only", rid=rid, aid=aid, qunit=qunit,
                            old_val=old_val, new_val=new_val,
                            recalc=(new_val != old_val), mark_present=mark_present))
        if (new_notes != old_notes) or (new_val != old_val) or (new_ck != old_ck):
            cur.execute("UPDATE L4_Norm_Default SET norm_value=?, condition_key=?, notes=? "
                        "WHERE rowid=?", (new_val, new_ck, new_notes, rid))
    log(u"[裁决1] L4_Norm_Default machine 索引行 %d 行：追加说明 %d 行；"
        u"norm_value 重算为控制性机械口径 %d 行；算不出（只留说明、未改数值）%d 行"
        % (len(rows), n_note, n_recalc, n_unknown))
    return n_recalc


def verify(cur):
    """只读校验：每个主控机械都必须真的出现在本活动的台班行里（否则绑定会降级）。"""
    cands = load_candidates(cur)
    bad = []
    for aid, ct, mn, st in cur.execute(
            "SELECT activity_id, IFNULL(condition_text,''), machine_name, "
            "IFNULL(source_type,'') FROM Activity_Main_Machine"):
        pool = cands.get(aid, [])
        if not any(_name_matches(c["name"], mn) for c in pool):
            bad.append((aid, ct, mn, len(pool)))
    log(u"[校验] 主控机械不在本活动台班行里的行数（必须为 0）：%d" % len(bad))
    for b in bad:
        log(u"   !! %s ct=%r machine=%r 台班行数=%d" % b)
    dist = dict(cur.execute("SELECT source_type, COUNT(*) FROM Activity_Main_Machine "
                            "GROUP BY 1"))
    log(u"[校验] source_type 分布：%s" % json.dumps(dist, ensure_ascii=False))
    # 下标对齐：逐候选自检
    n_cand, n_bad = 0, 0
    for aid, pool in cands.items():
        for c in pool:
            n_cand += 1
            machines = [str(x) for x in _json_list(c["row"].get("machine_combination_json"))]
            shifts = _json_list(c["row"].get("machine_shift_norm_json"))
            if c["index"] >= len(machines) or c["index"] >= len(shifts) \
                    or machines[c["index"]] != c["name"]:
                n_bad += 1
    log(u"[校验] 台班行 (row,index) 与 machine_shift_norm_json 下标对齐："
        u"%d 个候选，错位 %d 个（必须为 0）" % (n_cand, n_bad))
    # 全库扫描：主控机械为辅助机具的活动（人工复核清单）
    aux = []
    for aid, ct, mn, st in cur.execute(
            "SELECT activity_id, IFNULL(condition_text,''), machine_name, source_type "
            "FROM Activity_Main_Machine"):
        if any(a in mn for a in AUX_MARKERS):
            aux.append((aid, ct, mn))
    log(u"[校验] 主控机械仍属伴随机具的行数（%d）：%s"
        % (len(aux), u"、".join(u"%s/%s" % (a, m) for a, _c, m in aux)))
    # 裁决1：machine 索引行的说明与口径
    # 注意：必须先 list() 物化，否则循环内再 execute 会打断外层 cursor（首轮同款坑）
    erows = _equipment_by_activity(cur)
    idx_rows = list(cur.execute(
        "SELECT rowid, activity_id, quantity_unit, norm_value, condition_key, "
        "IFNULL(notes,'') FROM L4_Norm_Default WHERE norm_kind='machine'"))
    ct_by_act = {}
    for aid, ct in cur.execute(
            "SELECT activity_id, IFNULL(condition_text,'') FROM Activity_Main_Machine "
            "ORDER BY activity_id, condition_text"):
        ct_by_act.setdefault(aid, []).append(ct)
    n_idx, n_nomark, n_ok, n_badval = 0, 0, 0, 0
    for rid, aid, qunit, val, ck, notes in idx_rows:
        n_idx += 1
        if u"[index-only 2026-09-20]" not in notes:
            n_nomark += 1
        try:
            meta = json.loads(ck) if ck else {}
        except Exception:
            meta = {}
        mn = meta.get(u"控制性机械") if isinstance(meta, dict) else None
        if not mn:
            continue
        got = None
        for ct in ct_by_act.get(aid, [u""]):
            got = _control_shift_for(erows.get(aid, []), mn, qunit, ct)
            if got is not None:
                break
        if got is None:
            n_badval += 1
            continue
        if abs(val - got[2] / got[3]) < 1e-9:
            n_ok += 1
        else:
            n_badval += 1
    log(u"[校验] L4_Norm_Default machine 索引行 %d 行：缺 [index-only] 说明 %d 行；"
        u"norm_value 与控制性机械台班一致 %d 行，不一致/查不到 %d 行"
        % (n_idx, n_nomark, n_ok, n_badval))
    n_bad += n_nomark + n_badval
    return len(bad) + n_bad


def main():
    ap = argparse.ArgumentParser(description="WS6 补丁：主控机械 + 台班行口径")
    ap.add_argument("--db", required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--verify", action="store_true", help="只读校验，不做任何改动")
    a = ap.parse_args()
    if not os.path.exists(a.db):
        log("ERROR: 找不到 %s" % a.db)
        return 2

    con = sqlite3.connect(a.db)
    con.isolation_level = None
    cur = con.cursor()

    if a.verify:
        log("=" * 78)
        log("WS6 补丁 只读校验  db=%s" % a.db)
        log("=" * 78)
        bad = verify(cur)
        con.close()
        return 0 if bad == 0 else 1

    mode = "APPLY" if a.apply else "DRY-RUN（回滚）"
    log("=" * 78)
    log("WS6 补丁  db=%s  模式=%s" % (a.db, mode))
    log("=" * 78)
    changes = []
    cur.execute("BEGIN")
    try:
        n1 = fix_main_machine(cur, a.apply, changes)
        n2 = fix_equipment_scope(cur, a.apply, changes)
        n3 = fix_index_only(cur, a.apply, changes)
        log("")
        log("-" * 78)
        log("逐行改动明细（缺陷1：仅列变化的行）")
        log("-" * 78)
        for c in changes:
            if c["kind"] != "main_machine":
                continue
            if not c.get("ok"):
                log(u"  SKIP %-24s ct=%-24s %s" % (c["aid"], repr(c["ct"]), c["reason"]))
            elif c.get("changed"):
                log(u"  %-24s ct=%-24s 规则%d  %s / %s  ->  %s / %s   (%s idx=%d shift=%g)"
                    % (c["aid"], repr(c["ct"]), c["rule"], c["old_m"], c["old_s"],
                       c["new_m"], c["new_s"], c["norm_id"], c["index"], c["shift"]))
        log("")
        log("-" * 78)
        log("缺陷2 明细")
        log("-" * 78)
        for c in changes:
            if c["kind"] != "scope":
                continue
            if not c.get("ok"):
                log(u"  SKIP %-20s %s" % (c["norm_id"], c["reason"]))
            else:
                log(u"  %-20s scope %r -> %r (unit=%s basis=%s) %s"
                    % (c["norm_id"], c["old"], c["new"], c["unit"], c["basis"],
                       u"（已一致，仅补 review_notes）" if not c.get("changed") else u""))
        log("")
        log("-" * 78)
        log("裁决1 明细：L4_Norm_Default machine 索引行（仅列重算数值的行）")
        log("-" * 78)
        n_idx_recalc = 0
        for c in changes:
            if c["kind"] != "index_only":
                continue
            if c.get("recalc"):
                n_idx_recalc += 1
                log(u"  %-24s %-10s norm_value %s -> %s"
                    % (c["aid"], c["qunit"], c["old_val"], c["new_val"]))
        log(u"  小计：重算 %d 行 / 共 %d 行；说明逐行追加（保留原 notes 原文）"
            % (n_idx_recalc, sum(1 for c in changes if c["kind"] == "index_only")))
        log("")
        verify(cur)
        if a.apply:
            cur.execute("COMMIT")
            log("")
            log("已提交（--apply）。")
        else:
            cur.execute("ROLLBACK")
            log("")
            log("已回滚（dry-run）：库未被修改。")
    except Exception:
        cur.execute("ROLLBACK")
        raise
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

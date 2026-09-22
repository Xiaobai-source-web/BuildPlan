#!/usr/bin/env python3
"""BuildPlan 查询工具 - 工期计算

用法:
    python query_duration.py --activity <id> --quantity <q> --resource-limit <n> [--condition <t> | --match <kw,kw>]
    python query_duration.py --activity <id> --quantity <q> --machines <n> [--shifts-per-day <n>]
    python query_duration.py --activity <id> --list-conditions      # 列条件 + 推荐 --match 串
    python query_duration.py --demo

工程量单位:
    Q 必须按**定额的真实计量单位**传入，工具输出里的「工程量: <Q> <unit>」即为该单位。
    若活动表登记的单位与定额计量单位不一致，工具会在该行给出显式提示。
    （历史上 unit 曾被 quantity_basis 污染成 '100m'/'10m³'，会造成数量级错误。）

公式（两表口径不同，切勿混用）:
    labor_driven:      D = Q × labor_norm_value / Crew_Size
                       （labor_norm_value 已标准化为 per 1×quantity_unit，不除 quantity_basis）
    equipment_driven:  D = Q / quantity_basis × Machine_Shift_Norm / Machines / Shifts_Per_Day
                       （machine_shift_norm 是原始值，按 basis×unit 计，必须除以 basis）

条件定位（两种方式，二选一）:
    --condition "<condition_text>"   精确匹配 condition_text（必须唯一命中一行）
    --match "框架梁,≤25"             多关键字 AND 匹配：关键字须全部出现在
                                     condition_combination + condition_text 中
    多条件活动必须指定其中之一；命中不唯一时报错并列出候选，不会盲取首行。
    不知道关键字该写什么时，先用 --list-conditions 让它直接给出可复制的 --match 串。

机械主导（equipment_driven）的主控机械口径:
    工期只由【主控机械】决定，辅助机械（振捣器/焊机/水泵等）只参与台班与配员展示。
    主控机械来自 Activity_Main_Machine（支持按 condition_text 分别标注）。
    未标注主控机械的活动会报错并列出全部机械，不会用全部机械取 MAX。
    若同一条件下有多行都含主控机械（条件不足以区分），同样报错并列出这些行。
    --machines 指主控机械的台数。

调整系数:
    H4/H5（2026-09-21）：`Norm_Adjustment` / `Norm_Adjustment_Target` 两张表已删除，
    `--adjustment` / `--list-adjustments` 通道随之整体移除（数据源已不存在）。

资源投入口径:
    有效班组 = min(用户资源限额, 工作面容量)
      - 用户资源限额 --resource-limit（人工）/ --machines（机械）
      - 工作面容量   Workface_Capacity_Rule.legacy_max_labor / legacy_max_machine（每施工段）
        （合表后唯一一张容量表；这两列是旧 v1 表的兼容列，与公式上限
          crew_max / machine_max 是两个口径，交付口径沿用旧值）
      - 两者都无 → 无法确定，报错
    输出中 binding_constraint 指出哪一侧在起约束，capacity_source 给出来源。
    --crew-size 为旧接口，语义等同 --resource-limit，若两者同时给出以 --resource-limit 为准。
"""

import sqlite3
import argparse
import json
import os
import math
import re
import sys
import io

if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'kb.db')


def get_conn():
    return sqlite3.connect(DB_PATH)


def get_workface_capacity(cur, activity_id):
    """查该活动的工作面容量规则。返回 dict 或 None。

    单一来源：`Workface_Capacity_Rule`（合表后的唯一容量表）。`max_labor` /
    `max_machine` 取表里的**兼容列** `legacy_max_labor` / `legacy_max_machine`
    （= 旧 v1 表的原值），别名出来保持本函数历来返回的字典结构不变。
    """
    cur.execute('''
        SELECT unit_basis, legacy_max_labor, legacy_max_machine,
               source_type, confidence, notes
        FROM Workface_Capacity_Rule WHERE activity_id = ?
    ''', (activity_id,))
    row = cur.fetchone()
    if not row:
        return None
    return {
        'unit_basis': row[0],
        'max_labor': row[1],
        'max_machine': row[2],
        'source_type': row[3],
        'confidence': row[4],
        'notes': row[5],
    }


def decide_resource(user_limit, workface_cap, kind):
    """有效资源 = min(用户限额, 工作面容量)。

    kind: 'labor' / 'machine'，用于生成说明文字。
    返回 (effective, decision_dict)；effective 为 None 表示无法确定。
    """
    label = '人工' if kind == 'labor' else '机械'
    unit = '人' if kind == 'labor' else '台'

    candidates = []
    if user_limit is not None:
        candidates.append(('user_limit', user_limit))
    if workface_cap is not None:
        candidates.append(('workface_cap', workface_cap))

    decision = {
        'resource_kind': kind,
        'user_limit': user_limit,
        'workface_cap': workface_cap,
        'capacity_source': None,
        'binding_constraint': None,
    }

    if not candidates:
        decision['error'] = (
            f'无法确定{label}投入：既未提供用户资源限额，数据库中也无工作面容量规则。'
            f'请用 --resource-limit 指定（机械用 --machines）。'
        )
        return None, decision

    effective = min(v for _, v in candidates)
    binding = [name for name, v in candidates if v == effective]
    decision['binding_constraint'] = 'both' if len(binding) > 1 else binding[0]
    decision['effective'] = effective
    decision['effective_unit'] = unit
    if workface_cap is None:
        decision['no_workface_rule'] = True
    return effective, decision


# ------------------------------------------------------------------
# 条件定位
# ------------------------------------------------------------------

def parse_match_keywords(args):
    if not args.match:
        return None
    return [k.strip() for k in args.match.split(',') if k.strip()]


def row_blob(condition_text, condition_combination):
    """关键字匹配用的文本：条件原文 + 结构化条件组合"""
    return f'{condition_text or ""} {condition_combination or ""}'


def match_rows(rows, keywords, text_idx=0, comb_idx=None):
    """返回关键字全部命中的行。comb_idx 为 None 时只用 condition_text。"""
    hits = []
    for r in rows:
        blob = row_blob(r[text_idx], r[comb_idx] if comb_idx is not None else '')
        if all(k in blob for k in keywords):
            hits.append(r)
    return hits


def fmt_labor_options(rows, limit=None):
    """格式化工人工定额候选项"""
    out = []
    for r in (rows if limit is None else rows[:limit]):
        out.append({
            'condition_text': r[0],
            'condition_combination': r[6],
            'norm_value': r[1],
            'norm_unit': r[2],
        })
    return out


# ------------------------------------------------------------------
# 主控机械
# ------------------------------------------------------------------

def get_main_machine(cur, activity_id, condition_text):
    """查主控机械。先按 condition_text 精确匹配，再回退到默认行（condition_text=''）。"""
    cur.execute('''
        SELECT condition_text, machine_name, machine_spec, confidence, notes
        FROM Activity_Main_Machine
        WHERE activity_id = ? AND condition_text IN (?, '')
        ORDER BY CASE WHEN condition_text = ? THEN 0 ELSE 1 END
        LIMIT 1
    ''', (activity_id, condition_text or '', condition_text or ''))
    row = cur.fetchone()
    if not row:
        return None
    return {
        'machine_name': row[1],
        'machine_spec': row[2],
        'confidence': row[3],
        'notes': row[4],
        'matched_condition': row[0],
        'is_condition_override': bool(row[0]),
    }


# ------------------------------------------------------------------
# 调整系数（H4/H5 2026-09-21：数据表已删除，整块功能移除）
# ------------------------------------------------------------------
# `Norm_Adjustment` / `Norm_Adjustment_Target` 两张表已按 H4/H5 删除，
# 原来的 `list_adjustments` / `_adjustment_unit_mismatch` / `apply_adjustments`
# 三个函数与 `--adjustment` / `--list-adjustments` 两个开关一并移除。
# 总工日 = Q × labor_norm_value，不再有任何系数通道。


PLACEHOLDER_CT = {'-', '—', '–', '无', '无条件'}


def _match_candidates(ct, comb):
    """把 condition_text 与 condition_combination 的取值拆成候选关键字。"""
    kws = []
    for v in (comb or {}).values():
        for part in re.split(r'[|，,]', str(v)):
            p = part.strip()
            if p and p not in kws:
                kws.append(p)
    for part in re.split(r'[|，,]', ct or ''):
        p = part.strip()
        if p and p not in PLACEHOLDER_CT and p not in kws:
            kws.append(p)
    return kws


def suggest_match(rows, text_i, comb_i, target_i):
    """为该行求一条唯一的 --match 串。贪心剔除冗余关键字，返回 (串, 是否唯一)。"""
    ct = rows[target_i][text_i] or ''
    comb = _parse_comb(rows[target_i][comb_i])
    kws = _match_candidates(ct, comb)
    if not kws:
        return None, len(rows) == 1

    def uniq(ks):
        return len(match_rows(rows, ks, text_idx=text_i, comb_idx=comb_i)) == 1

    if not uniq(kws):
        return ','.join(kws), False
    changed = True
    while changed and len(kws) > 1:
        changed = False
        for k in list(kws):
            trial = [x for x in kws if x != k]
            if trial and uniq(trial):
                kws = trial
                changed = True
    return ','.join(kws), True


def list_conditions(cur, args, act):
    """列出该活动的全部定额条件，并给出可直接复制的 --match 关键字串。"""
    labor = cur.execute('''
        SELECT condition_text, labor_norm_value, labor_norm_unit, condition_combination
        FROM Norm_Labor_Table WHERE activity_id = ? ORDER BY condition_text''',
        (args.activity,)).fetchall()
    equip = cur.execute('''
        SELECT condition_text, condition_combination FROM Norm_Equipment_Table
        WHERE activity_id = ? ORDER BY condition_text''', (args.activity,)).fetchall()

    out = {'activity_id': args.activity, 'activity_name': act[0],
           'labor_conditions': [], 'equipment_conditions': []}
    for i, r in enumerate(labor):
        kw, ok = suggest_match(labor, 0, 3, i)
        out['labor_conditions'].append({
            'condition_text': r[0], 'norm_value': r[1], 'norm_unit': r[2],
            'condition_combination': _parse_comb(r[3]),
            'suggested_match': kw, 'unique': ok,
        })
    for i, r in enumerate(equip):
        kw, ok = suggest_match(equip, 0, 1, i)
        out['equipment_conditions'].append({
            'condition_text': r[0], 'condition_combination': _parse_comb(r[1]),
            'suggested_match': kw, 'unique': ok,
        })

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return

    print(f"=== 条件清单 ===")
    print(f"活动: {act[0]} ({args.activity})   模式: {act[3]}")
    for label, key, unit_i in (('人工定额', 'labor_conditions', 'norm_unit'),
                               ('机械定额', 'equipment_conditions', None)):
        items = out[key]
        if not items:
            continue
        print(f"\n{label}: {len(items)} 条")
        uniq_n = sum(1 for it in items if it['unique'])
        print(f"  可唯一寻址 {uniq_n}/{len(items)}")
        for it in items:
            mark = 'OK ' if it['unique'] else '!! '
            val = (f"  {it['norm_value']} {it['norm_unit']}" if unit_i else '')
            print(f"  {mark}--match \"{it['suggested_match'] or ''}\"{val}")
            print(f"      condition_text = {it['condition_text']!r}")
    bad = [it for it in out['labor_conditions'] + out['equipment_conditions'] if not it['unique']]
    if bad:
        print(f"\n注意: {len(bad)} 条无法用关键字唯一寻址，需人工补充关键字或补齐库中条件。")


def calc_duration(args):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute('''
        SELECT activity_name, work_type_id, unit, recommended_production_mode
        FROM L4_Activity_Dictionary WHERE activity_id = ?
    ''', (args.activity,))
    act = cur.fetchone()
    if not act:
        print(f"错误: 未找到活动 '{args.activity}'")
        conn.close()
        return

    mode = act[3]
    mode_cn = {'labor_driven': '人工主导', 'equipment_driven': '机械主导'}.get(mode, mode)

    # --list-conditions：列出全部定额条件与推荐 --match 串后返回
    if args.list_conditions:
        list_conditions(cur, args, act)
        conn.close()
        return

    capacity = get_workface_capacity(cur, args.activity)

    if mode == 'labor_driven':
        calc_labor(cur, args, act, mode_cn, capacity)
    elif mode == 'equipment_driven':
        calc_equipment(cur, args, act, mode_cn, capacity)
    else:
        print(f"错误: 活动 {args.activity} 的 production_mode 为空或未知")

    conn.close()


def calc_labor(cur, args, act, mode_cn, capacity):
    # 第 37 轮：`Norm_Labor_Table.quantity_basis` 已改名 `raw_quantity_basis`
    # （labor_norm_value 已归一，basis 只作溯源；下面 `basis` 仅用于说明文案，
    #  不参与任何乘法 —— 见模块头公式：labor_driven 不除 basis）。
    _basis_col = "raw_quantity_basis" if "raw_quantity_basis" in {
        r[1] for r in cur.execute("PRAGMA table_info(Norm_Labor_Table)")} else "quantity_basis"
    cur.execute('''
        SELECT condition_text, labor_norm_value, labor_norm_unit, __BASIS__, quantity_unit,
               source_code, condition_combination
        FROM Norm_Labor_Table WHERE activity_id = ?
        ORDER BY condition_text
    '''.replace("__BASIS__", _basis_col), (args.activity,))
    rows = cur.fetchall()

    if not rows:
        print(f"错误: 活动 {args.activity} 无人工定额")
        return

    # 用户资源限额：--resource-limit 优先，--crew-size 为旧接口
    user_limit = args.resource_limit if args.resource_limit is not None else args.crew_size
    legacy_used = (args.resource_limit is None and args.crew_size is not None)

    workface_cap = capacity['max_labor'] if capacity else None
    crew, decision = decide_resource(user_limit, workface_cap, 'labor')

    if crew is None:
        print(f"错误: {decision['error']}")
        print()
        _list_labor_options(rows)
        return
    if capacity:
        decision['capacity_source'] = (
            f"Workface_Capacity_Rule(source_type={capacity['source_type']}, "
            f"confidence={capacity['confidence']}, unit_basis={capacity['unit_basis']})"
        )

    # 选择定额行：必须唯一定位，禁止盲取首行
    keywords = parse_match_keywords(args)
    if keywords:
        hits = match_rows(rows, keywords, text_idx=0, comb_idx=6)
        if not hits:
            print(f"错误: --match {args.match!r} 未命中任何人工定额")
            print()
            _list_labor_options(rows)
            return
        if len(hits) > 1:
            print(f"错误: --match {args.match!r} 命中 {len(hits)} 条人工定额，无法唯一确定，请补充关键字")
            print()
            _list_labor_options(hits)
            return
        row = hits[0]
    elif args.condition:
        matched = [r for r in rows if (r[0] or '') == args.condition]
        if not matched:
            print(f"错误: 条件 '{args.condition}' 不匹配任何人工定额")
            print()
            _list_labor_options(rows)
            return
        if len(matched) > 1:
            print(f"错误: 条件 '{args.condition}' 对应 {len(matched)} 条定额，无法唯一确定")
            print(f"      请改用 --match 并补充构件关键字，例如 "
                  f"--match \"{args.condition},矩形柱\"")
            print()
            _list_labor_options(matched)
            return
        row = matched[0]
    elif len(rows) == 1:
        row = rows[0]
    else:
        print(f"错误: 活动 {args.activity} 有 {len(rows)} 条人工定额，"
              f"必须用 --condition 或 --match 指定条件")
        print()
        _list_labor_options(rows)
        return

    Q = args.quantity
    norm_value = row[1]
    norm_unit = row[2]
    basis = row[3] or 1
    source = row[5]
    q_unit = row[4]

    # 人工定额已标准化为 per 1×quantity_unit（V2 迁移取的是 normalized_time_norm），
    # 因此此处【不得再除以 quantity_basis】。
    base_labor = Q * norm_value

    # H4/H5（2026-09-21）：调整系数通道已随两张数据表一并移除，总工日 = base_labor。
    total_labor = base_labor
    adjustments, adj_warnings = [], []

    duration = total_labor / crew

    result = {
        'activity_id': args.activity,
        'activity_name': act[0],
        'unit': act[2],
        'l4_unit': act[2],
        'production_mode': mode_cn,
        'condition_text': row[0],
        'condition_combination': _parse_comb(row[6]),
        'quantity': Q,
        'quantity_unit': q_unit,
        'norm_value': norm_value,
        'norm_unit': norm_unit,
        'quantity_basis': basis,
        'basis_note': 'basis 仅作出处记录，labor_norm_value 已标准化，不参与计算',
        'source': source,
        'crew_size': crew,
        'resource_decision': decision,
        'legacy_crew_size_option': legacy_used,
        'base_labor_days': round(base_labor, 2),
        'adjustments_applied': adjustments,
        'adjustment_warnings': adj_warnings,
        'total_labor_days': round(total_labor, 2),
        'duration_days': round(duration, 2),
        'duration_planned': math.ceil(duration),
        'formula': f'D = Q × Norm / Crew = {Q} × {norm_value} / {crew} = {round(duration, 2)}天'
    }
    if adjustments:
        result['formula'] = (
            f'D = 调整后总工日 / Crew = {round(total_labor, 2)} / {crew} = {round(duration, 2)}天'
            f'（调整前 {round(base_labor, 2)} 工日）'
        )

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"=== 工期计算 ===")
        print(f"活动: {act[0]} ({args.activity})")
        print(f"模式: {mode_cn}")
        print(f"工程量: {Q} {q_unit}{_unit_hint(act[2], q_unit)}")
        print(f"条件: {row[0] or '无'}  {row[6] or ''}")
        print(f"定额: {norm_value} {norm_unit}  来源: {source}")
        print(f"班组: {crew}人")
        _print_resource_decision(decision, legacy_used)
        if adjustments:
            print(f"调整系数:")
            for a in adjustments:
                print(f"  [{a['factor_type']}] {a['adjustment_id']} {a['factor_name']} "
                      f"= {a['factor_value']}  ({a['formula']})  来源: {a['source']}")
            print(f"  调整前总工日: {round(base_labor, 2)} → 调整后: {round(total_labor, 2)}")
            for w in adj_warnings:
                print(f"  [警告] {w}")
        print(f"总工日: {round(total_labor, 2)} 工日")
        print(f"工期: {round(duration, 2)}天 → 计划{math.ceil(duration)}天")
        print(f"公式: {result['formula']}")
        print(f"注: 人工定额已标准化到 per 1×{q_unit}，不除以 quantity_basis（={basis}）")


def _print_resource_decision(decision, legacy_used):
    if legacy_used:
        print(f"  [提示] 正在使用旧接口 --crew-size，建议改用 --resource-limit")
    print(f"  用户资源限额: {decision['user_limit'] if decision['user_limit'] is not None else '未指定'}")
    cap = decision['workface_cap']
    print(f"  工作面容量:   {cap if cap is not None else '无规则(no_workface_rule)'}")
    binding = decision['binding_constraint']
    binding_cn = {'user_limit': '用户资源限额', 'workface_cap': '工作面容量', 'both': '两者相等'}.get(binding, binding)
    print(f"  有效投入:     {decision['effective']}{decision['effective_unit']}（由 {binding_cn} 起约束）")
    if decision.get('capacity_source'):
        print(f"  容量来源:     {decision['capacity_source']}")
    if decision.get('no_workface_rule'):
        print(f"  [警告] 该活动无工作面容量规则，未做容量约束，结果可能偏乐观")


def _parse_comb(raw):
    if not raw:
        return None
    try:
        d = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return d or None


def _list_labor_options(rows):
    print("可用人工定额（请用 --condition 精确指定）:")
    print(f"  {'condition_text':<16} {'condition_combination':<46} {'定额值':<10} {'单位'}")
    print('  ' + '-' * 95)
    for r in rows:
        print(f"  {(r[0] or ''):<16} {(r[6] or ''):<46} {r[1]:<10} {r[2] or ''}")


def _equipment_options(rows):
    """列出机械定额的可选条件（按 condition_text 分组）"""
    groups = {}
    for r in rows:
        ct = r[0] or '(空)'
        machines = json.loads(r[1]) if isinstance(r[1], str) else r[1]
        norms = json.loads(r[3]) if isinstance(r[3], str) else r[3]
        specs = json.loads(r[2]) if isinstance(r[2], str) and r[2] else [None] * len(machines)
        pairs = []
        for i, m in enumerate(machines):
            spec = specs[i] if i < len(specs) else ''
            norm = norms[i] if i < len(norms) else ''
            pairs.append(f'{m}({spec}) {norm} 台班')
        groups.setdefault(ct, {'basis': r[5], 'unit': r[6], 'machines': []})
        groups[ct]['machines'].extend(pairs)
    return groups


def _print_equipment_options(rows, activity_id):
    print(f"活动 {activity_id} 的机械定额条件（多条件时必须指定 --condition 或 --match）:")
    for ct, g in _equipment_options(rows).items():
        print(f"  条件: {ct}   基数: {g['basis']} {g['unit']}")
        for m in g['machines']:
            print(f"    {m}")


def calc_equipment(cur, args, act, mode_cn, capacity):
    cur.execute('''
        SELECT condition_text, machine_combination_json, machine_spec_json,
               machine_shift_norm_json, machine_shift_unit_json,
               quantity_basis, quantity_unit, source_code, condition_combination
        FROM Norm_Equipment_Table WHERE activity_id = ?
        ORDER BY condition_text
    ''', (args.activity,))
    rows = cur.fetchall()

    if not rows:
        print(f"错误: 活动 {args.activity} 无机械定额")
        return

    # ---- 1. 条件过滤（原先完全被忽略，是工期失真的主因之一）----
    keywords = parse_match_keywords(args)
    if keywords:
        # 机械定额的 condition_combination 常为 '{}'，匹配主要落在 condition_text 上
        hits = match_rows(rows, keywords, text_idx=0, comb_idx=8)
        if not hits:
            print(f"错误: --match {args.match!r} 未命中任何机械定额")
            print()
            _print_equipment_options(rows, args.activity)
            return
        sel_texts = {(r[0] or '') for r in hits}
        if len(sel_texts) > 1:
            print(f"错误: --match {args.match!r} 命中 {len(sel_texts)} 个不同条件，无法唯一确定:")
            for t in sorted(sel_texts):
                print(f"    {t or '(空)'}")
            print(f"      请补充关键字。")
            return
        filtered = hits
        sel_condition = hits[0][0] or ''
    elif args.condition is not None:
        filtered = [r for r in rows if (r[0] or '') == args.condition]
        if not filtered:
            print(f"错误: 条件 '{args.condition}' 不匹配任何机械定额")
            print()
            _print_equipment_options(rows, args.activity)
            return
        sel_condition = args.condition
    else:
        sel_texts = {(r[0] or '') for r in rows}
        if len(sel_texts) > 1:
            print(f"错误: 活动 {args.activity} 有 {len(sel_texts)} 个机械定额条件，"
                  f"必须用 --condition 或 --match 指定")
            print()
            _print_equipment_options(rows, args.activity)
            return
        filtered = rows
        sel_condition = next(iter(sel_texts))

    # ---- 2. 主控机械 ----
    main_info = get_main_machine(cur, args.activity, sel_condition)
    if not main_info:
        all_machines = sorted({m for r in filtered
                               for m in (json.loads(r[1]) if isinstance(r[1], str) else r[1])})
        print(f"错误: 活动 {args.activity} 未标注主控机械，无法确定工期口径。")
        print(f"      该条件({sel_condition or '(空)'})下的机械: {all_machines}")
        print(f"      需先在 Activity_Main_Machine 中标注主控机械（辅助机械不应决定工期）。")
        return

    main_name = main_info['machine_name']

    # ---- 2.5 条件是否足以区分 ----
    # 若同一条件下有多条定额都含主控机械，说明条件不足以区分这些行，禁止任选
    rows_with_main = []
    for r in filtered:
        machines = json.loads(r[1]) if isinstance(r[1], str) else r[1]
        if main_name in machines:
            rows_with_main.append(r)
    if len(rows_with_main) > 1:
        print(f"错误: 条件 {sel_condition or '(空)'!r} 下有 {len(rows_with_main)} 条机械定额都含主控机械 "
              f"{main_name!r}，条件不足以区分，禁止任选：")
        for r in rows_with_main:
            print(f"    {r[0] or '(空)':<20} 台班={r[3]}  基数={r[5]:g}{r[6]}  {r[7]}")
        print("      请补充 --match 关键字，或先补齐这些行的定额条件。")
        return

    Q = args.quantity
    shifts_per_day = args.shifts_per_day or 1

    def build_entry(r, m, spec, norm, unit, basis, source, is_main):
        required_shifts = Q / basis * norm
        duration = required_shifts / shifts_per_day  # 单台机械口径
        cur.execute('''
            SELECT default_crew_size, crew_composition
            FROM Equipment_Crew_Mapping WHERE machine_name = ?
        ''', (m,))
        crew = cur.fetchone()
        crew_size = crew[0] if crew else 1
        crew_comp = crew[1] if crew else ''
        return {
            'machine': m,
            'spec': spec,
            'norm': norm,
            'unit': unit,
            'basis': basis,
            'quantity_unit': r[6],
            'required_shifts': round(required_shifts, 2),
            'duration_1_machine': round(duration, 2),
            'crew_per_machine': crew_size,
            'crew_composition': crew_comp,
            'source': source,
            'is_main_machine': is_main,
            'condition_text': r[0],
        }

    # ---- 3. 拆分主控 / 辅助 ----
    main_entries = []
    aux_entries = []
    for r in filtered:
        machines = json.loads(r[1]) if isinstance(r[1], str) else r[1]
        norms = json.loads(r[3]) if isinstance(r[3], str) else r[3]
        specs = json.loads(r[2]) if isinstance(r[2], str) and r[2] else [None] * len(machines)
        units = json.loads(r[4]) if isinstance(r[4], str) and r[4] else [None] * len(machines)
        basis = r[5] or 1
        source = r[7]
        for i, m in enumerate(machines):
            spec = specs[i] if i < len(specs) else ''
            norm = norms[i] if i < len(norms) else 0
            unit = units[i] if i < len(units) else '台班'
            entry = build_entry(r, m, spec, norm, unit, basis, source, m == main_name)
            (main_entries if m == main_name else aux_entries).append(entry)

    if not main_entries:
        print(f"错误: 主控机械 {main_name!r} 在条件({sel_condition or '(空)'})下没有定额行")
        return

    # 同一主控机械可能有多行（不同规格/基数），取需求台班最大者
    main_entry = max(main_entries, key=lambda e: e['required_shifts'])

    # ---- 4. 资源投入 ----
    workface_cap = capacity['max_machine'] if capacity else None
    machines_count, decision = decide_resource(args.machines, workface_cap, 'machine')
    if capacity:
        decision['capacity_source'] = (
            f"Workface_Capacity_Rule(source_type={capacity['source_type']}, "
            f"confidence={capacity['confidence']}, unit_basis={capacity['unit_basis']})"
        )
    if machines_count is None:
        print("错误: equipment_driven 活动必须指定 --machines（且数据库中无工作面容量规则）")
        print()
        _print_equipment_options(rows, args.activity)
        return

    # ---- 5. 工期只由主控机械决定 ----
    duration = main_entry['required_shifts'] / machines_count / shifts_per_day
    duration_planned = math.ceil(duration)

    total_crew = main_entry['crew_per_machine'] * machines_count

    result = {
        'activity_id': args.activity,
        'activity_name': act[0],
        'unit': act[2],
        'production_mode': mode_cn,
        'condition_text': sel_condition or None,
        'quantity': Q,
        'quantity_unit': main_entry['quantity_unit'],
        'l4_unit': act[2],
        'machines_count': machines_count,
        'shifts_per_day': shifts_per_day,
        'main_machine': {
            'machine': main_name,
            'spec': main_entry['spec'],
            'norm': main_entry['norm'],
            'unit': main_entry['unit'],
            'basis': main_entry['basis'],
            'required_shifts': main_entry['required_shifts'],
            'source': main_entry['source'],
            'selected_by': f"Activity_Main_Machine(confidence={main_info['confidence']}"
                           f"{', 按条件覆盖' if main_info['is_condition_override'] else ', 默认'})",
            'notes': main_info['notes'],
        },
        'auxiliary_machines': aux_entries,
        'resource_decision': decision,
        'total_crew': total_crew,
        'duration_days': round(duration, 2),
        'duration_planned': duration_planned,
        'formula': (f'D = Q / Basis × 主控台班 / 台数 / 班次 = {Q:g} / {main_entry["basis"]:g} × '
                    f'{main_entry["norm"]} / {machines_count} / {shifts_per_day} = {round(duration, 2)}天'),
        'note': '工期只由主控机械决定；辅助机械不参与工期，仅列出台班与配员',
    }

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"=== 工期计算 ===")
        print(f"活动: {act[0]} ({args.activity})")
        print(f"模式: {mode_cn}")
        print(f"工程量: {Q} {main_entry['quantity_unit']}"
              f"{_unit_hint(act[2], main_entry['quantity_unit'], main_entry['basis'])}")
        print(f"条件: {sel_condition or '(空)'}")
        print(f"主控机械: {main_name} ({main_entry['spec']})  "
              f"[{main_info['confidence']}{'·按条件覆盖' if main_info['is_condition_override'] else '·默认'}]")
        print(f"  台班定额: {main_entry['norm']} {main_entry['unit']}  基数: {main_entry['basis']}"
              f"  来源: {main_entry['source']}")
        print(f"  需求台班: {main_entry['required_shifts']}  配置 {machines_count}台 / 每天{shifts_per_day}班")
        print(f"  配员: {main_entry['crew_per_machine']}人/台 ({main_entry['crew_composition']})"
              f"  共{total_crew}人")
        _print_resource_decision(decision, False)
        if aux_entries:
            print(f"辅助机械（不参与工期）:")
            for e in aux_entries:
                print(f"  {e['machine']} ({e['spec']}): {e['required_shifts']} 台班，"
                      f"配员 {e['crew_per_machine']}人/台")
        print(f"工期: {round(duration, 2)}天 → 计划{duration_planned}天")
        print(f"公式: {result['formula']}")


def _unit_hint(l4_unit, norm_unit, basis=1):
    """工程量必须按定额的真实计量单位传入。

    若活动表登记的 L4.unit 与定额的 quantity_unit 不一致（历史上曾把 quantity_basis
    拼进 unit，如 '100m' / '10m³'），这里显式提示，避免按错单位传入导致数量级错误。
    """
    if not norm_unit or not l4_unit or l4_unit == norm_unit:
        return ''
    extra = f'，基数 {basis:g}' if basis and basis != 1 else ''
    return (f"  ← 注意：活动表登记单位为 {l4_unit!r}，定额计量单位为 {norm_unit}{extra}；"
            f"请按 {norm_unit} 传入工程量")


def main_entry_unit(rows):
    return rows[0][6] if rows else None


def run_demo(args):
    conn = get_conn()
    cur = conn.cursor()

    print("=" * 70)
    print("BuildPlan 完整案例演示（所有数值均从数据库实时计算）")
    print("项目: 10层框架住宅, 标准层500㎡, 1层地下室, 层高3m")
    print("说明: 工程量与班组为 AI 假设，仅用于演示计算机制")
    print("=" * 70)
    print()

    # 案例1: 基坑开挖 (labor_driven, 单条件)
    print("【案例1】基坑开挖 (labor_driven)")
    print('-' * 60)
    cond1 = '坑底面积≤1m²，深度≤2m，二类土'
    cur.execute('''SELECT condition_text, labor_norm_value, labor_norm_unit, source_code
                   FROM Norm_Labor_Table WHERE activity_id=? AND condition_text=?''',
                ('EARTH0029', cond1))
    r = cur.fetchone()
    if r:
        Q, crew = 800.0, 15
        norm = r[1]
        total = Q * norm
        dur = total / crew
        print(f"条件: {r[0]}")
        print(f"工程量: {Q:g} m³ (AI假设)   班组: {crew}人 (AI假设)")
        print(f"定额: {norm} {r[2]}  来源: {r[3]}")
        print(f"总工日: {total:.2f}   工期: {dur:.2f}天 → 计划{math.ceil(dur)}天")
        print(f"公式: D = {Q:g} × {norm} / {crew} = {dur:.2f}天")
    else:
        print(f"  (未找到条件 '{cond1}'，跳过)")
    print()

    # 案例2: 柱钢筋 (labor_driven, 多条件分别计算后求和)
    print("【案例2】柱钢筋 (labor_driven, 多条件分别计算)")
    print('-' * 60)
    parts = [('≤16', 5.0), ('>20', 3.0)]  # (condition_text, 工程量)
    total = 0.0
    for cond, q in parts:
        cur.execute('''SELECT labor_norm_value, source_code FROM Norm_Labor_Table
                       WHERE activity_id='REBAR_NEW_COL' AND condition_text=?''', (cond,))
        rr = cur.fetchone()
        if rr:
            sub = q * rr[0]
            total += sub
            print(f"  {cond:<6} {q:g}t × {rr[0]} 工日/t = {sub:.2f} 工日   ({rr[1]})")
    crew = 8
    dur = total / crew
    print(f"总工日: {total:.2f}   班组: {crew}人 (AI假设)")
    print(f"工期: {dur:.2f}天 → 计划{math.ceil(dur)}天")
    print(f"公式: D = (Q1×N1 + Q2×N2) / Crew = {dur:.2f}天")
    print("注: 直径分配 5t:3t 为 AI 假设，需施工图确认")
    print()

    # 案例3: 柱浇筑 (equipment_driven, 主控机械口径)
    print("【案例3】柱浇筑 (equipment_driven, 主控机械口径)")
    print('-' * 60)
    Q = 60.0
    machines_count = 1
    shifts = 1
    cond = '柱浇筑'
    main_info = get_main_machine(cur, 'CONC_NEW_COLUMN', cond)
    cur.execute('''SELECT machine_combination_json, machine_spec_json, machine_shift_norm_json,
                          machine_shift_unit_json, quantity_basis, quantity_unit, source_code
                   FROM Norm_Equipment_Table WHERE activity_id='CONC_NEW_COLUMN' AND condition_text=?
                   ORDER BY machine_combination_json''', (cond,))
    entries = []
    for er in cur.fetchall():
        machines = json.loads(er[0]) if isinstance(er[0], str) else er[0]
        specs = json.loads(er[1]) if isinstance(er[1], str) and er[1] else [None] * len(machines)
        norms = json.loads(er[2]) if isinstance(er[2], str) else er[2]
        basis = er[4] or 1
        q_unit = er[5] or ''
        for i, m in enumerate(machines):
            norm = norms[i]
            req = Q / basis * norm
            d = req / machines_count / shifts
            cur.execute('SELECT default_crew_size, crew_composition '
                        'FROM Equipment_Crew_Mapping WHERE machine_name=?', (m,))
            cw = cur.fetchone()
            entries.append((m, specs[i] if i < len(specs) else '', norm, req, d, cw,
                            er[6], basis, q_unit))

    if not main_info:
        print(f"  (活动未标注主控机械，跳过)")
        conn.close()
        return

    main_name = main_info['machine_name']
    for m, spec, norm, req, d, cw, src, basis, q_unit in entries:
        cs = cw[0] if cw else '?'
        cc = cw[1] if cw else ''
        tag = '【主控】' if m == main_name else '  辅助  '
        print(f"  {tag} {m} ({spec}): {norm} 台班/{basis:g}{q_unit} × {Q:g}{q_unit} ÷ {basis:g}"
              f" = {req:.2f} 台班 → {d:.2f}天")
        print(f"           配员: {cs}人 ({cc})   来源: {src}")

    main_entries = [e for e in entries if e[0] == main_name]
    if main_entries:
        mx = max(e[3] for e in main_entries) / machines_count / shifts
        print(f"  工期（仅主控机械 {main_name}）: {mx:.2f}天 → 计划{math.ceil(mx)}天")
        aux = [e[0] for e in entries if e[0] != main_name]
        print(f"  辅助机械不参与工期: {aux}")
        print(f"  依据: Activity_Main_Machine(confidence={main_info['confidence']})")

    conn.close()


def main():
    parser = argparse.ArgumentParser(description='BuildPlan 工期计算')
    parser.add_argument('--activity', help='活动ID')
    parser.add_argument('--quantity', type=float, help='工程量')
    parser.add_argument('--resource-limit', type=int, dest='resource_limit',
                        help='用户可用人工上限（labor_driven）。工具会与工作面容量取 min')
    parser.add_argument('--crew-size', type=int, help='[旧接口] 等价于 --resource-limit，建议改用后者')
    parser.add_argument('--condition', help='定额条件 condition_text（精确匹配，须唯一命中一行）')
    parser.add_argument('--match', help='多关键字 AND 匹配，逗号分隔，如 "框架梁,≤25"；'
                                        '关键字须全部出现在 condition_combination + condition_text 中')
    parser.add_argument('--list-conditions', action='store_true', dest='list_conditions',
                        help='列出该活动的全部定额条件，并给出可直接复制的 --match 关键字串')
    parser.add_argument('--machines', type=int, help='用户可用机械台数 (equipment_driven)，会与工作面容量取 min')
    parser.add_argument('--shifts-per-day', type=int, default=1, help='每天班次 (默认1)')
    parser.add_argument('--demo', action='store_true', help='运行完整案例演示')
    parser.add_argument('--json', action='store_true', help='JSON格式输出')

    args = parser.parse_args()

    if args.demo:
        run_demo(args)
    elif args.activity:
        if args.quantity is None and not args.list_conditions:
            print("错误: 必须指定 --quantity")
            return
        calc_duration(args)
    else:
        parser.print_help()


if __name__ == '__main__':
    main()

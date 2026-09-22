#!/usr/bin/env python3
"""BuildPlan 查询工具 - 定额查询

用法:
    python query_norms.py --activity <activity_id>    查询某活动的所有定额
    python query_norms.py --work-type <work_type_id>  查询某工种下所有活动的定额概览
    python query_norms.py --search <keyword>          模糊搜索活动名并显示定额
"""

import sqlite3
import argparse
import json
import os
import sys
import io

if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'kb.db')


def get_conn():
    return sqlite3.connect(DB_PATH)


def _parse_comb(raw):
    """condition_combination JSON -> dict；空对象/非法一律返回 None"""
    if not raw:
        return None
    try:
        d = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return d or None


def _build_notes(labor_rows, equip_rows):
    notes = []
    # 机械表 condition_combination 为空时，条件只能看 condition_text
    if equip_rows and all(_parse_comb(r[9]) is None for r in equip_rows):
        notes.append('机械定额的 condition_combination 为空，适用条件以 condition_text 为准')
    # 人工表多条件时，必须按 condition_combination 多维匹配后再取值
    if len(labor_rows) > 1 and any(_parse_comb(r[7]) for r in labor_rows):
        notes.append('人工定额存在多条件候选，请按 condition_combination 逐维匹配后再取值，不要取首行')
    return notes


def query_by_activity(args):
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

    # 人工定额
    # 第 37 轮：`Norm_Labor_Table.quantity_basis` 已改名 `raw_quantity_basis`
    # （labor_norm_value 早已归一为「工日 / 1×单位」，basis 只作溯源、不参与乘法）。
    # 迁移前的老库仍是旧名，两种都读得到同一个数。
    _basis_col = "raw_quantity_basis" if "raw_quantity_basis" in {
        r[1] for r in cur.execute("PRAGMA table_info(Norm_Labor_Table)")} else "quantity_basis"
    cur.execute('''
        SELECT condition_text, labor_norm_value, labor_norm_unit,
               __BASIS__, quantity_unit, source_code, source_item_code,
               condition_combination
        FROM Norm_Labor_Table
        WHERE activity_id = ?
        ORDER BY condition_text
    '''.replace("__BASIS__", _basis_col), (args.activity,))
    labor_rows = cur.fetchall()

    # 机械定额
    cur.execute('''
        SELECT condition_text, machine_combination_json, machine_spec_json,
               machine_shift_norm_json, machine_shift_unit_json,
               quantity_basis, quantity_unit, source_code, source_item_code,
               condition_combination
        FROM Norm_Equipment_Table
        WHERE activity_id = ?
        ORDER BY condition_text
    ''', (args.activity,))
    equip_rows = cur.fetchall()

    # 机组配置（按 机械名称 + 规格 精确匹配，避免同名不同规格取错）
    crew_info = []
    seen = set()
    for er in equip_rows:
        if er[1]:
            machines = json.loads(er[1]) if isinstance(er[1], str) else er[1]
            specs = json.loads(er[2]) if isinstance(er[2], str) and er[2] else [None] * len(machines)
            for idx, m in enumerate(machines):
                spec = specs[idx] if idx < len(specs) else None
                key = (m, spec)
                if key in seen:
                    continue
                seen.add(key)
                cur.execute('''
                    SELECT machine_name, machine_spec, default_crew_size, crew_composition
                    FROM Equipment_Crew_Mapping WHERE machine_name = ? AND machine_spec = ?
                ''', (m, spec))
                crew = cur.fetchone()
                if not crew:
                    cur.execute('''
                        SELECT machine_name, machine_spec, default_crew_size, crew_composition
                        FROM Equipment_Crew_Mapping WHERE machine_name = ?
                    ''', (m,))
                    crew = cur.fetchone()
                if crew:
                    crew_info.append(crew)

    # H4/H5（2026-09-21）：`Norm_Adjustment` / `Norm_Adjustment_Target` 已删除，
    # 不再查询/展示"调整系数"。

    # 工作面容量（决定有效班组/台数的上限）
    # 合表后只剩 `Workface_Capacity_Rule` 一张表；`legacy_max_*` 是旧 v1 表的兼容列，
    # 别名回 `max_labor` / `max_machine` 以保持下面按位置取值的代码不变。
    cur.execute('''
        SELECT unit_basis, legacy_max_labor AS max_labor,
               legacy_max_machine AS max_machine, source_type, confidence, notes
        FROM Workface_Capacity_Rule WHERE activity_id = ?
    ''', (args.activity,))
    cap_row = cur.fetchone()

    # 主控机械（仅机械主导活动有；决定 equipment_driven 的工期口径）
    cur.execute('''
        SELECT condition_text, machine_name, machine_spec, confidence, notes
        FROM Activity_Main_Machine WHERE activity_id = ?
        ORDER BY condition_text
    ''', (args.activity,))
    main_rows = cur.fetchall()

    conn.close()

    mode_cn = {'labor_driven': '人工主导', 'equipment_driven': '机械主导'}.get(act[3] or '', act[3] or '')

    if args.json:
        result = {
            'activity_id': args.activity,
            'activity_name': act[0],
            'work_type_id': act[1],
            'unit': act[2],
            'production_mode': act[3],
            'labor_norms': [
                {'condition_text': r[0], 'condition_combination': _parse_comb(r[7]),
                 'value': r[1], 'unit': r[2], 'basis': r[3], 'basis_unit': r[4],
                 'source': r[5], 'item_code': r[6]}
                for r in labor_rows
            ],
            'equipment_norms': [
                {'condition_text': r[0], 'condition_combination': _parse_comb(r[9]),
                 'machines': json.loads(r[1]) if isinstance(r[1], str) else r[1],
                 'specs': json.loads(r[2]) if isinstance(r[2], str) and r[2] else None,
                 'norms': json.loads(r[3]) if isinstance(r[3], str) else r[3],
                 'units': json.loads(r[4]) if isinstance(r[4], str) and r[4] else None,
                 'basis': r[5], 'basis_unit': r[6], 'source': r[7], 'item_code': r[8]}
                for r in equip_rows
            ],
            'crew': [{'name': r[0], 'spec': r[1], 'size': r[2], 'composition': r[3]} for r in crew_info],
            'workface_capacity': ({
                'unit_basis': cap_row[0], 'max_labor': cap_row[1], 'max_machine': cap_row[2],
                'source_type': cap_row[3], 'confidence': cap_row[4], 'notes': cap_row[5],
            } if cap_row else None),
            'main_machine': [
                {'condition_text': r[0] or '(默认)', 'machine_name': r[1], 'machine_spec': r[2],
                 'confidence': r[3], 'notes': r[4]}
                for r in main_rows
            ] or None,
            'notes': _build_notes(labor_rows, equip_rows)
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"activity_id: {args.activity}")
        print(f"activity_name: {act[0]}")
        print(f"work_type_id: {act[1]}")
        print(f"unit: {act[2]}")
        print(f"production_mode: {mode_cn} ({act[3]})")
        if cap_row:
            print(f"工作面容量: 最多 {cap_row[1]}人"
                  f"{(' / ' + str(cap_row[2]) + '台机械') if cap_row[2] else ''}"
                  f"  [{cap_row[0]}, source_type={cap_row[3]}, confidence={cap_row[4]}]")
        else:
            print(f"工作面容量: 无规则（工期计算时需显式提供资源限额，否则报错）")
        if main_rows:
            for r in main_rows:
                tag = '默认' if not r[0] else f'条件「{r[0]}」'
                print(f"主控机械({tag}): {r[1]} {r[2] or ''}  [confidence={r[3]}]")
        print()

        if labor_rows:
            print("人工定额:")
            print(f"  {'condition_text':<16} {'condition_combination':<46} {'定额值':<10} {'单位':<12} {'来源'}")
            print('  ' + '-' * 110)
            for r in labor_rows:
                comb = r[7] or ''
                print(f"  {(r[0] or ''):<16} {comb:<46} {r[1]:<10} {r[2] or '':<12} {r[5] or ''}")
            print()

        if equip_rows:
            print("机械定额:")
            for r in equip_rows:
                machines = json.loads(r[1]) if isinstance(r[1], str) else r[1]
                specs = json.loads(r[2]) if isinstance(r[2], str) and r[2] else [None] * len(machines)
                norms = json.loads(r[3]) if isinstance(r[3], str) else r[3]
                units = json.loads(r[4]) if isinstance(r[4], str) and r[4] else [None] * len(machines)
                comb = r[9] if r[9] and r[9] not in ('{}', '') else '（空，见 condition_text）'
                print(f"  条件: {r[0] or '无'}   组合: {comb}")
                for i, m in enumerate(machines):
                    spec = specs[i] if i < len(specs) else ''
                    norm = norms[i] if i < len(norms) else ''
                    unit = units[i] if i < len(units) else ''
                    print(f"    {m} ({spec}): {norm} {unit}")
                print(f"    基数: {r[5]} {r[6]}  来源: {r[7]}  子目: {r[8] or '—'}")
            print()

        if crew_info:
            print("机组配置:")
            print(f"  {'机械名称':<20} {'规格':<15} {'人数':<6} {'人员组成'}")
            print('  ' + '-' * 60)
            for r in crew_info:
                print(f"  {(r[0] or ''):<20} {(r[1] or ''):<15} {r[2] or '':<6} {r[3] or ''}")
            print()

        for n in _build_notes(labor_rows, equip_rows):
            print(f"注: {n}")


def query_by_work_type(args):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute('SELECT work_type_name FROM L3_Work_Type WHERE work_type_id = ?', (args.work_type,))
    row = cur.fetchone()
    if not row:
        print(f"错误: 未找到工种 '{args.work_type}'")
        conn.close()
        return

    cur.execute('''
        SELECT a.activity_id, a.activity_name, a.unit, a.recommended_production_mode,
               (SELECT COUNT(*) FROM Norm_Labor_Table n WHERE n.activity_id = a.activity_id) as labor_count,
               (SELECT COUNT(*) FROM Norm_Equipment_Table e WHERE e.activity_id = a.activity_id) as equip_count
        FROM L4_Activity_Dictionary a
        WHERE a.work_type_id = ?
        ORDER BY a.activity_id
    ''', (args.work_type,))
    rows = cur.fetchall()
    conn.close()

    if args.json:
        print(json.dumps({
            'work_type_id': args.work_type,
            'work_type_name': row[0],
            'activities': [
                {'id': r[0], 'name': r[1], 'unit': r[2], 'mode': r[3], 'labor_norms': r[4], 'equip_norms': r[5]}
                for r in rows
            ]
        }, ensure_ascii=False, indent=2))
    else:
        print(f"工种: {row[0]} ({args.work_type})")
        print('-' * 80)
        print(f"{'activity_id':<25} {'名称':<20} {'单位':<8} {'模式':<15} {'人工定额':<8} {'机械定额'}")
        print('-' * 80)
        for r in rows:
            mode_cn = {'labor_driven': '人工主导', 'equipment_driven': '机械主导'}.get(r[3] or '', r[3] or '')
            print(f"{r[0]:<25} {r[1]:<20} {r[2] or '':<8} {mode_cn:<15} {r[4]:<8} {r[5]}")


def search_norms(args):
    keyword = args.keyword
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('''
        SELECT a.activity_id, a.activity_name, a.unit, a.recommended_production_mode, l.work_type_name
        FROM L4_Activity_Dictionary a
        JOIN L3_Work_Type l ON a.work_type_id = l.work_type_id
        WHERE a.activity_id LIKE ? OR a.activity_name LIKE ?
        ORDER BY a.work_type_id, a.activity_id
    ''', (f'%{keyword}%', f'%{keyword}%'))
    rows = cur.fetchall()
    conn.close()

    if args.json:
        print(json.dumps([{'id': r[0], 'name': r[1], 'unit': r[2], 'mode': r[3], 'work_type': r[4]} for r in rows], ensure_ascii=False, indent=2))
    else:
        print(f"搜索: '{keyword}'")
        print(f"共 {len(rows)} 个结果")
        print('-' * 80)
        print(f"{'activity_id':<25} {'名称':<20} {'单位':<8} {'模式':<15} {'工种'}")
        print('-' * 80)
        for r in rows:
            mode_cn = {'labor_driven': '人工主导', 'equipment_driven': '机械主导'}.get(r[3] or '', r[3] or '')
            print(f"{r[0]:<25} {r[1]:<20} {r[2] or '':<8} {mode_cn:<15} {r[4]}")


def main():
    parser = argparse.ArgumentParser(description='BuildPlan 定额查询')
    parser.add_argument('--activity', help='查询某活动的所有定额')
    parser.add_argument('--work-type', help='查询某工种下所有活动的定额概览')
    parser.add_argument('--search', help='模糊搜索')
    parser.add_argument('--json', action='store_true', help='JSON格式输出')

    args = parser.parse_args()

    if args.activity:
        query_by_activity(args)
    elif args.work_type:
        query_by_work_type(args)
    elif args.search:
        search_norms(args)
    else:
        parser.print_help()


if __name__ == '__main__':
    main()

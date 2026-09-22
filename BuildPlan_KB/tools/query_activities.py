#!/usr/bin/env python3
"""BuildPlan 查询工具 - 活动查询

用法:
    python query_activities.py --l3                        列出所有L3及L4数量
    python query_activities.py --l4 <work_type_id>         列出某L3下的所有L4
    python query_activities.py --mode <mode>               按production_mode筛选
    python query_activities.py --search <keyword>          模糊搜索活动名
    python query_activities.py --stats                     整体统计

注: 某活动的可选用条件不在本工具查询。条件来源于 Norm 表，
    请用 query_norms.py --activity <activity_id> 查看 condition_combination。
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


def list_l3(args):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('''
        SELECT l.work_type_id, l.work_type_name, COUNT(a.activity_id) as l4_count
        FROM L3_Work_Type l
        LEFT JOIN L4_Activity_Dictionary a ON l.work_type_id = a.work_type_id
        GROUP BY l.work_type_id
        ORDER BY l.work_type_id
    ''')
    rows = cur.fetchall()
    conn.close()

    if args.json:
        print(json.dumps([{'id': r[0], 'name': r[1], 'l4_count': r[2]} for r in rows], ensure_ascii=False, indent=2))
    else:
        print(f"{'work_type_id':<25} {'名称':<25} {'L4数量'}")
        print('-' * 60)
        for r in rows:
            print(f"{r[0]:<25} {r[1]:<25} {r[2]}")


def list_l4(args):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute('SELECT work_type_name FROM L3_Work_Type WHERE work_type_id = ?', (args.work_type,))
    row = cur.fetchone()
    if not row:
        print(f"错误: 未找到L3 '{args.work_type}'")
        conn.close()
        return
    wt_name = row[0]

    structure = args.structure
    mapping_absent = False

    if structure:
        st = cur.execute('SELECT structure_type_name FROM Structure_Type_Dictionary WHERE structure_type_id = ?',
                         (structure,)).fetchone()
        if not st:
            print(f"错误: 未找到结构形式 '{structure}'（用 query_project.py --structures 查看全部）")
            conn.close()
            return

        mapped = cur.execute('''
            SELECT COUNT(*) FROM Structure_Type_L4_Mapping m
            JOIN L4_Activity_Dictionary a ON m.activity_id = a.activity_id
            WHERE a.work_type_id = ? AND m.structure_type_id = ?
        ''', (args.work_type, structure)).fetchone()[0]
        mapping_absent = (mapped == 0)

    if structure and not mapping_absent:
        cur.execute('''
            SELECT a.activity_id, a.activity_name, a.unit, a.recommended_production_mode, a.status,
                   m.applicability_level
            FROM L4_Activity_Dictionary a
            JOIN Structure_Type_L4_Mapping m
                 ON m.activity_id = a.activity_id AND m.structure_type_id = ?
            WHERE a.work_type_id = ? AND m.applicability_level != 'EXCLUDED'
            ORDER BY a.activity_id
        ''', (structure, args.work_type))
        rows = cur.fetchall()
        levels = {r[0]: r[5] for r in rows}
    else:
        cur.execute('''
            SELECT activity_id, activity_name, unit, recommended_production_mode, status
            FROM L4_Activity_Dictionary
            WHERE work_type_id = ?
            ORDER BY activity_id
        ''', (args.work_type,))
        rows = cur.fetchall()
    conn.close()

    if args.json:
        result = {
            'work_type_id': args.work_type,
            'work_type_name': wt_name,
            'structure_type_id': structure,
            'activities': [
                {'id': r[0], 'name': r[1], 'unit': r[2], 'mode': r[3], 'status': r[4],
                 **({'applicability_level': r[5]} if len(r) > 5 else {})}
                for r in rows
            ]
        }
        if mapping_absent:
            result['structure_mapping_absent'] = (
                f'L3 {args.work_type} 尚无结构形式映射数据，已返回全部活动，未按 {structure} 过滤'
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"work_type_id: {args.work_type}")
        print(f"work_type_name: {wt_name}")
        if structure:
            print(f"structure_type_id: {structure}")
        print(f"total: {len(rows)} L4")
        if mapping_absent:
            print(f"[提示] 该 L3 尚无结构形式映射数据，未按 {structure} 过滤，返回全部活动")
        print('-' * 80)
        print(f"{'activity_id':<25} {'名称':<20} {'单位':<8} {'生产模式'}")
        print('-' * 80)
        for r in rows:
            mode_cn = {'labor_driven': '人工主导', 'equipment_driven': '机械主导'}.get(r[3] or '', r[3] or '')
            suffix = f"  [{r[5]}]" if len(r) > 5 else ''
            print(f"{r[0]:<25} {r[1]:<20} {r[2] or '':<8} {mode_cn}{suffix}")


def filter_by_mode(args):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('''
        SELECT a.activity_id, a.activity_name, a.unit, l.work_type_name
        FROM L4_Activity_Dictionary a
        JOIN L3_Work_Type l ON a.work_type_id = l.work_type_id
        WHERE a.recommended_production_mode = ?
        ORDER BY a.work_type_id, a.activity_id
    ''', (args.mode,))
    rows = cur.fetchall()
    conn.close()

    mode_cn = {'labor_driven': '人工主导', 'equipment_driven': '机械主导'}.get(args.mode, args.mode)

    if args.json:
        print(json.dumps([{'id': r[0], 'name': r[1], 'unit': r[2], 'work_type': r[3]} for r in rows], ensure_ascii=False, indent=2))
    else:
        print(f"生产模式: {mode_cn} ({args.mode})")
        print(f"共 {len(rows)} 个活动")
        print('-' * 70)
        print(f"{'activity_id':<25} {'名称':<20} {'单位':<8} {'工种'}")
        print('-' * 70)
        for r in rows:
            print(f"{r[0]:<25} {r[1]:<20} {r[2] or '':<8} {r[3]}")


def search_activities(args):
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


def show_stats(args):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute('SELECT COUNT(*) FROM L3_Work_Type')
    l3_count = cur.fetchone()[0]
    cur.execute('SELECT COUNT(*) FROM L4_Activity_Dictionary')
    l4_count = cur.fetchone()[0]
    cur.execute('SELECT COUNT(*) FROM Condition_Dictionary')
    cond_count = cur.fetchone()[0]
    cur.execute('SELECT COUNT(*) FROM Norm_Labor_Table')
    nl_count = cur.fetchone()[0]
    cur.execute('SELECT COUNT(*) FROM Norm_Equipment_Table')
    ne_count = cur.fetchone()[0]
    cur.execute('SELECT COUNT(*) FROM Equipment_Crew_Mapping')
    ecm_count = cur.fetchone()[0]
    # H4/H5（2026-09-21）：`Norm_Adjustment` / `Norm_Adjustment_Target` 已删除，
    # 统计里不再有"调整系数"这一项。

    cur.execute('SELECT recommended_production_mode, COUNT(*) FROM L4_Activity_Dictionary GROUP BY recommended_production_mode')
    modes = cur.fetchall()

    cur.execute('SELECT COUNT(DISTINCT activity_id) FROM Norm_Labor_Table')
    labor_activities = cur.fetchone()[0]
    cur.execute('SELECT COUNT(DISTINCT activity_id) FROM Norm_Equipment_Table')
    equip_activities = cur.fetchone()[0]

    conn.close()

    if args.json:
        print(json.dumps({
            'l3_count': l3_count, 'l4_count': l4_count, 'condition_count': cond_count,
            'norm_labor_count': nl_count, 'norm_equipment_count': ne_count,
            'equipment_crew_count': ecm_count,
            'production_modes': {r[0]: r[1] for r in modes},
            'labor_covered_activities': labor_activities,
            'equipment_covered_activities': equip_activities
        }, ensure_ascii=False, indent=2))
    else:
        print("=== BuildPlan 数据库统计 ===")
        print(f"L3工程类型:     {l3_count}")
        print(f"L4活动:         {l4_count}")
        print(f"Condition条件:  {cond_count}")
        print(f"人工定额:       {nl_count}条 (覆盖{labor_activities}个L4)")
        print(f"机械定额:       {ne_count}条 (覆盖{equip_activities}个L4)")
        print(f"机组配置:       {ecm_count}条")
        print()
        print("生产模式分布:")
        for r in modes:
            mode_cn = {'labor_driven': '人工主导', 'equipment_driven': '机械主导'}.get(r[0], r[0])
            print(f"  {mode_cn}: {r[1]}")


def main():
    parser = argparse.ArgumentParser(description='BuildPlan 活动查询')
    parser.add_argument('--l3', action='store_true', help='列出所有L3')
    parser.add_argument('--l4', dest='work_type', help='列出某L3下的L4')
    parser.add_argument('--structure', help='结构形式ID（配合 --l4 使用，过滤该结构体系不适用的 L4）')
    parser.add_argument('--mode', help='按production_mode筛选')
    parser.add_argument('--search', help='模糊搜索')
    parser.add_argument('--stats', action='store_true', help='整体统计')
    parser.add_argument('--json', action='store_true', help='JSON格式输出')

    args = parser.parse_args()

    if args.l3:
        list_l3(args)
    elif args.work_type:
        list_l4(args)
    elif args.mode:
        filter_by_mode(args)
    elif args.search:
        search_activities(args)
    elif args.stats:
        show_stats(args)
    else:
        parser.print_help()
        print('\n提示: 查询某活动的可选用条件请用 query_norms.py --activity <activity_id>')
        print('      （条件来自 Norm 表的 condition_combination，不作为独立查询入口）')


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""BuildPlan 查询工具 - 项目上下文查询

用法:
    python query_project.py --types                     列出所有建筑类型
    python query_project.py --structures                列出所有结构形式
    python query_project.py --l3-for <ID或中文名>        某建筑类型适用的L3
    python query_project.py --l3-for <ID或中文名> --structure <s>  指定结构形式
    python query_project.py --search <keyword>          模糊搜索建筑类型/结构形式
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


def list_building_types(args):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('SELECT building_type_id, building_type_name, description FROM Building_Type_Dictionary ORDER BY building_type_id')
    rows = cur.fetchall()
    conn.close()

    if args.json:
        print(json.dumps([{'id': r[0], 'name': r[1], 'desc': r[2]} for r in rows], ensure_ascii=False, indent=2))
    else:
        print(f"{'ID':<20} {'名称':<15} {'说明'}")
        print('-' * 60)
        for r in rows:
            print(f"{r[0]:<20} {r[1]:<15} {r[2] or ''}")


def list_structure_types(args):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('SELECT structure_type_id, structure_type_name, description FROM Structure_Type_Dictionary ORDER BY structure_type_id')
    rows = cur.fetchall()
    conn.close()

    if args.json:
        print(json.dumps([{'id': r[0], 'name': r[1], 'desc': r[2]} for r in rows], ensure_ascii=False, indent=2))
    else:
        print(f"{'ID':<20} {'名称':<15} {'说明'}")
        print('-' * 60)
        for r in rows:
            print(f"{r[0]:<20} {r[1]:<15} {r[2] or ''}")


def query_l3_for_type(args):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute('SELECT building_type_id, building_type_name FROM Building_Type_Dictionary '
                'WHERE building_type_id = ? OR building_type_name = ?', (args.type, args.type))
    row = cur.fetchone()
    if not row:
        print(f"错误: 未找到建筑类型 '{args.type}'（可用 ID 或中文名，--types 查看全部）")
        conn.close()
        return
    type_id, type_name = row[0], row[1]

    query = '''
        SELECT m.work_type_id, l.work_type_name, m.applicability_level, m.confidence, m.notes
        FROM Building_Type_L3_Mapping m
        LEFT JOIN L3_Work_Type l ON m.work_type_id = l.work_type_id
        WHERE m.building_type_id = ?
        ORDER BY
            CASE m.applicability_level
                WHEN 'REQUIRED' THEN 1
                WHEN 'OPTIONAL' THEN 2
                WHEN 'USUAL' THEN 2
                WHEN 'EXCLUDED' THEN 3
            END,
            m.work_type_id
    '''
    cur.execute(query, (type_id,))
    rows = [list(r) for r in cur.fetchall()]

    structure = args.structure
    structure_name = None
    counts = {}

    if structure:
        st = cur.execute('SELECT structure_type_name FROM Structure_Type_Dictionary WHERE structure_type_id = ?',
                         (structure,)).fetchone()
        if not st:
            print(f"错误: 未找到结构形式 '{structure}'（用 --structures 查看全部）")
            conn.close()
            return
        structure_name = st[0]

        # 该结构形式下，每个 L3 适用的 L4 数量（排除 EXCLUDED）
        cur.execute('''
            SELECT a.work_type_id,
                   SUM(CASE WHEN m.applicability_level != 'EXCLUDED' THEN 1 ELSE 0 END)
            FROM Structure_Type_L4_Mapping m
            JOIN L4_Activity_Dictionary a ON a.activity_id = m.activity_id
            WHERE m.structure_type_id = ?
            GROUP BY a.work_type_id
        ''', (structure,))
        counts = {r[0]: r[1] for r in cur.fetchall()}

    conn.close()

    # 按结构形式调整适用性
    for r in rows:
        wt = r[0]
        if not structure:
            continue
        if wt not in counts:
            # 该 L3 尚无结构映射数据，保持原级别并标注
            r.append(None)          # structure_applicable_l4_count
            r.append(True)          # structure_mapping_absent
            continue
        n = counts[wt]
        r.append(n)
        r.append(False)
        if n == 0:
            r[2] = 'EXCLUDED'
            reason = f'{structure_name}体系下无适用活动'
            r[4] = f'{r[4]}；{reason}' if r[4] else reason
        elif r[2] == 'EXCLUDED':
            # 建筑类型判为不适用，但该结构形式下确实有适用活动 → 上调为"可选"
            # （A1 三档化后枚举名是 OPTIONAL；旧的 USUAL 已被合并进 OPTIONAL）
            r[2] = 'OPTIONAL'
            reason = f'{structure_name}体系下存在 {n} 个适用活动，覆盖建筑类型映射的不适用判断'
            r[4] = f'{r[4]}；{reason}' if r[4] else reason

    if args.json:
        result = {
            'building_type': type_id,
            'building_type_name': type_name,
            'structure': structure or None,
            'structure_type_name': structure_name,
            'work_types': [
                {'id': r[0], 'name': r[1], 'level': r[2], 'confidence': r[3], 'notes': r[4],
                 **({'structure_applicable_l4_count': r[5],
                     'structure_mapping_absent': r[6]} if len(r) > 5 else {})}
                for r in rows
            ]
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"建筑类型: {type_name} ({type_id})")
        if structure:
            print(f"结构形式: {structure_name} ({structure})")
        print('-' * 70)
        header = f"{'work_type_id':<25} {'名称':<20} {'适用级别':<12} {'置信度'}"
        if structure:
            header += f" {'适用L4数'}"
        print(header)
        print('-' * 70)
        for r in rows:
            level_cn = {'REQUIRED': '必须包含', 'OPTIONAL': '可选', 'USUAL': '可选',
                        'EXCLUDED': '不适用'}.get(r[2], r[2])
            line = f"{r[0]:<25} {r[1] or '':<20} {level_cn:<12} {r[3] or ''}"
            if structure:
                if len(r) > 6 and r[6]:
                    cnt = '无映射'
                else:
                    cnt = str(r[5])
                line += f" {cnt}"
            print(line)


def search_types(args):
    keyword = args.search
    conn = get_conn()
    cur = conn.cursor()

    cur.execute('SELECT building_type_id, building_type_name FROM Building_Type_Dictionary WHERE building_type_id LIKE ? OR building_type_name LIKE ?',
                (f'%{keyword}%', f'%{keyword}%'))
    bt_rows = cur.fetchall()

    cur.execute('SELECT structure_type_id, structure_type_name FROM Structure_Type_Dictionary WHERE structure_type_id LIKE ? OR structure_type_name LIKE ?',
                (f'%{keyword}%', f'%{keyword}%'))
    st_rows = cur.fetchall()

    conn.close()

    if args.json:
        print(json.dumps({
            'building_types': [{'id': r[0], 'name': r[1]} for r in bt_rows],
            'structure_types': [{'id': r[0], 'name': r[1]} for r in st_rows]
        }, ensure_ascii=False, indent=2))
    else:
        if bt_rows:
            print("建筑类型:")
            for r in bt_rows:
                print(f"  {r[0]}: {r[1]}")
        if st_rows:
            print("结构形式:")
            for r in st_rows:
                print(f"  {r[0]}: {r[1]}")
        if not bt_rows and not st_rows:
            print(f"未找到匹配 '{keyword}' 的结果")


def main():
    parser = argparse.ArgumentParser(description='BuildPlan 项目上下文查询')
    parser.add_argument('--types', action='store_true', help='列出所有建筑类型')
    parser.add_argument('--structures', action='store_true', help='列出所有结构形式')
    parser.add_argument('--l3-for', dest='type', help='某建筑类型适用的L3（传ID或中文名，如 residential / 住宅）')
    parser.add_argument('--structure', help='指定结构形式（配合 --l3-for 使用）')
    parser.add_argument('--search', help='模糊搜索')
    parser.add_argument('--json', action='store_true', help='JSON格式输出')

    args = parser.parse_args()

    if args.types:
        list_building_types(args)
    elif args.structures:
        list_structure_types(args)
    elif args.type:
        query_l3_for_type(args)
    elif args.search:
        search_types(args)
    else:
        parser.print_help()


if __name__ == '__main__':
    main()

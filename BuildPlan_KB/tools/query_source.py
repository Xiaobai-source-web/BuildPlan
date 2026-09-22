#!/usr/bin/env python3
"""BuildPlan 查询工具 - 数据来源查询

用法:
    python query_source.py --list                  列出所有数据源
    python query_source.py --for <work_type_id>    某工种的数据来源
    python query_source.py --quality               数据质量概览
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


def list_sources(args):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('''
        SELECT source_code, document_name, standard_code, publisher, year,
               region, source_type, primary_purpose, coverage_scope
        FROM sources ORDER BY source_code
    ''')
    rows = cur.fetchall()
    conn.close()

    if args.json:
        print(json.dumps([
            {'code': r[0], 'name': r[1], 'standard': r[2], 'publisher': r[3],
             'year': r[4], 'region': r[5], 'type': r[6], 'purpose': r[7], 'scope': r[8]}
            for r in rows
        ], ensure_ascii=False, indent=2))
    else:
        print(f"{'代码':<25} {'名称':<45} {'类型':<15} {'年份'}")
        print('-' * 100)
        for r in rows:
            print(f"{r[0]:<25} {(r[1] or '')[:43]:<45} {(r[6] or ''):<15} {r[4] or ''}")


def query_for_work_type(args):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute('SELECT work_type_name FROM L3_Work_Type WHERE work_type_id = ?', (args.work_type,))
    row = cur.fetchone()
    if not row:
        print(f"错误: 未找到工种 '{args.work_type}'")
        conn.close()
        return

    cur.execute('''
        SELECT DISTINCT s.source_code, s.document_name, s.source_type,
               COUNT(DISTINCT n.activity_id) as activity_count,
               COUNT(n.norm_id) as norm_count
        FROM sources s
        JOIN Norm_Labor_Table n ON s.source_code = n.source_code
        JOIN L4_Activity_Dictionary a ON n.activity_id = a.activity_id
        WHERE a.work_type_id = ?
        GROUP BY s.source_code
        UNION
        SELECT DISTINCT s.source_code, s.document_name, s.source_type,
               COUNT(DISTINCT e.activity_id) as activity_count,
               COUNT(e.norm_id) as norm_count
        FROM sources s
        JOIN Norm_Equipment_Table e ON s.source_code = e.source_code
        JOIN L4_Activity_Dictionary a ON e.activity_id = a.activity_id
        WHERE a.work_type_id = ?
        GROUP BY s.source_code
    ''', (args.work_type, args.work_type))
    rows = cur.fetchall()
    conn.close()

    if args.json:
        print(json.dumps({
            'work_type_id': args.work_type,
            'work_type_name': row[0],
            'sources': [
                {'code': r[0], 'name': r[1], 'type': r[2], 'activities': r[3], 'norms': r[4]}
                for r in rows
            ]
        }, ensure_ascii=False, indent=2))
    else:
        print(f"工种: {row[0]} ({args.work_type})")
        print('-' * 80)
        print(f"{'来源代码':<25} {'文档名':<40} {'活动数':<8} {'定额数'}")
        print('-' * 80)
        for r in rows:
            print(f"{r[0]:<25} {(r[1] or '')[:38]:<40} {r[3]:<8} {r[4]}")


def show_quality(args):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute('''
        SELECT issue_type, severity, COUNT(*)
        FROM data_quality_log
        GROUP BY issue_type, severity
        ORDER BY severity, COUNT(*) DESC
    ''')
    rows = cur.fetchall()

    cur.execute('SELECT COUNT(*) FROM data_quality_log')
    total = cur.fetchone()[0]

    cur.execute('SELECT COUNT(*) FROM data_quality_log WHERE resolved_at IS NOT NULL')
    resolved = cur.fetchone()[0]

    conn.close()

    if args.json:
        print(json.dumps({
            'total_issues': total,
            'resolved': resolved,
            'pending': total - resolved,
            'by_type_severity': [{'type': r[0], 'severity': r[1], 'count': r[2]} for r in rows]
        }, ensure_ascii=False, indent=2))
    else:
        print(f"=== 数据质量概览 ===")
        print(f"总问题数: {total}")
        print(f"已解决: {resolved}")
        print(f"待处理: {total - resolved}")
        print()
        print(f"{'问题类型':<30} {'严重度':<10} {'数量'}")
        print('-' * 50)
        for r in rows:
            print(f"{r[0]:<30} {r[1]:<10} {r[2]}")


def main():
    parser = argparse.ArgumentParser(description='BuildPlan 数据来源查询')
    parser.add_argument('--list', action='store_true', help='列出所有数据源')
    parser.add_argument('--for', dest='work_type', help='某工种的数据来源')
    parser.add_argument('--quality', action='store_true', help='数据质量概览')
    parser.add_argument('--json', action='store_true', help='JSON格式输出')

    args = parser.parse_args()

    if args.list:
        list_sources(args)
    elif args.work_type:
        query_for_work_type(args)
    elif args.quality:
        show_quality(args)
    else:
        parser.print_help()


if __name__ == '__main__':
    main()

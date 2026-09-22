# -*- coding: utf-8 -*-
"""一次性迁移：技术说明文档里"AI 估算一律只作参考"的旧口径 → 现行口径。

为什么必须改：文档 4 处断言「AI 估算的定额与工作面容量**不参与**计算与封顶」，
而第 37–42 轮的实际口径已经反过来了——
  · 工作面容量（全表 ai_estimate / LOW）**照常参与计算与封顶**，LOW 只表示置信度；
  · 默认定额**按来源分档**：parsed / verified 放行（交付物标注「未经人工审定」），
    只有 estimated 不参与工期。
文档说"不参与"、代码却在参与，用户按文档理解就会误判数字来源——这是要交付的文档，
不能留这种矛盾。顺带修掉同一段里过时的「16 张业务表」（现为 22 张）。

跑法（仓库根目录）：
    python devtools/update_doc_ai_claim.py

幂等：已经改过再跑会报"0 处"（正常）。
"""
import sys
from pathlib import Path

import docx

DOC = Path(__file__).resolve().parents[1] / "建策BuildPlan_技术说明文档_v3.0.docx"

# (旧文, 新文) —— 旧文必须与文档里的**原话**逐字一致（含标点与全角空格）
REPLACEMENTS = [
    # ① 正文：第 6 章口径段
    ("•  证据门：只有「有据可查」的定额与工作面容量才参与计算与封顶；"
     "标记为 AI 估算的定额与工作面容量一律只作参考。",
     "•  证据门（第 37–42 轮现行口径）：默认定额按来源分档——从资料解析出来的"
     "（confidence=parsed / verified）放行，并在交付物上标注「未经人工审定」；"
     "纯 AI 估的（estimated）不参与工期。工作面容量与默认定额一律参与计算与封顶，"
     "ai_estimate / LOW 只表示置信度（提醒你复核），不等于禁用。"),
    # ② 正文：知识库章节（同一段里还有过时的表数量）
    ("系统内置施工领域知识库（16 张业务表）",
     "系统内置施工领域知识库（22 张业务表）"),
    ("凡是标记为 AI 估算的定额只作参考、不参与工期计算——",
     "默认定额按来源分档：资料解析出来的（parsed / verified）照常参与并在交付物上"
     "标注「未经人工审定」，纯 AI 估的（estimated）不参与工期——"),
    # ③ 表 1（节点/口径表）里的证据门一格
    ("只有「有据可查」的定额与工作面容量才参与计算与封顶；AI 估算只作参考",
     "默认定额按来源分档（parsed / verified 放行并标注「未经人工审定」，"
     "estimated 不参与工期）；工作面容量（ai_estimate / LOW）照常参与计算与封顶，"
     "LOW 只表示置信度"),
    # ④ 表 4（26 节点表）里 norm_bind 一行的说明
    ("每条工序锚一条定额并记来源；AI 估算的定额只作参考、不参与计算",
     "每条工序锚一条定额并记来源；默认定额按来源分档：parsed / verified 放行并标注"
     "「未经人工审定」，estimated 不参与工期"),
    # ⑤ 表 12 单元格里的知识库规模标签（与正文那句各算一处）
    ("16 张业务表：字典/映射/定额/治理",
     "22 张业务表：字典/映射/定额/治理"),
]


def _replace_in_paragraph(p) -> int:
    """先逐 run 替换（保住字体/字号），run 被拆碎时退化为整段重写。"""
    hit = 0
    for old, new in REPLACEMENTS:
        if old not in p.text:
            continue
        for r in p.runs:
            if old in r.text:
                r.text = r.text.replace(old, new)
                hit += 1
        if old in p.text:                 # run 里没命中（被拆碎）→ 整段重写
            p.text = p.text.replace(old, new)
            hit += 1
    return hit


def _walk_tables(tables):
    for tb in tables:
        for row in tb.rows:
            for cell in row.cells:
                yield from (cell.paragraphs or [])
                yield from _walk_tables(cell.tables or [])


def main():
    d = docx.Document(str(DOC))
    hit = 0
    for p in d.paragraphs:
        hit += _replace_in_paragraph(p)
    for p in _walk_tables(d.tables):      # ⚠️ 有两处藏在表格单元格里
        hit += _replace_in_paragraph(p)
    if hit:
        d.save(str(DOC))
    print("段落改了 %d 处" % hit)
    return 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""一次性迁移：技术说明文档 §5.5 补上"粒度也作用于甘特图与监督报告"。

跑法（仓库根目录）：
    python devtools/update_doc_gantt_caliber.py

幂等：已经改过再跑会报"0 处"（正常）。
"""
import sys
from pathlib import Path

import docx

DOC = Path(__file__).resolve().parents[1] / "建策BuildPlan_技术说明文档_v3.0.docx"

OLD = "选中后，导出的 Word 与看板会真的按该粒度合并展示行，并在文首写明口径。"
NEW = ("选中后，导出的 Word 与看板会真的按该粒度合并展示行，并在文首写明口径；"
       "看板的甘特图与 WBS 表行数一致（组行的起止取组内最早开始 → 最晚完成，"
       "仍是真实日历日期），监督报告里也固定带一条「展示口径」要点，"
       "写明本报告按哪个粒度、共多少行。")

OLD2 = "这一句在报告里的写法"
NEW2 = OLD2  # 占位：无第二处


def main():
    d = docx.Document(str(DOC))
    hit = 0
    for p in d.paragraphs:
        if OLD in p.text:
            # 逐 run 替换：整段重写会丢掉字体/字号设置
            for r in p.runs:
                if OLD in r.text:
                    r.text = r.text.replace(OLD, NEW)
                    hit += 1
            if OLD in p.text:            # run 被拆碎时退化为整段重写
                p.text = p.text.replace(OLD, NEW)
                hit += 1
    if hit:
        d.save(str(DOC))
    print("段落改了 %d 处" % hit)
    return 0


if __name__ == "__main__":
    sys.exit(main())

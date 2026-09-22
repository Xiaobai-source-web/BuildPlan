# -*- coding: utf-8 -*-
"""把技术说明文档里"AI 估算只作参考 / 按来源分档"的旧口径改成 **2026-09-20 新政策**。

背景（用户 2026-09-20 亲自拍板）：
  · 旧口径：`L4_Norm_Default.confidence == "estimated"`、绑定来源 `AI_ESTIMATE_V1` /
    `match_type == "ai"` 的 AI 经验估算定额，以及 `source_type=ai_estimate` 的工作面容量，
    一律"只作参考、不进入计算与封顶"；默认定额"按来源分档"。
  · 新口径：**AI 经验估算定额允许参与计算**（照常决定工序工期与班组人数），但**必须逐条标注**
    交付物「依据 / 资源」列写「AI 经验估算定额（无规范依据，待审）」，并在「数据来源与置信度」
    章节给出条数（状态名 `released_ai`）。仍然拦住的只有四类：人工否决（review_state == "rejected"，
    唯一硬开关）/ 单位不可换算 / 定额口径与任务不符（method conflict）/ 定额值为空。
    工作面容量（ai_estimate / LOW）**早就已参与计算**（LOW 只表示置信度），本次变更针对的是定额。

跑法（仓库根目录）：
    python devtools/update_doc_ai_norm_policy.py            # 改前自动备份，再写盘
    python devtools/update_doc_ai_norm_policy.py --dry-run  # 只看命中数，不写盘

幂等：已经改过再跑会报"0 处"（正常）。
"""
import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

import docx

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "建策BuildPlan_技术说明文档_v3.0.docx"
BAK_SUFFIX = "ai_norm_policy"          # → *.bak_20260920_ai_norm_policy.docx

# 旧措辞扫描词：改前给出命中数，改后应为全 0
SCAN_KEYS = ["只作参考", "不参与计算", "不参与工期", "不参与封顶", "按来源分档",
             "证据门", "AI 估算", "estimated 不参与"]

# (旧文, 新文) —— 旧文必须与文档里的**原话**逐字一致（含标点与全角空格）
REPLACEMENTS = [
    # ① 正文 · 第 6 章口径段（整段替换；必须排在"证据门"通用替换之前）
    ("•  证据门（第 37–42 轮现行口径）：默认定额按来源分档——从资料解析出来的"
     "（confidence=parsed / verified）放行，并在交付物上标注「未经人工审定」；"
     "纯 AI 估的（estimated）不参与工期。工作面容量与默认定额一律参与计算与封顶，"
     "ai_estimate / LOW 只表示置信度（提醒你复核），不等于禁用。"
     "缺证据时沿用原值并写明中文来源，比编一个更像样的数字更诚实。",
     "•  定额准入口径（2026-09-20 变更，取代旧表述）：AI 经验估算定额"
     "（L4_Norm_Default.confidence == \"estimated\"，以及绑定来源为 AI_ESTIMATE_V1 / "
     "match_type == \"ai\" 的定额）允许参与计算，可以像从规范解析出来的定额一样决定"
     "工序工期与班组人数；但必须逐条标注——交付物（Word 与看板）在逐条工序的"
     "「依据 / 资源」列写明「AI 经验估算定额（无规范依据，待审）」，并在"
     "「数据来源与置信度」章节给出条数（状态名 released_ai）。仍然拦住的只有四类："
     "① 人工否决（review_state == \"rejected\"，唯一的硬开关）；② 单位不可换算"
     "（如任务 m² vs 定额分母 m³ 且缺厚度参数）；③ 定额口径与任务不符（method conflict）；"
     "④ 定额值为空——这四类任务仍然没有班组、工期沿用 WBS 估算，交付物仍如实写"
     "「工期沿用 WBS 估算，未计算班组」。工作面容量的 AI 标定（Workface_Capacity_Rule，"
     "ai_estimate / LOW）早就已参与计算（LOW 只表示置信度，不等于禁用），本次变更针对的是定额。"
     "导入真实规范后应逐条清退这些 AI 经验估算定额。"),
    # ② 正文 · 知识库章节
    ("默认定额按来源分档：资料解析出来的（parsed / verified）照常参与并在交付物上标注"
     "「未经人工审定」，纯 AI 估的（estimated）不参与工期——缺证据时沿用原值并写明中文来源，"
     "比编一个更像样的数字更诚实。",
     "AI 经验估算定额（confidence==\"estimated\" / 来源 AI_ESTIMATE_V1 / match_type==\"ai\"）"
     "自 2026-09-20 起照常参与算工期与班组人数，但交付物逐条在「依据 / 资源」列标注"
     "「AI 经验估算定额（无规范依据，待审）」，并在「数据来源与置信度」章节给出条数"
     "（状态名 released_ai）；只有人工否决 / 单位不可换算 / 定额口径与任务不符 / 定额值为空"
     "这四类仍被拦住，其工期沿用 WBS 估算并如实写明。"),
    # ③ 正文 · 定额绑定一节
    ("•  定额绑定记录来源编号、匹配方式与证据等级（知识库 / 用户提供 / AI 估算）。",
     "•  定额绑定记录来源编号、匹配方式与证据等级（知识库 / 用户提供 / AI 经验估算）；"
     "AI 经验估算定额照常参与算工期与班组人数，但交付物逐条标注"
     "「AI 经验估算定额（无规范依据，待审）」，并计入「数据来源与置信度」章节的 "
     "released_ai 条数。"),
    # ④ 表 1（节点/口径表）里的行标签（与 ⑥ 同一格内的说明文字分开处理）
    ("证据门", "定额准入"),
    # ⑤ 表 1（节点/口径表）里的一格
    ("默认定额按来源分档（parsed / verified 放行并标注「未经人工审定」，estimated 不参与工期）；"
     "工作面容量（ai_estimate / LOW）照常参与计算与封顶，LOW 只表示置信度",
     "AI 经验估算定额（estimated / AI_ESTIMATE_V1 / match_type=ai）照常参与算工期与班组人数，"
     "交付物逐条标注「AI 经验估算定额（无规范依据，待审）」并在置信度章节计 released_ai 条数；"
     "仍拦四类：人工否决 / 单位不可换算 / 定额口径与任务不符 / 定额值为空。"
     "工作面容量（ai_estimate / LOW）照常参与计算与封顶，LOW 只表示置信度"),
    # ⑥ 表 4（26 节点表）里 norm_bind 一行的说明
    ("每条工序锚一条定额并记来源；默认定额按来源分档：parsed / verified 放行并标注"
     "「未经人工审定」，estimated 不参与工期",
     "每条工序锚一条定额并记来源；AI 经验估算定额（estimated / AI_ESTIMATE_V1 / match_type=ai）"
     "照常参与算工期与班组人数并逐条标注「AI 经验估算定额（无规范依据，待审）」"
     "（置信度章节计 released_ai）；仍拦四类：人工否决 / 单位不可换算 / "
     "定额口径与任务不符 / 定额值为空"),
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


def _all_paragraphs(d):
    yield from d.paragraphs
    yield from _walk_tables(d.tables)


def _census(d):
    cnt = {k: 0 for k in SCAN_KEYS}
    for p in _all_paragraphs(d):
        for k in SCAN_KEYS:
            if k in p.text:
                cnt[k] += 1
    return cnt


def _report(title, cnt):
    print("%s：%s" % (title, "；".join("%s=%d" % (k, v) for k, v in cnt.items())))
    return sum(cnt.values())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not DOC.exists():
        raise SystemExit("找不到文档：%s" % DOC)

    d = docx.Document(str(DOC))
    before_total = _report("改前扫描", _census(d))

    hit = 0
    for p in _all_paragraphs(d):
        hit += _replace_in_paragraph(p)
    print("替换命中 %d 处" % hit)

    after_cnt = _census(d)
    after_total = _report("改后扫描", after_cnt)

    if args.dry_run:
        print("[dry-run] 未写盘。")
        return 0

    if not hit:
        print("没有可改的内容（幂等：已是最新口径），未写盘。")
        return 0

    bak = DOC.with_name("%s.bak_%s_%s.docx"
                        % (DOC.stem, datetime.now().strftime("%Y%m%d"), BAK_SUFFIX))
    shutil.copy2(DOC, bak)
    print("已备份 -> %s" % bak.name)

    d.save(str(DOC))
    print("已写回 -> %s（%.0f KB）" % (DOC.name, DOC.stat().st_size / 1024))

    # 自检：重新打开再扫一遍
    d2 = docx.Document(str(DOC))
    print("自检：段落 %d、表格 %d" % (len(d2.paragraphs), len(d2.tables)))
    total2 = _report("自检扫描", _census(d2))
    if total2:
        print("！仍有旧措辞命中 %d 处，请检查" % total2)
        return 1
    if before_total == 0:
        print("说明：改前就是 0 处（本次实际未改动文字）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""探针：第 6 步"补全边界条件"为什么这么慢、为什么不补参数了。

跑法（backend/ 下）：python -X utf8 _probe_boundary.py
用**真模型**跑一次 BoundaryNode，把耗时、原始返回、异常都打出来（不许静默）。
"""
import json
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pipeline import config, usage
from pipeline.llm import LLMClient, LLMError
from pipeline.nodes import boundary as B
from pipeline.prompts_loader import load

print("端点:", config.describe_provider())

params = {
    "project_name": "某住宅小区二期5号楼",
    "total_area": 14200,
    "building_count": 1,
    "floor_count": 18,
    "building_type": "住宅",
    "structure_type": "剪力墙结构",
    "planned_start_date": "2026-06-01",
}
node = B.BoundaryNode()
node.llm = LLMClient()
user = ("已提取的项目参数：\n%s\n\n用户补充/修正：\n混凝土参数改为6500立方米\n\n"
        "请结合上面的参数、用户补充与施工领域常识，补全边界条件与缺失参数；以用户明确给出的数值为准。"
        % node._dump(params))
ctx = {"prompt": "某住宅小区二期5号楼 18 层 14200 平", "extracted_params": dict(params),
       "_manual_param_input": "混凝土参数改为6500立方米"}

print("\n== 直接调一次 chat_json（复现第 6 步）==")
t = time.time()
raw = None
try:
    raw = node.llm.chat_json(load("boundary_conditions.txt"), B.combine(ctx, user))
    print("  耗时 %.2fs  OK" % (time.time() - t))
except LLMError as e:
    print("  耗时 %.2fs  LLMError: %s" % (time.time() - t, str(e)[:400]))
except Exception as e:
    print("  耗时 %.2fs  %s: %s" % (time.time() - t, type(e).__name__, str(e)[:400]))
    traceback.print_exc()

if raw:
    got = {k: raw.get(k) for k in ("total_concrete", "total_rebar", "total_earthwork",
                                   "total_wall", "total_precast", "quality_target")}
    print("  关键字段:", json.dumps(got, ensure_ascii=False))
    print("  boundary_conditions 键数:", len(raw.get("boundary_conditions") or {}))

print("\n== 走整个节点（看它是否静默降级）==")
node2 = B.BoundaryNode()
node2.llm = LLMClient()
t = time.time()
out = node2.run(dict(ctx))
dt = time.time() - t
print("  节点耗时 %.2fs" % dt)
print("  done_summary:", node2.done_summary)
print("  补全后参数:", json.dumps({k: ctx["extracted_params"].get(k) for k in
                                   ("total_concrete", "total_rebar", "total_earthwork")},
                                  ensure_ascii=False))
print("  boundary_conditions 键数:", len(out and {} or
                                        (ctx.get("boundary_conditions") or {})))
print("  usage:", json.dumps(usage.snapshot(), ensure_ascii=False)[:200])

# -*- coding: utf-8 -*-
"""探针：这个端点能不能关掉"思考"（reasoning）。

背景：第 6 步 97 秒里 2860/3247 token 是内部思考（reasoning_tokens），
正文只有 387 token。若能关掉思考，这一环节就能从 ~97s 降到 ~10-15s。

跑法（backend/ 下）：python -X utf8 _probe_reasoning.py
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pipeline.llm import LLMClient
from pipeline.prompts_loader import load

c = LLMClient()
sysp = load("boundary_conditions.txt")
user = ("已提取的项目参数：\n  total_area = 14200\n  floor_count = 18\n"
        "请补全边界条件与缺失参数。")

VARIANTS = [
    ("基线（现状）", {}),
    ("reasoning_effort=low", {"reasoning_effort": "low"}),
    ("reasoning_effort=none", {"reasoning_effort": "none"}),
    ("enable_thinking=false", {"enable_thinking": False}),
    ("thinking disabled", {"thinking": {"type": "disabled"}}),
    ("chat_template_kwargs", {"chat_template_kwargs": {"enable_thinking": False}}),
]

for label, extra in VARIANTS:
    payload = {"model": c.model,
               "messages": [{"role": "system", "content": sysp},
                            {"role": "user", "content": user}]}
    payload.update(extra)
    t = time.time()
    try:
        d = c._post(payload)
        msg = d["choices"][0]["message"]
        body = msg.get("content") or ""
        u = d.get("usage") or {}
        rt = (u.get("completion_tokens_details") or {}).get("reasoning_tokens")
        print("%-24s %7.2fs 正文 %4d 字 思考 %s" % (label, time.time() - t, len(body), rt))
    except Exception as e:
        print("%-24s 失败 %s" % (label, str(e)[:130]))

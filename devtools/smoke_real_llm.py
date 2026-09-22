# -*- coding: utf-8 -*-
"""真实 API Key 端到端冒烟 —— 走**真实 LLM 节点**（不打桩）。

为什么需要它：所有自动化测试都在 conftest 里强制清空 API Key，
所以"配了 Key 之后 LLM 节点能不能真跑通"从未被验证过。
本脚本用最小项目跑一次完整 26 节点流水线，自动应答所有人工门，
并报告：真实 token 用量与费用、是否发生了 LLM 回退、总工期与产物路径。

用法：
    python tools/smoke_real_llm.py
    python tools/smoke_real_llm.py --timeout 1800
"""
import argparse
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from pipeline import config                      # noqa: E402
from pipeline import usage as usage_mod          # noqa: E402
from pipeline.builder import build_pipeline      # noqa: E402
from pipeline.llm import LLMClient               # noqa: E402

TMP = ROOT / "_smoke_tmp"

PROMPT = ("1 栋 3 层框架结构办公楼，总建筑面积 1500 ㎡，1 层地下室，"
          "混凝土约 1200 m³，钢筋约 90 吨，开工 2026-03-01")
# 注：必须带上「栋数 / 层数 / 总建筑面积」这三项硬必要参数 —— 否则会被
# 参数复核门的必要参数校验拦下（v2.4 起"按通过"不再放行，见 param_review.py）。


def isolate():
    """只隔离产物目录；**绝不碰 API Key**（这正是本冒烟的目的）。"""
    out = TMP / "outputs"
    plans = TMP / "plans"
    out.mkdir(parents=True, exist_ok=True)
    plans.mkdir(exist_ok=True)
    config.DELIVERABLES_DIR = out
    config.PLANS_DIR = plans


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=int, default=1500)
    args = ap.parse_args()

    isolate()
    print("=" * 74)
    print("真实 Key 冒烟")
    print("  Key 是否就位       : %s" % ("是（%d 字符）" % len(config.LLM_API_KEY)
                                       if config.LLM_API_KEY else "否 —— 会全部走兜底！"))
    print("  base_url           : %s" % config.LLM_BASE_URL)
    print("  model              : %s" % config.LLM_MODEL)
    print("  timeout            : %s s" % config.LLM_TIMEOUT)
    print("  项目描述           : %s" % PROMPT)
    print("=" * 74)
    if not config.LLM_API_KEY:
        print("⚠️ 没有 Key，本冒烟失去意义。请先建 backend/.env。")
        return

    # 先单独探一次，确认 Key/网络/模型名都对（失败就早停，别烧整个流水线）
    print("\n[0] 单次连通性探测…")
    try:
        t0 = time.time()
        txt = LLMClient().chat_text("你是测试助手。", "只回复两个字：可用", retries=0)
        print("    ✅ 连通：%r（%.1fs）" % (txt.strip()[:40], time.time() - t0))
    except Exception as exc:
        print("    ❌ 连通失败：%s" % str(exc)[:200])
        print("    结论：Key/网络/模型名有问题，先修这个，不要继续烧额度。")
        return

    print("\n[1] 跑完整 26 节点流水线（自动应答人工门）…")
    pipeline = build_pipeline(run_id="smoke", llm=LLMClient())
    events = []
    resolved = set()

    def emit(e, d):
        events.append((e, d))

    ctx = {"prompt": PROMPT, "_run_id": "smoke"}
    t = threading.Thread(target=lambda: pipeline.run(ctx, emit=emit), daemon=True)
    t.start()

    deadline = time.time() + args.timeout
    last_report = 0.0
    gate_count = 0
    while time.time() < deadline:
        if not t.is_alive():
            break
        for e, d in list(events):
            key = d.get("pause_id") or d.get("confirm_id") or d.get("review_id")
            if not key or key in resolved:
                continue
            resolved.add(key)
            gate_count += 1
            print("    · 应答人工门 #%d（事件 %s，节点 %s）"
                  % (gate_count, e, d.get("node") or d.get("purpose") or ""))
            try:
                # 超集载荷：不同门读不同键，一次给全，避免误判为"驳回"
                pipeline.registry.resolve(key, {
                    "action": "continue", "decision": True, "passed": True,
                    "choice": "continue", "approved": True,
                })
            except Exception as exc:
                print("      （应答失败，忽略：%s）" % str(exc)[:80])
        if time.time() - last_report > 20:
            last_report = time.time()
            print("    …已过 %.0fs，事件 %d 条，人工门已应答 %d 次"
                  % (time.time() - (deadline - args.timeout), len(events), gate_count))
        time.sleep(0.05)

    alive = t.is_alive()
    t.join(timeout=10)
    finished = not alive

    print("\n" + "=" * 74)
    print("结果")
    print("  流水线跑完         : %s" % ("是" if finished else "否（超时，可能仍在等门）"))
    print("  事件总数           : %d" % len(events))
    print("  人工门应答次数     : %d" % gate_count)

    snap = {}
    try:
        snap = usage_mod.meter().snapshot() or {}
    except Exception as exc:
        print("  用量统计读取失败   : %s" % str(exc)[:80])
    if snap:
        print("  token 用量         : %s" % snap)
    else:
        print("  token 用量         : （空 —— 说明**一次真实 LLM 都没调用**）")

    plan = ctx.get("plan_json") or {}
    ov = plan.get("overview") or {}
    if ov:
        leaves = [l for ph in (plan.get("wbs") or {}).get("phases", [])
                  for wp in ph.get("work_packages", [])
                  for l in (wp.get("sub_packages") or [])]
        print("  叶子任务数         : %d" % len(leaves))
        print("  总工期             : %s 天" % ov.get("total_duration_days"))
        print("  交付物             : %s" % (plan.get("_paths") or "见 ctx"))
    else:
        print("  未产出 plan_json   : ctx 键 = %s" % sorted(ctx.keys())[:18])

    warn = (ctx.get("wbs_warnings") or [])[:6]
    if warn:
        print("  兜底/告警（前 6）  :")
        for w in warn:
            print("     - %s" % str(w)[:110])

    print("\n  产物目录           : %s" % (TMP / "outputs"))
    print("=" * 74)


if __name__ == "__main__":
    main()

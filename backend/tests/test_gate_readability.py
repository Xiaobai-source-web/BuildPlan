# -*- coding: utf-8 -*-
"""终端**可读性**回归：门的返回必须是"竖版"（一条一行），不是"一坨"。

用户反馈原文：
  · 「这一块看不懂啊有点，应该用中文解释清楚一点」（文件加载门）
  · 「你这个中间的返回，能不能做成竖版，一坨丢过来可读性太差了」（暂停门的摘要）

根因（实测确认）：
  · 文件门把 `files: []` / `unreadable: []` 这类**原始字段名**直接甩给用户，
    再配一句"你刚才输入的是否为全部项目数据？"的反问 → 用户不知道在问什么；
  · `wbs_agent._human_gate` 把多条问题各自**截断到 40 字**、再用 ` · ` 拼成**一整行**
    → 终端里糊成一片。

运行：python -m pytest backend/tests/test_gate_readability.py -q
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
# 本项目惯例：测试模块自己把 backend / terminal 加进 sys.path
# （conftest 的 autouse fixture 里 `from pipeline import config` 依赖它）
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(ROOT / "terminal"))

import renderer  # noqa: E402

_ANSI = re.compile(r"\033\[[0-9;]*m")


def _plain(text):
    return _ANSI.sub("", text)


# ---------------- 1. 文件加载门：人话 + 编号选项 ----------------
def test_文件门要讲清为什么问并给编号选项():
    out = _plain(renderer.render_event("param_review", {
        "purpose": "doc", "message": "输入里没有文件路径，确认数据是否齐全",
        "params": {"files": [], "unreadable": [], "reason": "no_path"}}))
    assert "项目文件加载门" in out, out
    assert "没有出现本地文件路径" in out, "要说清为什么问：\n%s" % out
    assert "①" in out and "②" in out, "要给编号选项：\n%s" % out
    # 不能把原始字段名甩给用户
    assert "files:" not in out and "unreadable:" not in out, out
    assert "hint:" not in out, out


def test_文件门读不到时必须回显路径与原因():
    ghost = r"D:\项目资料\施组.xlsx"
    out = _plain(renderer.render_event("param_review", {
        "purpose": "doc", "message": "给了文件路径，但读不到内容",
        "params": {"files": [ghost], "unreadable": [ghost], "reason": "unreadable"}}))
    assert ghost in out, out
    assert "读不到" in out, out
    assert ".xlsx" in out, "要说明哪些格式读不了：\n%s" % out


# ---------------- 2. 暂停门：一条一行 ----------------
def test_结构化问题清单必须一条一行且不截断():
    long_finding = ("脚本自检显示 conc_m3=2112.0 m³，远超项目参数 "
                    "total_concrete=1200，量级可能翻倍，请核对混凝土总量口径")
    out = _plain(renderer.render_event("node_paused", {
        "node": "wbs_agent", "output_summary": "评审发现 2 条高优先级问题，请决定",
        "issues": [{"severity": "HIGH", "dimension": "工程量", "finding": long_finding},
                   {"severity": "MID", "dimension": "结构", "finding": "装修阶段缺少收口工序"}]}))
    lines = out.splitlines()
    l_high = [i for i, l in enumerate(lines) if "[HIGH]" in l]
    l_mid = [i for i, l in enumerate(lines) if "[MID]" in l]
    assert len(l_high) == 1 and len(l_mid) == 1, out
    assert l_high[0] != l_mid[0], "两条问题不能在同一行：\n%s" % out
    # 完整描述必须都在（老实现截断到 40 字）
    joined = "".join(l.strip() for l in lines)
    assert "请核对混凝土总量口径" in joined, "描述被截断了：\n%s" % out
    assert "装修阶段缺少收口工序" in joined, out
    # 每条都有编号
    assert any(l.strip().startswith("1.") for l in lines), out
    assert any(l.strip().startswith("2.") for l in lines), out


def test_老式一坨摘要也要被拆成竖版():
    """兼容路径：仍拿 ` · ` 拼的摘要（其它节点/日志）不能原样糊出去。"""
    out = _plain(renderer.render_event("node_paused", {
        "node": "x", "output_summary": "3 条问题",
        "context_summary": "[HIGH]工程量: 混凝土量远超参数 · [HIGH]层数: 未按标准层展开"
                           " · [MID]结构: 缺收口工序"}))
    for l in out.splitlines():
        assert l.count("[HIGH]") + l.count("[MID]") <= 1, "同一行有多条：%r" % l
    assert "[MID]" in out and "[HIGH]" in out, out


def test_确认门长摘要同样竖版():
    out = _plain(renderer.render_event("confirm_required", {
        "message": "是否交付？",
        "context": {"summary": "[HIGH]工程量: 混凝土量远超参数 · [HIGH]层数: 未按标准层展开"}}))
    for l in out.splitlines():
        assert l.count("[HIGH]") <= 1, "同一行有多条：%r" % l


# ---------------- 3. 折行工具 ----------------
def test_折行按显示宽度算中文():
    assert renderer._disp_width("中文abc") == 7          # 2+2+1+1+1
    lines = renderer.wrap_cjk("中" * 100, width=20)
    assert len(lines) >= 10, lines
    assert all(renderer._disp_width(l) <= 21 for l in lines), lines
    # 优先在标点后断
    lines = renderer.wrap_cjk("第一句话，第二句话，第三句话，第四句话。", width=14)
    assert lines[0].endswith("，") or lines[0].endswith("。"), lines


def test_空摘要不报错():
    for text in ("", None, "   "):
        assert renderer.verticalize(text) == []
    assert _plain(renderer.render_event("node_paused", {"node": "x"}))

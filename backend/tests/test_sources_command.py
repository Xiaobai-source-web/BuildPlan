# -*- coding: utf-8 -*-
"""逐值溯源命令 /sources 测试

覆盖三种用法：无参汇总、按 id 精确查询、按关键词模糊匹配，
以及边界情况：空计划、未知 id。

运行：python -m pytest backend/tests/test_sources_command.py -q
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
if str(ROOT / "terminal") not in sys.path:
    sys.path.insert(0, str(ROOT / "terminal"))

import commands  # noqa: E402

_ANSI = __import__("re").compile(r"\x1b\[[0-9;]*m")


def _plain(text):
    return _ANSI.sub("", str(text))


class _Ctx(object):
    def __init__(self, plan=None):
        self.current_plan = plan
        self.current_plan_id = None
        self.history = []
        self.backend = "local"
        self.running = False
        self.run_id = "t"
        self.show_html = None
        self.client = None


# ────────────────── 构造一个有真实结构的假计划 ──────────────────

def _make_plan():
    return {
        "plan_id": "test-plan-001",
        "meta": {
            "credibility": {"user": 0.1, "kb": 0.5, "ai": 0.4},
            "norm_coverage": {
                "total": 12,
                "bound": 8,
                "bound_pct": 66.7,
                "unbound": 4,
                "unbound_pct": 33.3,
                "by_reason": {"缺少定额系数": 2, "工序未标准化": 2},
                "by_reason_pct": {},
                "top_activities": [],
            },
            "machine_labor_demand": {},
        },
        "wbs": {
            "phases": [
                {
                    "work_packages": [
                        {
                            "sub_packages": [
                                {
                                    "id": "5.1.1.1",
                                    "name": "Ⅰ区 1-1层 钢筋绑扎",
                                    "quantity": 1179,
                                    "unit": "m²",
                                    "duration_days": 12,
                                    "work_type": "labor",
                                    "_qty_source": "参数推算",
                                    "_qty_formula": "单栋标准层471.5㎡×2.5（模板接触面积系数）÷1区 = 1179 m²/层",
                                    "norm_binding": {
                                        "mode": "labor",
                                        "norm_value": 4.2,
                                        "productivity_value": 0.238,
                                        "unit": "工日/m²",
                                        "source_code": "LD_T72_7_2008",
                                        "match_type": "exact",
                                        "condition_text": "现浇混凝土框架结构",
                                        "crew": {"钢筋工": 8, "普工": 2},
                                        "labor_types": ["钢筋工", "普工"],
                                        "crew_source": "数据库默认",
                                        "provenance": {
                                            "value": 4.2,
                                            "origin": "kb",
                                            "ref": "LD_T72_7_2008",
                                            "confidence": 0.92,
                                            "note": "2008 定额",
                                        },
                                    },
                                },
                                {
                                    "id": "5.1.1.2",
                                    "name": "Ⅰ区 1-1层 模板安装",
                                    "quantity": 950,
                                    "unit": "m²",
                                    "duration_days": 8,
                                    "work_type": "labor",
                                    "_qty_source": "基线默认",
                                    "_qty_formula": None,
                                    "norm_binding": {
                                        "mode": "labor",
                                        "norm_value": None,
                                        "productivity_value": 0.35,
                                        "unit": "m²",
                                        "source_code": "LD_T73_7_2008",
                                        "match_type": "ai",
                                        "condition_text": "综合取值",
                                        "crew": {},
                                        "crew_source": None,
                                        "provenance": {
                                            "value": None,
                                            "origin": "ai",
                                            "ref": None,
                                            "confidence": 0.6,
                                            "note": "无精确对应定额，AI 估算",
                                        },
                                    },
                                },
                            ]
                        }
                    ]
                }
            ]
        },
    }


# ────────────────── 无参汇总 ──────────────────

def test_sources_no_arg_shows_summary():
    out = _plain(commands.dispatch(_Ctx(plan=_make_plan()), "/sources"))
    # 可信度
    assert "用户提供" in out
    assert "数据库" in out
    assert "AI 假设" in out
    assert "10.0%" in out   # user: 0.1/(0.1+0.5+0.4)=10%
    assert "50.0%" in out   # kb
    assert "40.0%" in out   # ai
    # 定额覆盖率
    assert "定额覆盖率" in out
    assert "12" in out       # total
    assert "8" in out        # bound
    assert "66.7%" in out    # bound_pct
    # 一句话
    assert "叶子任务" in out
    assert "有数据库依据" in out
    assert "/sources <任务id>" in out


def test_sources_no_arg_empty_plan():
    out = _plain(commands.dispatch(_Ctx(plan=None), "/sources"))
    assert "还没有已生成的计划" in out


def test_sources_no_arg_no_meta():
    """计划存在但 meta 为空也不崩。"""
    plan = {"plan_id": "p1", "wbs": {"phases": []}}
    out = _plain(commands.dispatch(_Ctx(plan=plan), "/sources"))
    assert "叶子任务" in out
    assert "可信度数据" in out


# ────────────────── 按 id 精确查询 ──────────────────

def test_sources_by_id_shows_full_detail():
    out = _plain(commands.dispatch(_Ctx(plan=_make_plan()), "/sources 5.1.1.1"))
    # 标题
    assert "5.1.1.1" in out
    assert "Ⅰ区 1-1层 钢筋绑扎" in out
    # 工程量
    assert "1179" in out and "m²" in out
    assert "参数推算" in out
    assert "471.5㎡×2.5" in out   # 算式
    # 定额
    assert "LD_T72_7_2008" in out
    assert "精确匹配" in out
    assert "现浇混凝土框架结构" in out
    # 班组
    assert "钢筋工×8" in out and "普工×2" in out
    assert "数据库默认" in out
    # 来源与置信度
    assert "数据库" in out
    assert "0.92" in out
    assert "2008 定额" in out
    # 工期
    assert "12" in out and "labor" in out


def test_sources_by_id_ai_task():
    """AI 假设定额的任务，来源标注为 AI。"""
    out = _plain(commands.dispatch(_Ctx(plan=_make_plan()), "/sources 5.1.1.2"))
    assert "AI 假设" in out
    assert "无精确对应定额" in out
    assert "LD_T73_7_2008" in out


# ────────────────── 按关键词模糊匹配 ──────────────────

def test_sources_by_keyword_matches_multiple():
    """"Ⅰ区"匹配两个任务（钢筋绑扎 + 模板安装），走列表分支。"""
    out = _plain(commands.dispatch(_Ctx(plan=_make_plan()), "/sources Ⅰ区"))
    assert "找到" in out and "条匹配" in out
    assert "5.1.1.1" in out
    assert "5.1.1.2" in out
    assert "想看某一条" in out


def test_sources_by_keyword_single_match():
    """只有一个匹配时直接展示详情（等同按 id）。"""
    out = _plain(commands.dispatch(_Ctx(plan=_make_plan()), "/sources 模板安装"))
    assert "5.1.1.2" in out
    assert "模板安装" in out


# ────────────────── 找不到 ──────────────────

def test_sources_unknown_id_gives_hint():
    out = _plain(commands.dispatch(_Ctx(plan=_make_plan()), "/sources 9.9.9.9"))
    assert "清单里没有" in out
    assert "9.9.9.9" in out
    assert "/sources" in out


def test_sources_unknown_keyword_gives_hint():
    out = _plain(commands.dispatch(_Ctx(plan=_make_plan()), "/sources 粉刷"))
    assert "清单里没有" in out
    assert "粉刷" in out

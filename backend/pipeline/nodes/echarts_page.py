# -*- coding: utf-8 -*-
"""ECharts 交互看板（甘特 / 人员曲线 / 设备峰值）。

设计要点 —— **纯 JSON option + glue 补函数**
============================================

ECharts 的 option 里有三类东西无法用 JSON 表达：

* ``xAxis.axisLabel.formatter`` / ``tooltip.formatter`` —— 需要函数；
* ``series[0].renderItem`` —— custom series 的画法需要函数；
* 各类事件回调。

本项目的看板 HTML 是确定性拼出来的字符串（见 ``delivery.build_plan_html``），
不能也不需要跑一个 JS 构建步骤。因此这里把职责切成两半：

1. :func:`build_chart_options` **只产出纯数据 + 结构**的 option ——
   全部是 dict / list / str / int / float，保证
   ``json.dumps(opt, ensure_ascii=False)`` 一定能序列化，**不含函数、不含 set**。
   凡是"函数才知道的信息"，都改成**标记键**（``__`` 前缀）随数据一起带出去：

   ====================  ==================================================
   标记键                含义
   ====================  ==================================================
   ``__dateTicks``       ``[[百分比, "M-D"], ...]``，已算好的日历刻度，
                         供 glue 写 ``axisLabel.formatter`` 用
   ``__startDateISO``    项目开工日（``overview.planned_start_date``）
   ``__endDateISO``      项目竣工日（``overview.planned_end_date``）
   ``__unit``            数值单位（"人" / "台数"）
   ``__projectName``     项目名，供 tooltip 标题用
   ``__ganttFilters``    阶段筛选规则 ``[{key,label,segments:[int],crit_only}]``
   ``__criticalCount``   关键路径任务数（标题里显示）
   ``__empty``           该图无数据（glue 画"暂无数据"占位）
   ====================  ==================================================

2. :func:`chart_cards_html` 产出三张卡片 + **一段 glue ``<script>``**，
   由这段固定 JS 读取内联的 ``var DSH_OPT = {...};``，在 ``window.echarts``
   存在时把上面那些函数补齐（``formatter`` / ``renderItem``）并 ``setOption``；
   ``window.echarts`` 不存在时直接 return，调用方自行回退到 ``delivery`` 的 SVG 看板。

   这样 option 仍然可以单独序列化、单独测试、单独喂给任何前端，
   而"函数"只在一个地方出现（glue），不会散落在 Python 字符串拼接里。

.. note::

   **这一条是本次实现的关键。** "纯 JSON option + 标记键 + 固定 glue 补函数"
   的约定一旦破坏（例如为了省事在 Python 里塞一个 ECharts 函数对象，或者让
   ``build_chart_options`` 返回的函数被 ``json.dumps`` 撞上），整个看板就会
   在序列化那一步直接炸掉，或者在浏览器里静默丢图。改这个模块时请守住：
   ``build_chart_options`` 的返回值永远可以 ``json.dumps``。

其他约定
--------

* :func:`echarts_bundle_html` 只返回 ``<script>…echarts 源码…</script>``，
  **不含** 三张卡片。bundle 由调用方决定放哪里（通常是 ``<head>`` 或
  body 末尾），便于和回退逻辑解耦。已核实 vendor 文件既不含 ``</script``
  也不含 ``<!--``，所以内联不需要额外转义；即便如此仍做了 ``</`` → ``<\\/``
  的兜底。
* 甘特图数据源 = **展示粒度生效后的行**：逐叶子时用
  ``plan["all_tasks_schedule"]``；用户在计划细度门选了合并粒度
  （``meta.display_granularity`` ≠ 工序级 × 按层）时，改走
  ``delivery.rolled_gantt_rows(plan)`` —— 与 Word 表 / 内联 SVG 同一口径
  （组内最早开始 → 最晚完成，真实日历日期，不重算工期）。
  历史做法是"甘特永远逐叶子"，于是同一张看板上 WBS 表写 164 行、甘特画 307 条，
  用户根本看不出选择有没有生效；这条口径统一由交付侧负责，这里只取数。
* 设备桶用 ``view["equip_daily"]``（``delivery._compute_view`` 已按 ``LABOR``
  集合把人工/机械分流）。另有一个并行任务负责把 ``泵工/辅助/操作工/司机``
  这类**机械配员**从机械桶移到人工桶 —— 本模块天然受益；但为避免两处修改
  的时序耦合，这里仍保留一道 ``_is_machine`` 兜底过滤（见该函数注释），
  过滤后为空时退回 ``plan["resource_plan"]["equipment_peak"]``。
"""

from __future__ import annotations

import datetime
import json
import re
from pathlib import Path

__all__ = [
    "ECHARTS_VERSION",
    "has_echarts",
    "echarts_source",
    "echarts_bundle_html",
    "build_chart_options",
    "chart_cards_html",
    "gantt_filter_buttons_html",
]

#: 内联的 ECharts 发行版本号（vendor 文件内容自述）。
ECHARTS_VERSION = "5.5.1"

#: vendor 路径：以本文件位置向上推 → backend/static/vendor/echarts.min.js
#: echarts_page.py 在 backend/pipeline/nodes/ 下，parents[2] == backend。
_VENDOR_PATH = (Path(__file__).resolve().parents[2]
                / "static" / "vendor" / "echarts.min.js")

#: 浅色主题色板（对齐 delivery._HTML_CSS：白卡片 / #eef1f6 底 / #1f2530 正文）
_BG = "transparent"
_C_BLUE = "#5470c6"      # 普通任务 / 主色
_C_RED = "#ee6666"       # 关键路径
_C_PURPLE = "#7a6bd4"    # 设备柱
_C_TEXT = "#44506a"      # 轴文字
_C_AXIS = "#e8edf5"      # 轴线 / 分割线
_C_DEEP = "#16325c"      # 深蓝（标题）
_C_MUTED = "#6b7688"     # 次要文字

#: 进程级 bundle 缓存：{路径字符串: 源码} —— 1MB 的文件只读一次。
_SOURCE_CACHE: dict = {}

#: glue 里 ``category`` 系列每项数据自带的日期区间（tooltip 用）。
_EXTRA_FIELD = "__extra"


# ============================================================
# vendor 装载
# ============================================================

def has_echarts() -> bool:
    """vendor 文件存在且非空。"""
    try:
        return _VENDOR_PATH.is_file() and _VENDOR_PATH.stat().st_size > 0
    except OSError:
        return False


def echarts_source() -> str:
    """读取 vendor 文件内容（进程级缓存）；缺失 / 读失败返回 ``""``。"""
    key = str(_VENDOR_PATH)
    if key in _SOURCE_CACHE:
        return _SOURCE_CACHE[key]
    text = ""
    if has_echarts():
        try:
            text = _VENDOR_PATH.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
    _SOURCE_CACHE[key] = text
    return text


def _js_safe(text: str) -> str:
    """内联进 ``<script>`` 的安全化：``</`` → ``<\\/``（挡住提前闭合）。"""
    return str(text or "").replace("</", "<\\/")


def echarts_bundle_html() -> str:
    """``"<script>…echarts 源码…</script>"``；无源码时返回 ``""``。"""
    src = echarts_source()
    if not src:
        return ""
    return "<script>" + _js_safe(src) + "</script>"


# ============================================================
# 通用小工具
# ============================================================

def _s(value) -> str:
    """任意值 → 去掉首尾空白的字符串（None → ""）。"""
    if value is None:
        return ""
    return str(value).strip()


def _i(value, default=0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _f(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_date(value, default=None):
    """``"YYYY-MM-DD"`` → ``datetime.date``；失败退 ``default``。"""
    if isinstance(value, datetime.date):
        return value
    try:
        return datetime.date.fromisoformat(_s(value)[:10])
    except (ValueError, TypeError):
        return default


def _md(d: datetime.date) -> str:
    """``date`` → ``"M-D"``（对齐参考实现的 ``fmt``）。"""
    return "%d-%d" % (d.month, d.day)


def _segment(task_id) -> int:
    """``"4.1.1.1"`` → ``4``；非数字段返回 ``0``。"""
    head = _s(task_id).split(".", 1)[0]
    return int(head) if head.isdigit() else 0


def _sorted_names(names) -> list:
    """中文名排序：按字符串稳定排，保证同一份 plan 每次输出一致。"""
    return sorted({_s(n) for n in names if _s(n)})


# ============================================================
# plan / view 归一化
# ============================================================

def _overview(plan) -> dict:
    ov = (plan or {}).get("overview")
    return ov if isinstance(ov, dict) else {}


def _all_tasks(plan) -> list:
    tasks = (plan or {}).get("all_tasks_schedule")
    return [t for t in tasks if isinstance(t, dict)] if isinstance(tasks, list) else []


def _rolled_gantt_rows(plan) -> list:
    """展示粒度上卷后的甘特行（真实日历）；未上卷或口径取不到时返回 []。

    口径只有一处真源（``delivery.rolled_gantt_rows`` → ``quantity.group_rows``）：
    本模块不重复实现"怎么分组、组名怎么写"，否则看板与 Word 会再次分叉。
    ``delivery`` 在模块顶部 import 本模块，所以这里**只能按需反向引用**（循环导入）。
    """
    try:
        from . import delivery
        return delivery.rolled_gantt_rows(plan) or []
    except Exception:                    # 交付模块缺失/口径异常 → 退回逐叶子（旧行为）
        return []


def _critical_ids(plan) -> set:
    """关键路径任务 id 集合。

    ``critical_path_tasks`` 在本项目里是**对象数组**（``[{task_id,...}]``），
    但参考实现 ``getCriticalSet`` 兼容字符串数组，这里同样两种都吃。
    另外把 ``cpm_result.critical_path``（``delivery._critical_ids`` 的口径）
    并进来 —— 叶子的 id 两种来源未必完全一致，取并集不会漏判关键路径。
    """
    out = set()
    for item in (plan or {}).get("critical_path_tasks") or []:
        if isinstance(item, dict):
            tid = _s(item.get("task_id") or item.get("id"))
        else:
            tid = _s(item)
        if tid:
            out.add(tid)
    for tid in ((plan or {}).get("cpm_result") or {}).get("critical_path") or []:
        tid = _s(tid)
        if tid:
            out.add(tid)
    return out


def _phase_segments(plan) -> list:
    """读 ``plan["wbs"]["phases"]``，返回 ``[(段号, 相位名), ...]``（按出现顺序去重）。

    段号取自 ``work_packages[*].id`` 的首段（``"7.1"`` → ``7``）；
    工作包 id 缺失时退化为"列表下标 + 1"。**先读真实映射再判阶段**，
    绝不照抄参考实现的 ``/^[23]\\./`` 硬编码 —— 本项目 WBS 是 1..10 段。
    """
    phases = ((plan or {}).get("wbs") or {}).get("phases")
    if not isinstance(phases, list):
        return []
    seen, out = set(), []
    for idx, ph in enumerate(phases):
        if not isinstance(ph, dict):
            continue
        name = _s(ph.get("phase") or ph.get("name"))
        segs = []
        for wp in ph.get("work_packages") or []:
            if isinstance(wp, dict):
                seg = _segment(wp.get("id"))
                if seg:
                    segs.append(seg)
        seg = min(segs) if segs else (idx + 1)
        if seg in seen:
            continue
        seen.add(seg)
        out.append((seg, name))
    return out


# 阶段关键词：按"命中优先级"排列。``closeout`` 放最前，因为
# "竣工验收" 里既有"验收"也可能带"工程"字样；``mep`` 在 ``finish`` 之前，
# 因为"装饰装修"与"管线安装"都可能共现。判定只看相位名，段号只用来定位。
_STAGE_KEYWORDS = [
    ("closeout", ("竣工", "验收", "移交", "试运转", "调试", "收尾", "资料", "室外", "绿化",
                  "道路", "管网", "附属")),
    ("mep", ("机电", "安装", "管线", "管道", "电气", "给排水", "暖通", "通风", "消防",
             "设备", "预留预埋")),
    ("finish", ("装修", "装饰", "抹灰", "涂料", "保温", "门窗", "面层", "涂饰")),
    ("structure", ("主体", "结构", "混凝土", "钢筋", "模板", "砌体", "砌筑", "楼层",
                   "地上", "地下室")),
    ("foundation", ("桩", "地基", "基础", "基坑", "支护", "土方", "岩溶", "溶洞")),
    ("prep", ("准备", "临时", "测量", "放线", "手续", "场地平整")),
]


def _classify_phase(name, seg) -> str:
    """相位名 → 阶段桶；名字认不出来时按段号兜底（前 1 段=准备，末 1 段=收尾）。"""
    text = _s(name)
    for bucket, words in _STAGE_KEYWORDS:
        for word in words:
            if word in text:
                return bucket
    if seg <= 1:
        return "prep"
    if seg >= 10:
        return "closeout"
    return "other"


#: 五个按钮的 (key, 标签, 收纳哪些阶段桶)。
#: 参考实现是「全部 / 仅关键路径 / 结构阶段 / 机电与装修 / 收尾验收」，
#: 这里保持同样的**五按钮形态**，但桶由 :func:`_resolve_filters` 从真实
#: ``wbs.phases`` 动态解出，不做 ``/^[23]\\./`` 式的编号硬编码。
_FILTER_SPECS = [
    ("all", "全部任务", None),
    ("critical", "仅关键路径", "critical"),
    ("structure", "结构阶段", ("prep", "foundation", "structure")),
    ("mep", "机电与装修", ("mep", "finish", "other")),
    ("closeout", "收尾验收", ("closeout",)),
]


def _resolve_filters(plan) -> list:
    """相位段号 → 筛选规则（``__ganttFilters`` 的数据源）。

    返回 ``[{key,label,segments,crit_only}]``。``segments`` 为空表示该按钮
    **不可靠**（没有相位落到这个桶），调用方 :func:`gantt_filter_buttons_html`
    会把它隐藏 —— 这就满足了"若无法可靠判断就退化"的要求：真实映射在
    ``wbs.phases`` 里读得到就出五个（或更少）按钮，读不到就只有
    「全部任务 / 仅关键路径」两个，且按钮与 glue 看到的是**同一份规则**。
    """
    phases = _phase_segments(plan)
    buckets = {}
    for seg, name in phases:
        buckets.setdefault(_classify_phase(name, seg), []).append(seg)

    rules = []
    for key, label, spec in _FILTER_SPECS:
        segments = []
        crit_only = False
        if spec is None:
            pass                                  # 全部任务
        elif spec == "critical":
            crit_only = True
        else:
            for bucket in spec:
                segments.extend(buckets.get(bucket) or [])
        rules.append({
            "key": key,
            "label": label,
            "segments": sorted({int(s) for s in segments}),
            "crit_only": crit_only,
        })

    # 退化：读不到 wbs.phases 时只留前两个按钮（可点击的筛选必须有意义）
    if not phases:
        return [r for r in rules if r["key"] in ("all", "critical")]
    return rules


# ============================================================
# 1) 甘特 option
# ============================================================

def _gantt_source(plan, start, rolled=None) -> list:
    """横道数据源 → ``[(tid, name, start_date, finish_date, 天数, is_crit)]``。

    默认逐叶子（与历史一致：``all_tasks_schedule`` 的真实日历）；一旦展示粒度
    需要合并，就换成组行 —— 组行同样带真实日历，取组内最早开始 → 最晚完成。
    ``rolled`` 由调用方传入（同一份 plan 只算一次组行）。

    **天数 = 本条自己的日期跨度（含首尾）**，不是排程行上的 `duration_days`：
    工具栏的气泡就画在这根条上，写的必须是这根条的宽度。
    P0-B 之后这两者**已经一致**（`plan_assembler` 把 `duration_days` 改成排程跨度、
    WBS 目标另存 `wbs_target_days`；真计划 310/310 行 `(finish-start).days+1 ==
    duration_days`）。这里仍然坚持**由日期算**：图表的口径只能有一个来源，
    老计划（第 37 轮之前落盘的）里的 `duration_days` 装的是 WBS 目标天数，
    照抄就会画出"条宽 3 天、气泡写 31 天"的自相矛盾。
    """
    if rolled is None:
        rolled = _rolled_gantt_rows(plan)
    if rolled:
        out = []
        for r in rolled:
            sd = _as_date(r.get("start"), start) or start
            fd = _as_date(r.get("finish"), sd) or sd
            out.append((_s(r.get("id")), _s(r.get("name")), sd, fd,
                        # 含首尾：日期差 + 1（见 `_build_gantt` 里的闭区间口径说明）
                        _i(r.get("duration"), max((fd - sd).days + 1, 1)),
                        bool(r.get("crit"))))
        return out
    crit = _critical_ids(plan)
    out = []
    for t in _all_tasks(plan):
        sd = _as_date(t.get("start_date"), start) or start
        fd = _as_date(t.get("finish_date"), sd) or sd
        tid = _s(t.get("task_id") or t.get("id"))
        tname = _s(t.get("task_name") or t.get("name"))
        # 天数 = 本条自己的日期跨度（含首尾），与条形宽度同一口径（见函数 docstring）
        days = max((fd - sd).days + 1, 1)
        out.append((tid, tname, sd, fd, days, tid in crit))
    return out


def _build_gantt(plan, filters) -> dict:
    ov = _overview(plan)
    start = _as_date(ov.get("planned_start_date")) or _as_date(ov.get("start_date"))
    end = _as_date(ov.get("planned_end_date")) or _as_date(ov.get("end_date"))
    if start is None:                              # 极端兜底，保证分母 > 0
        start = datetime.date(2026, 1, 1)
    if end is None or end <= start:
        end = start + datetime.timedelta(days=1)
    # 闭区间口径（与 `delivery._compute_view` / `plan_assembler` 同源）：日期由
    # `plan_assembler` 按 `finish = 开工 + (ef - 1)` 生成、`planned_end_date =
    # 开工 + (total - 1)`，即**含首尾**。所以"天数"= `(end - start).days + 1`
    # （= `overview.total_duration_days`）；横轴百分比的分母必须用它，用
    # `(end - start).days` 会让图表比看板少报一天（687 vs 688），与新口径对不上。
    total_days = max((end - start).days + 1, 1)

    rolled = _rolled_gantt_rows(plan)
    rows = _gantt_source(plan, start, rolled)
    y_data, series_data = [], []
    for idx, (tid, tname, sd, fd, days, is_crit) in enumerate(rows):
        offset = (sd - start).days
        # 跨度含首尾：+1，与 `total_days` 同一口径（= ef - es）
        span = max((fd - sd).days + 1, 1)
        # y 轴标签必须带工序编号（用户硬需求）
        y_data.append(("%s  %s" % (tid, tname)).strip())
        series_data.append([
            idx,
            round(offset / total_days * 100.0, 4),
            round(max(span / total_days * 100.0, 0.4), 4),   # 至少 0.4% 保证看得见
            tname,
            tid,
            sd.isoformat(),
            fd.isoformat(),
            _i(days, span),
            is_crit,
        ])

    opt = {
        "__startDateISO": start.isoformat(),
        "__endDateISO": end.isoformat(),
        "__dateTicks": _date_ticks(start, end),
        "__projectName": _s(ov.get("project_name")),
        "__criticalCount": sum(1 for d in series_data if d[8]),
        "__ganttFilters": filters,
        "__totalTasks": len(series_data),
        "backgroundColor": _BG,
        "animation": False,                        # 307 行 custom series，关动画更快
        "grid": {"left": 340, "right": 40, "top": 30, "bottom": 56, "containLabel": False},
        "xAxis": {
            "type": "value", "min": 0, "max": 100,
            "axisLine": {"lineStyle": {"color": _C_AXIS}},
            "axisTick": {"lineStyle": {"color": _C_AXIS}},
            "splitLine": {"lineStyle": {"color": _C_AXIS}},
            "axisLabel": {"color": _C_TEXT, "fontSize": 11, "hideOverlap": True},
        },
        "yAxis": {
            "type": "category", "data": y_data, "inverse": True,
            "axisLine": {"lineStyle": {"color": _C_AXIS}},
            "axisTick": {"show": False},
            "splitLine": {"show": False},
            "axisLabel": {"width": 320, "overflow": "truncate", "fontSize": 12,
                          "color": _C_TEXT},
        },
        "dataZoom": [
            {"type": "inside", "xAxisIndex": 0, "start": 0, "end": 100},
            {"type": "slider", "xAxisIndex": 0, "start": 0, "end": 100,
             "bottom": 4, "height": 18},
        ],
        "series": [{
            "name": "任务",
            "type": "custom",
            "encode": {"x": [1, 2], "y": 0},
            "data": series_data,
        }],
    }
    # 仅在真的上卷时才加这两个标记键：未上卷路径必须与旧版**逐字节一致**。
    if rolled:
        opt["__rolledUp"] = True
        opt["__leafTasks"] = len(_all_tasks(plan))
    return opt


def _date_ticks(start: datetime.date, end: datetime.date, max_ticks=14) -> list:
    """``[[百分比, "M-D"], ...]`` —— 供 glue 写 ``axisLabel.formatter``。

    option 必须是纯 JSON，``formatter`` 只能是函数，所以这里把"百分比 → 日期"
    的换算**预先在 Python 侧算好**，glue 只管查表。
    刻度取：开工日 + 每 1 号 + 月末 + 竣工日（超过 ``max_ticks`` 时按等距抽稀，
    首尾必留）。
    """
    ticks = set()
    ticks.add(start)
    ticks.add(end)
    cur = start
    while cur < end:                      # 必须是 < end：用 <= 会先自增再判断，
        cur = cur + datetime.timedelta(days=1)   # 把 end 之后的那天也算进来
        if cur <= end and cur.day == 1:
            ticks.add(cur)
    # 月末
    cur = start
    while cur <= end:
        nxt = cur + datetime.timedelta(days=1)
        if nxt.month != cur.month:
            ticks.add(cur)
        cur = nxt
    ordered = sorted(ticks)
    if len(ordered) > max_ticks:
        step = (len(ordered) - 1) / float(max_ticks - 1)
        picked = {ordered[min(int(round(i * step)), len(ordered) - 1)]
                  for i in range(max_ticks)}
        picked.add(ordered[0])
        picked.add(ordered[-1])
        ordered = sorted(picked)
    # 与 `_build_gantt` 的分母同源：天数 = 日期差 + 1（含首尾），否则刻度会与
    # 横道条错位（刻度假定第 0 天占 1/687，条形假定占 1/688）。
    span = max((end - start).days + 1, 1)
    return [[round((d - start).days / span * 100.0, 4), _md(d)] for d in ordered]


# ============================================================
# 2) 人员曲线 option
# ============================================================

def _downsample_indices(n, target=400, pin=()):
    """把 ``n`` 个点降采样到约 ``target`` 个，**强制保留** ``pin`` 里的下标。

    人员曲线在粗粒度下是"物理量"，抽样只是为了可读性；峰值那一天必须留下，
    否则图上峰值会凭空塌下去 —— 例如 848 天抽成 401 天时若只钉全局峰值日，
    「司机」（峰值仅 2 人、只在两天出工）会被整条抽掉，峰值直接腰斩成 1。
    因此调用方把**每个工种的峰值日**都放进 ``pin``。
    """
    if n <= target:
        return list(range(n))
    idx = sorted({min(int(i * (n - 1) / float(target - 1)), n - 1)
                  for i in range(target)})
    extra = sorted({int(p) for p in pin if 0 <= int(p) < n})
    return sorted(set(idx) | set(extra) | {n - 1})


def _build_personnel(view) -> dict:
    daily = view.get("labor_daily") or []
    dates = [_md(_as_date(x.get("date")) or datetime.date(2000, 1, 1)) for x in daily]

    trades = _sorted_names(
        name for x in daily for name in (x.get("trades") or {}).keys())
    # 只保留至少有一天 > 0 的工种
    series = {}
    for name in trades:
        vals = [_i((x.get("trades") or {}).get(name)) for x in daily]
        if max(vals) > 0:
            series[name] = vals

    # 钉住"全局峰值日 + 每个工种各自的峰值日"：抽样只能减少横轴点数，
    # 不能改变任何一条曲线的峰值高度（峰值即物理量，改了就是改数据）。
    pins = set()
    if daily:
        pins.add(max(range(len(daily)), key=lambda i: _i(daily[i].get("total"))))
    for name in series:
        vals = series[name]
        pins.add(max(range(len(vals)), key=lambda i: vals[i]))
    keep = _downsample_indices(len(dates), target=400, pin=pins)
    dates = [dates[i] for i in keep]
    for name in list(series):
        series[name] = [series[name][i] for i in keep]

    return {
        "__unit": "人",
        "__empty": not series,
        "backgroundColor": _BG,
        "grid": {"left": 56, "right": 24, "top": 34, "bottom": 62, "containLabel": False},
        "legend": {"type": "scroll", "bottom": 0, "data": list(series),
                   "textStyle": {"color": _C_TEXT, "fontSize": 11}},
        "tooltip": {"trigger": "axis", "axisPointer": {"type": "line"}},
        "xAxis": {
            "type": "category", "data": dates, "boundaryGap": False,
            "axisLabel": {"rotate": 45, "fontSize": 9, "color": _C_TEXT},
            "axisLine": {"lineStyle": {"color": _C_AXIS}},
        },
        "yAxis": {
            "type": "value", "name": "人数",
            "nameTextStyle": {"color": _C_MUTED},
            "axisLine": {"show": True, "lineStyle": {"color": _C_AXIS}},
            "splitLine": {"lineStyle": {"color": _C_AXIS}},
            "axisLabel": {"color": _C_TEXT},
        },
        "dataZoom": [
            {"type": "inside", "xAxisIndex": 0},
            {"type": "slider", "xAxisIndex": 0, "bottom": 26, "height": 16},
        ],
        "series": [{"name": name, "type": "line", "smooth": True, "showSymbol": False,
                    "data": series[name]}
                   for name in sorted(series)],
    }


# ============================================================
# 3) 设备 option
# ============================================================

#: 机械配员工种 —— 不是机械，只在 `equip_daily` 尚未分流时出现。
_CREW_ROLES = {"泵工", "辅助", "操作工", "司机", "信号工", "起重工", "指挥",
               "振捣工", "焊工", "测量工", "试块工", "养护工"}


def _machine_names() -> set:
    """``pipeline.nodes.resource._MACHINERY_NAMES``（惰性导入，失败给空集）。"""
    try:
        from .. import resource as resource_mod
        return {_s(n) for n in getattr(resource_mod, "_MACHINERY_NAMES", set()) if _s(n)}
    except Exception:
        return set()


def _aux_substrings() -> tuple:
    try:
        from .. import resource as resource_mod
        return tuple(getattr(resource_mod, "_AUX_MACHINE_SUBSTR", ()) or ())
    except Exception:
        return ()


def _is_machine(name) -> bool:
    """``equip_daily`` 里的条目是不是"真机械"。

    背景：``delivery._compute_view`` 只按 ``LABOR``（纯工种集）分流，所以
    ``司机/泵工/操作工/辅助`` 这类**机械配员**会落进机械桶。另有一个并行
    任务负责把它们移到人工桶；为免两处修改时序耦合，这里做一道兜底：

    * 命中 ``resource._MACHINERY_NAMES`` → 是机械（最权威，放行）；
    * 命中 ``resource._AUX_MACHINE_SUBSTR``（振捣/抹光/打夯…）→ 是机械；
    * 否则**排除**已知配员工种、``LABOR`` 工种、以及"以 工 结尾"的名字
      （中文机械名基本不以"工"结尾）—— 宁可漏掉一台没登记的机械，
      也不能让"泵工 3 人"冒充"泵 3 台"上榜。
    """
    text = _s(name)
    if not text:
        return False
    if text in _machine_names():
        return True
    if any(sub and sub in text for sub in _aux_substrings()):
        return True
    if text in _CREW_ROLES:
        return False
    try:
        from .delivery import LABOR
        if text in LABOR:
            return False
    except Exception:
        pass
    return not text.endswith("工")


def _equipment_peaks(view, plan) -> dict:
    """设备名 → 单日峰值台数。

    优先级：``view["equip_daily"]``（已是机械桶，且只统计**真机械**）→
    过滤后为空时退回 ``plan["resource_plan"]["equipment_peak"]``。
    """
    peaks = {}
    for x in view.get("equip_daily") or []:
        for name, val in (x.get("items") or {}).items():
            name = _s(name)
            if not name:
                continue
            peaks[name] = max(peaks.get(name, 0), _i(val))
    peaks = {k: v for k, v in peaks.items() if v > 0 and _is_machine(k)}
    if peaks:
        return peaks
    for name, val in ((plan or {}).get("resource_plan") or {}).get("equipment_peak", {}).items():
        name = _s(name)
        val = _i(val)
        if name and val > 0 and _is_machine(name):
            peaks[name] = max(peaks.get(name, 0), val)
    return peaks


def _build_equipment(plan, view) -> dict:
    peaks = _equipment_peaks(view, plan)
    ordered = sorted(peaks.items(), key=lambda kv: (-kv[1], kv[0]))
    categories = [k for k, _ in ordered]
    values = [v for _, v in ordered]
    return {
        "__unit": "台数",
        "__empty": not categories,
        "backgroundColor": _BG,
        "grid": {"left": 60, "right": 24, "top": 34, "bottom": 74, "containLabel": False},
        "tooltip": {"trigger": "item"},
        "xAxis": {
            "type": "category", "data": categories,
            "axisLabel": {"rotate": 30, "fontSize": 11, "color": _C_TEXT,
                          "interval": 0, "width": 90, "overflow": "truncate"},
            "axisLine": {"lineStyle": {"color": _C_AXIS}},
        },
        "yAxis": {
            "type": "value", "name": "台数",
            "nameTextStyle": {"color": _C_MUTED},
            "splitLine": {"lineStyle": {"color": _C_AXIS}},
            "axisLabel": {"color": _C_TEXT},
        },
        "series": [{
            "name": "峰值台数", "type": "bar", "data": values,
            "barMaxWidth": 46,
            "itemStyle": {"color": _C_PURPLE, "borderRadius": [4, 4, 0, 0]},
            "label": {"show": True, "position": "top", "color": _C_TEXT, "fontSize": 11},
        }],
    }


# ============================================================
# 公开接口
# ============================================================

def build_chart_options(plan: dict, view: dict) -> dict:
    """产出 ``{"gantt":…, "personnel":…, "equipment":…}`` 三个**纯 JSON** option。

    返回值只含 dict / list / str / int / float —— 保证
    ``json.dumps(..., ensure_ascii=False)`` 可序列化；**不含函数、不含 set**。
    ECharts 需要的 ``formatter`` / ``renderItem`` 由 :func:`chart_cards_html`
    的 glue JS 依据 ``__dateTicks`` / ``__startDateISO`` / ``__endDateISO``
    / ``__ganttFilters`` 等标记键补齐（详见模块 docstring）。
    """
    plan = plan or {}
    view = view or {}
    filters = _resolve_filters(plan)
    return {
        "gantt": _build_gantt(plan, filters),
        "personnel": _build_personnel(view),
        "equipment": _build_equipment(plan, view),
    }


def gantt_filter_buttons_html() -> str:
    """阶段筛选按钮组（五按钮形态）。

    **按钮本身不携带规则** —— 规则在 :func:`_resolve_filters` 里由
    ``plan["wbs"]["phases"]`` 的真实段号-相位名映射解出，随 option 的
    ``__ganttFilters`` 一起内联给 glue。这样"显示哪些按钮"和"按钮点了筛什么"
    永远不会分叉（参考实现一处硬编码 ``/^[23]\\./`` 的问题正在于此：
    本项目 WBS 是 1..10 段，照抄必然筛空）。

    默认（无 JS）时五个按钮都渲染；glue 会按 ``__ganttFilters``
    把规则为空的按钮隐藏，读不到 ``wbs.phases`` 时退化为
    「全部任务 / 仅关键路径」两个。
    """
    buttons = []
    for key, label, _spec in _FILTER_SPECS:
        active = " active" if key == "all" else ""
        buttons.append(
            '<button type="button" class="dsh-gantt-btn%s" '
            'data-filter="%s">%s</button>' % (active, key, label))
    style = (
        "<style>"
        ".dsh-gantt-btns{margin:0 0 10px}"
        ".dsh-gantt-btn{font:12px/1.6 'Microsoft YaHei','PingFang SC',system-ui;"
        "margin:0 6px 6px 0;padding:4px 12px;border:1px solid #cfd8e6;border-radius:14px;"
        "background:#f7f9fc;color:#44506a;cursor:pointer}"
        ".dsh-gantt-btn:hover{border-color:#4a90d9;color:#16325c}"
        ".dsh-gantt-btn.active{background:#4a90d9;border-color:#4a90d9;color:#fff}"
        "</style>")
    note = ('<div class="chart-lbl">按阶段筛选显示的任务范围；'
            '筛选规则由计划的 WBS 阶段编号自动解析，读不到阶段定义时仅保留'
            '「全部任务 / 仅关键路径」。</div>')
    return ('<div class="dsh-gantt-btns" id="dsh-gantt-btns">'
            + "".join(buttons) + "</div>" + note + style)


# ------------------------------------------------------------
# glue JS
# ------------------------------------------------------------

#: glue：读内联 DSH_OPT，补 formatter / renderItem / 事件，再 setOption。
#: 纯 ES5 + ASCII 字面量（中文全在数据里），可安全内联进 HTML。
_GLUE_JS = r"""
(function () {
  "use strict";
  if (!window.echarts) { return; }            // 调用方回退到 SVG 看板
  var OPT = window.DSH_OPT || {};
  var MS_DAY = 86400000;

  function pad2(n) { return (n < 10 ? "0" : "") + n; }

  function parseISO(s) {
    var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(s || ""));
    if (!m) { return null; }
    return Date.UTC(+m[1], +m[2] - 1, +m[3]);  // 全程 UTC，杜绝时区漂移
  }

  function md(ms) {
    var d = new Date(ms);
    return (d.getUTCMonth() + 1) + "-" + d.getUTCDate();
  }

  function isod(ms) {
    var d = new Date(ms);
    return d.getUTCFullYear() + "-" + pad2(d.getUTCMonth() + 1) + "-" + pad2(d.getUTCDate());
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // fontSize 12 下的近似字宽：全角（中日韩）约 12.2px，半角约 6.7px。
  // 不用 canvas 量，避免在无 canvas 环境（SSR / 老浏览器）里拿到 0 而算错栏宽。
  function estTextPx(s) {
    var w = 0, t = String(s == null ? "" : s);
    for (var i = 0; i < t.length; i++) {
      w += (t.charCodeAt(i) > 0x2e80) ? 12.2 : 6.7;
    }
    return w;
  }

  // 左栏宽度按**最长工序名**自适应。
  // 原来固定 340px：短名字右对齐后左半边全是空白，还把绘图区挤得很窄
  // （用户反馈「左边工序名太多空白」）。上限 40% 容器宽，保证绘图区至少占六成。
  function fitLabelWidth(dom, yData) {
    var cw = (dom && dom.clientWidth) ? dom.clientWidth : 960;
    var maxPx = 0;
    var list = yData || [];
    for (var i = 0; i < list.length; i++) {
      var w = estTextPx(list[i]);
      if (w > maxPx) { maxPx = w; }
    }
    var cap = Math.max(170, Math.min(cw * 0.40, 420));
    var lw = maxPx + 26;
    if (lw > cap) { lw = cap; }
    if (lw < 150) { lw = 150; }
    return Math.round(lw);
  }

  function mkChart(id, opt) {
    var dom = document.getElementById(id);
    if (!dom || !opt) { return null; }
    var inst = null;
    try {
      inst = echarts.getInstanceByDom(dom) || echarts.init(dom);
      inst.setOption(opt, true);
      return inst;
    } catch (e) {
      try { console.warn("[dsh-echarts] init failed: " + id, e); } catch (e2) {}
      return null;
    }
  }

  function noData(id, text) {
    var dom = document.getElementById(id);
    if (!dom) { return; }
    dom.innerHTML = '<div style="height:100%;display:flex;align-items:center;'
      + 'justify-content:center;color:#6b7688;font-size:13px">' + esc(text) + "</div>";
  }

  var charts = [];
  var g = OPT.gantt, p = OPT.personnel, e = OPT.equipment;

  /* ---------------- 甘特 ---------------- */
  var ganttChart = null, ganttCache = null;
  if (g) {
    var startMs = parseISO(g.__startDateISO);
    var endMs = parseISO(g.__endDateISO);
    var spanMs = (startMs != null && endMs != null) ? Math.max(endMs - startMs, 1) : null;

    // __dateTicks → 查表用的 {百分比: "M-D"}；glue 在 init 之前就备好，
    // 供初始化时就用上（setOption 之后再改会闪一下）
    var tickMap = {};
    var ticks = g.__dateTicks || [];
    for (var i = 0; i < ticks.length; i++) {
      tickMap[String(ticks[i][0])] = String(ticks[i][1]);
    }
    var nearestTick = function (v) {
      var best = null, bestD = Infinity;
      for (var k in tickMap) {
        if (!Object.prototype.hasOwnProperty.call(tickMap, k)) { continue; }
        var d = Math.abs(parseFloat(k) - v);
        if (d < bestD) { bestD = d; best = tickMap[k]; }
      }
      return best;
    };
    var percentToDate = function (v) {
      if (spanMs != null) { return md(startMs + v / 100 * spanMs); }
      var t = nearestTick(v);
      return t == null ? "" : t;
    };

    var ganttData = [];
    if (g.series && g.series.length) {
      ganttData = g.series[0].data || [];
      g.series[0].renderItem = function (params, api) {
        var catIdx = api.value(0);
        var off = api.value(1);
        var dur = api.value(2);
        var isCrit = !!api.value(8);
        var pt = api.coord([0, catIdx]);
        if (!pt) { return null; }
        var y = pt[1];
        var x1 = api.coord([off, 0])[0];
        var x2 = api.coord([off + dur, 0])[0];
        var size = api.size([0, 1]);
        var h = (size && size[1] ? size[1] : 12) * 0.58;
        if (h < 4) { h = 4; }
        return {
          type: "rect",
          shape: { x: x1, y: y - h / 2, width: Math.max(x2 - x1, 2), height: h, r: 3 },
          style: api.style({ fill: isCrit ? "#ee6666" : "#5470c6" })
        };
      };
    }
    g.xAxis.axisLabel.formatter = percentToDate;
    g.tooltip = {
      trigger: "item",
      backgroundColor: "#ffffff",
      borderColor: "#e8edf5",
      textStyle: { color: "#1f2530", fontSize: 12 },
      formatter: function (p0) {
        var d = p0 && p0.data;
        if (!d) { return ""; }
        return "<b>" + esc(d[4]) + " " + esc(d[3]) + "</b><br/>"
          + "起止：" + d[5] + " ~ " + d[6] + "<br/>"
          + "工期：" + d[7] + " 天<br/>"
          + "关键路径：" + (d[8] ? "是" : "否");
      }
    };
    // 左栏宽度按最长工序名自适应（见 fitLabelWidth 注释）
    g.grid.left = fitLabelWidth(document.getElementById("dsh-gantt"), g.yAxis.data);
    g.yAxis.axisLabel.width = g.grid.left - 14;
    ganttChart = mkChart("dsh-gantt", g);
    if (ganttChart) {
      charts.push(ganttChart);
      // 用初始 option 本身当 cache —— getOption() 返回可能是合并后的视图，
      // 也可能为 null；yAxis.data 的兜底全靠它，不能为空。
      ganttCache = null;
      try { ganttCache = ganttChart.getOption(); } catch (e) { ganttCache = null; }
      if (!ganttCache || !ganttCache.yAxis || !ganttCache.yAxis[0]
          || !ganttCache.yAxis[0].data) {
        ganttCache = g;
      }
    }
    applyFilters("all");
  }

  /* ---------------- 阶段筛选 ---------------- */
  function segmentOf(taskId) {
    var seg = String(taskId == null ? "" : taskId).split(".")[0];
    return /^\d+$/.test(seg) ? parseInt(seg, 10) : 0;
  }

  function ruleOf(key) {
    var rules = (g && g.__ganttFilters) || [];
    for (var i = 0; i < rules.length; i++) {
      if (rules[i].key === key) { return rules[i]; }
    }
    return null;
  }

  function applyFilters(key) {
    if (!ganttChart) { return; }
    var rule = ruleOf(key) || ruleOf("all");
    if (!rule) { return; }
    var segs = rule.segments || [];
    var rows = [];
    for (var i = 0; i < ganttData.length; i++) {
      var rec = ganttData[i];
      var keep;
      if (rule.crit_only) {
        keep = !!rec[8];
      } else if (segs.length) {
        keep = segs.indexOf(segmentOf(rec[4])) >= 0;
      } else {
        keep = (rule.key === "all");
      }
      if (keep) { rows.push(i); }
    }
    var yData = (ganttCache && ganttCache.yAxis && ganttCache.yAxis[0]
      && ganttCache.yAxis[0].data) ? ganttCache.yAxis[0].data : g.yAxis.data;
    var newY = [], newData = [];
    for (var j = 0; j < rows.length; j++) {
      newY.push(yData[rows[j]]);
      var r = ganttData[rows[j]].slice();
      r[0] = j;                                  // 行号必须重排，否则和图对不上
      newData.push(r);
    }
    // 【必须带上 type/encode/renderItem】custom series 只要没有 renderItem 就一条也不画。
    // 这里曾写成 series:[{data:newData}]，replaceMerge 会把整个 series 换成这个残缺对象，
    // 于是 init 时画好的甘特条在 applyFilters("all") 之后**全部消失**
    // —— 这正是用户报的「甘特图渲染失败」（只有左侧工序名，右侧空白）。
    // 已用真实 ECharts SSR 复现并验证：现状 blue=0/red=0，补齐字段后 6/6 条在。
    var ganttTpl = (g.series && g.series[0]) || {};
    ganttChart.setOption({
      yAxis: { data: newY },
      series: [{
        type: ganttTpl.type || "custom",
        encode: ganttTpl.encode || { x: [1, 2], y: 0 },
        renderItem: ganttTpl.renderItem,
        data: newData
      }]
    }, { replaceMerge: ["series"] });
  }

  var btnBox = document.getElementById("dsh-gantt-btns");
  if (btnBox && ganttChart) {
    var btns = btnBox.getElementsByTagName("button");
    for (var b = 0; b < btns.length; b++) {
      (function (btn) {
        var key = btn.getAttribute("data-filter");
        var rule = ruleOf(key);
        // 规则为空的按钮没有意义（相位映射里没有段落到这个桶）→ 隐藏
        if (!rule || (!rule.crit_only && key !== "all" && !(rule.segments || []).length)) {
          btn.style.display = "none";
          return;
        }
        btn.onclick = function () {
          for (var k = 0; k < btns.length; k++) {
            btns[k].className = "dsh-gantt-btn";
          }
          btn.className = "dsh-gantt-btn active";
          applyFilters(key);
        };
      })(btns[b]);
    }
  }

  /* ---------------- 人员曲线 ---------------- */
  if (p) {
    if (p.__empty) {
      noData("dsh-personnel", "暂无人员配置数据");
    } else {
      p.tooltip = {
        trigger: "axis",
        backgroundColor: "#ffffff",
        borderColor: "#e8edf5",
        textStyle: { color: "#1f2530", fontSize: 12 },
        formatter: function (params) {
          var arr = params || [], hit = [], i;
          for (i = 0; i < arr.length; i++) {
            if (arr[i].value) { hit.push(arr[i]); }
          }
          if (!hit.length) { return ""; }
          var unit = p.__unit || "";
          var html = "<b>" + esc(hit[0].axisValue) + "</b>";
          for (i = 0; i < hit.length; i++) {
            html += "<br/>" + hit[i].marker + esc(hit[i].seriesName)
              + "\uff1a" + hit[i].value + unit;
          }
          return html;
        }
      };
      var pc = mkChart("dsh-personnel", p);
      if (pc) { charts.push(pc); }
    }
  }

  /* ---------------- 设备 ---------------- */
  if (e) {
    if (e.__empty) {
      noData("dsh-equipment", "暂无设备负荷数据");
    } else {
      e.tooltip = {
        trigger: "item",
        backgroundColor: "#ffffff",
        borderColor: "#e8edf5",
        textStyle: { color: "#1f2530", fontSize: 12 },
        formatter: function (p0) {
          var d = p0 && p0.data;
          var unit = e.__unit || "";
          return "<b>" + esc(p0.name) + "</b><br/>" + "峰值：" + d + " " + unit;
        }
      };
      var ec = mkChart("dsh-equipment", e);
      if (ec) { charts.push(ec); }
    }
  }

  /* ---------------- 自适应 ---------------- */
  window.addEventListener("resize", function () {
    for (var i = 0; i < charts.length; i++) {
      try { charts[i].resize(); } catch (err) {}
    }
  });
})();
"""


def _json_for_script(obj) -> str:
    """``json.dumps`` + 内联安全化（``</`` → ``<\\/``，挡住提前闭合 script）。"""
    text = json.dumps(obj, ensure_ascii=False)
    return text.replace("</", "<\\/")


def chart_cards_html(plan: dict, view: dict) -> str:
    """三张 ``.card``（甘特 / 人员曲线 / 设备峰值）+ 一段 glue ``<script>``。

    **不含 ECharts bundle** —— bundle 由调用方用 :func:`echarts_bundle_html`
    放在别处。``window.echarts`` 不存在时 glue 直接 return，调用方回退到
    ``delivery`` 的 SVG 看板。
    """
    plan = plan or {}
    view = view or {}
    opts = build_chart_options(plan, view)

    ov = _overview(plan)
    start = _as_date(ov.get("planned_start_date")) or _as_date(ov.get("start_date"))
    crit_n = opts["gantt"].get("__criticalCount") or 0
    rows = len(opts["gantt"]["series"][0]["data"])
    height = max(420, rows * 24)
    height = min(height, 12000)
    # 上卷后必须写清"这些行是怎么来的"，否则读者会以为计划只有 164 条任务。
    if opts["gantt"].get("__rolledUp"):
        rows_txt = "共 %d 行（按展示粒度合并，逐叶子 %d 项）" % (
            rows, opts["gantt"].get("__leafTasks") or rows)
    else:
        rows_txt = "共 %d 项任务" % rows

    gantt_card = (
        '<div class="card"><h2>横道图（甘特 · \u2605=关键路径 %d 项）</h2>'
        '<div class="leg"><span><span class="k" style="background:%s"></span>普通任务</span>'
        '<span><span class="k" style="background:%s"></span>关键路径</span>'
        '<span><span class="k" style="background:#4a90d9"></span>%s</span></div>'
        '%s'
        '<div class="chart-lbl">横轴=日历工期（%s 起）· 纵轴带工序编号 · 可框选/滚轮缩放</div>'
        '<div id="dsh-gantt" style="width:100%%;height:%dpx"></div></div>'
        % (crit_n, _C_BLUE, _C_RED, rows_txt,
           gantt_filter_buttons_html(),
           start.isoformat() if start else "\u2014", height))

    personnel_n = len(opts["personnel"].get("series") or [])
    peak_txt = "%s 人 · 峰值工种 %s" % (view.get("peak_total", "\u2014"),
                                        view.get("peak_trade") or "\u2014")
    personnel_card = (
        '<div class="card"><h2>主要工种人员配置曲线</h2>'
        '<div class="chart-lbl">峰值 %s · 每日曲线，横轴为日历日期</div>'
        '<div id="dsh-personnel" style="width:100%%;height:380px"></div>'
        '<div class="chart-lbl">共 %d 个工种（仅显示至少有一天出工的工种）</div></div>'
        % (peak_txt, personnel_n))

    equip_series = (opts["equipment"].get("series") or [{}])[0]
    equip_n = len(equip_series.get("data") or [])
    equip_card = (
        '<div class="card"><h2>设备峰值需求统计</h2>'
        '<div class="chart-lbl">按单日峰值台数降序 · 仅统计机械（不含司机/泵工等配员工种）</div>'
        '<div id="dsh-equipment" style="width:100%%;height:400px"></div>'
        '<div class="chart-lbl">共 %d 类设备</div></div>'
        % equip_n)

    script = ('<script>var DSH_OPT = %s;\n%s</script>'
              % (_json_for_script(opts), _GLUE_JS))
    return gantt_card + personnel_card + equip_card + script

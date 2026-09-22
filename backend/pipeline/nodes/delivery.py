"""交付节点 —— plan_json → Word / HTML（纯本地生成，不改数据契约）

从 ctx["plan_json"]（schemas.PlanJson 已验证）生成两类交付物到
deliverables/<plan_id>/ 目录：
  WordExportNode : 施工进度计划.docx  —— python-docx：总览/WBS/关键路径/甘特表/资源人月表等
  HtmlPageNode   : 计划看板.html      —— 自包含：ECharts 甘特 + 分工种人员曲线 + 设备峰值
                                        （vendor 缺失时自动回退内联 SVG 图表）

图表数据全部由 _compute_view() 从 all_tasks_schedule 确定性算出（每任务含起止日期 +
每日资源），不使用 LLM，保证数值与 CPM/资源结果一致。**绘制**交给 ECharts：option 由
echarts_page.build_chart_options() 生成（纯 JSON），交互函数由固定 glue 注入。

资源口径：LABOR ∪ MACHINE_CREW = 人；其余 = 机械。泵工/辅助/操作工/司机 是**人**，
历史上被当成设备写进「设备资源荷载」，已修。

文件：
  terminal 的 /show 是终端侧简易版；本模块是后端交付版，产出更完整。
"""

import datetime
import html as _html
import json
import re
import shutil
import sys
from pathlib import Path

from .. import branding, config
from .. import quantity as quantity_mod
from ..base import BaseNode
from ..llm import LLMClient
from ..prompts_loader import load
from .docctx import combine
from .boundary import param_label_list

# ECharts 图表模块（vendor 的 echarts.min.js 内联进看板）。
# 用 try 包住：模块缺失、vendor 文件被裁掉、任何导入期异常，都**不能**让交付链断掉 ——
# 拿不到 ECharts 就自动回退到确定性 SVG 图表（_svg_gantt / _svg_line_chart / _svg_bars）。
try:
    from . import echarts_page
except Exception:                      # pragma: no cover - 兜底路径
    echarts_page = None

# 防旧文件污染：保留最近 MAX_KEEP 个运行目录，更早的自动清理
MAX_KEEP = 6


# ============================================================
# G5 · 交付物单位清零：CJK 兼容方块平米符号 `U+33A1`（U+33A1）
# ------------------------------------------------------------
# 判据的唯一真源在 `plan_assembler.CJK_COMPAT_SQUARE_METRE` /
# `find_cjk_compat_square_metre` / `assert_no_cjk_compat_square_metre`，这里只转发
# （两处各写一份判据 = 迟早漂移）。
#
# 两档强度（刻意不同）：
#   · **生成期（plan_assembler.PlanDeliverNode）**：硬断言 —— 新产出的 plan_json
#     里 0 处 U+33A1，命中即抛错（报路径 + 原文）。
#   · **交付物渲染期（本文件）**：**响亮报告、不中止**。为什么不能硬断言：已落盘的
#     历史计划里，`resource_demand.tasks[*]._unit_assumed` 这类**人话串**写着
#     `≈1.8 m²/U+33A1 建筑面积`（旧 `resource.py` 的产物，W2-C 已修），计划是**不可变档案**，
#     硬断言会让所有历史计划无法再出交付物（真实可用性回归）。这里改为把命中处
#     （路径 + 原文）打到 stderr，并**绝不静默 replace**。
# ============================================================
def _normalize_deliverable_u33a1(text, where):
    """交付物侧 G5 **归一收口**：把 U+33A1 归一为 `m²`，**逐处留痕**（不静默），返回新串。

    为什么既要归一又要报告：`U+33A1`(U+33A1) 与 `m²` 是同一量纲的两种写法，§5「单位贯通」
    的规范写法就是 `m²`；已落盘的历史计划里，`resource_demand.tasks[*]._unit_assumed`
    这类**人话串**仍写着旧写法（旧 `resource.py` 的产物，W2-C 已修）。历史计划是不可变
    档案，硬断言会让它们永远出不了交付物（真实可用性回归）——所以这里归一，并把命中处
    （路径偏移 + 上下文原文）写到 stderr，**绝不静默**。
    """
    from . import plan_assembler as _pa
    new_text, fixed = _pa.normalize_cjk_compat_square_metre_in_text(text)
    if fixed:
        sys.stderr.write(
            "[G5] %s 里有 %d 处 %s 已按 §5 单位贯通归一为 m²"
            "（上游产出点如下，非静默）：\n%s\n"
            % (where, len(fixed), _pa.CJK_COMPAT_SQUARE_METRE_NAME,
               "\n".join("  · %s → %s" % (p, _pa._hit_excerpt(t)) for p, t in fixed[:10])))
    return new_text


def _report_no_cjk_compat_square_metre(obj, where):
    """交付物侧 G5 报告（判据与 plan 侧同一份）：命中列表 + stderr 响亮提示。"""
    from . import plan_assembler as _pa
    hits = _pa.find_cjk_compat_square_metre(obj, where)
    if hits:
        sys.stderr.write(
            "[G5] %s 里仍残留 %d 处 U+33A1（应写 m²；来源见下方路径与原文）：\n%s\n"
            % (where, len(hits),
               "\n".join("  · %s → %s" % (p, _pa._hit_excerpt(t)) for p, t in hits[:10])))
    return hits


def _assert_docx_no_cjk_compat_square_metre(path):
    """Word（docx）产物侧 G5 报告 —— 直接查 `word/document.xml` 原文。

    为什么查落盘文件而不是内存里的 doc 对象：正文 / 表格 / 页眉页脚 / 脚注都在 XML 里，
    查 XML 才算把整份产物看完；查不到（文件损坏等）不在这里报错。
    """
    import zipfile
    try:
        with zipfile.ZipFile(str(path)) as z:
            xml = z.read("word/document.xml").decode("utf-8")
    except Exception:
        return []
    return _report_no_cjk_compat_square_metre(xml, "施工进度计划.docx（word/document.xml）")

# 与 plan_assembler.LABOR_NAMES 保持一致的工种集合，其余资源视为设备
LABOR = {
    "普工", "钢筋工", "模板工", "混凝土工", "瓦工", "抹灰工", "泥工", "油漆工",
    "保温工", "装修工", "绿化工", "防水工", "安装工", "管道工", "电工", "通风工",
    "架子工", "灌浆工", "装配式安装工", "桩机工", "铺装工", "水泥工", "测量工",
    # 「木工」「砌筑工」：用户申报的 labor.by_trade 里就有（实测真实计划见
    # `meta.boundary_conditions.labor.by_trade`），分类表里缺了它们，同名资源就会被
    # 当成机械画进「设备资源荷载」。与 plan_assembler.LABOR_NAMES 同步补全。
    "木工", "砌筑工",
}

# 机械配员：随机械台数配置的操作人员。它们确实出现在任务的 assigned_resources 里
# （如 `混凝土浇筑` 挂 `混凝土输送泵车17 / 泵工17 / 辅助17`），但**是人不是设备**。
# 历史实现把它们归进 equip_day，于是看板「设备资源荷载」里出现了人（泵工17/辅助17/
# 操作工15/司机1），真正的人工需求反而看不见 —— 这是用户明确指出的缺陷。
# 口径：LABOR ∪ MACHINE_CREW = 人；其余 = 机械。与 plan_assembler.MACHINE_CREW 同源。
# 「信号工」= 塔吊/施工电梯的配员（`Equipment_Crew_Mapping`：司机1名+信号工1名）。
# 缺了它，看板「设备资源荷载」里会出现一个叫"信号工"的"机械"。
# 「振捣工」= 混凝土振捣器的配员（`Equipment_Crew_Mapping`：`混凝土振捣器 → 振捣工1人`）。
# 资源层把配员并进 resources 后，实测计划 `plan_sample3_after_fix` 的
# `resource_plan.equipment_peak` 里出现了 `"振捣工": 1`，Word 设备峰值段落也印出
# 「振捣工1」（与「混凝土振捣器1」并列）—— 人又一次被当成机械。
# 口径不变：它是配员 → 进人数曲线 + `machine_crew_peak`，**绝不进 equipment_peak**。
MACHINE_CREW = {
    "泵工", "辅助", "操作工", "司机", "信号工",
    # 全表解析 `Equipment_Crew_Mapping.crew_composition`（26 行 / 5 种配员原文）后，
    # `振捣工` 是唯一没被覆盖的角色；其余 5 个（司机 操作工 信号工 泵工 辅助）都已在表内。
    "振捣工",
}


def _is_labor(name) -> bool:
    """某个资源名算「人」还是「机械」。两处口径必须一致，故集中在这一个函数里。"""
    return name in LABOR or name in MACHINE_CREW


def _split_equipment_peak(rp):
    """``(真设备峰值, 机械配员峰值)`` —— 展示层对**旧落盘**计划的口径兜底。

    为什么展示层还要过滤一次：`equipment_peak` 是 `plan_assembler` 在**计划落盘时**
    算好写进去的。分类表补了 `振捣工` 之后，**新**计划不会再有它；但已经落盘的旧计划
    （实测 `plan_sample3_after_fix`：`{"…": …, "振捣工": 1}`）不会自己变干净 ——
    Word 的「设备峰值台数」表读的就是这个字段，实测仍印出「振捣工1」。
    这里按当前分类表过滤，并把这些名字**并进机械配员峰值**（取 max），
    使看板、Word、ECharts 三处展示与 `machine_crew_peak` 口径一致。
    """
    rp = rp if isinstance(rp, dict) else {}
    raw = rp.get("equipment_peak") or {}
    raw = raw if isinstance(raw, dict) else {}
    equip = sorted(((k, v) for k, v in raw.items() if k not in MACHINE_CREW),
                   key=lambda x: -x[1])
    crew = dict(rp.get("machine_crew_peak") or {})
    for k, v in raw.items():
        if k in MACHINE_CREW:
            try:
                crew[k] = max(int(crew.get(k, 0) or 0), int(v))
            except (TypeError, ValueError):
                crew.setdefault(k, v)
    return equip, sorted(crew.items(), key=lambda x: -x[1])


def _echarts_ok() -> bool:
    """ECharts 是否可用（模块在 + vendor 文件在）。任一不满足则走 SVG 兜底。"""
    try:
        return echarts_page is not None and bool(echarts_page.has_echarts())
    except Exception:                  # pragma: no cover
        return False


# ============================================================
# 共用
# ============================================================

def _plan_dir(plan) -> Path:
    ident = plan.get("plan_id") or "plan_latest"
    d = config.DELIVERABLES_DIR / f"计划_{ident}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _maintain(root):
    """防旧文件污染：保留最近 MAX_KEEP 份运行目录，并在根目录维护一个索引页。

    索引按时间倒序列出所有运行，标注"当前"，用户据此一眼分清新旧，避免误打开旧产物。
    """
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    runs = sorted(
        [d for d in root.glob("计划_*") if d.is_dir()],
        key=lambda d: d.stat().st_mtime, reverse=True,
    )

    # 1) 保留策略：只留最新 MAX_KEEP 份，更早的移除（防堆积）
    for stale in runs[MAX_KEEP:]:
        try:
            shutil.rmtree(stale, ignore_errors=True)
        except OSError:
            pass

    # 2) 顶层索引页（人类可读，白底）
    rows = []
    for i, d in enumerate(runs[:MAX_KEEP]):
        ts = datetime.datetime.fromtimestamp(d.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        tag = "当前" if i == 0 else "历史"
        ident = d.name.replace("计划_", "")
        html_file = d / "计划看板.html"
        link = f"<a href='计划_{_html.escape(ident)}/计划看板.html'>打开看板</a>" if html_file.exists() else "（无看板）"
        rows.append(
            f"<tr><td><b>{tag}</b></td><td>{_html.escape(ident)}</td><td>{ts}</td><td>{link}</td></tr>")
    index = (
        "<!DOCTYPE html><html lang='zh'><head><meta charset='utf-8'>"
        "<title>输出结果 · 索引</title><style>body{font-family:'Microsoft YaHei',system-ui;margin:24px}"
        "table{border-collapse:collapse;width:100%}th,td{border:1px solid #ddd;padding:8px;text-align:left}"
        "th{background:#4a90d9;color:#fff}"
        + branding.BRAND_CSS + "</style></head><body>"
        + branding.html_brand_head()
        + "<h1>📁 输出结果（施工计划交付物）</h1>"
        f"<p>当前 = 最近一次运行；历史自动保留最近 {MAX_KEEP} 份，更早会随新运行清理。</p>"
        "<table><tr><th>状态</th><th>计划编号</th><th>生成时间</th><th>看板</th></tr>"
        + "".join(rows) + "</table>"
        + branding.html_brand_foot(datetime.date.today().isoformat())
        + "</body></html>")
    (root / "索引.html").write_text(index, encoding="utf-8")


def _to_date(s, default=None):
    try:
        return datetime.date.fromisoformat(str(s))
    except (ValueError, TypeError):
        return default or datetime.date.today()


def _critical_ids(plan):
    return set((plan.get("cpm_result") or {}).get("critical_path") or [])


def _tasks(plan):
    return plan.get("all_tasks_schedule") or []


# ---------------- 展示粒度上卷（只改展示，不改数据） ----------------
# 用户在设计门里选的两维粒度（① 工序拆解深度 ② 楼层分组）此前**只影响对话**，
# 导出的 Word/看板仍然铺全部叶子 —— 选择被问了、被记了、却没被执行。
# 这里把它接进交付物。三条铁律：
#   ① 只合并**展示行**，不动工程量、不动工期口径、不动资源曲线；
#   ② 合并行的工期取组内任务的**排程时间跨度**，**绝不重算**
#      （否则粗粒度会算出与细粒度不同的总工期，同一份计划自相矛盾）；
#   ③ 未选合并时行为与旧版**逐字节一致**（默认路径不变）。

def params_banner(plan):
    """参数不完整 / 取了默认值 / 试算模式下的显著标注；一切正常时返回空串。

    为什么必须出现在**交付物**上：用户选了"用默认值试算"之后，那份计划看起来和正常
    计划一模一样。一旦流出去被人当依据使用，就是事故。

    两级语气（避免"狼来了"）：
      · 试算 / 缺硬必要参数 → ⚠️ 红字"不可用于施工"
      · 只是取了默认值（如栋数按单栋）→ ℹ️ 中性提示"如与实际不符请补充"
    """
    meta = plan.get("meta") if isinstance(plan.get("meta"), dict) else {}
    comp = meta.get("params_completeness") or {}
    missing = list(comp.get("missing_required") or [])
    defaulted = list(comp.get("missing_default") or [])
    trial = bool(meta.get("trial_mode"))
    if not (trial or missing or defaulted):
        return ""

    parts = []
    if trial:
        parts.append("本计划为「用默认值试算」结果，不可用于施工。")
    if missing:
        # 交付物上也要用中文名（这段会原样写进 Word 的"未审计/试算"说明里，
        # 直接打 total_concrete 这种内部键名用户看不懂）。
        parts.append("缺少编制所必需的项目事实：%s。" % param_label_list(missing))
    if defaulted:
        parts.append("以下参数未提供，已按默认值编制：%s。" % param_label_list(defaulted))
    if comp.get("note"):
        parts.append(str(comp["note"]) + "。")
    if comp.get("default_note"):
        parts.append(str(comp["default_note"]) + "。")

    if trial or missing:
        return "⚠️ " + "".join(parts) + "请补齐参数后重新生成，方可作为施工依据。"
    return "ℹ️ " + "".join(parts) + "如与实际不符，请在参数门补充后重新生成。"


def model_participation_notice(plan):
    """「本次运行模型没有参与」的显著提示文案；模型确实参与（level == "ok"）时返回空串。

    为什么必须出现在**交付物**上（真实事故）：`plan_sample3_after_org` 的
    `meta["usage"]["calls"] == 0` —— 一次模型都没调用、**整条主链**全部静默走确定性兜底，
    （不写死节点数：主链节点数会随域 5 等新增节点变化，这里只描述现象）
    可交付物看起来与正常计划**一模一样**（209 条任务、门都答了、"未审计"），而
    `boundary_conditions` 的 labor/equipment/materials/工期全无值、`equipment_binding`
    为空、`extracted_params.total_concrete / total_rebar` 是 null。用户没有第二种办法
    知道"这份表里的劳动力/设备/材料/定额都是缺的"，所以这句话必须自己站到正文里。

    判据与文案都来自数据（`meta["model_participation"]`，由 `plan_assembler.build_meta`
    恒写入）；调用次数**取自数据，不许写死**。老计划（加该字段之前落盘的）没有这个键，
    就从 `meta["usage"]` 用**同一个判据函数**现算 —— 单一真源，免得交付侧再写一套规则。
    """
    if not isinstance(plan, dict):
        return ""
    meta = plan.get("meta") if isinstance(plan.get("meta"), dict) else {}
    mp = meta.get("model_participation")
    if not isinstance(mp, dict):
        try:
            # 函数内导入：`plan_assembler` 与 `delivery` 互不依赖，这样写不引入导入环。
            from .plan_assembler import model_participation
            mp = model_participation(meta.get("usage"))
        except Exception:                                  # pragma: no cover - 兜底
            mp = {"participated": None, "calls": None, "level": "unknown",
                  "note": "本次运行未记录模型用量，无法判断模型是否参与。"}
    if mp.get("level") == "ok":
        return ""                       # 模型参与了：一个字都不加（逐字回归门）
    calls = mp.get("calls")
    count = "未记录" if calls is None else "%d" % calls
    note = str(mp.get("note") or "").strip()
    if mp.get("level") == "none":
        head = "⚠️ 模型未参与本次计划生成（模型调用次数：%s）。" % count
    else:
        head = "⚠️ 模型未参与情况未知（模型调用次数：%s）。" % count
    return head + note


def _display_granularity(plan):
    """用户选的展示粒度。缺省 = 工序级 × 按层 = 逐叶子（现状）。"""
    g = (plan.get("meta") or {}).get("display_granularity") or {}
    depth = g.get("depth") or quantity_mod.DEPTH_COMPONENT
    grouping = g.get("floor_grouping") or quantity_mod.FLOOR_PER_FLOOR
    if depth not in quantity_mod.DEPTHS:
        depth = quantity_mod.DEPTH_COMPONENT
    if grouping not in quantity_mod.FLOOR_GROUPINGS:
        grouping = quantity_mod.FLOOR_PER_FLOOR
    return depth, grouping


def _is_rolled_up(plan):
    """是否需要合并展示行。False 时一切与旧版一致。"""
    return _display_granularity(plan) != (quantity_mod.DEPTH_COMPONENT,
                                          quantity_mod.FLOOR_PER_FLOOR)


def granularity_note(plan):
    """交付物里的一句口径说明；未合并时返回空串。"""
    if not _is_rolled_up(plan):
        return ""
    depth, grouping = _display_granularity(plan)
    g = (plan.get("meta") or {}).get("display_granularity") or {}
    rows = g.get("rows")
    cnt = ("%s 行" % rows) if rows else "已合并"
    return ("展示粒度：%s × %s（%s）。仅合并展示行，工程量/工期/资源均未改动；"
            "合并行的工期取组内任务的排程时间跨度，故总工期与逐叶子口径一致。"
            % (quantity_mod.DEPTH_LABELS.get(depth, depth),
               quantity_mod.FLOOR_LABELS.get(grouping, grouping), cnt))


# ============================================================
# 【第 2 批 · 域 2 / 2.7】材料计划**不在本计划内** —— 交付物声明（Word + 看板同源）
# ------------------------------------------------------------
# 为什么必须有这句话：本批把材料清单从**输入（提示词）/ 边界条件 / 计划 / 展示**四处
# 一起删掉了。删掉之后，用户看到产物里没有材料相关内容，无法区分两种可能：
#   ① 这是**设计**（材料按"管够"处理，不参与工期与资源计算）；还是
#   ② 我申报的材料被漏掉了 / 模型没读到。
# 没有这句话，②的怀疑无法排除 —— 交付物的"如实"要求（本仓库的既有纪律：
# 宁可写"本次没取得设备清单"，也不许让用户以为"我没申报设备"）在材料这一项上就会失守。
#
# 一字不改地照本批口径（docs/修改项总清单_最终版.md:87）：
#   「本计划不含材料计划。材料按"管够"处理，不参与工期与资源计算。」
# ⚠️ Word（`build_plan_docx`）与看板（`build_plan_html` / `build_plan_html_agent`）
# **共用这一个常量**，两处不允许各写一份（措辞漂移 = 两份产物口径不一致）。
MATERIALS_EXCLUDED_NOTICE = '本计划不含材料计划。材料按"管够"处理，不参与工期与资源计算。'
#: 看板里判定"这句声明在不在"的标记（LLM 编排页面可能整段不写，见 `_ensure_materials_notice`）。
MATERIALS_NOTICE_MARKER = "不含材料计划"
MATERIALS_NOTICE_FALLBACK_COMMENT = "<!-- 追加材料计划声明"

#: 域 8.1：AI 补的资源限额被丢弃时的披露行（确定性渲染，看板 + Word 共用）。
_IGNORED_MODEL_LIMITS_LABEL = "以下资源限额由模型按常见做法补齐（非用户输入），未作为限额使用"


def _ignored_model_limits_text(plan):
    """域 8.1：AI 补的限额被丢弃的披露行。空 → 空串（不渲染）。"""
    meta = plan.get("meta") if isinstance(plan.get("meta"), dict) else {}
    items = list(meta.get("_ignored_model_limits") or [])
    if not items:
        return ""
    return "%s：%s" % (_IGNORED_MODEL_LIMITS_LABEL, "、".join(str(x) for x in items))


#: 域 8.8①：未人工核验的 L4 条数标注（确定性渲染，看板 + Word 共用）。
_L4_REVIEW_NOTICE_LABEL = "知识库 L4 工序核验状态"


def _l4_review_notice_text():
    """域 8.8①：未人工核验的 L4 条数。读 kb.db，取不到时不渲染。"""
    try:
        from .. import config
        import sqlite3
        db_path = getattr(config, "KB_DB_PATH", None)
        if not db_path:
            return ""
        conn = sqlite3.connect(db_path)
        total = conn.execute("SELECT COUNT(*) FROM L4_Activity_Dictionary").fetchone()[0]
        verified = conn.execute(
            "SELECT COUNT(*) FROM L4_Activity_Dictionary WHERE status = 'verified'"
        ).fetchone()[0]
        pending = conn.execute(
            "SELECT COUNT(*) FROM L4_Norm_Default WHERE review_state = 'pending'"
        ).fetchone()[0]
        conn.close()
        if total == 0:
            return ""
        non_verified = total - verified
        return ("%s：共 %d 条 L4 工序，其中 %d 条已人工核验（verified），"
                "%d 条未核验（parsed/needs_review）；定额表中 %d 条待审（pending）"
                % (_L4_REVIEW_NOTICE_LABEL, total, verified, non_verified, pending))
    except Exception:  # noqa: BLE001
        return ""


def _report_text(plan):
    """报告正文 + **保证带着展示口径句** + **保证关键路径口径不乱**。

    为什么在交付侧再补这两道（reporter 已经补过）：交付节点读的是 ``plan["report"]``，
    而计划 JSON 可能是**旧的一次运行留下的**（重导出 / 看板刷新 / 打开历史计划）。
    只在生成那一刻补，等于老计划永远缺这句 —— 而它正是"选择到底有没有体现在
    成果里"的凭证。已提到"展示口径"则原样返回，不会出现两条。

    第二道是**关键路径口径**（真实缺陷，真计划 `plans/plan_run_1789895021.json`）：
    归档报告里写着「当前计划总工期为604天，**关键路径长度为81天**」—— 81 是关键路径
    **任务的条数（个）**，不是天数；同一份交付物的表格里明明写着「关键路径任务数 81 个」
    「关键路径工期·排程版 604 天」。改提示词管不到**已经落盘**的报告，所以在印出来之前
    做确定性改写（`plan_assembler.fix_critical_path_wording`：只改数字与计划真值对得上
    的句子，对不上一个字不动）。docx / 看板 / 交付 facts 三条出口共用本函数 → 同一份口径。
    """
    text = plan.get("report") or ""
    try:
        from . import plan_assembler
    except Exception:                      # 兜底：口径句缺失也不能挡住交付
        return text
    try:
        text, _cp_fixes = plan_assembler.fix_critical_path_wording(text, plan)
    except Exception:                      # 改写失败 → 用原文，绝不让交付链断
        pass
    try:
        return plan_assembler.with_caliber(
            text, plan_assembler.display_caliber(plan)["note"])
    except Exception:                      # 兜底：口径句缺失也不能挡住交付
        return text


def rolled_rows(plan):
    """按展示粒度上卷后的行（含覆盖的叶子 id）。未上卷时返回 []。"""
    if not _is_rolled_up(plan):
        return []
    depth, grouping = _display_granularity(plan)
    sched = {str(r.get("task_id")): r
             for r in ((plan.get("cpm_result") or {}).get("schedule") or [])}
    try:
        return quantity_mod.group_rows(plan.get("wbs") or {}, depth, grouping,
                                       schedule=sched)
    except Exception:
        return []


def _group_label(g, n):
    return "%s · %s（%d 项）" % (g.get("work_package") or "",
                                 g.get("工序/工种") or "", n)


def _group_id(ids):
    return ids[0] if len(ids) == 1 else "%s 等%d项" % (ids[0], len(ids))


def _rolled_gantt(plan, leaf_gantt, crit):
    """把逐叶子横道按展示粒度合并：起止 = 组内最早开始 → 最晚完成（时间跨度）。"""
    span = {}
    for g in leaf_gantt:
        span[str(g.get("id"))] = (g.get("start_day") or 0, g.get("end_day") or 0)
    out = []
    for g in rolled_rows(plan):
        ids = [str(i) for i in (g.get("ids") or []) if str(i) in span]
        if not ids:
            continue
        d0 = min(span[i][0] for i in ids)
        d1 = max(span[i][1] for i in ids)
        out.append({
            "id": _group_id(ids), "name": _group_label(g, len(ids)),
            "start_day": d0, "end_day": d1, "duration": max(1, d1 - d0 + 1),
            "crit": any(i in crit for i in ids), "n_tasks": len(ids),
        })
    return out


def _add_days(start, days):
    return (start + datetime.timedelta(days=int(days))).isoformat()


def rolled_gantt_rows(plan):
    """按展示粒度上卷的**甘特行（真实日历 ISO 日期）**；未上卷时返回 []。

    与 :func:`_rolled_gantt` 同一口径，差别只在日期形态：那边给"相对开工日的
    天数"（Word 表 / 内联 SVG 用），ECharts 交互看板要真实日历轴。
    起止仍是组内**最早开始 → 最晚完成**（时间跨度），不重算工期。
    """
    if not _is_rolled_up(plan):
        return []
    sched = {}
    for t in _tasks(plan):
        if isinstance(t, dict):
            sched[str(t.get("task_id"))] = t
    crit = _critical_ids(plan)
    out = []
    for g in rolled_rows(plan):
        ids = [str(i) for i in (g.get("ids") or []) if str(i) in sched]
        if not ids:
            continue
        starts, finishes = [], []
        for i in ids:
            sd = _to_date(sched[i].get("start_date"), None)
            if sd is None:                 # 没开始日期就没法算跨度，跳过这一条
                continue
            starts.append(sd)
            finishes.append(_to_date(sched[i].get("finish_date"), sd))
        if not starts:
            continue
        d0 = min(starts)
        d1 = max(finishes) if finishes else d0
        if d1 < d0:
            d1 = d0
        out.append({
            "id": _group_id(ids), "name": _group_label(g, len(ids)),
            "start": d0.isoformat(), "finish": d1.isoformat(),
            "duration": max((d1 - d0).days + 1, 1),   # 含首尾：日期差 + 1
            "crit": any(i in crit for i in ids), "n_tasks": len(ids),
        })
    return out


# ---------------- 确定性统计视图（甘特 + 人员/设备工日曲线） ----------------

def _site_equipment_map(plan):
    """``{task_id: {资源名: 逐日台/人数}}`` —— 任务里的**场地级设备**投入（塔吊/施工电梯）。

    从 `resource_demand.tasks[*]._site_equipment` 读（资源层写入的唯一真源），与
    `plan_assembler.site_equipment_contrib()` **同一个口径**：场地级设备逐日取 max，
    不按任务叠加（一个工地 1 台塔吊服务所有楼层的所有任务）。
    旧计划没有该键 → 返回 {}，逐日视图与改动前**逐字一致**。
    """
    try:
        from .plan_assembler import site_equipment_contrib
    except Exception:                                     # pragma: no cover
        return {}
    out = {}
    for t in ((plan.get("resource_demand") or {}).get("tasks") or []):
        if not isinstance(t, dict):
            continue
        contrib = site_equipment_contrib(t)
        if contrib:
            out[str(t.get("task_id") or "")] = contrib
    return out


def _compute_view(plan):
    """从 all_tasks_schedule 确定性地算：甘特条 + 每日人员曲线 + 每日设备曲线。

    返回:
      gantt[]: {id,name,start_day,end_day,duration,crit}
      labor_daily[]: {day,date,total,trades:{工种:人数}}   # 含机械配员（泵工/辅助/操作工/司机/信号工）——他们是人
      equip_daily[]: {day,date,total,items:{设备:台数}}     # 纯机械，机械配员已被剔除
      total_days, peak_total, peak_trade

    逐日口径（与 `plan_assembler._daily_peak` **逐字同源**，改一处必须改另一处）：
      · **任务级**资源（泵车/挖掘机/钢筋工…）：按任务**闭区间日期**（`start_date..finish_date`，
        含首尾）逐日 **累加**；日期由 `plan_assembler` 按 `finish = 开工 + (ef - 1)` 生成
        （`ef` 是**半开上界**），所以天数恒 = `ef - es` = 排程跨度；
      · **场地级**设备（塔吊/施工电梯，见 `_site_equipment_map`）：逐日 **max**，
        不跨任务叠加 —— 同一天 5 条任务需要塔吊也只算 1 台；
      · 同名资源同时来自两类时（"司机"既是挖掘机配员、又是塔吊配员）：
        逐日量 = 任务级之和 + 场地级 max，分开记账再相加。
    """
    tasks = _tasks(plan)
    crit = _critical_ids(plan)
    ov = plan.get("overview") or {}
    start = _to_date(ov.get("planned_start_date"))
    site_map = _site_equipment_map(plan)

    gantt = []
    max_day = 0
    for t in tasks:
        sd = _to_date(t.get("start_date"), start)
        fd = _to_date(t.get("finish_date"), sd)
        d0 = max(0, (sd - start).days)
        d1 = max(d0, (fd - start).days)
        max_day = max(max_day, d1)
        gantt.append({
            "id": t.get("task_id"), "name": t.get("task_name"),
            "start_day": d0, "end_day": d1,
            # 天数 = 本条日期跨度（含首尾），**不是**排程行上的 `duration_days`：
            # 后者是"计划/WBS 口径"，实测真计划 180/322 行与自己的日期对不上
            # （`1.5.1` 该字段 1 天、日期跨度 67 天）。同一行里的天数与日期必须同源。
            "duration": max(1, d1 - d0 + 1),
            "crit": t.get("task_id") in crit,
        })

    # 展示粒度：合并横道行。注意**曲线不合并** —— 人员/设备是物理量，
    # 与"给谁看、看多粗"无关，合并了就是改数据。
    if _is_rolled_up(plan):
        _rolled = _rolled_gantt(plan, gantt, crit)
        if _rolled:
            gantt = _rolled

    labor_day = [{} for _ in range(max_day + 1)]
    equip_day = [{} for _ in range(max_day + 1)]
    # 场地级设备单独一本账（逐日取 max），最后与任务级账本相加
    site_labor_day = [{} for _ in range(max_day + 1)]
    site_equip_day = [{} for _ in range(max_day + 1)]
    for t in tasks:
        sd = _to_date(t.get("start_date"), start)
        fd = _to_date(t.get("finish_date"), sd)
        d0 = max(0, (sd - start).days)
        d1 = min(max_day, max(d0, (fd - start).days))
        contrib = site_map.get(str(t.get("task_id"))) or {}
        assigned = dict(t.get("assigned_resources") or {})
        # 场地级设备以 `resource_demand.tasks[*]._site_equipment` 为准：
        # `all_tasks_schedule` 若被工期重算（`recompute.py`）整表重建过，它只从排程行
        # 抄 crew，会把资源层注入的塔吊/施工电梯漏掉 —— 只要这条任务的资源登记还在
        # resource_demand 里，场地级设备就该继续出现在曲线上（绝不静默消失）。
        for name, per_day in contrib.items():
            if name not in assigned:
                try:
                    assigned[name] = int(per_day)
                except (TypeError, ValueError):
                    pass
        for name, per_day in assigned.items():
            try:
                per_day = int(per_day)
            except (TypeError, ValueError):
                continue
            # 场地级那一份从任务级计数里扣出来，避免同一条任务里 1 台塔吊被算两次
            site_part = 0
            if name in contrib:
                try:
                    site_part = int(contrib[name])
                except (TypeError, ValueError):
                    site_part = 0
            flat_part = max(0, per_day - site_part)
            if flat_part > 0:
                bucket = labor_day if _is_labor(name) else equip_day
                for d in range(d0, d1 + 1):
                    bucket[d][name] = bucket[d].get(name, 0) + flat_part
            if site_part > 0:
                bucket = site_labor_day if _is_labor(name) else site_equip_day
                for d in range(d0, d1 + 1):
                    if site_part > bucket[d].get(name, 0):
                        bucket[d][name] = site_part
    for day_buckets, site_buckets in ((labor_day, site_labor_day),
                                      (equip_day, site_equip_day)):
        for d, sb in enumerate(site_buckets):
            for name, v in sb.items():
                day_buckets[d][name] = day_buckets[d].get(name, 0) + v

    def dates_name():
        return [(_add_days(start, d), d, f"D{d}") for d in range(max_day + 1)]

    # 逐日的**场地级设备配员**单独留一份（`site` / `site_trades`）：它不是"按任务叠加"
    # 的口径（逐日 max），而调度器的 `resource_plan.curve_peak_manpower` 不含这一份 ——
    # 两个峰值差多少必须能从数据算出来（见 `_peak_curve_diff`），不许手写死。
    labor_daily = [{"day": d, "date": _add_days(start, d), "total": sum(ld.values()),
                    "site": sum(site_labor_day[d].values()),
                    "site_trades": dict(site_labor_day[d]),
                    "trades": dict(ld)} for d, ld in enumerate(labor_day)]
    equip_daily = [{"day": d, "date": _add_days(start, d), "total": sum(ed.values()),
                    "items": dict(ed)} for d, ed in enumerate(equip_day)]
    peak_total = max((x["total"] for x in labor_daily) or [0])
    peak_trade, peak_trade_val = None, 0
    for x in labor_daily:
        for k, v in x["trades"].items():
            if v > peak_trade_val:
                peak_trade, peak_trade_val = k, v

    # `total_days` 是**末日下标**（0 基）：日期 `start+total_days` 才是末日。凡是"天
    # 数"的地方（环比、日均、总工期回退值）都该用 `total_day_count = max_day + 1`，
    # 否则系统性小 1 天（688 天的一份计划会报 687）。契约上它必须等于
    # `planned_end_date - planned_start_date + 1`（= `overview.total_duration_days`）。
    return {"gantt": gantt, "labor_daily": labor_daily, "equip_daily": equip_daily,
            "total_days": max_day, "total_day_count": max_day + 1,
            "peak_total": peak_total, "peak_trade": peak_trade}


# ============================================================
# T3 · WordExportNode
# ============================================================

class WordExportNode(BaseNode):
    name = "word_export"
    title = "导出 Word 文档"

    def __init__(self, draft=False):
        """draft=True → 出**草案**（文件名带"草案·未审计"，封面盖未审计戳）。

        三轮回审的第 3 轮看的就是这份草案；用户打 Y 之后才由 draft=False 的实例
        整理出最终计划。同一个节点类跑两次，靠 `name` 区分（引擎按 name 存检查点，
        两个实例的 name 必须不同，见 builder 里的装配）。
        """
        super().__init__()
        self.draft = bool(draft)
        if self.draft:
            self.name = "word_draft"
            self.title = "导出 Word 草案（未审计）"

    def run(self, ctx):
        plan = ctx.get("plan_json")
        if not plan:
            return {"_stop": "无 plan_json，跳过 Word 导出"}
        try:
            path = build_plan_docx(plan, draft=self.draft)
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            ctx.setdefault("wbs_warnings", []).append(f"Word 导出失败：{msg}")
            self.emit("node_progress", {"node": self.name, "progress": 100,
                                        "message": f"✖ Word 导出失败（不影响计划）：{msg}"})
            self.done_summary = f"Word 导出失败：{msg}"
            return {}
        artifacts = dict(ctx.get("artifacts") or {})
        artifacts["docx_draft" if self.draft else "docx"] = path
        if not self.draft:
            artifacts["docx"] = path
        # 维护/索引必须跟随**本次实际写入的目录**，而不是 config.DELIVERABLES_DIR：
        # 测试与被复用的构建脚本会把 _plan_dir 重定向到临时目录，若这里仍读常量，
        # 就会去改写真实「输出结果/」的索引页、并按 MAX_KEEP 剪掉真实运行目录
        # —— 隔离形同虚设（第 37 轮实测踩到：真实看板被覆盖、凭空多出测试运行目录）。
        _maintain(Path(path).parent.parent)
        self.done_summary = ("已导出 Word %s：%s"
                             % ("草案（未审计）" if self.draft else "定稿", path))
        return {"artifacts": artifacts}


# ============================================================
# 「数据来源与置信度」章节
#
# 背景（用户实测）：计划 JSON 里 `meta.credibility` / `meta.norm_coverage` /
# `meta.data_sources` / `meta.kb_warnings` / `meta.schedule_versions` /
# `meta.boundary_conditions` 什么都有，交付物里**一个字都不印** ——
# 搜「置信」「来源」「覆盖率」命中 0。用户拿到的是一份看不出哪些数是算的、
# 哪些是估的计划。
#
# 三条铁律（本段就是在守它们）：
#   ① 数字**全部**来自 plan，一个都不新造；拿不到就整行/整块不输出（优雅降级）；
#   ② 只读，不改 plan；
#   ③ 不同口径**各自显式标注口径名**，绝不都叫「峰值」（见 _manpower_peaks）。
# ============================================================

# 数据来源代码 → 人话（认不出来就原样打代码，不编）。
_SOURCE_LABELS = {
    "AI_ESTIMATE_V1": "AI 经验估算",
    "USER": "用户提供",
    "PROJECT_PARAMS": "项目参数",
}
# 编码前缀 → 人话（GD_* 国标、LD_* 地方定额、LN_*/KB_* 知识库定额）。
_SOURCE_PREFIX_LABELS = (
    ("GD_", "国标定额"),
    ("LD_", "地方定额"),
    ("LN_", "知识库劳动定额"),
    ("KB_", "知识库"),
    ("AI_", "AI 经验估算"),
    ("USER", "用户提供"),
)
# 溯源里的 confidence 可能是中文也可能是英文，统一成人话。
_CONFIDENCE_LABELS = {
    "high": "高", "mid": "中", "medium": "中", "low": "低",
    "HIGH": "高", "MID": "中", "MEDIUM": "中", "LOW": "低",
    "高": "高", "中": "中", "低": "低",
}


def _fnum(v, nd=1):
    """数字 → 紧凑字符串；非数字返回 None（调用方据此整行不输出）。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f == int(f):
        return str(int(f))
    return ("%%.%df" % nd) % f


def _pct_of_ratio(v):
    """credibility 里的占比（0.28）→ 28.0%；已经是百分数（28）也认。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if 0 <= f <= 1:
        f *= 100.0
    return "%.1f%%" % f


def _source_label(code):
    """来源代码 → 人话。优先精确表，再按前缀，最后原样（绝不编造）。"""
    s = str(code or "").strip()
    if not s:
        return ""
    if s in _SOURCE_LABELS:
        return _SOURCE_LABELS[s]
    for pre, label in _SOURCE_PREFIX_LABELS:
        if s.startswith(pre):
            return label
    return s


def _source_type_of(code):
    s = str(code or "").strip()
    if not s:
        return ""
    for pre, label in _SOURCE_PREFIX_LABELS:
        if s.startswith(pre):
            return label
    return ""


def _norm_unit_family(u):
    """单位 → 族；解析不出返回空串（= 不比，绝不误报）。"""
    try:
        from .. import kb_units
        return kb_units.unit_family(kb_units.normalize_unit(u)) or ""
    except Exception:
        return ""


def _unit_text(u):
    """单位规范化写法（m3 → m³）；失败就原样。"""
    try:
        from .. import kb_units
        return kb_units.normalize_unit(u) or str(u or "")
    except Exception:
        return str(u or "")


def _norm_unit_denominator(norm_unit):
    """定额单位的分母：工日/m² → m²；工日/项 → 项；解析不出返回空串。"""
    try:
        from .. import kb_units
        return kb_units.parse_norm_unit(norm_unit).get("denominator") or ""
    except Exception:
        s = str(norm_unit or "")
        return s.split("/", 1)[1].strip() if "/" in s else ""


def _leaf_bindings(plan):
    """{task_id: WBS 叶子}。

    降级清单要读 `norm_binding` / `raw_quantity_basis`，而这两样只挂在 WBS 叶子上
    （all_tasks_schedule 是排程行，按契约不带定额锚定）。
    """
    out = {}
    phases = ((plan.get("wbs") or {}).get("phases") or [])
    for ph in phases:
        if not isinstance(ph, dict):
            continue
        for wp in (ph.get("work_packages") or []):
            if not isinstance(wp, dict):
                continue
            for sp in (wp.get("sub_packages") or []):
                if isinstance(sp, dict) and sp.get("id") is not None:
                    out[str(sp.get("id"))] = sp
    return out


def _unit_assumed_ids(plan):
    """``{task_id}``：单位已按**写明假定的换算**算出了资源的任务。

    判据是任务级字段 `resource_demand.tasks[*]._unit_assumed`（资源层写的完整换算
    过程与结果，如「按 AI 假定墙厚 200mm 换算：1420 m² × 0.2 m = 284 m³」），
    **不按任务名硬编码**。

    ⚠ 为什么必须单独拎出来：这类任务的工程量单位族与定额分母单位族**仍然不同**
    （m² vs m³），于是 `_norm_degradations` 的历史口径会把它们也列进
    「已降级为「仅参考」」清单 —— 而同一份文档的「依据 / 资源」列又写着
    「单位换算按 AI 假定…；班组 架子工 9 人（99 工日）」。实测计划
    `计划_plan_sample3_after_fix` 里 18 条 ALC 墙板安装同时出现在两处，
    两份互相矛盾的结论一起印在 Word 里，用户读到的就是"这计划自己打架"。
    判据取任务级字段而不是任务名：换一份计划、换一种构件同样成立。
    """
    out = set()
    for t in ((plan.get("resource_demand") or {}).get("tasks") or []):
        if isinstance(t, dict) and t.get("_unit_assumed") and t.get("task_id") is not None:
            out.add(str(t.get("task_id")))
    return out


# 章节标题（Word 与看板共用同一份原文，避免两处措辞漂移）。
# ⚠ 5b 节的标题 / 引导句 / 脚注**不再写死**：换算参数的来源逐条不同（定额条件档位 /
# AI 估算 / 用户给定 / 未记录），措辞必须按该节实际出现的来源生成 —— 见
# `assumed_section_title` / `assumed_section_lead` / `assumed_section_foot`（写死的
# 「已按 AI 假定换算」正是那处**虚假溯源**）。
NORM_DEGRADED_TITLE = "5. 单位与定额降级清单（已降级为「仅参考」）"


def _norm_degradations(plan, cap=10):
    """单位/定额降级清单（WS5 要求）。

    三个来源，全部是**既有字段**，一条都不新造：
      · `_norm_flagged` / `_warning`（绑定层显式标记的降级行）；
      · `norm_binding.raw_quantity_basis`（原始基准 10/100/1000，只作溯源）；
      · 叶子工程量单位族 ≠ 定额分母单位族（例：根 vs 工日/m³）
        → 该定额无法直接换算，只能「仅参考」。

    **排除**已按写明假定换算出资源的任务（`_unit_assumed`，见 `_unit_assumed_ids`）：
    它们算得出来，就不是"仅参考"。

    政策变更（2026-09-20）：旧政策把 **AI 经验估算定额**也塞进这份"降级"清单，原文是
    「AI 估算定额（只作参考，不用来算班组）」—— 新政策下 AI 来源本身不再是拦截理由，
    这里若读到 AI 字样的标记，只换成如实描述（依据是 AI 经验估算定额、本次未据此计算
    班组），**绝不复述旧口径**；新计划里真的按 AI 定额算了班组的任务不会带这种标记，
    因此不会出现在本节（见 `_degraded_reason`）。

    返回 (明细[:cap], 总数)；总数不截断。
    """
    bindings = _leaf_bindings(plan)
    assumed = _unit_assumed_ids(plan)
    rows, seen = [], set()
    for t in _tasks(plan):
        if not isinstance(t, dict):
            continue
        tid = str(t.get("task_id"))
        if tid in seen or tid in assumed:
            continue
        leaf = bindings.get(tid) or {}
        nb = leaf.get("norm_binding")
        nb = nb if isinstance(nb, dict) else {}
        name = t.get("task_name") or leaf.get("name") or tid
        reason = None

        flag = t.get("_norm_flagged") or leaf.get("_norm_flagged")
        if flag:
            reason = str(flag)
        if not reason:
            warn = t.get("_warning") or leaf.get("_warning")
            if warn:
                reason = str(warn)
        if not reason:
            lu = _unit_text(leaf.get("unit") or t.get("unit"))
            nu = _norm_unit_denominator(nb.get("unit"))
            fam_l, fam_n = _norm_unit_family(lu), _norm_unit_family(nu)
            if lu and nu and fam_l and fam_n and fam_l != fam_n:
                reason = "定额单位 %s 与工程量单位 %s 不一致" % (nu, lu)
        if not reason:
            continue
        seen.add(tid)
        # 交付物里显示的那一格原因（政策变更 2026-09-20）：旧政策措辞一律不许复述。
        reason = _degraded_reason(reason)
        rows.append({"task_id": tid, "task_name": name, "reason": reason,
                     "ai": _ai_norm_text_marks_ai(reason)})
    cap = int(cap) if cap else 0
    return (rows[:cap] if cap else rows), len(rows)


# ══════════════════════════════════════════════════════════════════════════════
# 换算参数**来源** → 用户可见文案（**唯一真源**，5b 节 / 「依据列」 / facts 三处复用）
# ------------------------------------------------------------
# 问题（虚假溯源）：换算参数过去一律被说成「AI 假定」。D5/P3 之后面积↔体积的墙厚
# 是从**定额行适用条件的厚度档位**解析出来的（如条件写 `≤200mm` → 取 0.2 m），
# 来源标 `norm_condition` —— 把它标成 AI 估算，与"把 AI 估算藏起来"是同一类错误
# （验收要求第 1 条：每个数据项逐行可溯源，AI 估算必须标注；反过来也成立）。
#
# 分流规则（**以数据为准，不猜**）：
#   norm_condition → 定额条件档位换算参数（依据定额行适用条件，非 AI 估算）
#   ai_estimate    → AI估算换算参数（仅当来源键真的写着 ai_estimate）
#   user           → 用户给定换算参数
#   text           → 文本抽取换算参数
#   其它 / 缺失     → 换算参数来源未记录（**绝不默认归到 AI**）
# ══════════════════════════════════════════════════════════════════════════════
UNIT_ASSUMPTION_SOURCE_LABELS = {
    "norm_condition": "定额条件档位换算参数（依据定额行适用条件，非 AI 估算）",
    "ai_estimate": "AI估算换算参数",
    "user": "用户给定换算参数",
    "text": "文本抽取换算参数",
}
UNIT_ASSUMPTION_SOURCE_UNRECORDED = "换算参数来源未记录"
#: 只有这些来源才允许在文案里出现「AI」二字（判据层用，避免别处再写死）。
UNIT_ASSUMPTION_AI_SOURCES = ("ai_estimate",)


def unit_assumption_source(leaf, task=None):
    """换算参数**是谁给的** → ``(source, label, evidence)``。

    `source` 取 `ctx_source`（P3 留痕键）：优先 WBS 叶子的 `norm_binding`，
    其次任务行自带的同名键（旧计划 / 别的搬运路径）。两个都没有 → `""` +
    「换算参数来源未记录」——**绝不默认归到 AI**。

    `evidence` 是依据原文：`unit_assumption.note` 优先（那里写清"厚度取 0.2 m，
    来自定额行适用条件的厚度档位"），其次定额行 `condition_text`（如
    「加气混凝土砌块，≤200mm」）；取不到就是空串，一个字都不编。
    """
    lf = leaf if isinstance(leaf, dict) else {}
    tk = task if isinstance(task, dict) else {}
    binding = lf.get("norm_binding") if isinstance(lf.get("norm_binding"), dict) else {}
    raw = ""
    for holder in (binding, tk, lf):
        v = holder.get("ctx_source") if isinstance(holder, dict) else None
        if v is not None and str(v).strip():
            raw = str(v).strip()
            break
    source = raw.lower()
    if source in UNIT_ASSUMPTION_SOURCE_LABELS:
        label = UNIT_ASSUMPTION_SOURCE_LABELS[source]
    elif source:
        label = "换算参数来源 %s" % raw          # 认不出的来源原样打出来，不硬套
    else:
        label = UNIT_ASSUMPTION_SOURCE_UNRECORDED
    asm = binding.get("unit_assumption") if isinstance(binding.get("unit_assumption"), dict) else {}
    evidence = ""
    for cand in (asm.get("note"), binding.get("condition_text"), tk.get("condition_text")):
        if cand is not None and str(cand).strip():
            evidence = str(cand).strip()
            break
    return source, label, evidence


def assumed_source_labels(rows):
    """5b 节这些行里**实际出现过**的来源标签（按首次出现顺序去重，取不到就是空表）。"""
    seen, out = set(), []
    for r in (rows or []):
        if not isinstance(r, dict):
            continue
        lab = str(r.get("source_label") or "").strip()
        if lab and lab not in seen:
            seen.add(lab)
            out.append(lab)
    return out


def assumed_section_title(rows):
    """5b 节标题 —— 只有**全部行**都是 AI 来源时才敢写「AI 假定」。"""
    labels = assumed_source_labels(rows)
    if labels and all(str(r.get("source") or "") in UNIT_ASSUMPTION_AI_SOURCES
                      for r in (rows or []) if isinstance(r, dict)):
        return "5b. 已按 AI 假定换算（非降级）"
    return "5b. 已按写明换算参数换算（非降级）"


def assumed_section_lead(rows):
    """5b 节引导句 —— 按这些行**真实的**来源说，不笼统说成 AI。"""
    labels = assumed_source_labels(rows)
    if not labels:
        return ""
    head = ("下列任务的工程量单位与定额分母单位不同，但已按写明的换算参数完成换算"
            "（换算参数的来源逐条列在表里：%s）" % "；".join(labels))
    return (head + "；依据原文写在进度计划表的「依据 / 资源」列，班组与工日已照此算出 —— "
            "它们不属于降级、定额不按「仅参考」处理，故不在上面的降级清单里：")


def assumed_section_foot(rows):
    """5b 节脚注 —— 只有 AI 来源的行才说「由模型给出、非规范来源」。"""
    labels = assumed_source_labels(rows)
    if not labels:
        return ""
    _common = "进入本节的任务不再计入降级清单的条数。"
    if all(str(r.get("source") or "") in UNIT_ASSUMPTION_AI_SOURCES
           for r in (rows or []) if isinstance(r, dict)):
        return ("上面每条都是「算出来了，但用的是 AI 假定换算」；假定值由模型给出、非规范来源，"
                "拿到规范换算参数（如墙厚 / 单根体积）后应替换重算。" + _common)
    return ("上面每条都是「算出来了，但用的是非用户实测的换算参数」（来源见逐条标注）；"
            "拿到用户实测参数后应替换重算。" + _common)


def _assumed_source_cell(row):
    """5b 节「换算参数来源」单元格：来源标签 + 依据原文（取不到就不写依据，不编）。"""
    if not isinstance(row, dict):
        return ""
    label = str(row.get("source_label") or "").strip()
    ev = str(row.get("evidence") or "").strip()
    return ("%s；依据：%s" % (label, ev)) if (label and ev) else (label or ev)


def _norm_assumed_rows(plan, cap=10):
    """已按写明换算参数换算的任务明细（`5b` 节）。
    与 `_norm_degradations` **互斥**：同一条任务不可能既"算出来了"又"仅参考"。
    任务名与假定原文都从计划里取（`_unit_assumed` 的首句），取不到就写「—」，
    绝不编数。

    **换算参数来源按 `ctx_source` 分流**（P3 / D5）：面积↔体积的墙厚现在从**定额行
    适用条件的厚度档位**解析（如条件写 `≤200mm` → 取 0.2 m），来源标 `norm_condition`，
    文案必须写「定额条件档位换算参数…非 AI 估算」；只有真的由 AI 猜出来的才写
    「AI估算换算参数」；取不到来源键就如实写「换算参数来源未记录」，
    **绝不默认归到 AI**（把定额条件推出的值谎报成 AI 估算，与把 AI 估算藏起来是同一类
    虚假溯源 —— 验收要求第 1 条是「每个数据项逐行可溯源，AI 估算必须标注」，
    反过来"不是 AI 的也不许标成 AI"同样成立）。

    返回 (明细[:cap], 总数)；明细每行：
    ``{task_id, task_name, reason, source, source_label, evidence}``。
    """
    bindings = _leaf_bindings(plan)
    rows = []
    for t in ((plan.get("resource_demand") or {}).get("tasks") or []):
        if not isinstance(t, dict) or not t.get("_unit_assumed"):
            continue
        tid = str(t.get("task_id"))
        leaf = bindings.get(tid) or {}
        source, label, evidence = unit_assumption_source(leaf, t)
        rows.append({"task_id": tid,
                     "task_name": t.get("task_name") or leaf.get("name") or tid,
                     "reason": str(t.get("_unit_assumed") or ""),
                     "source": source, "source_label": label,
                     "evidence": evidence})
    cap = int(cap) if cap else 0
    return (rows[:cap] if cap else rows), len(rows)


# ══════════════════════════════════════════════════════════════════════════════
# 终版修改 · WS3（接口冻结 §9）：D4 无定额依据暴露 / D6 来源档次 + 关键路径规范
# 依据覆盖率 / D7 人工覆盖留痕 / 口径换算留痕
#
# 全部沿用既有「结构性保证」写法：独立判据 + 确定性追加 + `ctx["wbs_warnings"]`
# （对照 `_ensure_confidence_section` / `_ensure_equipment_section` 等）。
#
# ⚠ 铁律：判据一律**取数据**（`_norm_applied` 是否为空、`norm_binding.not_usable_reason`、
# `_resource_source[工种].origin`、`_norm_flagged`），**绝不写死 task_id** —— 合同里点名的
# 那 10 条只是改造后的当前基线，知识库/绑定修好之后这个集合必须自己变小。
# ══════════════════════════════════════════════════════════════════════════════

# ---- D6 来源档次（合同 §9.2 的五个档次，逐字照抄，不新造文案）----
NORM_TIER_MACHINE = "规范台班"
NORM_TIER_LABOR = "规范人工"
NORM_TIER_AI = "AI 定额（已审）"
NORM_TIER_MODEL = "模型估算"
NORM_TIER_NONE = "无依据"
NORM_TIER_ORDER = (NORM_TIER_MACHINE, NORM_TIER_LABOR, NORM_TIER_AI,
                   NORM_TIER_MODEL, NORM_TIER_NONE)
# 「规范依据」只认真人规范定额两档：AI 经验估算定额**无规范依据**（政策文案自己就这么写），
# 所以它不进覆盖率的分子，但仍然是可计算的一档（在表格里如实单列）。
NORM_TIER_IS_SPEC = (NORM_TIER_MACHINE, NORM_TIER_LABOR)

NORM_TIER_TITLE = "6. 定额来源档次与规范依据覆盖率"


def _norm_tier_lead(counts):
    """档次表的口径说明 —— **只解释这份计划里真的出现过的档次**。

    为什么按出现情况拼：计划里一条 AI 经验估算定额都没有时，正文里却大写"AI 经验估算定额"，
    会让读者以为这份计划里有 AI 定额（`test_delivery_confidence_board` 那条"没有 AI 数据时
    一个字都不许出现"的回归门钉的正是这个）。
    """
    parts = ["每条工序的定额来源档次按数据判定（`_norm_applied` 的来源与 `mode`），"
             "不按任务名硬编码："]
    if counts.get(NORM_TIER_MACHINE):
        parts.append("「%s」= 由规范台班定额决定工期" % NORM_TIER_MACHINE)
    if counts.get(NORM_TIER_LABOR):
        parts.append("「%s」= 由规范人工定额决定工期" % NORM_TIER_LABOR)
    if counts.get(NORM_TIER_AI):
        parts.append("「%s」= %s（已按政策照用并逐条标注）" % (NORM_TIER_AI, AI_NORM_LABEL))
    if counts.get(NORM_TIER_MODEL):
        parts.append("「%s」= 计划里没有可用定额，工期与人数来自模型" % NORM_TIER_MODEL)
    if counts.get(NORM_TIER_NONE):
        parts.append("「%s」= 连资源记录都没有" % NORM_TIER_NONE)
    return "；".join(parts) + "。"

NORM_COVERAGE_LABEL = "关键路径规范依据覆盖率"
NORM_COVERAGE_TARGET_PCT = 80.0
# 覆盖率**挂在既有 `meta.norm_coverage` 下面**（合同 §9.2：不许新增 meta 顶层键）。
NORM_COVERAGE_META_KEY = "critical_norm_coverage"

# ---- D4 无定额依据（合同 §9.1）----
NORM_MISSING_TITLE = "7. 无定额依据工序（工期与人数来自模型）"
NORM_MISSING_MARKER = "本行无定额依据"
NORM_MISSING_LEAD = ("下列工序在计划里**没有可用的规范定额行**（`_norm_applied` 为空）："
                     "工期沿用模型估算（WBS 目标 → 排程），人数来自模型或施工组织层。"
                     "逐条写出原因，便于补齐知识库 / 修好绑定后重算：")
NORM_MISSING_FOOT = ("上面的条数**从数据算**（判据是 `_norm_applied` 为空），不是固定名单；"
                     "补齐定额或修好绑定后这些行会自动消失。")
# 原因归类：合同 §9.1 要求的五个类别 + 数据里确实存在的两类拦截。
# 匹配用**稳定子串**（先去空格），顺序即优先级 —— 具体拦截原因（口径/绑定/单位）优先于
# 笼统的「KB 无定额行」，否则「单位不可用：…」会被误归到「KB无定额行」。
_NORM_REASON_RULES = (
    ("口径无法对齐", "口径无法对齐"),
    ("活动绑定不一致", "活动绑定不一致"),
    ("绑定不一致", "活动绑定不一致"),
    ("定额口径不符", "定额口径不符"),
    ("口径不符", "定额口径不符"),
    ("口径不一致", "定额口径不符"),
    ("单位不可用", "单位不可用"),
    ("单位不一致且不可换算", "单位不可用"),
    ("不可换算", "单位不可用"),
    ("缺换算参数", "单位不可用"),
    ("人工否决", "人工否决"),
    ("量级不可信", "工程量量级不可信"),
    ("无定额行", "KB无定额行"),
    ("无定额绑定", "KB无定额行"),
    ("未找到定额", "KB无定额行"),
    ("无匹配定额", "KB无定额行"),
    ("AI估算定额", "KB无定额行"),
    ("L4默认定额行", "KB无定额行"),
)
NORM_MISSING_REASON_CODES = tuple(dict.fromkeys(c for _k, c in _NORM_REASON_RULES))

# ---- D7 人工覆盖入口（合同 §9.3）----
# 编号从 8 让到 9：域 5 的「工程量来源与未入树清单」按设计 §5.6 占 8（它插在
# 无定额依据工序之后、本节之前）。本节**没有覆盖文件时本就不出现**，编号唯一真源是这里。
NORM_OVERRIDE_TITLE = "9. 人工覆盖留痕（D7 复核入口）"
NORM_OVERRIDE_FILE = "定额覆盖.json"
NORM_OVERRIDE_ALT_SUFFIX = "_norm_override.json"
NORM_OVERRIDE_LEAD = ("下列条目的人工覆盖取自覆盖文件（%s）：原值 → 覆盖值 → 覆盖人 / 时间 / 说明。"
                      "**没有覆盖文件时这一节一个字都不出**，计划行为与覆盖前逐字一致。")

# ---- 口径换算留痕（合同 §9.4 / §2，字段由 WS1 写）----
NORM_BASIS_TITLE = "5d. 口径换算留痕（工程量 → 定额分母）"
NORM_BASIS_LEAD = ("下列工序的工程量口径与定额分母口径不一致，已按 `norm_binding.basis_adjust` "
                   "留痕（原口径 → 定额口径 → 换算方式 → 换算后工程量）；标「口径未确认」的"
                   "表示口径尚未人工确认，未阻断计算，但需复核。"
                   "（WS1 未写这些字段时本节一个字都不出。）")


# ---- 域 5：工程量来源与「未入树清单」（设计 §14.1 裁决 #6，硬要求）----
# 数据源 = 节点 `quantity_fill` 写进 `meta.quantity_coverage` 的覆盖表（设计 §4.4）。
# ⚠️ 该键**不存在**（节点没接线 / 老计划）⇒ 本节**优雅缺席**：一块都不加、不抛异常、
#    不留空壳、一个 "None" 都不印（见 `quantity_coverage_blocks` 的第一件事）。
QUANTITY_COVERAGE_TITLE = "8. 工程量来源与未入树清单"
#: 明细最多列几条，其余指向 `meta.quantity_coverage.not_in_tree`（裁决 #6：前 50 条）。
QUANTITY_NOT_IN_TREE_LIMIT = 50
#: 「为什么闭集里有、计划里没有」——裁决 #6 ③ 要求的一句人话。
QUANTITY_ABSENCE_WHY = (
    "**为什么有工序没进计划**：节拍引擎只对 4 个分部（地下室结构 / 地上主体结构 / "
    "二次结构与砌体 / 装饰装修）做节拍展开，逐层铺成叶子工序；其余分部（脚手架、场地准备、"
    "桩基等）按**工作包 / 单条列示**。所以「闭集里有、计划里没有」**不等于这活不用干** —— "
    "它们的工程量在下面逐条给出，补进 WBS 后即可进入定额锚定。"
)
#: 来源代码 → 人话（键与节点 §4.4 `summary.by_source` 一一对应；不认识的代码原样印）。
QUANTITY_SOURCE_LABELS = {
    "ratio": "占比表拆分",
    "tree": "既有参数 / 系数路径",
    "llm": "模型补量",
    "user": "用户指定",
    "none": "未取到量",
}


def _qc_int(v):
    """能当整数用吗（`bool` 不算）；取不到 → None（调用方据此降级，绝不猜 0）。"""
    if isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _qc_txt(v):
    """一格人话文本：`None` / 空串 → 「—」（交付物里**不许**出现 "None"）。

    ⚠️ 为什么必须在这里收口：Word 的 `add_kv` / `add_grid` 只兜得住**真 None**
    （写的是 `"" if v is None else str(v)`）—— 一旦在传进去之前被 f-string 串成了
    字符串 `"None"`，它就会原样印进交付物。所以本节的每个格子都过这一道。
    """
    if v is None:
        return "—"
    s = str(v).strip()
    return s or "—"


def _qc_qty(v):
    """工程量：数字走 `_fnum`（紧凑、不带小数点尾巴），取不到 → 「—」。"""
    n = _fnum(v)
    return n if n is not None else _qc_txt(v)


def _qc_rows(qc):
    """覆盖表的**逐条扁平列表**：`l4_rows` 优先，退化到 `l4` 字典的值。"""
    rows = qc.get("l4_rows")
    if isinstance(rows, list):
        rows = [r for r in rows if isinstance(r, dict)]
        if rows:
            return rows
    l4 = qc.get("l4")
    if isinstance(l4, dict):
        return [r for r in l4.values() if isinstance(r, dict)]
    return []


def _qc_group_key(rec):
    """该条 L4 的 **L3 工种** 名：`work_type_name` → `work_type_id` → 「未注明工种」。"""
    for k in ("work_type_name", "work_type_id", "work_type"):
        v = rec.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    return "未注明工种"


def _qc_groups(rows, not_in_tree):
    """按 L3 工种分组 → `{工种: [闭集数, 进树数, 未入树数]}`（判据全取数据）。

    口径：闭集数 / 进树数取自逐条表（`in_tree` 真值）；未入树数取自节点那份
    `not_in_tree` 清单 —— 它才是节点**逐条表态过**的名单，两处口径万一漂移，
    宁可显示"未入树比闭集差"也不漏报。
    """
    g = {}
    for r in rows:
        cell = g.setdefault(_qc_group_key(r), [0, 0, 0])
        cell[0] += 1
        if r.get("in_tree"):
            cell[1] += 1
    for r in not_in_tree:
        cell = g.setdefault(_qc_group_key(r), [0, 0, 0])
        cell[2] += 1
    return g


def _quantity_coverage_data(plan):
    """`meta.quantity_coverage` → 本节要用的归一数据；键缺失 / 空 / 无一行数据 → `None`。"""
    meta = plan.get("meta") if isinstance(plan.get("meta"), dict) else {}
    qc = meta.get("quantity_coverage")
    if not isinstance(qc, dict) or not qc:
        return None
    rows = _qc_rows(qc)
    nits = qc.get("not_in_tree")
    nits = [r for r in nits if isinstance(r, dict)] if isinstance(nits, list) else []
    if not rows and not nits:
        return None
    summary = qc.get("summary") if isinstance(qc.get("summary"), dict) else {}
    closed = _qc_int(summary.get("closed_total"))
    if closed is None and rows:
        closed = len(rows)
    in_tree = _qc_int(summary.get("in_tree"))
    if in_tree is None and rows:
        in_tree = sum(1 for r in rows if r.get("in_tree"))
    derived = (closed - in_tree) if (closed is not None and in_tree is not None) else None
    absent = derived if derived is not None else (len(nits) or None)
    return {"rows": rows, "not_in_tree": nits, "summary": summary,
            "closed": closed, "in_tree": in_tree, "absent": absent}


def quantity_coverage_blocks(plan):
    """域 5 的「工程量来源与未入树清单」块（设计 §14.1 裁决 #6，硬要求）。

    三样东西**必须在截断之前**依次给出：
      ① 总数：形如「闭集 N 个 L4，进树 M 个，未入树 N−M 个」；
      ② 按 **L3 工种** 的分布（闭集 / 进树 / 未入树三列，例：「脚手架 31 个 L4，进树 1 个」）；
      ③ 一句人话说明**为什么**会有工序没进树（节拍引擎只对 4 个分部做节拍展开）。
    **然后**才列明细：只列前 `QUANTITY_NOT_IN_TREE_LIMIT`（=50）条，其余指向
    `meta.quantity_coverage.not_in_tree`。

    `meta.quantity_coverage` 不存在（节点未接线 / 老计划）→ 返回 `[]`：本节**整节缺席**，
    不抛异常、不留空壳、不印 "None"。
    """
    d = _quantity_coverage_data(plan)
    if d is None:
        return []
    rows, nits = d["rows"], d["not_in_tree"]
    closed, in_tree, absent = d["closed"], d["in_tree"], d["absent"]

    # ---- ① 总数（一句话给全；缺数就写「—」，绝不写 null / None）----
    total_txt = ("闭集 %s 个 L4，进树 %s 个，未入树 %s 个"
                 % (_qc_txt(closed), _qc_txt(in_tree), _qc_txt(absent)))
    kv = [("覆盖范围（闭集 → 计划）", total_txt)]

    # 来源分档（有才出；占比以闭集为准，闭集取不到就不写百分比）
    by_source = d["summary"].get("by_source")
    if isinstance(by_source, dict):
        for code in ("ratio", "tree", "llm", "user", "none"):
            n = _qc_int(by_source.get(code))
            if not n:
                continue
            pct = ("（%.1f%%）" % (100.0 * n / closed)) if closed else ""
            kv.append(("其中：%s" % QUANTITY_SOURCE_LABELS.get(code, _qc_txt(code)),
                       "%d 个 L4%s" % (n, pct)))
    unit_unresolved = _qc_int(d["summary"].get("unit_unresolved"))
    if unit_unresolved:
        kv.append(("单位未落实", "%d 个 L4" % unit_unresolved))
    # ★裁决 #2：`unit_evidence == "dict+norm_differs"` 的条数必须露在交付物上
    #   （唯一真实案例 `GD_A13_截凿桩头`）。节点自己在 `summary.dict_norm_differs` 里
    #   已算好（判据同源，见 quantity_agent `_summary`）；取不到就按逐条表的
    #   `unit_evidence` 现数一遍 —— 不依赖单一键名，两边都不会漏。
    _dn_n = _qc_int(d["summary"].get("dict_norm_differs"))
    _dn_rows = [r for r in rows if str(r.get("unit_evidence") or "") == "dict+norm_differs"]
    if _dn_n is None:
        _dn_n = len(_dn_rows)
    if _dn_n:
        _names = "、".join(_qc_txt(r.get("activity_name") or r.get("activity_id"))
                          for r in _dn_rows[:5]) if _dn_n == len(_dn_rows) else ""
        kv.append(("字典单位与定额单位不一致",
                   "%d 个 L4%s" % (_dn_n, ("（%s）" % _names) if _names else "")))

    blocks = [("h3", QUANTITY_COVERAGE_TITLE), ("kv", kv)]

    # ---- ② 按 L3 工种的分布（未入树多的排前面，让问题最集中的工种先被看见）----
    groups = _qc_groups(rows, nits)
    if groups:
        _grows = [[k, str(v[0]), str(v[1]), str(v[2])]
                  for k, v in sorted(groups.items(),
                                     key=lambda kv_: (-kv_[1][2], -kv_[1][0], kv_[0]))]
        blocks.append(("para", "**未入树工序的工种分布**（按 L3 工种分组，只列闭集里确实有 L4 的工种；"
                               "「进入计划」= 已铺进 WBS 树的 L4 数）："))
        blocks.append(("grid", (["工种（L3）", "闭集 L4", "进入计划", "未入树"], _grows)))

    # ---- ③ 一句人话：为什么会有工序没进树 ----
    blocks.append(("para", QUANTITY_ABSENCE_WHY))

    # ---- 明细：前 50 条 + 其余见 meta（截断在①②③之后，顺序不可调）----
    if nits:
        shown = nits[:QUANTITY_NOT_IN_TREE_LIMIT]
        blocks.append(("para", "下列 %s 个 L4 **在可用范围内、但没有进入 WBS**（明细只列前 %d 条）："
                               % (_qc_txt(absent if absent is not None else len(nits)), len(shown))))
        blocks.append(("grid", (["工序 ID", "工序", "工种（L3）", "工程量", "单位", "来源", "未入树原因"],
                                [[_qc_txt(r.get("activity_id")), _qc_txt(r.get("activity_name")),
                                  _qc_txt(r.get("work_type_name") or r.get("work_type_id")),
                                  _qc_qty(r.get("quantity")), _qc_txt(r.get("unit")),
                                  QUANTITY_SOURCE_LABELS.get(str(r.get("source") or ""),
                                                             _qc_txt(r.get("source"))),
                                  _qc_txt(r.get("reason"))]
                                 for r in shown])))
        if absent is not None and absent > len(shown):
            blocks.append(("para", "（上面只列了前 %d 条；其余 %d 条见计划 "
                                   "`meta.quantity_coverage.not_in_tree`）"
                                   % (len(shown), absent - len(shown))))

    # 守恒口径（节点写了才出）：闭集 = 进树 + 未入树 + 用户未识别……等差异说明
    note = d["summary"].get("conservation_note")
    if note is not None and str(note).strip():
        blocks.append(("para", "**守恒口径**：" + str(note).strip()))
    return blocks


def _rd_of(rd):
    """`resource_demand.tasks[*]` 归一（非 dict / 空 → 空 dict）。"""
    return rd if isinstance(rd, dict) else {}


def _norm_tier_of(rd):
    """该工序的**定额来源档次**（合同 §9.2 D6）—— 判据全部取数据。

    返回 ``(档次, 备注)``：档次 ∈ `NORM_TIER_ORDER`；备注是附加说明（可空）。

    判据优先级：
      ① 有 `_norm_applied` 且它本身是 AI 来源（`_ai_norm_state(...)["norm_ai"]`）
         → 「AI 定额（已审）」；
      ② 有 `_norm_applied` 且 `mode == "machine"` → 「规范台班」；
      ③ 有 `_norm_applied`（其余 mode，含 labor / mixed）→ 「规范人工」；
      ④ 没有 `_norm_applied`（= 工期与人数不是定额算出来的）：
         有资源记录 → 「模型估算」；连资源都没有 → 「无依据」。

    ⚠ ④ 刻意与「依据是 AI 经验估算定额」分开：`_norm_applied` 为空时，那条 AI 定额
    **这一回并没有参与计算**（旧计划里 134 条就是这个状态），把它标成"已审"就是谎报。
    """
    rd = _rd_of(rd)
    na = rd.get("_norm_applied")
    na = na if isinstance(na, dict) else {}
    if na:
        st = _ai_norm_state(rd)
        if st and st.get("norm_ai"):
            return NORM_TIER_AI, AI_NORM_LABEL
        if str(na.get("mode") or "").strip().lower() == "machine":
            return NORM_TIER_MACHINE, ""
        return NORM_TIER_LABOR, ""
    if _rd_has_resources(rd):
        return NORM_TIER_MODEL, ""
    return NORM_TIER_NONE, ""


def _rd_has_resources(rd):
    """该任务有没有资源记录（班组 / 机械 / 工日）—— 「模型估算」与「无依据」的分界。"""
    rd = _rd_of(rd)
    res = rd.get("resources")
    if isinstance(res, dict) and res:
        return True
    for k in ("_crew", "_site_equipment"):
        v = rd.get(k)
        if isinstance(v, dict) and v:
            return True
        if isinstance(v, (list, tuple)) and v:
            return True
    return False


def _norm_tier_counts(plan):
    """``(counts, total)``：各来源档次的工序条数（逐条判据见 `_norm_tier_of`）。

    逐条把**有排程行的任务**都算一遍（`_tasks`＝all_tasks_schedule，与交付物表格同源）；
    拿不到任何任务级数据 → total = 0（调用方整段不出，绝不写 0 充数）。
    """
    rd_map = _rd_task_map(plan)
    counts = dict((t, 0) for t in NORM_TIER_ORDER)
    total = 0
    seen = set()
    for t in _tasks(plan):
        if not isinstance(t, dict):
            continue
        tid = str(t.get("task_id"))
        seen.add(tid)
        tier = _norm_tier_of(rd_map.get(tid))[0]
        counts[tier] = counts.get(tier, 0) + 1
        total += 1
    for tid in rd_map:                       # 有资源数据但没排程行（旧计划/退化数据）也数
        if tid in seen:
            continue
        counts[_norm_tier_of(rd_map.get(tid))[0]] += 1
        total += 1
    return counts, total


def _norm_tier_rows(plan, cap=0):
    """``(rows, total)``：逐条工序的档次（用于产物逐条列出；cap=0 表示不截断）。"""
    rd_map = _rd_task_map(plan)
    rows = []
    for t in _tasks(plan):
        if not isinstance(t, dict):
            continue
        tid = str(t.get("task_id"))
        tier, note = _norm_tier_of(rd_map.get(tid))
        rows.append({"task_id": tid, "task_name": t.get("task_name") or tid,
                     "tier": tier, "note": note})
    cap = int(cap) if cap else 0
    return (rows[:cap] if cap else rows), len(rows)


def _norm_critical_coverage(plan, view=None):
    """**关键路径规范依据覆盖率**（合同 §9.2 D6）。

    = （关键路径上由**规范定额**决定的工序工期之和）÷ 总工期。

    · 关键路径任务 = `cpm_result.critical_path`（`_critical_ids`）；
    · 工序工期 = 排程跨度（`_schedule_span`，与交付物表同一口径，含首尾）；
    · 分子只算 `NORM_TIER_IS_SPEC`（规范台班 / 规范人工）—— AI 经验估算定额
      **无规范依据**，计入它就是虚报覆盖率；
    · 总工期优先取 `meta.total_duration_days` → `overview.total_duration_days` →
      关键路径工序工期之和（拿不到就用关键路径本身当分母，口径写在产物里）。

    算不出来（没有关键路径 / 没有排程日期）→ 返回 ``None``，调用方**整行不出**。
    """
    crit = _critical_ids(plan)
    span = _schedule_span(plan)
    if not crit or not span:
        return None
    rd_map = _rd_task_map(plan)
    meta = plan.get("meta") if isinstance(plan.get("meta"), dict) else {}
    ov = plan.get("overview") if isinstance(plan.get("overview"), dict) else {}
    total_days = meta.get("total_duration_days")
    if total_days is None:
        total_days = ov.get("total_duration_days")
    if total_days is None and isinstance(view, dict):
        total_days = view.get("total_day_count")
    try:
        total_days = float(total_days) if total_days is not None else None
    except (TypeError, ValueError):
        total_days = None

    crit_days = norm_days = 0.0
    tiers = {}
    tasks = 0
    for tid in crit:
        d = span.get(str(tid))
        if not d:
            continue
        d = float(d)
        tasks += 1
        crit_days += d
        tier = _norm_tier_of(rd_map.get(str(tid)))[0]
        tiers[tier] = round(tiers.get(tier, 0.0) + d, 2)
        if tier in NORM_TIER_IS_SPEC:
            norm_days += d
    if tasks <= 0 or crit_days <= 0:
        return None
    denom = total_days if (total_days and total_days > 0) else crit_days
    return {"critical_tasks": tasks,
            "critical_days": round(crit_days, 2),
            "norm_days": round(norm_days, 2),
            "total_days": round(denom, 2),
            "total_days_source": ("meta.total_duration_days" if (total_days and total_days > 0)
                                  else "关键路径工序工期之和（计划没给总工期）"),
            "pct": round(100.0 * norm_days / denom, 1) if denom else None,
            "target_pct": NORM_COVERAGE_TARGET_PCT,
            "tiers": tiers}


def _norm_coverage_caliber_note(cov):
    """覆盖率的口径注（含"关键路径含并行工序"的如实说明）—— 看板与 Word 共用一段原文。"""
    if not isinstance(cov, dict) or cov.get("critical_days") is None:
        return ""
    txt = ("覆盖率口径：分子 = 关键路径上来源档次为「%s / %s」的工序工期之和（%s 天，"
           "共 %s 条关键路径工序）；分母 = 总工期（取 %s，%s 天）"
           % (NORM_TIER_MACHINE, NORM_TIER_LABOR, _fnum(cov["norm_days"]),
              cov.get("critical_tasks"), cov.get("total_days_source") or "总工期",
              _fnum(cov["total_days"])))
    try:
        if float(cov["critical_days"]) > float(cov["total_days"]):
            # 关键路径集合里有并行工序时，各工序工期相加大于总工期 —— 这是数据的性质，
            # 不是 bug；指标仍按合同口径计算，但必须说清楚，免得读者以为数错了。
            txt += ("；关键路径工序工期之和 %s 天 > 总工期 %s 天，说明该关键路径集合含并行工序，"
                    "指标按合同口径照实计算" % (_fnum(cov["critical_days"]),
                                                _fnum(cov["total_days"])))
    except (TypeError, ValueError, KeyError):
        pass
    return txt + "。"


def _norm_coverage_value_text(cov):
    """覆盖率那一格的显示文案（数字全从数据算，缺就写「—」）。"""
    if not isinstance(cov, dict) or cov.get("pct") is None:
        return ""
    hit = "达标" if float(cov["pct"]) >= float(cov.get("target_pct") or 0) else "未达标"
    return ("%s%%（规范依据 %s 天 / 总工期 %s 天；目标 ≥%s%%，%s）"
            % (_fnum(cov["pct"]), _fnum(cov["norm_days"]), _fnum(cov["total_days"]),
               _fnum(cov.get("target_pct")), hit))


def _norm_coverage_display(plan, cov_meta=None, view=None):
    """覆盖率显示文案：优先用交付侧**从数据现算**的值，算不出来才回退 meta 里存的。"""
    try:
        live = _norm_critical_coverage(plan, view)
    except Exception:
        live = None
    if live:
        return _norm_coverage_value_text(live), live
    stored = (cov_meta or {}).get(NORM_COVERAGE_META_KEY) \
        if isinstance(cov_meta, dict) else None
    if isinstance(stored, dict) and stored.get("pct") is not None:
        return _norm_coverage_value_text(stored), stored
    return "", None


def _record_norm_coverage(plan):
    """把覆盖率落进 `meta.norm_coverage.critical_norm_coverage` 并写回计划 JSON。

    合同 §9.2 要求"写进 `meta` 与产物"，同时**不许新增 meta 顶层键** —— 这里只往**既有**
    `norm_coverage` 里加一个子键；`norm_coverage` 本身不存在就一个字不写（不新建结构）。
    写回沿用 `_persist_usage_final` 的既有先例（只改已存在的计划 JSON，异常一律吞掉）。
    """
    if not isinstance(plan, dict):
        return False
    try:
        meta = plan.get("meta")
        if not isinstance(meta, dict):
            return False
        cov = meta.get("norm_coverage")
        if not isinstance(cov, dict):
            return False
        live = _norm_critical_coverage(plan)
        if not live or live.get("pct") is None:
            return False
        rec = {"pct": live["pct"], "norm_days": live["norm_days"],
               "critical_days": live["critical_days"], "total_days": live["total_days"],
               "total_days_source": live["total_days_source"],
               "target_pct": live["target_pct"], "critical_tasks": live["critical_tasks"],
               "tiers": dict(live["tiers"])}
        cov[NORM_COVERAGE_META_KEY] = rec
        _persist_norm_coverage(plan, rec)
        return True
    except Exception:
        return False


def _persist_norm_coverage(plan, rec):
    """把覆盖率写回**计划 JSON 文件**（`<PLANS_DIR>/<plan_id>.json`）—— 同 `_persist_usage_final`。

    只改已存在的文件（拿不到就返回 ""，绝不新建）；任何异常一律吞掉 —— 覆盖率写回是旁路，
    绝不能让它把交付流程挂掉。
    """
    try:
        if not isinstance(plan, dict) or not isinstance(rec, dict):
            return ""
        pid = str(plan.get("plan_id") or "").strip()
        if not pid:
            return ""
        path = Path(config.PLANS_DIR) / ("%s.json" % pid)
        if not path.is_file():
            return ""
        try:
            disk = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError, UnicodeDecodeError):
            return ""
        if not isinstance(disk, dict):
            return ""
        dmeta = disk.get("meta")
        if not isinstance(dmeta, dict):
            return ""
        dcov = dmeta.get("norm_coverage")
        if not isinstance(dcov, dict):
            return ""
        dcov[NORM_COVERAGE_META_KEY] = dict(rec)
        from ..plan_store import _write_json as _plan_write_json
        return _plan_write_json(str(path), disk)
    except Exception:
        return ""


def _norm_reason_code(text):
    """原因原文 → 合同 §9.1 的类别码；判不出来返回空串（**不编**）。"""
    flat = str(text or "").replace(" ", "")
    for key, code in _NORM_REASON_RULES:
        if key.replace(" ", "") in flat:
            return code
    return ""


def _norm_missing_reason(rd, leaf=None):
    """该工序"为什么没有定额依据" → ``(显示文案, 类别码)``。

    取值顺序（先具体后笼统）：`norm_binding.not_usable_reason` → 绑定的 `match_type=unbound`
    → `_norm_flagged` → `_warning`（任务级与叶子级都看）。
    全部拿不到 → ``("原因未记录", "")`` —— 报"不知道"比编一个原因诚实。
    """
    rd = _rd_of(rd)
    leaf = leaf if isinstance(leaf, dict) else {}
    nb = leaf.get("norm_binding")
    nb = nb if isinstance(nb, dict) else {}
    cands = [nb.get("not_usable_reason")]
    if str(nb.get("match_type") or "").strip().lower() == "unbound":
        # 合同 §3：绑定不一致 → match_type="unbound"、usable=False、not_usable_reason="活动绑定不一致"
        cands.append("活动绑定不一致")
    cands += [rd.get("_norm_flagged"), rd.get("_warning"),
              leaf.get("_norm_flagged"), leaf.get("_warning")]
    raws = [str(c) for c in cands if c]
    for raw in raws:
        code = _norm_reason_code(raw)
        if code:
            return code, code
    if raws:
        return _sanitize_norm_text(raws[0])[:120], ""
    return "来源未记录", ""


def _norm_missing_sentence(reason):
    """合同 §9.1 规定的**逐条可读条目**原文（看板与 Word 一字不差）。"""
    return "%s，工期与人数来自模型（原因：%s）" % (NORM_MISSING_MARKER,
                                                   str(reason or "来源未记录"))


def _norm_missing_rows(plan, cap=0):
    """``(rows, total)``：D4 清单 —— `_norm_applied` 为空的工序 + 原因归类（合同 §9.1）。"""
    rd_map = _rd_task_map(plan)
    bindings = _leaf_bindings(plan)
    rows, seen = [], set()
    for t in _tasks(plan):
        if not isinstance(t, dict):
            continue
        tid = str(t.get("task_id"))
        if tid in seen:
            continue
        seen.add(tid)
        rd = _rd_of(rd_map.get(tid))
        if rd.get("_norm_applied"):
            continue
        leaf = bindings.get(tid) or {}
        reason, code = _norm_missing_reason(rd, leaf)
        rows.append({"task_id": tid,
                     "task_name": t.get("task_name") or leaf.get("name") or tid,
                     "reason": reason, "reason_code": code,
                     "tier": _norm_tier_of(rd)[0],
                     "sentence": _norm_missing_sentence(reason)})
    cap = int(cap) if cap else 0
    return (rows[:cap] if cap else rows), len(rows)


def _norm_basis_adjust_rows(plan, cap=0):
    """``(rows, total)``：口径换算留痕（合同 §9.4，字段由 WS1 写）。

    `norm_binding.basis_adjust` = {task_scope, norm_scope, task_quantity,
    adjusted_quantity, method, note}；`basis_unconfirmed` = 口径未确认（不阻断，只打标）。

    ⚠ WS1 与 WS3 并行开发，这些字段**可能还不存在** → 一律 `.get()`；一个都没有就返回
    ``(rows=[], 0)``，调用方整段不出 —— 缺失时交付物行为与改造前**逐字一致**。
    """
    bindings = _leaf_bindings(plan)
    rows = []
    for tid in sorted(bindings):
        leaf = bindings.get(tid) or {}
        nb = leaf.get("norm_binding")
        nb = nb if isinstance(nb, dict) else {}
        adj = nb.get("basis_adjust")
        unconfirmed = bool(nb.get("basis_unconfirmed"))
        if not isinstance(adj, dict):
            adj = {}
        if not adj and not unconfirmed:
            continue
        rows.append({
            "task_id": tid,
            "task_name": leaf.get("name") or tid,
            "task_scope": str(adj.get("task_scope") or ""),
            "norm_scope": str(adj.get("norm_scope") or ""),
            "task_quantity": adj.get("task_quantity"),
            "adjusted_quantity": adj.get("adjusted_quantity"),
            "method": str(adj.get("method") or ""),
            "note": str(adj.get("note") or ""),
            "unconfirmed": unconfirmed,
        })
    cap = int(cap) if cap else 0
    return (rows[:cap] if cap else rows), len(rows)


def _norm_basis_adjust_text(row):
    """口径换算留痕一行的显示文案：原口径 → 定额口径 → 换算方式 → 换算后工程量。"""
    parts = []
    if row.get("task_scope") or row.get("norm_scope"):
        parts.append("口径 %s → %s" % (row.get("task_scope") or "—",
                                       row.get("norm_scope") or "—"))
    if row.get("task_quantity") is not None or row.get("adjusted_quantity") is not None:
        parts.append("工程量 %s → %s" % (
            "—" if row.get("task_quantity") is None else _fnum(row["task_quantity"]),
            "—" if row.get("adjusted_quantity") is None else _fnum(row["adjusted_quantity"])))
    if row.get("method"):
        parts.append("换算方式：%s" % row["method"])
    out = _sanitize_norm_text("；".join(parts))
    if row.get("note"):
        out = (out + "；" if out else "") + _sanitize_norm_text(row["note"])
    if row.get("unconfirmed"):
        out = (out + "；" if out else "") + "口径未确认（未阻断，按原口径继续，需人工复核）"
    return out or "（计划未给出换算明细）"


def _norm_override_path(plan):
    """覆盖文件路径；没有就返回 ``None``（= 优雅降级的唯一判据）。"""
    plan = plan if isinstance(plan, dict) else {}
    cands = []
    try:
        cands.append(Path(_plan_dir(plan)) / NORM_OVERRIDE_FILE)
    except Exception:
        pass
    pid = str(plan.get("plan_id") or "").strip()
    if pid:
        try:
            cands.append(Path(_plan_dir(plan)) / (pid + NORM_OVERRIDE_ALT_SUFFIX))
        except Exception:
            pass
    for p in cands:
        try:
            if p.is_file():
                return p
        except OSError:
            continue
    return None


def _norm_overrides(plan):
    """``(rows, 来源文件)``：人工覆盖留痕（合同 §9.3 D7）。

    覆盖文件格式（`<交付目录>/定额覆盖.json`）::

        {"overrides": [{"task_id": "1.3.4", "field": "工期",
                        "original": 3, "value": 5,
                        "by": "张三", "at": "2026-09-21T10:00:00",
                        "note": "现场踏勘后按实际调整"}]}

    **只展示、不参与计算**（本工作流只负责交付物；把覆盖值真正应用回计划属于上游节点的职责，
    见报告「需要其他流配合」）。读不到 / 读不动覆盖文件 → ``(rows=[], "")``：调用方整段不出，
    交付物行为与没有这个入口时**逐字一致**。
    """
    path = _norm_override_path(plan)
    if path is None:
        return [], ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return [], ""
    items = data.get("overrides") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return [], ""
    rows = []
    for it in items:
        if not isinstance(it, dict):
            continue
        rows.append({
            "task_id": str(it.get("task_id") or "—"),
            "name": str(it.get("task_name") or ""),
            "field": str(it.get("field") or it.get("item") or "—"),
            "original": it.get("original", it.get("from")),
            "value": it.get("value", it.get("to")),
            "by": str(it.get("by") or it.get("who") or "未记录"),
            "at": str(it.get("at") or it.get("time") or "未记录"),
            "note": str(it.get("note") or ""),
        })
    return rows, str(path)


def _norm_override_text(row):
    """覆盖一行的显示文案：原值 → 覆盖值 → 覆盖人 / 时间 / 说明。"""
    orig = "—" if row.get("original") is None else row["original"]
    val = "—" if row.get("value") is None else row["value"]
    out = "%s → %s；覆盖人 %s；时间 %s" % (orig, val, row.get("by"), row.get("at"))
    if row.get("note"):
        out += "；说明：%s" % row["note"]
    return _sanitize_norm_text(out)


def _norm_override_grid_rows(rows):
    """覆盖清单 → (表头, 二维行)（看板与 Word 共用同一份行数据）。"""
    return (["任务 ID", "任务", "覆盖项", "原值 → 覆盖值", "覆盖人 / 时间 / 说明"],
            [[r.get("task_id") or "—", r.get("name") or "—", r.get("field") or "—",
              _norm_override_text(r)] for r in rows])


def _kb_unmapped_work_types(meta):
    """`kb_warnings` 里「没有该工种值的映射数据」的**未映射工种数**。

    只数结构化前缀，不把整段警告原文抄进交付物 —— 交付物要的是数量，不是日志。

    实测原文（plan_run_1789818211，26 条）：
      `天棚工程（ceiling）：结构映射表中没有该工种的映射数据，已保留其全部 L4（无结构约束）。`
    ⚠ 别按整句匹配：`kb.py` 里这句话的措辞会随写作人漂移（"结构映射表中没有…" /
    "没有该工种值的映射数据" 两种写法都出现过，差一个字就整段漏掉）。这里按
    **最小稳定子串**「没有该工种」+「映射数据」判定，并兼容「未映射」。
    """
    n = 0
    for w in (meta.get("kb_warnings") or []):
        s = str(w or "")
        if ("没有该工种" in s and "映射数据" in s) or "未映射" in s:
            n += 1
    return n


def _manpower_peaks(plan, view):
    """人工峰值的**两个口径**，各自显式命名（WS5 唯一允许的「数字修复」）。

    历史缺陷（实测）：交付物里同一个词「峰值」指两个不同的数 ——
      总览「人工峰值」   = `resource_plan.peak_manpower`（实测 120）
      四、人员配置「峰值总人数」= `_compute_view()['peak_total']`（实测 245）

    追源头 `plan_assembler.py:213/236`：
      peak_total = (boundary.get("labor") or {}).get("peak_total") or boundary.get("labor_peak")
      "peak_manpower": int(peak_total) if peak_total else peak
    —— **用户给的限额一旦存在就顶掉算出来的曲线峰值**，于是两处本是两种口径、
    却被同一个词指代，读起来就是「一份文档自相矛盾」。

    修法（不新造数字、不改上游）：两处统一读**这一个函数**的返回值，各自标注口径名。
    """
    rp = plan.get("resource_plan") if isinstance(plan.get("resource_plan"), dict) else {}
    m = plan.get("meta") if isinstance(plan.get("meta"), dict) else {}
    bnd = (m.get("boundary_conditions") or {}).get("labor") or {}
    curve = view.get("peak_total") if isinstance(view, dict) else None
    limit = rp.get("peak_manpower")
    if limit is None:
        limit = bnd.get("peak_total")
    return {
        "curve": curve,                        # 资源曲线峰值（逐日累加，含机械配员）
        "limit": limit,                        # 用户给定人工限额（= 编制依据上限）
        "user_limit": bnd.get("peak_total"),   # 边界条件里的限额（可能缺）
    }


def _workface_summary(plan):
    """班组人数的**两个真源**各起了什么作用（第 39 轮 + 施工组织层 WS6）。

    返回 ``{"applied","capped","examples","org_count","org_examples",
    "below_org_count","below_org_examples","peak_shaving_count","peak_shaving_examples"}``。

    为什么必须分开报：资源层改成「**有组织层的任务，班组以组织层为唯一真源**」
    （`resource.py`：`_organization_crew` + `_resource_source[工种].origin == "org_layer"`）
    之后，那批任务的人数**不再**是"按本施工段工程量用标定公式算出来"的 —— 原来那句
    "共 N 条任务的班组人数按本标定公式算出"对它们就是**假话**。所以：

    · `org_*`：班组来自施工组织层（节拍 × 作业面数），单面上限已由组织层校验；
    · `applied/capped/examples`：**其余**任务仍走工作面容量标定公式（口径不变）；
    · `below_org_*`：`_organization_crew.resource_cap_below_org` 为真的行 ——
      组织层班组高于资源层按本段工程量算出的**合计**上限（两套「每面上限」不同源，
      以组织层为准、资源层不再封顶）；
    · `peak_shaving_*`：`_peak_shaving_skipped` 非空的行（本该按申报的工种总数削峰，
      因班组真源是组织层而**没有**削）—— 静默就是隐瞒。

    一个键都没取到 → 全 0 → 调用方整段不出（绝不写"0 条"冒充查过）。
    """
    tasks = ((plan.get("resource_demand") or {}).get("tasks") or [])
    applied = capped = 0
    examples = []
    org_examples, below_org, shaving = [], [], []
    for t in tasks:
        if not isinstance(t, dict):
            continue
        oc = t.get("_organization_crew")
        if isinstance(oc, dict) and oc.get("crew_total") is not None:
            rec = {
                "task_id": t.get("task_id"), "task_name": t.get("task_name"),
                "trade": oc.get("trade"),
                "n_faces": oc.get("n_faces"), "crew_per_face": oc.get("crew_per_face"),
                "crew_total": oc.get("crew_total"),
                "cap_per_face": oc.get("cap_per_face"), "cap_total": oc.get("cap_total"),
            }
            org_examples.append(rec)
            if oc.get("resource_cap_below_org"):
                below_org.append(rec)
            sk = t.get("_peak_shaving_skipped")
            if isinstance(sk, dict):
                shaving.append(dict(rec, declared_trade_limit=sk.get("declared_trade_limit")))
            # 有组织层的行**不再**计入"按本段工程量公式算出"的那一批（口径不同源）
            continue
        if t.get("_workface_note"):
            applied += 1
        recs = t.get("_workface_capped")
        if isinstance(recs, list) and recs:
            capped += 1
            r = recs[0]
            if isinstance(r, dict):
                examples.append((
                    "%s（%s）" % (t.get("task_name") or t.get("task_id") or "—",
                                  r.get("resource") or "—"),
                    "%s → %s" % (r.get("original_per_day"), r.get("capped_per_day")),
                    str(r.get("reason") or "")[:70]))
    return {"applied": applied, "capped": capped, "examples": examples[:6],
            "org_count": len(org_examples), "org_examples": org_examples[:3],
            "below_org_count": len(below_org), "below_org_examples": below_org[:6],
            "peak_shaving_count": len(shaving), "peak_shaving_examples": shaving[:3]}


def _org_crew_sentence(ws):
    """「班组来自施工组织层」那句（条数与样例全部从数据算，绝不写死）。"""
    ex = (ws.get("org_examples") or [{}])[0]
    n_faces, cpf = ex.get("n_faces"), ex.get("crew_per_face")
    tot = ex.get("crew_total")
    demo = ""
    if n_faces is not None and cpf is not None and tot is not None:
        demo = "，如 %s 面 × %s 人/面 = %s 人" % (n_faces, cpf, tot)
    elif tot is not None:
        demo = "，如 %s 人" % tot
    # C11（节拍只作对比参考）：旧文案「节拍决定天数」与新口径**冲突** —— 节拍
    # （`cadence_days`）现在不参与任何计算，只作对比。这里如实改成作业面口径。
    return ("共 %d 条任务的班组来自施工组织层（作业面数 × 每面人数 = 班组总人数%s；"
            "节拍仅作对比参考、不参与任何计算）；单面上限已由组织层校验，"
            "资源层不再二次封顶" % (ws.get("org_count") or 0, demo))


def _workface_below_org_lead(ws):
    """组织层班组 > 资源层合计上限的**条数**先说出来（数字从数据算，不写死）。"""
    n = ws.get("below_org_count") or 0
    if not n:
        return ""
    shown = len(ws.get("below_org_examples") or [])
    head = ("共 %d 条任务的组织层班组高于资源层按本段工程量算出的合计上限"
            "（以施工组织层为准、资源层不再封顶；两套「每面上限」不同源）：" % n)
    if shown < n:
        head += "以下列前 %d 条，其余逐条留痕在资源行。" % shown
    return head


def _workface_below_org_lines(ws):
    """组织层班组 > 资源层按段公式合计上限 → 逐条如实写（模板里的 A/N/B 全从数据取）。"""
    out = []
    for r in ws.get("below_org_examples") or []:
        if r.get("cap_per_face") is None or r.get("crew_total") is None:
            continue
        out.append(
            "%s（%s）本行以施工组织层为准；资源层按本段工程量算出的单面上限为 %s 人"
            "（低于组织层 %s 面后的 %s 人），两套「每面上限」目前不同源，已在资源行逐条留痕"
            % (r.get("task_name") or r.get("task_id") or "—", r.get("trade") or "—",
               r.get("cap_per_face"), r.get("n_faces"), r.get("crew_total")))
    return out


def _workface_peak_shaving_sentence(ws):
    """未削峰的那批任务如实报（条数从数据算）。"""
    n = ws.get("peak_shaving_count") or 0
    if not n:
        return ""
    ex = (ws.get("peak_shaving_examples") or [{}])[0]
    tail = ""
    if ex.get("trade") and ex.get("declared_trade_limit") is not None \
            and ex.get("crew_total") is not None:
        tail = "（如 %s：组织层班组 %s 人，申报工种总上限 %s 人）" % (
            ex["trade"], ex["crew_total"], ex["declared_trade_limit"])
    return ("本计划有 %d 条任务未按工种总人数二次削峰：班组真源是施工组织层，"
            "申报上限已在组织层按「每面」口径校验%s。" % (n, tail))


def _workface_sentence(plan):
    """班组人数口径的**一句话**（看板与 Word 共用，保证两处一字不差）。

    两个真源各说各的（**不再**把组织层那批说成"按标定公式算出"）：
      · 组织层那批 → `_org_crew_sentence`；
      · 其余 → 工作面容量标定公式那句（措辞与既有回归门逐字一致）。

    返回 "" 表示这份计划两类数据都没有 → 调用方**整段不出**（不许编 0 条）。
    """
    ws = _workface_summary(plan)
    parts = []
    if ws.get("org_count"):
        parts.append(_org_crew_sentence(ws))
    if ws.get("applied"):
        # 来源必须跟着一起写死：用户原话就是「写明『人数被工作面容量压到 X 人，
        # 来源=AI 估算/低置信度』」—— 只给条数不给来源，用户还是没法判断该不该信。
        parts.append("共 %d 条任务的班组人数按本施工段工程量用标定公式算出，"
                     "其中 %d 条顶到上限（工程量再大也不加人，只能延长工期）；"
                     "工作面容量标定来源：ai_estimate / LOW（经验标定，非规范来源）"
                     % (ws["applied"], ws["capped"]))
    return "；".join(parts)


def _site_const_source_text(const_source, quantity_source):
    """台数**来源**的人话（域 7.7 / 7.10 之后必须区分四种，不许再一律叫"AI 默认口径"）。

    为什么要改（父代理收口）：域 7.10 之前，非用户来源**恒等于写死的 1 台**，所以旧文案
    「AI 默认口径（用户未申报台数）」是诚实的。7.10 落地后它变成**按建筑参数（面积/栋数/
    层数）明示规则估出来的数**（实测 `215000/12/38 → 塔吊 12 台`），再叫"默认口径"就是
    **把算出来的数说成拍脑袋的数**，用户无法判断该不该信。
    """
    cs = str(const_source or "")
    if cs == "site_machine_const":
        return "项目级常量（边界节点一次定好、已冻结）"
    if cs == "equipment_peak":
        return "用户申报"
    if cs == "resource_estimate":
        return "AI 按已有建筑参数估算（规则见下）"
    if cs == "default_one":
        return "无可用依据（连建筑参数都没有）→ 按 1 台兜底"
    # 兼容老计划：没有 const_source 键时退回旧判据（旧产物的行为一字不变）
    return "用户申报" if quantity_source == "user" else "AI 默认口径（用户未申报台数）"


def _site_equipment_summary(plan):
    """**场地级设备**（塔吊 / 施工电梯）的实际投入 → 结构化汇总（看板/Word 共用）。

    数据源唯一：`resource_demand._site_level_equipment`（资源层 `resource._inject_site_equipment`
    写入）。没有该键（旧计划 / 没投任何场地级设备）→ 返回 {} → 调用方**整段不出**。
    """
    reg = (plan.get("resource_demand") or {}).get("_site_level_equipment")
    if not isinstance(reg, dict):
        return {}
    machines = reg.get("machines")
    if not isinstance(machines, dict) or not machines:
        return {}
    items = []
    for name, rec in sorted(machines.items()):
        rec = rec if isinstance(rec, dict) else {}
        items.append({
            "name": str(name),
            "quantity": rec.get("quantity"),
            "quantity_source": rec.get("quantity_source"),
            # 【域 7.7/7.10 · 父代理收口】真实来源用 `const_source` 区分，不再一律"AI 默认口径"
            "const_source": rec.get("const_source"),
            "source_text": _site_const_source_text(rec.get("const_source"),
                                                   rec.get("quantity_source")),
            # 7.10 的明示规则 + 冻结标记：用户要能看见"这个台数是怎么来的、冻没冻"
            "rule": str(rec.get("rule") or ""),
            "basis": str(rec.get("basis") or ""),
            "frozen": bool(rec.get("frozen")),
            "crew": rec.get("crew_composition") or "无配员数据",
            "crew_source": rec.get("crew_source"),
            "hit_tasks": rec.get("hit_tasks"),
            "note": str(rec.get("note") or ""),
        })
    return {"count": reg.get("count"), "items": items,
            "caliber": str(reg.get("caliber") or ""),
            "norm_source": str(reg.get("norm_source") or "")}


def _site_equipment_sentence(plan):
    """场地级设备口径的**一句话**（看板与 Word 共用，保证两处一字不差）。

    为什么必须写出来：塔吊/施工电梯被投到了上百条任务上，但**台数逐日取 max**
    （一个工地 1 台塔吊服务所有楼层的所有任务）。不写口径，用户会以为"157 条任务 =
    157 台塔吊"；不写来源，用户无法判断 1 台这个数是谁给的（用户申报 / AI 默认）。
    """
    s = _site_equipment_summary(plan)
    if not s:
        return ""

    def _one(i):
        # 【域 7.7/7.10 · 父代理收口】把"这个台数怎么来的、冻没冻"一并说给用户：
        # 7.10 之后台数是**按建筑参数明示规则算出来的**，只报数字不报规则 = 让用户无法复核。
        seg = "%s %s 台（%s，配员 %s%s）" % (
            i["name"], _fnum(i["quantity"]), i["source_text"], i["crew"],
            "，已冻结" if i.get("frozen") else "")
        if i.get("rule"):
            seg += "；依据：%s" % i["rule"]
        return seg

    parts = "、".join(_one(i) for i in s["items"])
    return ("%s；共投入 %s 条任务。口径：逐日曲线取 max（同一天多条任务需要也只算申报台数），"
            "任务级机械（泵车/挖掘机等）仍按日叠加；配员随该设备台数走、同为逐日 max。"
            "台班定额来源=%s（库内没有这两个设备的台班定额行，故**不引用任何规范台班**，"
            "导入真实规范后应清退）。" % (parts, s["count"], s["norm_source"] or "AI_ESTIMATE_V1"))


# ---------------- 逐条工序的「依据 / 资源」（看板与 Word 共用，同一句话） ----------------
# 用户原话：「为什么不给班组，还能算出工日，这不是编的吗」。逐条工序表上必须能一眼看出
# 这条任务是**算出来的**（定额来源 + 班组 + 机械）还是**没算出来**（⚠ 无可用定额，只沿用
# WBS 估算工期），或者**算了但用了写明的假定**（单位换算）。三者语义不同，不许混为一谈。

def _date_or_none(value):
    """能解析成 ISO 日期就返回 date，否则 None（**不退回今天** —— `_to_date` 的兜底是
    `default or today`，拿它算跨度会把缺日期的任务算成"从今天开始"）。"""
    try:
        return datetime.date.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None


def _schedule_span(plan):
    """``{task_id: 排程跨度(天，含首尾)}`` —— 由 `all_tasks_schedule` 的起止**日期**算。

    为什么必须由日期算：WBS 的 `duration_days` 是模型估的目标天数，排程排出来的起止日期
    才是真占用的跨度，两者可以差一个数量级（实测 ALC 6.1.1.1：日期跨 31 天、WBS 目标 3 天）。
    同一行里既印日期又印另一个数，用户只会读成"这是编的"。
    """
    out = {}
    for t in _tasks(plan):
        if not isinstance(t, dict):
            continue
        d0 = _date_or_none(t.get("start_date"))
        d1 = _date_or_none(t.get("finish_date"))
        if d0 is None:
            continue
        if d1 is None or d1 < d0:
            d1 = d0
        out[str(t.get("task_id"))] = max(1, (d1 - d0).days + 1)
    return out


# ---------------- 审计身份（谁答的门）与「天数」的语义 ----------------
# 这两组判据只写一次，Word 页头 / 计划总览 / 看板三处都读它 —— 用户审计 P0-A 的
# 教训就是"这几处各写一套"，于是草案诚实、定稿撒谎。
def _audit_identity(plan):
    """审计身份的**唯一判据**（实现见 `audit_gate.audit_honesty`）。

    要求 R1/R2/R3 三轮都有记录、都通过、且每一轮 `answered_by == "human"`。
    拿不到「谁答的门」就按未审计（宁缺勿假）。延迟导入：避免 delivery ↔ audit_gate
    的模块级互相导入（audit_gate 会反向用到 delivery 的渲染逻辑）。
    """
    from .audit_gate import audit_honesty
    meta = plan.get("meta") if isinstance(plan, dict) else None
    return audit_honesty(meta)


def _audit_display(plan):
    """交付物上「审计状态」一格该怎么写 + 为什么（+ 看板要的颜色）。"""
    h = _audit_identity(plan)
    if h["confirmed"]:
        return {"status": "已审计",
                "badge": "已审计 · R1/R2/R3 真人通过",
                "round_text": h["round_text"],
                "reason": "",
                "color": "#1f9d55"}
    return {"status": "未审计",
            "badge": "未审计 · 待人工复审",
            "round_text": h["round_text"],
            "reason": "；".join(h["reasons"]),
            "color": "#d97706"}


def _row_date_span(t):
    """一行任务由自己的起止日期算出的跨度（含首尾）；日期不可解析 → None。"""
    if not isinstance(t, dict):
        return None
    d0 = _date_or_none(t.get("start_date"))
    d1 = _date_or_none(t.get("finish_date"))
    if d0 is None:
        return None
    if d1 is None or d1 < d0:
        d1 = d0
    return max(1, (d1 - d0).days + 1)


def _task_wbs_target(t, span=None):
    """任务行上的 **WBS 目标天数** —— 模型在 WBS 叶子上估的目标，不是排程跨度。

    P0-B：`all_tasks_schedule[*].duration_days` 现在是**排程跨度**（与同行的起止日期
    同源），WBS 目标移到 `wbs_target_days`。第 37 轮之前落盘的老计划没有这个键 ——
    那时 `duration_days` 装的就是 WBS 目标，所以"缺键读旧键"是忠于原文的退化。

    ⚠️ 但缺键时**不是无条件读旧键**：`recompute.py`（重排/修订路径）会重建
    `all_tasks_schedule` 且**不带** `wbs_target_days`，那时 `duration_days` 是跨度。
    两种情形只能靠"字段是否等于本行日期跨度"区分 —— 相等时无法判断，返回 `None`
    （交付物印「—」）。**报"不知道"比猜一个数诚实**。
    """
    if not isinstance(t, dict):
        return None
    if "wbs_target_days" in t:
        return t.get("wbs_target_days")
    value = t.get("duration_days")
    if span is None:
        span = _row_date_span(t)
    if span is not None and value is not None:
        try:
            if int(value) == int(span):
                return None
        except (TypeError, ValueError):
            pass
    return value


def _critical_chain_sums(plan):
    """关键链上那些任务的两种合计：WBS 目标天数之和 / 排程跨度（日期）之和。

    两个数**都不是**总工期，也**不是**关键路径的工期：关键链上的任务首尾相接，
    合计必然大于整条链的跨度（实测真计划 89 条：WBS 目标合计 712、日期跨度合计 673，
    而整条链只有 608 天）。所以交付物必须分列并写清是哪种合计。
    """
    span = _schedule_span(plan)
    n = 0
    wbs_total = 0
    wbs_known = False
    span_total = 0
    for t in (plan.get("critical_path_tasks") or []):
        if not isinstance(t, dict):
            continue
        n += 1
        v = _task_wbs_target(t, span.get(str(t.get("task_id"))))
        if v is not None:
            try:
                wbs_total += int(v)
                wbs_known = True
            except (TypeError, ValueError):
                pass
        s = span.get(str(t.get("task_id")))
        if s is not None:
            span_total += int(s)
    return {"count": n,
            "wbs_target_days": wbs_total if wbs_known else None,
            "span_days": span_total or None}


def _schedule_versions_of(plan):
    """`schedule_versions`：先看 `meta` 再看顶层（老计划两处都有可能）。"""
    meta = plan.get("meta") if isinstance(plan.get("meta"), dict) else {}
    sv = meta.get("schedule_versions")
    if not isinstance(sv, dict):
        sv = plan.get("schedule_versions")
    return sv if isinstance(sv, dict) else {}


# 摘要卡片取值时要剥掉的**尾部括号注解**：从**最后一个**「（…）」块（它一直延伸到行尾）
# 的起点切掉，因此多级嵌套括号也能处理 —— 例如
# 「649 天（来源 cpm_result.cpm_total_duration_days；口径见上表「纯 CPM（未计资源约束）」一行）」。
# 用「扫描配对」而不是正则：正则要为每个可能的起点回扫一次尾巴（长串上是平方级），
# 这里的字符串虽然短，但一眼能看清代价的写法更稳妥。
_OPEN_PARENS = "（("
_CLOSE_PARENS = "）)"


def _tail_annotation_start(s):
    """``s`` 里那个**延伸到行尾**的括号注解的起点；没有则 ``-1``。

    只看最外层括号：``（…（…）…）`` 整块算一个注解，起点是最外层那个左括号。
    """
    depth = 0
    start = -1
    for i, ch in enumerate(s):
        if ch in _OPEN_PARENS:
            if depth == 0:
                start = i
            depth += 1
        elif ch in _CLOSE_PARENS:
            if depth == 0:
                start = -1          # 落单的右括号 → 这一趟不算，重新找
            else:
                depth -= 1
    if depth != 0:                  # 括号没配平 → 不动它（宁可不切，也不切出半句）
        return -1
    return start


def _card_value(value, unit="天"):
    """看板顶部摘要卡片的**纯结果**取值：只有数与单位，不带尾部括号注解。

    为什么单列一个函数：摘要卡片的读者是"扫一眼拿数"的人，不是来审计的人。卡片上
    「604 天（来源 overview.total_duration_days；满足工作面/资源约束后的实排工期）」这种
    写法把**来源键**（`overview.total_duration_days` 这种内部字段名）与口径注解混进了
    结果里 —— 用户实测原话：「摘要栏不需要把每条数据的来源也写进去，只写结果」。

    只切**尾部**那个括号块，前面的数与其单位一律保留：
      · 「81 个（来源 overview.critical_path_length）」→「81 个」；
      · 「91 人（资源曲线口径）」→「91 人」（口径本身仍在「资源计划」卡片里逐条写明）；
      · 「120 人（模型估算，非用户输入）」这种**不是**来源的括注也会被切掉，所以调用方
        必须保证它在该页别处照印 —— 看板由 `_resource_card_html` 负责（峰值人数一行，
        E1 之后**不再有**「申报峰值」那一项）。

    来源键与口径说明的去处：
      · Word 计划总览表仍带「（来源 …）」（`_critical_path_rows` / `_duration_caliber_rows`
        的 `with_source=True`，交付物的可追溯性一个字都不减）；
      · 看板的解释性文字留在 `_resource_card_html` / 置信度卡 / 施工组织口径段里，
        那里本来就有完整口径，不是靠摘要卡片一行塞下。
    """
    s = str(value).strip()
    if not s:
        return s
    while True:
        pos = _tail_annotation_start(s)
        if pos < 0:
            break
        head = s[:pos].strip()
        if not head:          # 整串就是一个括号注解（如"（无）"）→ 原样留着，别切成空
            break
        s = head
    return s if (not unit or unit in s) else "%s %s" % (s, unit)


def _critical_path_rows(plan, with_source=True):
    """「关键路径」两行（P0-B 要求）：**条数**与**工期**是两个不同的东西。

    老交付物只印「关键路径长度 89」—— 把 89 **条任务**读成"长度/天数"。这里拆成：
      · 关键路径任务数   ← `overview.critical_path_length`（条数）
      · 关键路径工期·排程版 ← `cpm_result.total_duration_days`（整条链占用的天数）
      · 关键路径工期·纯 CPM ← `cpm_result.cpm_total_duration_days`（按 WBS 目标天数计价，
        未计资源约束；口径不同，**不是**排程版的"理想下界"，见函数外的说明）
    取不到的键整行不出现（绝不用别的数代替）。

    `with_source=True`（Word 计划总览表）在值里带「（来源 <字段名>）」；看板摘要卡片传
    `False`，只要结果 —— 见 `_card_value` 的说明。
    """
    ov = plan.get("overview") or {}
    cpm = plan.get("cpm_result") if isinstance(plan.get("cpm_result"), dict) else {}
    rows = []
    n = ov.get("critical_path_length")
    if n is not None:
        rows.append(("关键路径任务数", ("%s 个（来源 overview.critical_path_length）" if with_source
                                    else "%s 个") % _fnum(n)))
    v = cpm.get("total_duration_days")
    if v is not None:
        rows.append(("关键路径工期·排程版", ("%s 天（来源 cpm_result.total_duration_days）"
                                        if with_source else "%s 天") % _fnum(v)))
    v = cpm.get("cpm_total_duration_days")
    if v is not None:
        rows.append(("关键路径工期·纯 CPM（按 WBS 目标天数计价）",
                     ("%s 天（来源 cpm_result.cpm_total_duration_days；口径见上表"
                      "「纯 CPM（未计资源约束）」一行）" if with_source else "%s 天") % _fnum(v)))
    return rows


def _duration_caliber_rows(plan, with_source=True):
    """四个「天数」分列 + 各自的语义与来源键（取不到就**不出现**）。

    真计划实测并存四个数：总工期 608 / 理论最短（排程版）608 / 纯 CPM 644 /
    关键链合计 712（WBS 目标）或 673（日期跨度）。它们**互不相等也不该相等**，
    老交付物一个都没标语义 —— 用户只能自己猜哪个是哪个。

    `with_source=True`（Word 计划总览）在值里带来源键与口径说明；看板摘要卡片传
    `False`，只要结果（见 `_card_value`）。
    """
    ov = plan.get("overview") or {}
    cpm = plan.get("cpm_result") if isinstance(plan.get("cpm_result"), dict) else {}
    meta = plan.get("meta") if isinstance(plan.get("meta"), dict) else {}
    sv = _schedule_versions_of(plan)
    rows = []
    total = ov.get("total_duration_days")
    if total is None:
        total = cpm.get("total_duration_days")
    if total is not None:
        rows.append(("总工期（排程实排）",
                     ("%s 天（来源 overview.total_duration_days；满足工作面/资源约束后的实排工期）"
                      if with_source else "%s 天") % _fnum(total)))
    v = sv.get("theory_min_days")
    if v is not None:
        rows.append(("理论最短（排程版）",
                     ("%s 天（来源 meta.schedule_versions.theory_min_days；排程器算的理论最短）"
                      if with_source else "%s 天") % _fnum(v)))
    v = cpm.get("cpm_total_duration_days")
    if v is not None:
        if with_source:
            _basis = cpm.get("cpm_duration_basis") or "wbs_target_days+dependencies"
            rows.append(("纯 CPM（未计资源约束）",
                         "%s 天（来源 cpm_result.cpm_total_duration_days，口径 "
                         "cpm_result.cpm_duration_basis=%s；按 WBS 目标天数 + 依赖关系正推，"
                         "与排程版不同源，所以它可以比排程版更长）" % (_fnum(v), _basis)))
        else:
            rows.append(("纯 CPM（未计资源约束）", "%s 天" % _fnum(v)))
    chain = _critical_chain_sums(plan)
    if chain["wbs_target_days"] is not None:
        if with_source:
            rows.append(("关键链合计（WBS 目标天数）",
                         "%s 天（%d 条关键任务的 wbs_target_days 之和；任务首尾相接，不是工期）"
                         % (_fnum(chain["wbs_target_days"]), chain["count"])))
        else:
            rows.append(("关键链合计（WBS 目标天数）", "%s 天" % _fnum(chain["wbs_target_days"])))
    if chain["span_days"] is not None:
        if with_source:
            rows.append(("关键链合计（排程跨度）",
                         "%s 天（%d 条关键任务的日期跨度之和；不是工期）"
                         % (_fnum(chain["span_days"]), chain["count"])))
        else:
            rows.append(("关键链合计（排程跨度）", "%s 天" % _fnum(chain["span_days"])))
    return rows


# ============ AI 经验估算定额（政策变更：用户 2026-09-20 亲自决定）============
# 旧政策：AI 凭经验编的定额（KB `sources.AI_ESTIMATE_V1`）"只作参考，不用来算班组"，
# 交付物因此把这类任务写成「⚠ 无可用定额：AI 估算定额（只作参考，不用来算班组）」。
# 新政策：AI 经验估算定额**照用**，与真人定额同等参与工期与班组计算；状态名
# `released_ai`，标注文案固定为 `norm_defaults.LABEL_AI_ESTIMATE`（判据层同一份原文）。
# 唯一不能省的部分就是**逐条标注**：交付物必须让读者一眼看出"这条的工期/班组依据来自
# AI 经验估算、无规范依据、待审"，并在置信度章节给出条数。
# 仍然拦下的只有人工否决（rejected）/ 单位不可换算 / 定额口径不符 —— 这些任务照旧写
# 「⚠ 无可用定额：…（工期沿用 WBS 估算，未计算班组）」，那句话仍然是真的，一个字不许改。
#
# 判据**全部从数据派生**（不依赖尚不存在的开关，也不按任务名硬编码），三路任一命中：
#   ① `_norm_applied`：`source_code` 以 `AI_` 开头 / 含 `AI_ESTIMATE` /
#      `origin` ∈ {ai, ai_estimate} / `match_type == "ai"`（= 真的拿 AI 定额算了）；
#   ② 任务级 `_norm_flagged` / `_warning` 原文点名 AI 估算定额
#      （旧计划的 AI 行就是这样留痕的 —— 那正是新政策要放行的对象）；
#   ③ `_resource_source[*]` 的 origin/ref 指向 `AI_ESTIMATE_V1`
#      （如场地级塔吊台数用的是 AI 默认口径）。
# 三路都拿不到 → 退回旧行为，如实显示原文（"来源未记录"），绝不猜。
try:                                   # 与判据层同一份文案/状态名，避免两处措辞漂移
    from ..norm_defaults import LABEL_AI_ESTIMATE as AI_NORM_LABEL
    from ..norm_defaults import STATE_RELEASED_AI as AI_NORM_STATE
except Exception:                      # 判据层那侧尚未落地时退回字面量（渲染不受影响）
    AI_NORM_LABEL = "AI 经验估算定额（无规范依据，待审）"
    AI_NORM_STATE = "released_ai"

# 旧政策措辞的**精确**特征串：交付物里再出现就是假话（政策变更 2026-09-20）。
_FORBIDDEN_AI_NORM_PHRASES = ("只作参考，不用来算班组", "只作参考、不用来算班组")
# 认「这句话在复述已废除的旧政策（AI 定额不参与计算）」的措辞。
# 判据 = 提到 AI **且**带旧口径的说法 —— 只提到 AI 不够（例如
# "AI 估算定额单位不可换算" 是真实的单位拦截，不该被说成"已照用"）。
_OLD_POLICY_CLAIMS = ("只作参考", "仅作参考", "不作工期证据", "不参与计算", "不参与工期")
# 明确指向"真降级"的拦截词：带这些词的任务**不是** AI 放行对象，仍写无可用定额。
_NON_AI_BLOCKERS = ("单位不可用", "单位不一致且不可换算", "单位不可换算",
                    "人工否决", "定额口径不符", "rejected")
# 认「这段原文在说 AI 估算定额」的最小稳定子串（判据层措辞会漂移，别按整句匹配；
# 带空格与不带空格两种写法都出现过：`norm_bind` 写的是「AI估算定额：KB 无定额行…」，
# 闸门写的是「AI 经验估算定额…」）。
_AI_NORM_MARKERS = ("AI 估算定额", "AI估算定额", "AI 经验估算", "AI经验估算",
                    "AI_ESTIMATE", "ai_estimate", AI_NORM_LABEL)
# `_resource_source` 里认 AI 来源的 origin 值。
_AI_NORM_ORIGINS = ("ai", "ai_estimate")


def _ai_norm_source_code_ai(code):
    """来源代号是不是**非规范来源**（AI 经验估算 / 类别占位 `SCAFFOLD_V1`）。

    判据（政策变更 2026-09-20 + 用户 2026-09-20 裁定「保留占位，但必须全面如实标注」）：
      · `AI_ESTIMATE_V1` / `AI_*` —— AI 凭经验编的定额，**无规范依据**；
      · `SCAFFOLD*`（如 `SCAFFOLD_V1`）—— WS6 的类别占位定额，同样**无规范依据**：
        它是"没有真规范行可用"时的临时值，算成「规范台班 / 规范人工」就是虚报。

    函数名沿用历史名（调用方多，不做改名手术）；语义就是这里的"非规范来源"。
    SCAFFOLD 若漏判：占位任务会被 `_norm_tier_of` 判成规范档次，并作为
    `NORM_TIER_IS_SPEC` 计入 `critical_norm_coverage` 的分子 → 虚报覆盖率（WS8 缺陷 ②）。
    """
    s = str(code or "").strip().upper()
    return bool(s) and (s.startswith("AI_") or "AI_ESTIMATE" in s
                        or s.startswith("SCAFFOLD"))


def _ai_norm_text_marks_ai(text):
    """这段原文是否点名了 AI 估算定额（旧计划的 `_norm_flagged` 就是这样留痕的）。"""
    s = str(text or "")
    return any(m and m in s for m in _AI_NORM_MARKERS)


def _ai_old_policy_claim(text):
    """这段原文是否在复述**已废除的旧口径**（AI 定额只作参考 / 不参与计算）。

    判据是"提到 AI" + "带旧口径的说法"，两个条件都要满足 —— 只提 AI 不算
    （例如「AI 估算定额单位不可换算」是真实的单位拦截，AI 来源并没有被放行）。
    """
    s = str(text or "")
    if "AI" not in s.upper() and not _ai_norm_text_marks_ai(s):
        return False
    return any(w in s for w in _OLD_POLICY_CLAIMS)


def _ai_flag_marks_ai(text):
    """绑定层的拦截原文是否在说"依据是 AI 估算定额"（政策变更 2026-09-20）。

    ⚠ 带明确"真降级"拦截词（单位不可换算 / 人工否决 / 定额口径不符）的原文**不算**：
    那批任务仍然"没有班组、工期沿用 WBS 估算"，措辞一个字不许改。
    """
    s = str(text or "")
    if any(b in s for b in _NON_AI_BLOCKERS):
        return False
    return _ai_norm_text_marks_ai(s) or _ai_old_policy_claim(s)


def _ai_norm_refs(rd):
    """`_resource_source` 里指向 AI 估算来源的 ``[(资源名, ref)]``（取不到 = 空表）。"""
    out = []
    src = rd.get("_resource_source") if isinstance(rd, dict) else None
    if isinstance(src, dict):
        for name, rec in src.items():
            if not isinstance(rec, dict):
                continue
            origin = str(rec.get("origin") or "").strip().lower()
            ref = str(rec.get("ref") or rec.get("source_code") or "").strip()
            if origin in _AI_NORM_ORIGINS or _ai_norm_source_code_ai(ref):
                out.append((str(name), ref or origin))
    return out


def _ai_norm_state(rd):
    """该任务的**依据**是不是 AI 经验估算定额（政策变更 2026-09-20）。

    返回 ``None`` = 与 AI 无关 / 判不出来 → 调用方**退回旧行为**（如实显示原文，不猜）；
    否则返回::

        {"norm_ai":   bool,   # `_norm_applied` 本身就是 AI 定额（= 本次真的照用算了）
         "applied":   bool,   # 有 `_norm_applied`（不论来源）
         "flag_ai":   bool,   # `_norm_flagged`/`_warning` 原文点名 AI 估算定额
         "flag_text": str,    # 原文（**可能仍带旧政策措辞，渲染时不许复述**）
         "ai_refs":   [(名, ref)],   # `_resource_source` 里的 AI 来源
         "strong":    bool}   # 定额依据本身是 AI → 逐条标注 `AI_NORM_LABEL`
    """
    if not isinstance(rd, dict) or not rd:
        return None
    na = rd.get("_norm_applied")
    na = na if isinstance(na, dict) else {}
    norm_ai = bool(na) and (
        str(na.get("origin") or "").strip().lower() in _AI_NORM_ORIGINS
        or str(na.get("match_type") or "").strip().lower() == "ai"
        or _ai_norm_source_code_ai(na.get("source_code")))
    flag_text = str(rd.get("_norm_flagged") or rd.get("_warning") or "")
    flag_ai = _ai_flag_marks_ai(flag_text)
    ai_refs = _ai_norm_refs(rd)
    if not (norm_ai or flag_ai or ai_refs):
        return None
    return {"norm_ai": norm_ai, "applied": bool(na), "flag_ai": flag_ai,
            "flag_text": flag_text, "ai_refs": ai_refs,
            # 「定额依据本身是 AI」：真的按 AI 定额算了，或旧计划的 AI 拦截文案点名了它。
            # 若已经有一条**非 AI** 的 `_norm_applied`，那依据仍是真人定额 → 只算弱标注。
            "strong": bool(norm_ai or (flag_ai and not na))}


def _ai_norm_weak_note(ai):
    """真人定额 + 个别资源来自 AI 估算时的**弱标注**（逐条说清哪一项是 AI 来源）。

    为什么还要写：场地级设备（塔吊/施工电梯）的台数走 `AI_ESTIMATE_V1` 默认口径，
    这类任务的定额是真人定额、但机械投入的依据是 AI —— 政策变更（2026-09-20）要求
    逐条标出"这条的班组/工期依据来自 AI 经验估算"，不能只靠资源卡那一句总述。
    """
    if not ai or ai.get("strong"):
        return ""
    if not ai.get("ai_refs"):
        return ""
    names = "、".join("%s（%s，无规范依据，待审）" % (n, r) for n, r in ai["ai_refs"][:3])
    return "含 AI 经验估算来源：%s" % names


def _by_reason_display(key):
    """`meta.norm_coverage.by_reason` 那一格的显示文案（政策变更 2026-09-20）。

    原文是**计划 JSON 的原始记录**，一字不改（改了就和计划对不上、也无法追溯），
    但旧政策那些"AI 估算只作参考 / 不作工期证据"的说法在新政策下已经不成立 ——
    逐条挂上澄清，读者不会把原始记录误读成现行口径。
    """
    s = _sanitize_norm_text(key)
    if _ai_old_policy_claim(key) and "AI 经验估算定额已照用" not in s:
        s += "（政策变更 2026-09-20：AI 经验估算定额已照用）"
    return s


def _sanitize_norm_text(text):
    """洗掉旧政策的**假话**（政策变更 2026-09-20）。

    渲染任何取自计划原文的降级/拦截文案前都过一遍：旧政策那句
    「只作参考，不用来算班组」在新政策下已经不成立，出现在交付物里就是假话。
    只替换这一个特征串，其余原文一字不动（不新造、不改写其他内容）。
    """
    s = str(text or "")
    for bad in _FORBIDDEN_AI_NORM_PHRASES:
        s = s.replace(bad, "（政策变更 2026-09-20：AI 经验估算定额已照用）")
    return s


def _degraded_reason(text):
    """「降级清单」里显示的那一格原因（政策变更 2026-09-20）。

    旧政策把 AI 经验估算定额的拦截原文写成
    「AI 估算定额（只作参考，不用来算班组）」/「L4默认定额行仅有 AI 估算，不作工期证据」。
    新政策下 AI 来源本身不再是拦截理由，交付物不许复述这些已废除的口径 —— 一律换成
    如实描述：依据是 AI 经验估算定额（无规范依据、待审），**本次运行未据此计算班组**。
    非 AI 的原因（单位不可换算 / 人工否决 / 定额口径不符）原文保留，只洗掉旧措辞。
    """
    if _ai_norm_text_marks_ai(text) or _ai_old_policy_claim(text):
        return "%s；本次运行未据此计算班组" % AI_NORM_LABEL
    return _sanitize_norm_text(text)


# §5「单位与定额降级清单」的引导句。清单里混进 AI 经验估算定额行时口径要说全，
# 否则引导句那句「其定额仅作参考、不参与工期与资源计算」会把 AI 那一档说错。
NORM_DEGRADED_LEAD = ("下列任务的定额单位与工程量单位不一致或绑定被标记，"
                      "其定额仅作参考、不参与工期与资源计算：")


def _degraded_lead(degraded):
    """按清单内容选引导句（政策变更 2026-09-20）。"""
    if any(r.get("ai") for r in (degraded or [])):
        return ("下列任务的定额本次未用于计算工期与班组：单位与工程量单位不一致且不可换算的，"
                "其定额仅作参考；标「%s」的依据是 AI 经验估算定额，本次运行未据此计算班组："
                % AI_NORM_LABEL)
    return NORM_DEGRADED_LEAD


# 置信度章节 §1 里 AI 条数那一行的两种标签（政策变更 2026-09-20）。
# `released_ai` 来自上游（= 已放行并参与计算）时用第一句；上游还没写这个键、由交付侧
# 从任务级数据推出来的条数用第二句（那批任务本次并未据此算出班组，不许说"已放行"）。
AI_NORM_ROW_LABEL = "其中：AI 经验估算定额（无规范依据，已按来源放行并逐条标注）"
AI_NORM_ROW_LABEL_LEGACY = ("其中：AI 经验估算定额（无规范依据，待审；"
                            "逐条标注见进度计划表「依据 / 资源」列）")


def _ai_norm_counts(plan, cov=None):
    """AI 经验估算定额的**条数**及其状态（政策变更 2026-09-20）。

    返回 ``(total, applied, from_meta)`` 或 ``None``（数不出来 → 调用方**整行不出**，
    绝不用 0 充数）::

        total      依据为 AI 经验估算定额的任务条数
        applied    其中**本次已据此算出班组/工日**的条数
        from_meta  总数是否取自上游 `meta.norm_coverage.released_ai`（新口径）

    数字来源优先级（缺失不编）：
      ① `meta.norm_coverage.released_ai` —— 上游口径。上游只在"已放行并参与计算"时写它，
         故此时 applied == total。
      ② 上游还没写这个键 → 从任务级数据自己数（判据见 `_ai_norm_state`，`strong` 才算
         "依据是 AI 定额"；弱标注那批的定额仍是真人定额，不计入本条数）。
    """
    cov = cov if isinstance(cov, dict) else {}
    n = cov.get("released_ai")
    if n is not None:
        try:
            n = int(n)
        except (TypeError, ValueError):
            n = None
        if n is not None:
            return (n, n, True) if n > 0 else None
    tasks = (plan.get("resource_demand") or {}).get("tasks") or []
    if not isinstance(tasks, (list, tuple)) or not tasks:
        return None                      # 没有任务级数据 → 数不出来（不是 0）
    total = applied = 0
    for t in tasks:
        st = _ai_norm_state(t)
        if st and st["strong"]:
            total += 1
            if st["norm_ai"]:
                applied += 1
    return (total, applied, False) if total > 0 else None


def _ai_norm_caliber_para(total, applied, from_meta):
    """「AI 经验估算定额已参与工期与班组计算」的说明段（政策变更 2026-09-20）。

    政策口径与**本次运行的实际状态**分开写：新计划里这批 AI 定额真的算了班组；旧计划里
    它们按当时的旧口径没算（`applied == 0`）。两种都如实说，绝不含糊成"已参与"。
    """
    head = ("政策口径（用户 2026-09-20 决定）：AI 经验估算定额（无规范依据，待审）"
            "与真人定额同等参与工期与班组计算，来源无规范依据，导入真实规范后应整体清退；"
            "本次计划中依据为 AI 经验估算定额的任务 %d 条，已逐条标注在进度计划表"
            "「依据 / 资源」列。" % total)
    if applied >= total and total > 0:
        tail = "本次运行中这些任务均已据此算出班组与工日。"
    elif applied <= 0:
        tail = ("本次运行的这些任务未据此计算班组（工期沿用 WBS 估算）—— 它们是本次运行"
                "按已废除的旧口径留下的记录，交付物照实标注，不替它们编班组。")
    else:
        tail = ("本次运行中其中 %d 条已据此算出班组与工日，其余 %d 条未据此计算班组"
                "（工期沿用 WBS 估算）。" % (applied, total - applied))
    return head + tail


def _rd_task_map(plan):
    """``{task_id: resource_demand.tasks[*]}`` —— 逐条工序的定额/资源依据（只读，显示用）。"""
    out = {}
    for t in ((plan.get("resource_demand") or {}).get("tasks") or []):
        if isinstance(t, dict) and t.get("task_id"):
            out[str(t["task_id"])] = t
    return out


def _source_refs(rd):
    """定额来源代号（`_resource_source` → ref，取不到再退回 `_norm_applied.source_code`）。"""
    refs = []
    src = rd.get("_resource_source") if isinstance(rd, dict) else None
    if isinstance(src, dict):
        for _name, rec in src.items():
            if not isinstance(rec, dict):
                continue
            ref = str(rec.get("ref") or "").strip()
            if ref and ref not in refs:
                refs.append(ref)
    if not refs:
        norm = rd.get("_norm_applied") if isinstance(rd, dict) else None
        if isinstance(norm, dict) and norm.get("source_code"):
            refs.append(str(norm["source_code"]))
    return refs


def _is_flagged(rd):
    """该任务是不是"没算出来"（无可用定额：定额不可作证据 / 量级不可信）。

    ⚠ 政策变更（2026-09-20）后，**AI 经验估算定额**那批不该再被当作"没算出来"：
    它们的依据是 AI 定额（`_ai_norm_state(...)["strong"]`），交付侧换成逐条标注
    「AI 经验估算定额（无规范依据，待审）」。这里保持原判据（字段是否带标记）不动，
    由调用方先分流 AI 那一支（见 `_evidence_text`）。
    """
    return bool(isinstance(rd, dict) and (rd.get("_norm_flagged") or rd.get("_warning")))


def _evidence_core(rd, leaf=None):
    """逐条工序的**依据 / 资源**一句话（看板与 Word 一字不差）。

    优先级（字段全部来自资源层产物，**拿不到就写"来源未记录"，绝不猜**）：
      ① `_unit_assumed`  → 单位按**写明来源的**换算参数换算（写明过程与结果）+ 定额来源 + 班组/工日；
      ② 依据是 AI 经验估算定额（政策变更 2026-09-20）→ 标注
         「AI 经验估算定额（无规范依据，待审）」+ 本次是否据此算了班组 + 班组/机械；
      ③ 真降级（单位不可换算 / 人工否决 / 无定额绑定）→
         ⚠ 无可用定额：<原因原文>（工期沿用 WBS 估算，未计算班组）—— **一个字不许改**；
      ④ 有 `resources` → 定额来源 + 班组 + 机械（场地级设备另标口径）；
      ⑤ 什么都没有 → 来源未记录。

    `leaf`（WBS 叶子）用来读换算参数的来源键 `norm_binding.ctx_source` —— 见
    `unit_assumption_source`：**不是 AI 算出来的，不许标成 AI**（虚假溯源）。
    叶子拿不到就按任务行自己的 `ctx_source` 判，再拿不到就如实写「换算参数来源未记录」。

    ⚠ 政策变更（2026-09-20）：旧政策那句「只作参考，不用来算班组」已经不成立，
    任何分支都不许复述（`_sanitize_norm_text` 是最后一道保险）。
    """
    if not isinstance(rd, dict) or not rd:
        return "来源未记录（该任务没有资源记录）"
    refs = _source_refs(rd)
    ai = _ai_norm_state(rd)
    labor, machine = _crew_and_machine_text(rd)
    body = []
    if labor:
        body.append("班组 " + labor)
    if machine:
        body.append("机械 " + machine)
    # 依据是 AI 经验估算定额 → 定额来源头换成政策文案（其余来源代号保留，可溯源）。
    if ai and ai["strong"]:
        others = [r for r in refs if not _ai_norm_source_code_ai(r)]
        head = "、".join([AI_NORM_LABEL] + others[:2])
    else:
        # 弱标注（真人定额 + 个别资源来自 AI）时，AI 代号已在弱标注里点名，
        # 来源头里不再重复列它（避免同一行两处出现 AI_ESTIMATE_V1）。
        _head_refs = ([r for r in refs if not _ai_norm_source_code_ai(r)]
                      if ai is not None else refs)
        head = ("定额 " + "、".join(_head_refs[:3])) if _head_refs else "来源未记录"
    weak = _ai_norm_weak_note(ai)
    assumed = rd.get("_unit_assumed")
    if assumed:
        # 换算参数的**来源**按 `ctx_source` 分流（唯一真源见 `unit_assumption_source`）：
        #   norm_condition → 「定额条件档位换算参数（依据定额行适用条件，非 AI 估算）」
        #   ai_estimate    → 「AI估算换算参数」（仅当来源键真的这么写）
        #   缺失 / 认不出   → 「换算参数来源未记录」，**绝不默认归到 AI**
        _src, _src_label, _src_ev = unit_assumption_source(leaf, rd)
        _prefix = (("单位换算按%s：" % _src_label) if _src
                   else "单位换算（%s）：" % _src_label)
        _ev = ("；换算参数依据：%s" % _src_ev) if _src_ev else ""
        out = "%s%s%s；%s" % (_prefix, assumed, _ev, "；".join([head] + body))
        return out + (("；" + weak) if weak else "")
    if ai and ai["strong"]:
        # `unused`：定额依据是 AI、且本次**没有** AI 的 `_norm_applied` —— 这批任务这一回
        # 并没有因此拿到班组，交付物必须说清楚（旧计划里 134 条就是这个状态）。
        unused = not ai["norm_ai"]
        if unused and body:
            out = ("%s；本计划未按该定额计算班组（工期沿用 WBS 估算，未计算班组）；"
                   "已投入资源：%s" % (head, "；".join(body)))
        elif unused:
            out = ("%s；本计划未按该定额计算班组（工期沿用 WBS 估算，未计算班组）" % head)
        else:
            out = "；".join([head] + body)
        return out + (("；" + weak) if weak else "")
    if _is_flagged(rd):
        # 真降级（单位不可换算 / 人工否决 / 无定额绑定）：这句话**仍然是真的**，不许删。
        reason = _sanitize_norm_text(rd.get("_norm_flagged") or rd.get("_warning") or "")
        tail = ("；已投入资源：" + "；".join(body)) if body else ""
        return ("⚠ 无可用定额：%s（工期沿用 WBS 估算，未计算班组）%s" % (reason, tail))
    if body and not _rd_of(rd).get("_norm_applied"):
        # D4（合同 §9.1）：这一行**没有定额依据** —— 工期与人数来自模型，原因如实写出来。
        # 不在上面 ③ 真降级那支里：这类行没有拦截标记，但同样"没有定额依据"，
        # 用户必须能一眼看出来（否则只看到来源代号空着，以为我们忘了写）。
        reason, _code = _norm_missing_reason(rd, None)
        # 来源代号（`_resource_source` 里的 kb 定额代号）该留的仍然留 —— 它证明确实绑过定额，
        # 只是这次没被用来算工期/班组；只把"来源未记录"这种无信息量的头省掉。
        _parts = ([head] if head and head != "来源未记录" else [])
        _parts.append(_norm_missing_sentence(reason))
        _parts.extend(body)
        out = "；".join(_parts)
        return out + (("；" + weak) if weak else "")
    if body:
        out = "；".join([head] + body)
        return out + (("；" + weak) if weak else "")
    return "来源未记录（无资源记录）"


def _evidence_text(rd, leaf=None):
    """`_evidence_core` + **D6 来源档次前缀**（合同 §9.2：每条工序标注档次）。

    前缀只在该工序的来源**判得出来**时加（有 `_norm_applied` / 有资源记录 / 有拦截标记）；
    "什么都没有"的行不加前缀 —— 那种行本来就写着「来源未记录」，贴一个档次反而是编的，
    也与既有逐字回归门（`test_delivery_ai_norm_label` 的那两条精确断言）冲突。
    """
    core = _evidence_core(rd, leaf)
    if not isinstance(rd, dict) or not rd:
        return core
    _known = (rd.get("_norm_applied") or rd.get("_norm_flagged") or rd.get("_warning")
              or _rd_has_resources(rd))
    if not _known:
        return core
    tier, _note = _norm_tier_of(rd)
    return "【%s】%s" % (tier, core)


def _group_evidence(ids, rd_map):
    """合并展示行（组）的依据摘要：组内来源代号 + AI 条数 + 有几项"没算出来"。"""
    refs, flagged, ai_n, ai_weak = [], 0, 0, 0
    for i in ids:
        rd = rd_map.get(str(i)) or {}
        for r in _source_refs(rd):
            if r not in refs:
                refs.append(r)
        st = _ai_norm_state(rd)
        if st and st["strong"]:
            # 政策变更（2026-09-20）：AI 经验估算定额单独计数、单独措辞，不再混进
            # 「无可用定额」那一类（那类说的是"真的没算出来"）。
            ai_n += 1
        elif st and st.get("ai_refs"):
            # 弱标注（真人定额 + AI 来源的场地级设备台数）也要逐条说清楚。
            ai_weak += 1
        elif _is_flagged(rd):
            flagged += 1
    parts = [("定额 " + "、".join(refs[:4])) if refs else "来源未记录"]
    if ai_n:
        parts.append("%d 项依据为 %s（逐条见进度计划表）" % (ai_n, AI_NORM_LABEL))
    if ai_weak:
        parts.append("%d 项含 AI 经验估算来源（设备台数按 AI 默认口径，无规范依据，待审）"
                     % ai_weak)
    if flagged:
        parts.append("⚠ 其中 %d 项无可用定额（工期沿用 WBS 估算，未计算班组）" % flagged)
    return "；".join(parts)


def _crew_and_machine_text(rd):
    """该任务的班组（人 + 工日）与机械（台/日，场地级额外标注）。"""
    res = rd.get("resources") if isinstance(rd, dict) else None
    res = res if isinstance(res, dict) else {}
    site = set()
    for item in (rd.get("_site_equipment") or []):
        if isinstance(item, dict) and item.get("name"):
            site.add(str(item["name"]))
    labor, machine = [], []
    for name, q in res.items():
        pd = q.get("per_day") if isinstance(q, dict) else q
        td = q.get("total_days") if isinstance(q, dict) else None
        if _is_labor(name):
            if name in MACHINE_CREW:
                machine.append("%s %s 人/日" % (name, _fnum(pd)))
            else:
                labor.append("%s %s 人%s" % (name, _fnum(pd),
                                             ("（%s 工日）" % _fnum(td)) if td else ""))
        else:
            machine.append("%s %s 台/日%s" % (name, _fnum(pd),
                                              "（场地级·逐日取max）" if name in site else ""))
    if not labor:
        crew = rd.get("_crew") if isinstance(rd.get("_crew"), dict) else {}
        labor = ["%s %s 人" % (k, _fnum(v)) for k, v in sorted(crew.items()) if v]
    return "、".join(labor), "、".join(machine)


# `resource_plan.peak_manpower_source` → 人话。键名由计划端
# （`plan_assembler.resource_plan`）给出，交付侧**只做忠实翻译**：认不出来就原样打出来，
# 绝不替它编一个口径名。
#
# ⚠ E1（用户 2026-09-21 裁定）：这里只管**该展示哪个数**（`user` = 用户明确给的限额 /
# `resource_curve` = 逐日曲线实算）。旧口径里的 `model_estimate`（模型替用户补的申报值）
# 已随病根 3 一起删除：**申报峰值不再是一个展示项**，计划端也不再落盘
# `declared_peak_manpower` / `declared_peak_manpower_source`。
_PEAK_SOURCE_LABELS = {
    "user": "用户给定上限",
    "model_estimate": "模型估算，非用户输入",
    "resource_curve": "资源曲线口径",
}


def _peak_caliber(plan, view):
    """峰值人数（`resource_plan.peak_manpower`）的**口径**。

    背景（用户实测质问）：交付物过去把 `peak_manpower` 一律写成"用户限额口径"，
    可实测那份计划里 120 人根本不是用户给的 —— 是模型估的。把估算说成用户输入，
    用户第一反应就是"我什么时候说过 120 人"。口径由计划端给出
    （`peak_manpower_source` = user / resource_curve），交付侧照译。

    E1（2026-09-21 裁定）：**「申报峰值」这一展示项已整体删除** —— 旧实现会把
    `declared_peak_manpower`（模型补的 120）用「计划里另记申报峰值 …」再印一遍，
    等于把 AI 编的数又摆回用户面前。模型补的申报值**连源头一起删**，交付物不再有它；
    只有用户明确给的限额（`source == "user"`）才继续显示，并标清来源 = 用户。

    返回 ``None`` = 计划端没给这个键（旧计划）→ 调用方**退回老显示**，不报错、不猜。
    否则返回 ``{"source": ..., "label": "用户给定上限", "peak": 120,
    "text": "120 人（用户给定上限）"}``。
    """
    rp = plan.get("resource_plan") if isinstance(plan.get("resource_plan"), dict) else {}
    src = rp.get("peak_manpower_source")
    src = str(src).strip() if src else ""
    if not src:
        return None
    # 认不出的来源不硬套"资源曲线口径"（那也是一句没依据的话），原样打出来
    label = _PEAK_SOURCE_LABELS.get(src) or ("口径 %s" % src)
    peak = rp.get("peak_manpower")
    if peak is None:                      # 语义修正后的兜底：没给申报值就退回曲线峰值
        peak = rp.get("curve_peak_manpower")
    if peak is None and isinstance(view, dict):
        peak = view.get("peak_total")
    if peak is None:
        return None
    return {"source": src, "label": label, "peak": peak,
            "text": "%s 人（%s）" % (_fnum(peak), label)}


def _peak_curve_diff(plan, view):
    """交付侧曲线峰值 vs 调度器 `curve_peak_manpower` 的差，**从数据算出来**。

    背景（实测，`replay_org8_r_full.json`）：调度器口径 85 人，交付侧重算 88 人。
    查清后不是 bug、是两个口径：

      · 调度器 `resource_plan.curve_peak_manpower` = **任务级**配员逐日叠加的峰值；
      · 交付侧 `view["peak_total"]` 额外含**场地级设备配员**（塔吊/施工电梯的司机、
        信号工），按**逐日 max** 计入（同一天 5 条任务要塔吊也只算 1 台）。

    实测峰值日 D417（2027-07-23）：任务级 85（瓦工 81 + 混凝土工 4）+ 场地级 3
    （司机 2 + 信号工 1）= 88；把场地级摘掉的独立重算峰值 = 85 = 调度器的数
    （两条独立计算互相印证），Σ曲线 - Σattendance 的 964 也 100% 归因到
    「场地级配员 887 + 无组织层任务 77」、残差 0。

    所以这里不许写死 3：`task_only_peak` 与 `site_peak` 都是**另算一遍**得出的，
    只有「摘掉场地级后恰好等于调度器口径」时才敢把差额归因给场地级配员。
    返回 ``None`` = 两个数一致 / 键缺失 → 调用方一个字都不加。
    """
    rp = plan.get("resource_plan") if isinstance(plan.get("resource_plan"), dict) else {}
    curve = rp.get("curve_peak_manpower")
    if curve is None:
        return None
    daily = (view or {}).get("labor_daily") or []
    if not daily:
        return None
    view_peak = view.get("peak_total")
    try:
        view_peak = int(view_peak)
        curve = int(curve)
    except (TypeError, ValueError):
        return None
    if view_peak == curve:
        return None
    peak_day = max(range(len(daily)), key=lambda i: daily[i].get("total") or 0)
    site_peak = int(daily[peak_day].get("site") or 0)                 # 峰值日的场地级贡献
    task_only_peak = max(int(x.get("total") or 0) - int(x.get("site") or 0) for x in daily)
    if task_only_peak != curve:
        # 摘掉场地级后仍对不上调度器口径 → 差额不能全归因给场地级配员，如实少写
        return {"curve_peak": curve, "view_peak": view_peak,
                "task_only_peak": task_only_peak, "site_peak": site_peak,
                "attributed": False, "sentence": (
                    "峰值人数口径：本页峰值 %d 人 = 逐日人员曲线峰值；调度器口径"
                    "（resource_plan.curve_peak_manpower）%d 人，差额 %d 人未完全"
                    "归因到场地级设备配员（摘掉场地级配员后为 %d 人，仍与调度器差 %d 人），"
                    "两个口径都列出、不合并。"
                    % (view_peak, curve, view_peak - curve, task_only_peak,
                       task_only_peak - curve))}
    # 逐日场地级配员的总额与人·日（"哪几天"也要能从数据说清）
    site_days = sum(1 for x in daily if (x.get("site") or 0) > 0)
    site_pd = sum(int(x.get("site") or 0) for x in daily)
    trades = daily[peak_day].get("site_trades") or {}
    trade_txt = "、".join("%s %s 人" % (k, _fnum(v))
                          for k, v in sorted(trades.items(), key=lambda kv: -float(kv[1])))
    return {"curve_peak": curve, "view_peak": view_peak,
            "task_only_peak": task_only_peak, "site_peak": site_peak,
            "site_days": site_days, "site_person_days": site_pd,
            "peak_day": daily[peak_day].get("date"), "site_trades": trades,
            "attributed": True, "sentence": (
                "峰值人数口径：本页「峰值人数 %d 人」= 逐日人员曲线峰值（任务级配员 + "
                "场地级设备配员）。其中任务级配员峰值 %d 人 —— 与调度器口径"
                "（resource_plan.curve_peak_manpower = %d 人，按任务叠加、不含场地级配员）"
                "一致；余下 %d 人来自场地级设备配员（%s，逐日取 max：同一天多条任务要塔吊"
                "也只算 1 台；全场 %d 人·日、分布在 %d 天）。调度器口径不含这一份，"
                "两者不是同一个数。"
                % (view_peak, task_only_peak, curve, site_peak,
                   trade_txt or "场地级设备配员", site_pd, site_days))}


def _equipment_binding_rows(plan):
    """用户申报设备 → 是否绑定到计划里的机械资源（逐项对账）。

    读 `meta.equipment_binding`（计划端写入）。兼容两种落盘形态：
      · `{用户申报名: {"declared": 台数, "bound_to": 资源名或 None, "effective": bool,
                       "note": 中文说明}}`（`scheduler.equipment_binding_report` 原形）；
      · `[{name, quantity, bound_to, note}, ...]`（列表形态）。
    **未生效的排在最前面**：用户申报了却被排程当没看见，是必须显眼的事。
    缺字段/没这段数据 → 返回 []（调用方整段不出）。
    """
    meta = plan.get("meta") if isinstance(plan.get("meta"), dict) else {}
    raw = meta.get("equipment_binding")
    items = []
    if isinstance(raw, dict):
        for k, v in raw.items():
            items.append((str(k), v if isinstance(v, dict) else {"declared": v}))
    elif isinstance(raw, list):
        for v in raw:
            if not isinstance(v, dict):
                continue
            items.append((str(v.get("name") or v.get("resource") or ""), v))
    else:
        return []
    rows = []
    for key, rec in items:
        name = rec.get("name") or rec.get("resource") or key
        if not name:
            continue
        qty = rec.get("quantity")
        if qty is None:
            qty = rec.get("declared")
        eff = rec.get("effective")
        if eff is None:                      # 没给 effective 就按"绑到资源即生效"判
            eff = bool(rec.get("bound_to"))
        note = rec.get("note") or ""
        if not eff and not note:
            note = "未匹配到计划中的机械资源，该限额未生效"
        rows.append({"name": name, "quantity": qty, "effective": bool(eff),
                     "bound_to": rec.get("bound_to"), "note": str(note)})
    rows.sort(key=lambda r: (r["effective"], r["name"]))     # 未生效的在前
    return rows


# ============================================================
# 设备清单的**三态**（第 44 轮：不许再把"本次没取得"说成"用户没申报"）
# ============================================================
# 真实事故（`输出结果/计划_plan_sample3_after_org_v2/计划看板.html` 原文）：
#   「用户申报设备限额对账 本计划无用户申报设备限额（equipment_binding 为空），
#     所有设备台数均为 AI 默认口径。」
# 而真相是 `meta.boundary_conditions.equipment == []` 且 `_source["equipment"] == "model"`
# —— 模型**答了**，只是把设备项留空，本次**根本没有取得设备清单**。
# "没取得清单" 与 "用户没申报" 是两件事：把系统的缺口写成用户的缺失就是甩锅。
# 判据全部来自数据（`boundary_conditions` / `_source` / `equipment_binding` /
# `node_warnings`）；三态的文案由 `equipment_declared_sentence` 一处产出，看板 / Word /
# facts 共用同一句话，口径不可能漂移。
EQUIPMENT_SECTION_TITLE = "用户申报设备限额对账"
EQUIPMENT_FALLBACK_COMMENT = "<!-- 追加设备清单口径段"
#: 三态各自的判据标记（"这句实话在不在页面上"）。
EQUIPMENT_EMPTY_MARKER = "未取得设备清单"
EQUIPMENT_MODEL_MARKER = "未作为限额使用"
EQUIPMENT_UNMARKED_MARKER = "来源未标注"
#: 模型补的估算必须点明的那半句（用户原话要求："明确标注…非用户输入，且未作为限额使用"）。
MODEL_EQUIPMENT_LABEL = "模型按常见做法补的估算，非用户输入，且未作为限额使用"
EMPTY_EQUIPMENT_TEXT = ("本次未取得设备清单（边界条件的设备项为空），"
                        "因此没有可对账的申报设备；这不等于「你没有申报设备」。")
#: 与设备 / 边界条件相关的告警判据（**只用于计数**，不用于改文案）。
EQUIPMENT_WARNING_MARKERS = ("设备", "机械", "边界条件", "三类全空", "equipment", "boundary")


def _equipment_items_text(items):
    """设备清单 → 「塔吊 1 台、施工电梯 1 台」（数量 / 单位缺就不编）。"""
    parts = []
    for it in items:
        name = str(it.get("name") or "").strip()
        if not name:
            continue
        qty = it.get("quantity")
        unit = str(it.get("unit") or "").strip()
        if qty in (None, ""):
            parts.append("%s%s" % (name, ("（%s）" % unit) if unit else ""))
        else:
            qty_txt = _fnum(qty)
            if qty_txt is None:                 # 非数字（"若干"这类）：原样抄，不编
                qty_txt = str(qty)
            parts.append(("%s %s %s" % (name, qty_txt, unit)).strip() if unit
                         else ("%s %s" % (name, qty_txt)))
    return "、".join(parts)


def _equipment_qty_text(qty):
    """设备数量 → 单元格文本（缺 / 非数字都不编：缺写「—」，非数字原样抄）。"""
    if qty in (None, ""):
        return "—"
    txt = _fnum(qty)
    return str(qty) if txt is None else txt


def _boundary_equipment_items(bc):
    """`boundary_conditions.equipment` → 规范化条目（字符串条目也认）。取不到 → []。"""
    raw = bc.get("equipment") if isinstance(bc, dict) else None
    if not isinstance(raw, (list, tuple)):
        return []
    out = []
    for it in raw:
        if isinstance(it, dict):
            if not it:
                continue
            out.append({"name": str(it.get("name") or it.get("resource")
                                    or it.get("item") or ""),
                        "quantity": it.get("quantity"),
                        "unit": it.get("unit")})
        elif isinstance(it, str) and it.strip():
            out.append({"name": it.strip(), "quantity": None, "unit": ""})
    return [x for x in out if x["name"]]


def equipment_declared_state(plan):
    """设备清单的**三态判据**（看板 / Word / facts 共用的唯一判据）。

    返回 `(state, items, source)`：
      · `"user"`     —— `_source["equipment"] == "user"` 且清单非空（用户真申报了 → 逐条对账）；
      · `"model"`    —— `_source["equipment"] == "model"` 且清单非空（模型补的估算，非限额）；
      · `"empty"`    —— 清单为空（**无论 `_source` 是什么**）：本次没取得清单；
      · `"unmarked"` —— 有清单但来源没标注（`_source` 认不出）→ 照抄来源，不替它猜；
      · `None`       —— `boundary_conditions` / `_source` 缺失（**老计划 → 按旧行为**）。
    """
    if not isinstance(plan, dict):
        return None, [], None
    meta = plan.get("meta") if isinstance(plan.get("meta"), dict) else {}
    bc = meta.get("boundary_conditions")
    if not isinstance(bc, dict):
        return None, [], None
    src = bc.get("_source")
    if not isinstance(src, dict):
        return None, [], None            # 老计划没有来源标注：按旧行为走（既有逐字门钉着）
    items = _boundary_equipment_items(bc)
    declared = str(src.get("equipment") or "").strip()
    if not items:
        return "empty", [], declared
    if declared == "user":
        return "user", items, declared
    if declared == "model":
        return "model", items, declared
    return "unmarked", items, declared


def _equipment_related_warning_count(plan):
    """与设备 / 边界条件相关的节点级告警**条数**（从 `meta.node_warnings` 现算）。

    取不到键 / 类型不是列表 → `None`（"不知道"）——调用方一个字都不加，绝不当成 0。
    """
    if not isinstance(plan, dict):
        return None
    meta = plan.get("meta") if isinstance(plan.get("meta"), dict) else {}
    warns = meta.get("node_warnings")
    if not isinstance(warns, (list, tuple)):
        return None
    total = 0
    for w in warns:
        if not isinstance(w, dict):
            continue
        text = "%s %s %s" % (w.get("node") or "", w.get("message") or "",
                             w.get("detail") or "")
        if any(m in text for m in EQUIPMENT_WARNING_MARKERS):
            try:
                total += max(1, int(w.get("count") or 1))
            except (TypeError, ValueError):
                total += 1
    return total


def equipment_declared_sentence(plan):
    """设备清单那一节的**一句话** —— 返回 `(state, text)`；看板 / Word / facts 一字不差。

    为什么三态必须分开（真实产物实证，见本节顶部注释）：
      · user  → 用户真申报了：照旧逐条对账（是否生效 + 原因），由既有对账块渲染；
      · model → 清单是模型按常见做法补的：必须点明"非用户输入、且未作为限额使用"；
      · empty → 本次没取得清单：**不许**写成"用户未申报"（那是甩锅给用户）。
    """
    state, items, _src = equipment_declared_state(plan)
    if state is None or state == "user":
        return state, ""
    if state == "model":
        return state, "%s：%s。" % (MODEL_EQUIPMENT_LABEL,
                                   _equipment_items_text(items) or "（清单为空）")
    if state == "unmarked":
        return state, ("设备清单（来源未标注）：%s。清单来自边界条件，但没有标明是用户申报"
                       "还是模型补齐，交付侧不替它猜来源。"
                       % (_equipment_items_text(items) or "（清单为空）"))
    text = EMPTY_EQUIPMENT_TEXT
    n = _equipment_related_warning_count(plan)
    if n:
        text += ("本次运行有 %d 条与设备/边界条件有关的节点级告警"
                 "（见「节点级告警」一节）。" % n)
    return state, text


def _equipment_declared_block_html(plan):
    """设备清单那一节的看板 HTML（设备三态文案**唯一**一处生成点）。

    `state is None`（老计划，没有来源标注）→ 一字不动地走既有对账块：那段有逐字回归门
    （`test_delivery_workface_visible.py::test_设备对账把未生效的限额显形`）。
    用户申报且排程端给了对账结果 → 同一段既有对账块（语义与文案都不变）。
    """
    state, items, _src = equipment_declared_state(plan)
    rows = _equipment_binding_rows(plan)
    e = _html.escape

    def _legacy_binding_block():
        """既有「用户申报设备限额对账」块（**逐字不动**；state 为 None / user 时共用）。"""
        if not rows:
            return ""
        eb_rows = "".join(
            f"<tr><td>{e(str(r['name']))}</td>"
            f"<td>{'—' if r['quantity'] is None else e(str(r['quantity']))}</td>"
            f"<td>{'生效' if r['effective'] else '⚠ 未生效'}</td>"
            f"<td>{e(r['note'])}</td></tr>" for r in rows)
        return (
            "<div class='rs-row'><b>用户申报设备限额对账：</b>"
            "<details><summary>查看逐条（是否生效）</summary>"
            "<table><tr><th>用户申报设备</th><th>数量</th><th>是否生效</th><th>说明</th></tr>"
            f"{eb_rows}</table></details>"
            + ("<div style='color:#c0392b'>未匹配的设备限额没有参与排程，"
               "如需生效请在计划里给它们安排工序。</div>"
               if any(not r["effective"] for r in rows) else "")
            + "</div>")

    if state is None:
        return _legacy_binding_block()
    if state == "user":
        block = _legacy_binding_block()
        if block:
            return block
        return ("<div class='rs-row'><b>%s：</b>边界条件里用户申报了 %s；排程端没有返回逐条"
                "对账结果（equipment_binding 为空），因此无法判断这些设备限额有没有生效。</div>"
                % (e(EQUIPMENT_SECTION_TITLE),
                   e(_equipment_items_text(items) or "（清单为空）")))
    _state, text = equipment_declared_sentence(plan)
    if not text:
        return ""
    if state == "empty":
        return ("<div class='rs-row'><b>%s：</b>%s</div>"
                % (e(EQUIPMENT_SECTION_TITLE), e(text)))
    head = ("<b>%s：</b>%s" % (e("设备清单（模型补充）" if state == "model" else "设备清单"),
                              e(text)))
    tbl = ""
    if items:
        src_label = MODEL_EQUIPMENT_LABEL if state == "model" else "来源未标注"
        rows_html = "".join(
            "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
                e(str(i["name"])), e(_equipment_qty_text(i.get("quantity"))),
                e(str(i.get("unit") or "—")), e(src_label))
            for i in items)
        tbl = ("<details><summary>查看逐项（共 %d 项）</summary>"
               "<table><tr><th>设备</th><th>数量</th><th>单位</th><th>来源</th></tr>"
               "%s</table></details>" % (len(items), rows_html))
    return "<div class='rs-row'>%s%s</div>" % (head, tbl)


def _equipment_declared_fact(plan):
    """交给 LLM 编排的**设备清单事实**（含"这一节该怎么写"的硬约束）。

    为什么连该写的那句话一起给：真实产物里模型把"本次没取得设备清单"写成了
    "用户没申报设备限额"（甩锅）。`equipment_binding: []` 确实容易被读成"用户没给"，
    所以这里把判据（`state`）与结论（`sentence`）一并给出，并要求原样使用。
    """
    state, items, src = equipment_declared_state(plan)
    _s, sentence = equipment_declared_sentence(plan)
    how = ("本节文案必须取自 sentence（原样使用）；清单为空时**不许**写成"
           "「用户未申报设备」/「无用户申报设备限额」——那是把「本次没取得清单」"
           "说成用户的缺失。")
    if state is None:
        how = ("老计划没有边界条件来源标注：本节按既有对账表渲染，缺数据就一个字不写，"
               "不许猜来源。")
    return {"state": state if state is not None else "legacy",
            "source": src, "items": items, "sentence": sentence,
            "how_to_write": how}


# ============================================================
# 节点级告警（第 44 轮：meta 里早就有，交付物一个字都没印）
# ============================================================
# `plan_assembler.build_meta` 恒写 `node_warnings` / `node_warning_count` /
# `model_call_failures`（见 plan_assembler.py:856-870），而看板与 Word 里一个字都不提
# —— 用户拿到的产物看起来完全正常，不知道"这次模型没帮上忙"。
NODE_WARNINGS_TITLE = "节点级告警"
NODE_WARNINGS_FALLBACK_COMMENT = "<!-- 追加节点级告警段"
#: 逐条清单行数达到这个量就折进 <details>（与 CONfIDENCE_DETAIL_MIN_ROWS 同一做法）。
NODE_WARNINGS_DETAIL_MIN_ROWS = 3


def _model_failure_count(items):
    """现算"模型调用失败"条数 —— 用 `plan_assembler` 的同一判据函数（唯一真源）。"""
    try:
        from .plan_assembler import model_call_failures_of
        return len(model_call_failures_of(items))
    except Exception:
        return 0


def node_warnings_model(plan):
    """节点级告警的展示模型 → `{"count", "failure_count", "items"}`；取不到 → `None`。

    判据只认数据：
      · `meta["node_warnings"]` 不是列表（键缺失 / 类型坏）→ `None` —— **取不到就不显示**，
        绝不猜成"没有告警"；
      · N 优先取 `meta["node_warning_count"]`（`build_meta` 在同一份数据上算出的显式条数），
        缺键 → 现算（`plan_assembler.model_call_failures_of` 那套"同一个判据函数"的做法）；
      · M 优先取 `len(meta["model_call_failures"])`，缺键 → 用判据函数现算。
    """
    if not isinstance(plan, dict):
        return None
    meta = plan.get("meta") if isinstance(plan.get("meta"), dict) else None
    if not isinstance(meta, dict):
        return None
    raw = meta.get("node_warnings")
    if not isinstance(raw, (list, tuple)):
        return None                     # 老计划 / 坏数据：取不到就不显示
    # 空壳条目（`{}` / 全空字符串）不携带任何信息，不算一条 —— 与
    # `plan_assembler._as_warning_list` 的"看不懂就不当有值"同向。
    items = [dict(w) for w in raw if isinstance(w, dict)
             and any(str(w.get(k) or "").strip() for k in ("node", "message", "detail"))]
    count = meta.get("node_warning_count")
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        count = len(items)
    failures = meta.get("model_call_failures")
    if isinstance(failures, (list, tuple)):
        fail_count = len(failures)
    else:
        fail_count = _model_failure_count(items)
    return {"count": int(count), "failure_count": int(fail_count), "items": items}


def node_warnings_summary_line(model):
    """摘要行（N / M **全部从数据算**，一个字都不写死）。"""
    return ("本次运行有 %d 条节点级告警，其中 %d 条是模型调用失败。"
            % (int(model["count"]), int(model["failure_count"])))


def _node_warnings_card_html(plan):
    """看板「节点级告警」卡片：**count == 0 时返回空串**（不空表、不占位）。"""
    model = node_warnings_model(plan)
    if not model or model["count"] <= 0:
        return ""
    e = _html.escape
    out = ["<div class='card'><h2>%s</h2>" % e(NODE_WARNINGS_TITLE),
           "<div class='rs-row'>%s</div>" % e(node_warnings_summary_line(model))]
    items = model["items"]
    if items:
        table = ("<table><tr><th>节点</th><th>告警</th><th>明细</th></tr>"
                 + "".join("<tr><td>%s</td><td>%s</td><td>%s</td></tr>"
                           % (e(str(w.get("node") or "")), e(str(w.get("message") or "")),
                              e(str(w.get("detail") or ""))) for w in items)
                 + "</table>")
        if len(items) >= NODE_WARNINGS_DETAIL_MIN_ROWS:
            out.append("<details><summary>共 %d 条，展开查看</summary>%s</details>"
                       % (len(items), table))
        else:
            out.append(table)
    out.append("</div>")
    return "".join(out)


def add_node_warnings_section(doc, plan):
    """Word 的「节点级告警」章节：**count == 0 时一个字都不加**（无空表、无"无告警"占位）。

    标题用 level=2：`audit_gate.draft_outline_payload` 的目录逐字镜像 Heading 1
    （那份目录在别人的文件里），二级标题既不进目录、又能让人找得到（同 `add_confidence_section`）。
    返回是否真的写了。
    """
    model = node_warnings_model(plan)
    if not model or model["count"] <= 0:
        return False
    doc.add_heading(NODE_WARNINGS_TITLE, level=2)
    doc.add_paragraph(node_warnings_summary_line(model))
    for w in model["items"]:
        detail = str(w.get("detail") or "")
        doc.add_paragraph("• %s：%s%s"
                          % (str(w.get("node") or "（未标注节点）"),
                             str(w.get("message") or ""),
                             ("（%s）" % detail) if detail else ""))
    return True


# ============================================================
# 模型用量（第 44 轮：`meta.usage` 是**计划定稿时**的快照，不是本次运行总量）
# ============================================================
# 实测：`plans/plan_sample3_after_org_v2.json` 的 `meta.usage` = calls 30 / ¥0.4038，
# 而那次运行真实是 calls 32 / ¥0.8018（`reporter`、`html_page` 在 `build_meta` 之后才跑，
# `html_page` 一家就 238K tokens）—— 用户拿到的花费**少一半**，且没有任何说明。
# 落点（读代码确认，不是猜）：`meta["usage"]` 由 `plan_assembler.build_meta` 在**装配阶段**
# 取快照（plan_assembler.py:808）；`builder._main_nodes()` 的节点顺序是
# … plan_assembler.PlanAssemblerNode → reporter → deliver → Word(draft) → html_page，
# 所以 `HtmlPageNode`（本文件 `delivery.py:2127`）是**最后一个**节点、也是最后一个 LLM 节点
# —— 只有在那里 `usage.meter().snapshot()` 才是完整的运行末尾快照。
USAGE_TITLE = "模型用量与费用"
USAGE_FALLBACK_COMMENT = "<!-- 追加用量口径段"
USAGE_NOTE = ("`usage` = 计划数据定稿（`plan_assembler.build_meta` 取快照）时的用量；"
              "`usage_final` = 本次运行末尾（HTML 看板导出之后）的用量快照。两者之差来自"
              "定稿之后才跑的节点（reporter / deliver / html_page 等），`usage` 因此**不是**"
              "本次运行总量。")


def _usage_calls(snap):
    """用量快照里的调用次数（取不到 → None）。"""
    if not isinstance(snap, dict):
        return None
    v = snap.get("calls")
    if isinstance(v, bool) or not isinstance(v, int):
        try:
            v = int(v)
        except (TypeError, ValueError):
            return None
    return v if v >= 0 else None


def _usage_snapshots(plan):
    """→ `(base, final_meta, live)`：

      · `base`       = `meta["usage"]`（**计划数据定稿时**的快照；老计划可能缺）；
      · `final_meta` = `meta["usage_final"]`（运行末尾快照；写回计划 JSON 后就恒在）；
      · `live`       = 本进程用量计量器此刻的快照（`calls > 0` 才算，否则 `None`）。
    """
    meta = (plan.get("meta") if isinstance(plan, dict)
            and isinstance(plan.get("meta"), dict) else {})
    base = meta.get("usage") if isinstance(meta.get("usage"), dict) else None
    final_meta = meta.get("usage_final") if isinstance(meta.get("usage_final"), dict) else None
    live = None
    try:
        from .. import usage as usage_mod
        snap = usage_mod.meter().snapshot()
        if isinstance(snap, dict) and (_usage_calls(snap) or 0) > 0:
            live = snap
    except Exception:
        live = None
    return base, final_meta, live


def _live_as_final(live, base):
    """`live` 能当"运行末尾"用吗？—— 调用次数**不许少于**定稿时的快照。

    少了就说明这个数是别的运行（同一进程里更早的一次）留下的：宁可不写，
    也不能把一个更小的数说成"本次运行总量"。
    """
    if live is None:
        return None
    lc, bc = _usage_calls(live), _usage_calls(base)
    if bc is not None and (lc is None or lc < bc):
        return None
    return live


def _usage_fmt(snap):
    """用量快照 → 一行中文（缺哪项就不写哪项，**不写 0 冒充**）。"""
    parts = []
    calls = _usage_calls(snap)
    if calls is not None:
        parts.append("%d 次调用" % calls)
    pt, ct, tt = (snap.get("prompt_tokens"), snap.get("completion_tokens"),
                  snap.get("total_tokens"))
    if isinstance(pt, int) and not isinstance(pt, bool) \
            and isinstance(ct, int) and not isinstance(ct, bool):
        parts.append("输入 {:,} tok / 输出 {:,} tok".format(pt, ct))
    if isinstance(tt, int) and not isinstance(tt, bool):
        parts.append("合计 {:,} tok".format(tt))
    cost = snap.get("cost_cny")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        parts.append("约 ¥%.4f" % float(cost))
    return " · ".join(parts)


def usage_final_line(plan):
    """**看板**的用量行：显示运行末尾用量 + 注明"计划数据定稿时为 N 次"。

    拿不到运行末尾快照时退而求其次：只说定稿时的数，并**明说这不是总量**。
    一点用量数据都没有 → 空串（一个字都不加）。
    """
    base, final_meta, live = _usage_snapshots(plan)
    final = final_meta or _live_as_final(live, base)
    if final is None:
        if base is None:
            return ""
        return ("本次运行模型用量：**计划数据定稿时**为 %s；计划落盘之后仍有节点调用模型，"
                "本页拿不到运行末尾快照，所以这一行是定稿时的数，**不是本次运行总量**。"
                % _usage_fmt(base))
    txt = "本次运行模型用量（运行末尾快照）：%s。" % _usage_fmt(final)
    bc = _usage_calls(base)
    if bc is not None:
        txt += "计划数据定稿时为 %d 次调用（其后还有节点调用，已计入上面的末尾快照）。" % bc
    else:
        txt += "（计划数据定稿时的用量快照缺失，无法给出定稿时的调用次数。）"
    return txt


def usage_draft_line(plan):
    """**Word** 的用量行：显示定稿时快照，并**必须**注明"其后还有节点调用未计入"。

    Word 是 `HtmlPageNode`（最后一个 LLM 节点）**之前**生成的，它拿不到运行末尾快照；
    把定稿时的数当成总量写，就是本次要修的少报。没有任何用量数据 → 空串。
    """
    base, final_meta, _live = _usage_snapshots(plan)
    if base is not None:
        return ("本次运行模型用量（计划数据定稿时的快照）：%s。"
                "其后还有节点调用未计入（本文件写入之后仍会有节点调用模型）。"
                % _usage_fmt(base))
    if final_meta is not None:
        return ("本次运行模型用量（运行末尾快照）：%s。"
                "计划数据定稿时没有用量快照，故给不出定稿时的调用次数。"
                % _usage_fmt(final_meta))
    return ""


def _usage_card_html(plan):
    """看板的用量卡片（用 `usage_final_line`；没有数据 → 空串，一个字都不加）。"""
    line = usage_final_line(plan)
    if not line:
        return ""
    text = _conf_md_bold_html(line) if "**" in line else _html.escape(line)
    return ("<div class='card'><h2>%s</h2><div class='rs-row'>%s</div></div>"
            % (_html.escape(USAGE_TITLE), text))


def add_usage_section(doc, plan):
    """Word 的用量段（用 `usage_draft_line`；没有数据 → 一个字都不加）。返回是否写了。"""
    line = usage_draft_line(plan)
    if not line:
        return False
    doc.add_paragraph(line)
    return True


def _record_usage_final(plan):
    """运行末尾（HTML 看板导出后）把最终用量落进 `meta.usage_final` 并写回计划 JSON。

    返回是否写入。取不到运行末尾快照（如离线复看老计划：本进程计量器是空的）→ 不动，
    绝不拿"定稿时的数"或 0 冒充运行末尾。
    """
    if not isinstance(plan, dict):
        return False
    try:
        base, _final_meta, live = _usage_snapshots(plan)
        live = _live_as_final(live, base)
        if live is None:
            return False
        meta = plan.get("meta")
        if not isinstance(meta, dict):
            meta = {}
            plan["meta"] = meta
        meta["usage_final"] = dict(live)
        meta["usage_note"] = USAGE_NOTE
        _persist_usage_final(plan, live)
        return True
    except Exception:
        return False


def _persist_usage_final(plan, snapshot):
    """把运行末尾用量写回**计划 JSON 文件**（`<PLANS_DIR>/<plan_id>.json`）。

    为什么必须写回：计划 JSON 是用户唯一可复核的用量记录。只在内存里加一个键，下次读回
    计划时"这次运行到底花了多少"又变回 `usage` 的定稿时快照（少报）。
    两条口径：
      · **只改已存在的文件**（拿不到就返回 ""，不新建）——测试 / 离线复看不许往 plans/ 里造档；
      · 沿用 `plan_store._write_json`（先写 `.tmp` 再 `os.replace`、UTF-8 全量缩进落盘），
        与 `PlanDeliverNode._save` 落的是同一份、同一格式，读回校验（`PlanJson.model_validate`）
        不受影响（`meta` 是 `extra="allow"`）。
    任何异常一律吞掉：用量写回是旁路，绝不能让交付流程挂掉。
    """
    try:
        if not isinstance(plan, dict) or not isinstance(snapshot, dict):
            return ""
        pid = str(plan.get("plan_id") or "").strip()
        if not pid:
            return ""
        path = Path(config.PLANS_DIR) / ("%s.json" % pid)
        if not path.is_file():
            return ""
        try:
            disk = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError, UnicodeDecodeError):
            return ""
        if not isinstance(disk, dict):
            return ""
        dmeta = disk.get("meta")
        if not isinstance(dmeta, dict):
            dmeta = {}
            disk["meta"] = dmeta
        dmeta["usage_final"] = dict(snapshot)
        dmeta["usage_note"] = USAGE_NOTE
        from ..plan_store import _write_json as _plan_write_json
        return _plan_write_json(str(path), disk)
    except Exception:
        return ""


def _workface_meta_rows(plan, cov):
    """「数据来源与置信度」里班组人数的几行（缺数据就整行不出）。

    · 数据来源那一行只在**真有任务按工作面容量口径算过人数**时才写
      （applied == 0 → 这句话就是凭空 claim）；
    · 有组织层的任务单独报条数（`_workface_summary().org_count`）—— 这批人不是
      按标定公式算的，写进"工作面容量数据来源"就是假话；
    · `workface_saturated` / `workface_ceiling_raised` 由计划端写进
      `meta.norm_coverage`，键缺失（旧计划 / 上游没算）就整行不出。
    """
    rows = []
    _ws = _workface_summary(plan)
    if not (_ws.get("applied") or _ws.get("org_count")):
        return rows
    if _ws.get("org_count"):
        # C11：节拍不参与计算 → 组织层的班组口径只写作业面数 × 每面人数。
        rows.append(("其中班组来自施工组织层（作业面数 × 每面人数）", "%s 条" % _ws["org_count"]))
    if not _ws.get("applied"):
        return rows
    rows.append(("工作面容量数据来源",
                 "ai_estimate / LOW（经验标定，非规范来源，按工程量联动）"))
    cov = cov if isinstance(cov, dict) else {}
    if cov.get("workface_saturated") is not None:
        rows.append(("其中顶到每施工段人数上限", "%s 条" % cov.get("workface_saturated")))
    if cov.get("workface_ceiling_raised") is not None:
        rows.append(("上限按第 39 轮口径抬高", "%s 条" % cov.get("workface_ceiling_raised")))
    return rows


def confidence_section_blocks(plan, view):
    """「数据来源与置信度」章节的内容块。

    返回 [(kind, payload)]，kind ∈ {"h3","para","kv","grid"}。
    一个块都算不出来 → 返回 []（调用方**连标题都不打**：拿不到数据时整段降级）。
    所有数字来自 plan，缺字段就整行不出。
    """
    meta = plan.get("meta") if isinstance(plan.get("meta"), dict) else {}
    blocks = []

    # ---------------- 1) 定额覆盖率 ----------------
    cov = meta.get("norm_coverage") if isinstance(meta.get("norm_coverage"), dict) else {}
    # 政策变更（用户 2026-09-20）：AI 经验估算定额（KB `AI_ESTIMATE_V1`）由
    # 「只作参考、不参与算工期/班组」改为**照用**（状态名 `released_ai`）。交付物这一层
    # 唯一不能省的就是：① 如实给出条数；② 说明它已参与工期与班组计算、无规范依据。
    # 条数先从数据算出来（`_ai_norm_counts`：优先 `meta.norm_coverage.released_ai`，
    # 缺该键就从任务级数据自己数；数不出来 → 整行不出，**绝不用 0 充数**）。
    _ai_pair = _ai_norm_counts(plan, cov)
    if cov.get("total"):
        rows = [
            ("定额口径任务总数", cov.get("total")),
            ("已绑定定额", "%s（%s）" % (
                cov.get("bound") if cov.get("bound") is not None else "—",
                ("%.1f%%" % float(cov["bound_pct"])) if cov.get("bound_pct") is not None else "—")),
            ("未绑定定额", "%s（%s）" % (
                cov.get("unbound") if cov.get("unbound") is not None else "—",
                ("%.1f%%" % float(cov["unbound_pct"])) if cov.get("unbound_pct") is not None else "—")),
        ]
        # 第 39 轮：已绑定定额里有多少条是"真人来源、但没人工审定"的行 ——
        # 闸门按来源放行它们，交付物必须把这件事写在明面上（"用，但要标出来"）。
        _rel = cov.get("released_unapproved")
        if _rel:
            _conf_txt = "、".join("%s %d 条" % (k, v) for k, v in
                                  sorted((cov.get("by_confidence") or {}).items()))
            rows.append(("其中：未经人工审定的真人定额（已放行）",
                         "%s（%.1f%%）%s" % (
                             _rel, float(cov.get("released_unapproved_pct") or 0.0),
                             ("；来源：%s" % _conf_txt) if _conf_txt else "")))
        # 政策变更（2026-09-20）：AI 经验估算定额那一档的条数（唯一不能省的披露）。
        # 上游给了 `released_ai`（= 已放行并参与计算）就照抄，并用政策原文的标签；
        # 没有这个键（旧计划）→ 用交付侧从任务级数据数出来的数，标签如实写"待审"。
        if _ai_pair:
            _ai_n, _ai_applied, _ai_from_meta = _ai_pair
            # by_confidence 里出现 `estimated`（= AI 经验估算那一档）时如实显示；
            # 上面那行"未经人工审定的真人定额"已经把整张 by_confidence 列出来了，
            # 免得同一个数在两行里重复。
            _est_conf = (cov.get("by_confidence") or {}).get("estimated") \
                if isinstance(cov.get("by_confidence"), dict) else None
            _ai_est = ("；来源置信度 estimated（AI 经验估算）%s 条" % _est_conf
                       if (_est_conf and not _rel) else "")
            if _ai_from_meta:
                _ai_pct = cov.get("released_ai_pct")
                _ai_val = ("%s（%.1f%%）" % (_ai_n, float(_ai_pct))
                           if _ai_pct is not None else "%s 条" % _ai_n)
                rows.append((AI_NORM_ROW_LABEL, _ai_val + _ai_est))
            else:
                if _ai_applied >= _ai_n:
                    _ai_state = "（本次运行已据此算出班组与工日）"
                elif _ai_applied:
                    _ai_state = "（本次运行已据此算出班组与工日 %d 条）" % _ai_applied
                else:
                    _ai_state = "（本次运行未据此计算班组）"
                rows.append((AI_NORM_ROW_LABEL_LEGACY, "%s 条%s%s"
                             % (_ai_n, _ai_state, _ai_est)))
        # 第 39 轮：工作面容量口径的**数据来源**必须写在明面上（经验标定、非规范来源）。
        # 只有真的有任务按这个口径算过人数才写 —— 否则这句话就是凭空 claim。
        rows.extend(_workface_meta_rows(plan, cov))
        # D6（合同 §9.2）：**关键路径规范依据覆盖率**。从数据现算（算不出来整行不出）；
        # 不新增 meta 顶层键 —— 存回时挂在 `norm_coverage.critical_norm_coverage` 下面。
        _cov_show, _cov_live = _norm_coverage_display(plan, cov, view)
        if _cov_show:
            rows.append((NORM_COVERAGE_LABEL, _cov_show))
        blocks.append(("h3", "1. 定额覆盖率"))
        blocks.append(("kv", rows))
        by_reason = cov.get("by_reason") if isinstance(cov.get("by_reason"), dict) else {}
        pct_reason = cov.get("by_reason_pct") if isinstance(cov.get("by_reason_pct"), dict) else {}
        if by_reason:
            blocks.append(("para", "未绑定原因（逐条，来自计划 meta.norm_coverage.by_reason）："))
            # 显示文案过 `_by_reason_display`：原始记录一字不改，但 AI 类目会挂上
            # 「政策变更 2026-09-20：AI 经验估算定额已照用」的澄清（见该函数）。
            blocks.append(("grid", (["未绑定原因", "任务数", "占比"],
                                    [[_by_reason_display(str(k)), v,
                                      ("%.1f%%" % float(pct_reason[k]))
                                      if pct_reason.get(k) is not None else "—"]
                                     for k, v in by_reason.items()])))
        if _rel or _ai_pair:
            # 写明"放行"与"拦下"的分界，避免用户以为所有定额都被采信了。
            # ⚠ 政策变更（2026-09-20）：旧句「**AI 经验估算（estimated）一律不参与计算**」
            # 从今天起是假话，必须删掉，换成新政策 + **本次运行的实际状态**。
            _paras = []
            if _rel:
                _paras.append(
                    "上表「未经人工审定的真人定额」= 从资料解析出来的定额行"
                    "（confidence=parsed / verified），尚无人工审定，本次**按来源放行**"
                    "并计入定额工日需求；如需否决某一行，可在默认定额表里把它标为 rejected。")
            if _ai_pair:
                _paras.append(_ai_norm_caliber_para(*_ai_pair))
            blocks.append(("para", "".join(_paras)))
        unmapped = _kb_unmapped_work_types(meta)
        if unmapped:
            blocks.append(("para", "知识库未映射工种：%d 个（相关工序降级为无结构约束，"
                                   "明细见计划 meta.kb_warnings）。" % unmapped))

    # norm_coverage 整块缺失时，工作面容量的那几行仍然要能印出来（不能因为
    # 覆盖率章节没数据，就把"人数是怎么来的"一起吞掉）。AI 条数同理：那批任务确实
    # 进了定额口径，没有 norm_coverage 也必须如实给出条数与政策口径。
    if not cov.get("total"):
        _wf_only = _workface_meta_rows(plan, cov)
        if _wf_only:
            blocks.append(("kv", _wf_only))
        if _ai_pair:
            blocks.append(("h3", "1. 定额覆盖率"))
            _ai_n, _ai_applied = _ai_pair[0], _ai_pair[1]
            _ai_state = ("（本次运行已据此算出班组与工日）" if _ai_applied >= _ai_n
                         else ("（本次运行已据此算出班组与工日 %d 条）" % _ai_applied
                               if _ai_applied else "（本次运行未据此计算班组）"))
            blocks.append(("kv", [(AI_NORM_ROW_LABEL_LEGACY,
                                   "%s 条%s" % (_ai_n, _ai_state))]))
            blocks.append(("para", _ai_norm_caliber_para(*_ai_pair)))

    # ---------------- 2) 来源构成 ----------------
    cred = meta.get("credibility") if isinstance(meta.get("credibility"), dict) else {}
    if cred:
        cred_rows = []
        for key, label in (("user", "用户输入"), ("kb", "知识库定额"), ("ai", "AI 估算")):
            if cred.get(key) is None:
                continue
            cred_rows.append((label, _pct_of_ratio(cred.get(key))))
        if cred_rows:
            blocks.append(("h3", "2. 来源构成（用户 / 知识库 / AI 占比）"))
            blocks.append(("kv", cred_rows))
    srcs = meta.get("data_sources")
    if isinstance(srcs, dict):                       # 兼容 {code: {...}} 形态
        srcs = [dict(v, code=k) if isinstance(v, dict) else k for k, v in srcs.items()]
    if isinstance(srcs, (list, tuple)):
        src_rows = []
        for s in srcs:
            if isinstance(s, dict):
                code = (s.get("code") or s.get("source_code") or s.get("name")
                        or s.get("name_cn"))
                if not code:
                    continue
                typ = s.get("type") or s.get("source_type") or _source_type_of(code)
                conf = s.get("confidence") or s.get("conf")
                label = s.get("label") or s.get("name_cn") or _source_label(code)
                src_rows.append((str(code), label or "—", typ or "—",
                                 _CONFIDENCE_LABELS.get(str(conf), str(conf)) if conf else "—"))
            elif s:
                code = str(s)
                src_rows.append((code, _source_label(code), _source_type_of(code) or "—", "—"))
        if src_rows:
            if not any(isinstance(b, tuple) and b[0] == "h3" and str(b[1]).startswith("2.")
                       for b in blocks):
                blocks.append(("h3", "2. 来源构成（用户 / 知识库 / AI 占比）"))
            blocks.append(("grid", (["来源代码", "名称", "类型", "置信度"], src_rows)))

    # ---------------- 3) 两版工期与用户目标 ----------------
    sv = meta.get("schedule_versions") if isinstance(meta.get("schedule_versions"), dict) else {}
    bc = meta.get("boundary_conditions") if isinstance(meta.get("boundary_conditions"), dict) else {}
    target = bc.get("project_duration_days")
    # E1（用户 2026-09-21 裁定）：**模型补的**目标工期（病根 3 里的 450 天）一律不展示；
    # **用户明确给出的**继续展示，作为对比参考，并标清来源 = 用户。
    # 来源键 = `boundary_conditions._source.project_duration_days`（boundary 节点写，
    # 与 `scheduler.parse_boundary_limits` 的 SOURCE_KEYS 同一套）。取不到 = **分不出**
    # 是不是用户给的 → 按"不是用户给的"处理（不展示），宁可少写一句也不猜。
    _bc_src = bc.get("_source") if isinstance(bc.get("_source"), dict) else {}
    target_is_user = (str(_bc_src.get("project_duration_days") or "").strip().lower()
                      == "user")
    if not target_is_user:
        target = None
    if sv or target is not None:
        theory = _fnum(sv.get("theory_min_days"))
        ok = _fnum(sv.get("resource_ok_days"))
        delta = _fnum(sv.get("delta_days"))
        rows = []
        if theory:
            rows.append(("理论最短版（班组顶满工作面上限）", "%s 天" % theory))
        if ok:
            rows.append(("资源不超额版（班组不超过用户限额）", "%s 天" % ok))
        if delta:
            rows.append(("两版差额", "%s 天" % delta))
        if target is not None:
            rows.append(("用户目标工期（来源：用户）", "%s 天" % (_fnum(target) or target)))
        blocks.append(("h3", "3. 两版工期与用户目标"))
        if rows:
            blocks.append(("kv", rows))
        unbound = cov.get("unbound")
        if ok and target is not None:
            d = _fnum(float(ok) - float(target)) or "—"
            concl = "资源不超额版 %s 天，比用户目标 %s 天多 %s 天" % (
                ok, _fnum(target) or target, d)
            if unbound:
                concl += "，差额主要来自 %s 条未绑定定额的任务" % unbound
            blocks.append(("para", concl + "。"))
        elif theory and ok:
            blocks.append(("para", "资源不超额版 %s 天，比理论最短版 %s 天多 %s 天。"
                           % (ok, theory, delta or "—")))

    # ---------------- 4) 人工/机械峰值（两个口径，各自标名） ----------------
    pk = _manpower_peaks(plan, view)
    cal = _peak_caliber(plan, view)
    rows = []
    if pk["curve"] is not None:
        rows.append(("人工峰值 · 资源曲线口径（逐日累加，含机械配员）",
                     "%s 人" % _fnum(pk["curve"])))
    # E1（用户 2026-09-21 裁定）：**「申报峰值」不再是一个展示项**。旧实现在这里另起一行
    # 「人工峰值 · 申报值（…）」，把模型替用户补的那个 120 再印一遍 —— 整行删除。
    # 只保留「用户限额口径」这一支：`cal is None`（旧计划 / 口径键缺）走老显示，
    # `cal["source"] == "user"`（用户明确给的限额，C9 允许保留）照旧显示；
    # 其余来源（模型估算 / 曲线口径）不出这一行。
    user_limit = (cal is None) or (str(cal.get("source") or "") == "user")
    if cal is None:
        declared = pk["limit"]
    elif user_limit:
        declared = cal["peak"]
    else:
        declared = None
    show_limit = (user_limit and declared is not None
                  and _fnum(declared) != _fnum(pk["curve"]))
    if show_limit:
        rows.append(("人工峰值 · 用户限额口径（编制上限，来自边界条件）" if cal is None
                     else "人工峰值 · 用户限额口径（用户给定上限）",
                     "%s 人" % _fnum(declared)))
    if rows:
        blocks.append(("h3", "4. 人工 / 机械峰值口径"))
        blocks.append(("kv", rows))
        if show_limit:
            try:
                differs = (pk["curve"] is not None
                           and float(pk["curve"]) > float(declared))
            except (TypeError, ValueError):
                differs = False
            if differs:
                # 只有**用户给的限额**才按"限额"来解读（模型估算的数已不再展示）。
                blocks.append(("para",
                               "两个口径不是同一个数：资源曲线峰值 %s 人是排程算出的需求，"
                               "用户限额 %s 人是编制依据的上限；资源不超额版按限额压缩班组。"
                               % (_fnum(pk["curve"]), _fnum(declared))))
    rp = plan.get("resource_plan") if isinstance(plan.get("resource_plan"), dict) else {}
    _ep_equip, _ep_crew = _split_equipment_peak(rp)
    if _ep_equip:
        blocks.append(("grid", (["设备", "峰值台数"], [[k, v] for k, v in _ep_equip])))
    if _ep_crew:
        # 机械配员是**人**：单列一行，绝不出现在上面的设备表里（与看板「机械配员峰值（人）」同口径）
        blocks.append(("para",
                       "机械配员（人，随机械台数配置，非设备）：%s"
                       % "、".join("%s %s 人" % (k, v) for k, v in _ep_crew)))

    # ---------------- 5) 单位与定额降级清单 ----------------
    # 已按写明假定换算出资源的任务（`_unit_assumed`）**不进**这份清单：
    # 它们算得出来，就不是「仅参考」。改由 5b 节单列（口径见 `_unit_assumed_ids`）。
    degraded, total = _norm_degradations(plan)
    assumed, assumed_total = _norm_assumed_rows(plan)
    if total:
        blocks.append(("h3", NORM_DEGRADED_TITLE))
        # 引导句按清单内容选（政策变更 2026-09-20：清单里可能有 AI 经验估算定额行，
        # 那时不能再说"其定额仅作参考、不参与工期与资源计算"）。
        blocks.append(("para", _degraded_lead(degraded)))
        blocks.append(("grid", (["任务 ID", "任务", "原因"],
                                [[r["task_id"], r["task_name"], r["reason"]] for r in degraded])))
        if total > len(degraded):
            blocks.append(("para", "（共 %d 条，此处列出前 %d 条）" % (total, len(degraded))))
    if assumed_total:
        # 标题 / 引导句 / 脚注按**实际来源**生成（`assumed_section_*`）：只有全行都是
        # AI 来源时才敢写「AI 假定」—— 把定额条件档位推出的值说成 AI 是虚假溯源。
        _as_title = assumed_section_title(assumed)
        _as_lead = assumed_section_lead(assumed)
        blocks.append(("h3", _as_title))
        if _as_lead:
            blocks.append(("para", _as_lead))
        blocks.append(("grid", (["任务 ID", "任务", "换算参数来源", "换算过程与结果（依据列原文）"],
                                [[r["task_id"], r["task_name"], _assumed_source_cell(r), r["reason"]]
                                 for r in assumed])))
        if assumed_total > len(assumed):
            blocks.append(("para", "（共 %d 条，此处列出前 %d 条）"
                                   % (assumed_total, len(assumed))))
        _as_foot = assumed_section_foot(assumed)
        if _as_foot:
            blocks.append(("para", _as_foot))

    # ---------------- 5d) 口径换算留痕（合同 §9.4；字段由 WS1 写，缺失时整段不出）-------
    basis_rows, basis_total = _norm_basis_adjust_rows(plan)
    if basis_total:
        blocks.append(("h3", NORM_BASIS_TITLE))
        blocks.append(("para", NORM_BASIS_LEAD))
        blocks.append(("grid", (["任务 ID", "任务", "留痕（原口径 → 定额口径 → 换算方式 → 换算后工程量）"],
                                [[r["task_id"], r["task_name"], _norm_basis_adjust_text(r)]
                                 for r in basis_rows])))
        if basis_total > len(basis_rows):
            blocks.append(("para", "（共 %d 条，此处列出前 %d 条）"
                                   % (basis_total, len(basis_rows))))

    # ---------------- 6) 来源档次 + 关键路径规范依据覆盖率（合同 §9.2 D6）----------------
    # 判据与门无关：条数 / 覆盖率全部从数据现算；算不出来（没有关键路径或没有排程日期）
    # → 覆盖率那一格不出，绝不写 0%。
    tier_counts, tier_total = _norm_tier_counts(plan)
    cov_txt, cov_live = _norm_coverage_display(plan, cov, view)
    if tier_total or cov_txt:
        blocks.append(("h3", NORM_TIER_TITLE))
        if tier_total:
            _tiers = [t for t in NORM_TIER_ORDER if tier_counts.get(t)]
            blocks.append(("grid", (["来源档次", "工序数", "占比"],
                                    [[t, tier_counts[t],
                                      "%.1f%%" % (100.0 * tier_counts[t] / tier_total)]
                                     for t in _tiers])))
            blocks.append(("para", _norm_tier_lead(tier_counts)))
        if cov_txt:
            blocks.append(("kv", [(NORM_COVERAGE_LABEL, cov_txt)]))
            if cov_live and cov_live.get("critical_days") is not None:
                _ai_note = ("；「%s」无规范依据，不计入分子。" % NORM_TIER_AI
                            if tier_counts.get(NORM_TIER_AI) else "。")
                _note = _norm_coverage_caliber_note(cov_live)
                if _ai_note != "。":
                    _note = _note[:-1] + _ai_note if _note.endswith("。") else _note + _ai_note
                blocks.append(("para", _note))

    # ---------------- 7) 无定额依据工序（合同 §9.1 D4）----------------
    missing_rows, missing_total = _norm_missing_rows(plan)
    if missing_total:
        blocks.append(("h3", NORM_MISSING_TITLE))
        blocks.append(("para", NORM_MISSING_LEAD))
        blocks.append(("grid", (["任务 ID", "任务", "来源档次", "无定额依据说明"],
                                [[r["task_id"], r["task_name"], r["tier"], r["sentence"]]
                                 for r in missing_rows])))
        blocks.append(("para", NORM_MISSING_FOOT))

    # ---------------- 8) 工程量来源与未入树清单（域 5，设计 §14.1 裁决 #6）----------------
    # 键缺失 → 本节一块都不加（优雅缺席），因此可以无条件 extend。
    blocks.extend(quantity_coverage_blocks(plan))

    # ---------------- 9) 人工覆盖留痕（合同 §9.3 D7）----------------
    # **没有覆盖文件 → 一块都不加**：交付物行为与没有这个入口时逐字一致（优雅降级）。
    override_rows, override_src = _norm_overrides(plan)
    if override_rows:
        blocks.append(("h3", NORM_OVERRIDE_TITLE))
        blocks.append(("para", NORM_OVERRIDE_LEAD % override_src))
        _oh, _orows = _norm_override_grid_rows(override_rows)
        blocks.append(("grid", (_oh, _orows)))

    # ---------------- 10) 垂直运输设备常量（域 7.7 / 7.8 / 7.10，设计 §14.1 裁决 #3）----------------
    # 为什么单开一段：这两个台数**不再来自任务工程量**，而是边界节点一次定好的项目级常量
    # （用户申报赢，否则按建筑面积/栋数/层数的明示规则估算），且口径是"默认够用、不进超限
    # 清单" ⇒ 它**不会**出现在任何超限/峰值口径里。不给它一段，用户在交付物上就完全看不到
    # "塔吊为什么是 12 台"。键不在（旧计划 / 没走边界节点）→ 一块都不加（优雅缺席）。
    _bc = meta.get("boundary_conditions")
    _smc = _bc.get("site_machine_const") if isinstance(_bc, dict) else None
    _sm = _smc.get("machines") if isinstance(_smc, dict) else None
    if isinstance(_sm, dict) and _sm:
        blocks.append(("h3", SITE_MACHINE_CONST_TITLE))
        blocks.append(("para", SITE_MACHINE_CONST_LEAD))
        _sm_rows = []
        for _name in sorted(_sm):
            _r = _sm.get(_name)
            _r = _r if isinstance(_r, dict) else {}
            _sm_rows.append((
                str(_name),
                "%s %s" % (_fnum(_r.get("count")), str(_r.get("unit") or "台")),
                _site_const_source_text(_r.get("count_source"), _r.get("quantity_source")),
                str(_r.get("rule") or "—"),
                "已冻结" if _r.get("frozen") else "未冻结",
            ))
        blocks.append(("grid", (("设备", "台数", "来源", "依据规则", "冻结"), _sm_rows)))
    return blocks


def has_confidence_section(plan, view):
    """`confidence_section_blocks` 是否算得出内容块（**不是**进出门）。

    ⚠ 进出门统一走 `has_confidence_meta`（Word 与看板同一判据，见该函数）：
    这里只判"块算不算得出来"，而 §4 峰值口径几乎每份计划都算得出来 ——
    拿它当门会让"只有峰值一行"的退化计划多出一整章空壳。
    """
    try:
        return bool(confidence_section_blocks(plan, view))
    except Exception:
        return False


# 置信度类元数据：只有计划**确实带了这些**才值得单开一章。
# 进出门 = 七项任一非空 **或** 存在 AI 经验估算定额条数（政策变更 2026-09-20，
# 见 `has_confidence_meta` 的说明）。Word（`add_confidence_section`）与看板
# （`_confidence_section_html`）调用的是**同一个** `has_confidence_meta`，
# 同一份输入必须同进同出（曾经担心过的"看板多卡一道门"在代码里不存在）。
# 为什么不能只判 `confidence_section_blocks` 是否非空：`resource_plan.peak_manpower`
# 在几乎每份计划里都有（§4 峰值口径），"只有峰值一行"的退化计划会多出一整章空壳。
# 第七项 `quantity_coverage` 是域 5 节点写的覆盖表（设计 §9.2）：有它才有
# 「8. 工程量来源与未入树清单」—— 该节自己会在缺数据时缺席，门只管"值不值得开这一章"。
CONFIDENCE_META_KEYS = ("norm_coverage", "credibility", "data_sources",
                        "kb_warnings", "schedule_versions", "boundary_conditions",
                        "quantity_coverage")

# 【域 7.7 / 7.8 / 7.10 · 设计 §14.1 裁决 #3】垂直运输设备常量段。
# 为什么它不需要新加 `CONFIDENCE_META_KEYS` 键：该常量落在 `meta.boundary_conditions`
# 里，而 `boundary_conditions` **本来就是**那七个门键之一 ⇒ 门已经开着了（父代理收口时
# 实测确认）。设计文档"出章门加键"那条建议因此是多余的。
SITE_MACHINE_CONST_TITLE = "垂直运输设备常量（塔吊 / 施工电梯）"
SITE_MACHINE_CONST_LEAD = (
    "塔吊与施工电梯的台数是**项目级常量**：在边界节点**一次定好并冻结**，全项目统一口径，"
    "**不随单条工序的工程量变化、也不分到 L4 时重估**。用户申报了台数就以用户为准；"
    "没申报就按**已有的建筑参数**（建筑面积 / 栋数 / 层数）按下面写明的规则估算 —— "
    "**不引用任何系数表，也不引入新数据源**。它们按**连续在场**计入每日资源账本，"
    "口径是「默认够用」，因此**不会出现在任何超限清单里**。")


def has_confidence_meta(plan):
    """计划是否带了**值得单开一章**的置信度 / 来源数据 —— Word 与看板**同一判据**。

    判据 = 七项置信度元数据任一非空 **或** 存在 AI 经验估算定额条数。

    政策变更（2026-09-20）为什么要把 AI 条数也算进来：本章是「数据来源与置信度」，
    而"哪几条的依据是 AI 经验估算定额"正是本章必须披露的置信度数据。只认七项的话，
    "有 AI 定额任务、但计划没带那七项 meta"的计划会在交付物里**整章消失、条数无处可写**
    —— 而"逐条标注 + 条数"是这次政策变更唯一不能省的部分。

    ⚠ 为什么门不能只判 `confidence_section_blocks` 非空：§4「人工 / 机械峰值口径」
    几乎每份计划都算得出来，于是"只有峰值一行"的退化计划会凭空多出一整章空壳。
    """
    meta = plan.get("meta") if isinstance(plan.get("meta"), dict) else {}
    for k in CONFIDENCE_META_KEYS:
        if meta.get(k):
            return True
    cov = meta.get("norm_coverage") if isinstance(meta.get("norm_coverage"), dict) else None
    return _ai_norm_counts(plan, cov) is not None


def add_confidence_section(doc, plan, view, add_h, add_kv, add_grid):
    """把「数据来源与置信度」写进 Word。

    标题用 level=2（**不是** Heading 1）：`audit_gate.draft_outline_payload` 的目录
    逐字镜像 Heading 1 的原文与顺序，而那份目录在别人的文件里（本轮不归我改）。
    二级标题既保证章节出现在靠前位置，又不让目录与真产物错位。

    返回是否真的写了（False = 整段降级，一个字都没打）。
    """
    if not has_confidence_meta(plan):
        return False
    try:
        blocks = confidence_section_blocks(plan, view)
    except Exception:
        return False
    if not blocks:
        return False
    add_h("数据来源与置信度", level=2)
    for kind, payload in blocks:
        if kind == "h3":
            add_h(payload, level=3)
        elif kind == "para":
            doc.add_paragraph(str(payload))
        elif kind == "kv":
            add_kv(payload)
        elif kind == "grid":
            add_grid(payload[0], payload[1])
    return True


def _add_md_lines(doc, text):
    """把 LLM 的 markdown 报告写进 Word —— **不泄漏 markdown 语法**。

    历史缺陷（实测泄漏 12 段）：旧实现只认行首 `#`，于是
      | 里程碑名称 | 计划日期 | 对应任务ID |
      | :--- | :--- | :--- |
    连同 `<br>` 一起被原样写成段落，用户在 Word 里看到一堆竖线。

    修法：
      · 连续的 `| a | b |` 行 → **真正的 Word 表格**（分隔行 `|:--|` 丢弃）；
      · 表格行仍然各自留一个空段落占位 —— 段落/表格计数与旧版一致，
        不破坏 test_delivery_granularity / test_gate_d_payloads 的计数断言；
      · 残余的 `<br>` 还原成 Word 软换行。
    """
    def _cells(line):
        s = str(line).strip()
        if s.startswith("|"):
            s = s[1:]
        if s.endswith("|"):
            s = s[:-1]
        return [c.strip() for c in s.split("|")]

    def _is_table_row(line):
        s = str(line).strip()
        return s.startswith("|") and s.count("|") >= 2

    def _is_sep_row(line):
        s = str(line).strip()
        if not _is_table_row(s):
            return False
        body = s.replace("|", "").replace(" ", "").replace(":", "")
        return bool(body) and set(body) <= set("-–—=")

    def _clean(s):
        return str(s).replace("<br/>", "\n").replace("<br />", "\n").replace("<br>", "\n")

    lines = str(text or "").splitlines()
    i = 0
    while i < len(lines):
        line = str(lines[i]).strip()
        if not line:
            doc.add_paragraph("")
            i += 1
            continue
        if _is_table_row(line):
            j = i
            while j < len(lines) and _is_table_row(lines[j]):
                j += 1
            rows = [_cells(lines[k]) for k in range(i, j) if not _is_sep_row(lines[k])]
            width = max((len(r) for r in rows), default=0)
            rows = [r + [""] * (width - len(r)) for r in rows]
            if width >= 2 and rows:
                t = doc.add_table(rows=0, cols=width)
                try:
                    t.style = "Table Grid"
                except Exception:
                    pass
                for r in rows:
                    cells = t.add_row().cells
                    for c, v in enumerate(r):
                        cells[c].text = _clean(v)
            for _ in range(i, j):          # 每行 markdown 仍占一个空段落（计数不变）
                doc.add_paragraph("")
            i = j
            continue
        p = doc.add_paragraph()
        if line.startswith("#"):
            p.add_run(_clean(line.lstrip("# "))).bold = True
        else:
            parts = _clean(line).split("\n")
            p.add_run(parts[0])
            for extra in parts[1:]:        # <br> → 软换行，不新起段落
                p.add_run().add_break()
                p.add_run(extra)
        i += 1


def build_plan_docx(plan, draft=False) -> str:
    from docx import Document
    from docx.shared import Pt

    plan = _strip_single_zone_prefix(plan)   # Word 同样不该出现无意义的「Ⅰ区」（见函数注释）
    doc = Document()
    ov = plan.get("overview") or {}
    rp = plan.get("resource_plan") or {}
    meta = plan.get("meta") if isinstance(plan.get("meta"), dict) else {}
    view = _compute_view(plan)

    def add_h(text, level=1):
        doc.add_heading(text, level=level)

    def add_kv(rows):
        t = doc.add_table(rows=0, cols=2); t.style = "Table Grid"
        for k, v in rows:
            c = t.add_row().cells; c[0].text = str(k)
            c[1].text = "" if v is None else str(v)

    def add_grid(header, rows):
        t = doc.add_table(rows=1, cols=len(header)); t.style = "Table Grid"
        for j, h in enumerate(header):
            t.rows[0].cells[j].text = str(h)
        for row in rows:
            c = t.add_row().cells
            for j, v in enumerate(row):
                c[j].text = "" if v is None else str(v)

    def add_md_lines(text):
        return _add_md_lines(doc, text)

    doc.add_heading(ov.get("project_name") or "施工进度计划", level=0)

    # ---- 品牌标识（单一真源：pipeline/branding.py）----
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    _cover = [
        (f"{branding.PRODUCT}  v{branding.VERSION}", True, 12),
        (branding.SLOGAN, False, 10),
        (branding.SUBTITLE, False, 9),
        (branding.SIGN, False, 9),
    ]
    for _txt, _bold, _sz in _cover:
        _p = doc.add_paragraph()
        _p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        _r = _p.add_run(_txt)
        _r.bold = _bold
        _r.font.size = Pt(_sz)
    doc.add_paragraph()

    # ---- 审计状态戳：草案与定稿必须一眼能分（防"未审计的计划被当成定稿"）----
    # 判据是**数据**，不是调用方传进来的 `draft`、也不是 meta 自称的 audit_status：
    # `_audit_display` → `audit_gate.audit_honesty` 要求 R1/R2/R3 都有记录、都通过、
    # 且每轮 `answered_by == "human"`。用户审计 P0-A 就是这样被印错的：三个门是
    # `devtools/rerun_sample3.py` 用脚本代答的，定稿却印出「已审计定稿 / R3 通过」。
    # 现在：脚本/系统代答或缺记录 → 一律「未审计 · 待人工复审」+ **写明原因**。
    _aud = _audit_display(plan)
    _stamp = ("草案 · 未审计" if draft
              else ("已审计定稿" if _aud["status"] == "已审计" else "未审计 · 待人工复审"))
    _p = doc.add_paragraph()
    _p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _r = _p.add_run("【%s】" % _stamp)
    _r.bold = True
    _r.font.size = Pt(14)
    _p2 = doc.add_paragraph()
    _p2.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _p2.add_run("三轮回审：%s" % _aud["round_text"]).font.size = Pt(9)
    if draft:
        _audit_note = ("本文件为审计用草案：进度表与资源表为文字表格、不含图表；"
                       "确认后才整理最终计划并绘制可视化看板。")
    elif _aud["status"] == "已审计":
        _audit_note = "R1 / R2 / R3 均由人工复核通过（每轮 answered_by=human）。"
    else:
        # ⚠️ 这一句里**不许出现「已审计定稿」**（哪怕是"不是已审计定稿"）：
        # 用户的复检脚本按禁语做子串匹配（`_probe_tmp/q_audit_check.py`），
        # 命中就判"定稿谎报已审计"。所以用"按未审计口径出具"这类不含禁语的说法。
        _audit_note = ("本文件按未审计口径出具：%s。"
                       "人工三轮复审通过后才算定稿。" % (_aud["reason"] or "数据里没有三轮人工通过的记录"))
    _p3 = doc.add_paragraph()
    _p3.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _p3.add_run(_audit_note).font.size = Pt(9)
    doc.add_paragraph()

    # ---- 模型参与度警示：**正文最前面**，且不容忽略 ----
    # 真实事故：calls == 0 的那次运行，交付物与正常计划一模一样（见
    # `model_participation_notice`）。level == "ok" 时这里一个字都不加。
    _pnotice = model_participation_notice(plan)
    if _pnotice:
        _pn = doc.add_paragraph()
        _pnr = _pn.add_run(_pnotice)
        _pnr.bold = True
        try:
            from docx.shared import RGBColor
            _pnr.font.color.rgb = RGBColor(0xC0, 0x39, 0x2B)
        except Exception:
            pass

    add_h("一、计划总览")
    # 峰值口径：总览与「四、人员配置」必须读**同一个** _manpower_peaks()，
    # 并各自把口径名写出来 —— 历史缺陷是两处都叫「峰值」却指两个不同的数
    # （120 = 用户限额 / 245 = 资源曲线峰值），同一份文档自相矛盾。
    _pk_ov = _manpower_peaks(plan, view)
    # 第 39 轮：峰值人数的**口径**由计划端给出（user / resource_curve），交付侧照译。
    # 旧计划没有这个键 → 退回"资源曲线口径 / 用户限额口径"的老写法。
    # E1（2026-09-21）：`model_estimate`（模型补的申报值）已不再是计划端会产出的口径。
    _cal_ov = _peak_caliber(plan, view)
    _peak_cells = []
    if _cal_ov is not None:
        _peak_cells.append("峰值人数 %s" % _cal_ov["text"])
        if _pk_ov["curve"] is not None:
            _peak_cells.append("每日用工峰值（按任务叠加）%s 人" % _fnum(_pk_ov["curve"]))
    else:
        if _pk_ov["curve"] is not None:
            _peak_cells.append("资源曲线口径 %s 人" % _fnum(_pk_ov["curve"]))
        if _pk_ov["limit"] is not None:
            _peak_cells.append("用户限额口径 %s 人" % _fnum(_pk_ov["limit"]))
    # ---- 计划总览：审计身份 + 四个「天数」的语义（P0-A / P0-B）----
    _fallback_total = f"{ov.get('total_duration_days') if ov.get('total_duration_days') is not None else view['total_day_count']} 天"
    _days_rows = _duration_caliber_rows(plan)
    if not any(str(k).startswith("总工期") for k, _ in _days_rows):
        _days_rows.insert(0, ("总工期", _fallback_total))
    _kv_rows = [
        ("计划编号", plan.get("plan_id")),
        ("审计状态", _aud["badge"]),
        ("三轮回审", _aud["round_text"]),
    ]
    if _aud["reason"]:
        # 「为什么不能印已审计」必须写在用户看得见的地方，否则读者以为是我们忘了审
        _kv_rows.append(("未按已审计交付的原因", _aud["reason"]))
    _kv_rows += [
        ("计划起止", f"{ov.get('planned_start_date')} → {ov.get('planned_end_date')}"),
    ]
    _kv_rows += _days_rows
    _kv_rows += _critical_path_rows(plan)
    _kv_rows += [
        ("工序总数", len(_tasks(plan))),
        ("人工峰值（口径见「四、人员配置」）",
         "；".join(_peak_cells) if _peak_cells else f"{rp.get('peak_manpower')} 人"),
        ("总人工·日", rp.get("total_manpower_days")),
    ]
    add_kv(_kv_rows)
    # ---- 模型用量（如实口径）：这里印的是**计划数据定稿时**的快照 ----
    # Word 在 `HtmlPageNode`（最后一个 LLM 节点）**之前**生成，拿不到运行末尾用量；
    # 把定稿时的数当总量写就是少报（实测差一半），所以这一句必须写明"其后未计入"。
    add_usage_section(doc, plan)
    if meta.get("caliber_note"):
        doc.add_paragraph(str(meta["caliber_note"]))
    _pbanner = params_banner(plan)
    if _pbanner:
        _p = doc.add_paragraph()
        _r = _p.add_run(_pbanner)
        _r.bold = True
        try:
            from docx.shared import RGBColor
            _r.font.color.rgb = RGBColor(0xC0, 0x39, 0x2B)
        except Exception:
            pass
    _gnote = granularity_note(plan)
    if _gnote:
        doc.add_paragraph(_gnote)
    # ---- 【第 2 批 · 域 2 / 2.7】交付物声明：本计划不含材料计划 ----
    # 与看板**同源**（同一个 `MATERIALS_EXCLUDED_NOTICE` 常量）。
    # 位置：紧跟"计划总览"里的参数/粒度说明之后（读者看总览时就会读到），
    # 排版沿用本文件既有的"加一段说明文字"写法（`doc.add_paragraph`），
    # 不新造标题层 / 不新造表格 —— 与 `_gnote` 同一处、同一纪律。
    # 无条件印（不是条件分支）：材料清单已从输入侧删除，"不含"是**恒真**的设计事实。
    doc.add_paragraph(MATERIALS_EXCLUDED_NOTICE)
    # ---- 域 8.1：AI 补的限额被丢弃的披露 ----
    _iml = _ignored_model_limits_text(plan)
    if _iml:
        doc.add_paragraph(_iml)
    # ---- 域 8.8①：未人工核验的 L4 条数 ----
    _l4r = _l4_review_notice_text()
    if _l4r:
        doc.add_paragraph(_l4r)
    for c in (meta.get("audit_comments") or [])[:3]:
        doc.add_paragraph("审计意见（R%s）：%s" % (c.get("round"), c.get("comment")))

    # ---- 数据来源与置信度（紧接计划总览）----
    # 为什么放在这么靠前：用户拿到的第一件事应该是「这份计划里哪些数是算的、
    # 哪些是估的、目标工期差多少」。放在文末等于没写。
    # 拿不到置信度类元数据 → 整段降级（一个字都不打，见 add_confidence_section）。
    add_confidence_section(doc, plan, view, add_h, add_kv, add_grid)

    # 里程碑（若不足 5 个，用关键路径头几项补齐）
    add_h("二、关键里程碑")
    ms = plan.get("key_milestones") or []
    if len(ms) < 5:
        seen = {m.get("name") for m in ms}
        for t in plan.get("critical_path_tasks") or []:
            if len(ms) >= 5:
                break
            nm = (t.get("task_name") or "") + "完成"
            if nm not in seen:
                ms.append({"name": nm, "date": t.get("finish_date"),
                           "task_id": t.get("task_id"), "description": "关键路径里程碑"})
                seen.add(nm)
    for m in ms:
        doc.add_paragraph(f"• {m.get('name')}（{m.get('date')}）— {m.get('description', '')}")

    # 甘特表（进度计划表）
    # 「工期(天·排程)」= 起止日期跨度（含首尾，`end_day - start_day + 1`）—— 与同一行的
    # 开始/完成列**必然一致**；WBS 模型写的目标天数单列成「WBS 目标(天)」。
    # 历史缺陷（用户实测）：ALC 行日期跨 31 天、"工期"列却印 3（WBS 目标），同一行两个数。
    add_h("三、横道图（甘特排程，★=关键路径）")
    _rd_map_word = _rd_task_map(plan)
    # 换算参数来源键 `norm_binding.ctx_source` 只挂在 WBS 叶子上（任务行不带），
    # 依据列要按来源分流就必须把叶子一起传下去（见 `unit_assumption_source`）。
    _leaf_map_word = _leaf_bindings(plan)
    # 「WBS 目标(天)」列读的是**WBS 目标**，不是排程跨度（P0-B：该行日期列已经给出
    # 排程跨度；`duration_days` 现在与日期同源，取它当 WBS 目标就会印出同一个数）。
    # 老计划 / 重排后的计划没有 `wbs_target_days` 键 → 由 `_task_wbs_target` 按
    # "字段是否等于本行日期跨度"判定，判不出来就印「—」。
    _span_word = _schedule_span(plan)
    _wbs_target = {}
    for t in _tasks(plan):
        if not isinstance(t, dict):
            continue
        _tid = str(t.get("task_id"))
        _v = _task_wbs_target(t, _span_word.get(_tid))
        # 判不出来就印「—」：`_wbs_target.get(...)` 只有键**不存在**时才用默认值，
        # 键在而值是 None 会印成空单元格（看起来像"这一格没有数据"，但用户读不出
        # "我们不知道"）。显式写「—」。
        _wbs_target[_tid] = "—" if _v is None else _v
    _g_ids, _g_target = {}, {}
    for _g in rolled_rows(plan):
        _ids = [str(i) for i in (_g.get("ids") or [])]
        _g_ids[_group_id(_ids)] = _ids
        _g_target[_group_id(_ids)] = _g.get("工期")
    _gantt_rows = []
    for g in view["gantt"]:
        _gid = str(g["id"])
        _ids = _g_ids.get(_gid) or [_gid]
        if _gid in _rd_map_word:
            _ev = _evidence_text(_rd_map_word.get(_gid), _leaf_map_word.get(_gid))
        else:
            _ev = _group_evidence(_ids, _rd_map_word)
        _gantt_rows.append((
            g["id"], g["name"], g["start_day"], g["end_day"],
            max(1, int(g.get("end_day") or 0) - int(g.get("start_day") or 0) + 1),
            _wbs_target.get(_gid, _g_target.get(_gid, "—")), _ev,
            "★" if g["crit"] else ""))
    add_grid(["任务 ID", "任务", "开始(D)", "完成(D)", "工期(天·排程)", "WBS 目标(天)",
              "依据 / 资源", "关键"], _gantt_rows)

    # 施工组织层口径（工日 → 工期 是怎么来的）+ 组织缺口 + 审计提示。
    # 与看板同源（`organization_section_model`），且**不进目录**（用加粗正文段当标题）。
    add_organization_section(doc, plan, view, add_grid)

    # 人员配置（按工种合计 + 峰值）
    add_h("四、人员配置（各工种总工日 / 峰值）")
    trade_totals = {}
    for x in view["labor_daily"]:
        for k, v in x["trades"].items():
            trade_totals[k] = trade_totals.get(k, 0) + v
    add_kv([("峰值总人数 · 资源曲线口径（逐日累加，含机械配员）",
             f"{view['peak_total']} 人"),
            ("峰值工种", view["peak_trade"] or "—"),
            ("总人·日", rp.get("total_manpower_days"))])
    # 容量口径三态（裁定 B）：全 mwi → 一个字都不印；`reported_missing` 的那几条
    # 必须让用户看到"工期不随工程量变化，原因是缺容量数据"（否则他会反复改工程量试）。
    for _cc_line in capacity_caliber_model(plan)["lines"]:
        doc.add_paragraph(_cc_line)
    # ---- 域 8.3：日级资源账单（确定性渲染，看板 + Word 共用）----
    _drb = _daily_resource_bill_model(plan)
    if _drb["present"]:
        doc.add_paragraph(_daily_resource_bill_text(_drb))
    # 峰值人数的口径标注。为什么不能只写一个数字：用户实测质问"为什么资源这么少"，
    # 因为他看到的 120 人**根本不是他给的**，而文档却写着"用户限额"。
    # E1（2026-09-21 裁定）：**「申报峰值」已不是展示项** —— 旧实现在这里把模型补的
    # 120 用「计划里另记申报峰值 …」再印一遍，已整段删除。
    _cal4 = _peak_caliber(plan, view)
    if _cal4 is not None:
        _p = doc.add_paragraph()
        _p.add_run("峰值人数：%s 人（" % _fnum(_cal4["peak"]))
        _p.add_run(_cal4["label"]).bold = True
        _p.add_run("）；每日用工峰值（按任务叠加）：%s 人。"
                   % f"{view['peak_total']}")
        doc.add_paragraph(
            "口径说明：本表「峰值总人数 %s 人」= 逐日人员曲线峰值（%s 人，含机械配员）；"
            "上面的「峰值人数 %s 人」口径为「%s」%s。"
            % (view["peak_total"], view["peak_trade"] or "—", _fnum(_cal4["peak"]),
               _cal4["label"],
               "，两者是同一个数"
               if _fnum(_cal4["peak"]) == _fnum(view["peak_total"])
               else "，两者不是同一个数"))
    elif _pk_ov["limit"] is not None:
        doc.add_paragraph(
            "口径说明：本表「峰值总人数 %s 人」= 逐日人员曲线峰值（%s 人，含机械配员）；"
            "总览里的「用户限额口径 %s 人」= 用户给定的人工上限，两者不是同一个数。"
            % (view["peak_total"], view["peak_trade"] or "—", _fnum(_pk_ov["limit"])))
    # 交付侧重算的曲线峰值 vs 调度器 `curve_peak_manpower`：实测差 3 人，是**口径**不是 bug
    # （场地级设备配员逐日 max）。差额由 `_peak_curve_diff` 两条独立计算得出，不写死数字。
    _pcd = _peak_curve_diff(plan, view)
    if _pcd is not None:
        doc.add_paragraph(_pcd["sentence"])
    # D2：班组人数的**两个真源**都必须报（有组织层的任务不再走标定公式；见
    # `_workface_summary` 的 docstring）—— 用户看到"38 人"要能知道这个数是谁定的、
    # 哪批任务不是按本段工程量算的、以及哪批本该削峰却没削。
    _wf_txt = _workface_sentence(plan)
    if _wf_txt:
        _wfs = _workface_summary(plan)
        doc.add_paragraph("工作面容量口径：" + _wf_txt + "。")
        if _wfs["examples"]:
            add_grid(["任务（资源）", "原始 → 上限", "原因"], list(_wfs["examples"]))
        _below = _workface_below_org_lines(_wfs)
        if _below:
            doc.add_paragraph(_workface_below_org_lead(_wfs))
            for _line in _below:
                doc.add_paragraph(_line + "。")
        _shave = _workface_peak_shaving_sentence(_wfs)
        if _shave:
            doc.add_paragraph(_shave)
    if trade_totals:
        # 日均 = 物理量 ÷ **天数**，不是 ÷ 末日下标：`total_days` 是 0 基末日下标，
        # 拿它当分母会系统性偏大 1/天数（688 天的计划偏大 0.15%）。这里用
        # `total_day_count`（末日下标 + 1，含首尾），与 `overview.planned_end_date`
        # −`planned_start_date`+1 恒等；同一表达式只在这一个地方定义。
        _day_count = max(1, int(view["total_day_count"]))
        add_grid(["工种", "累计人·日", "日均"],
                 [(k, v, round(v / _day_count, 1))
                  for k, v in sorted(trade_totals.items(), key=lambda x: -x[1])])

    # 设备荷载
    add_h("五、设备资源荷载（峰值）")
    equip_totals = {}
    for x in view["equip_daily"]:
        for k, v in x["items"].items():
            equip_totals[k] = max(equip_totals.get(k, 0), v)
    if equip_totals:
        add_grid(["设备", "单日峰值(台)", "累计台·日"],
                 [(k, equip_totals.get(k, 0), sum(x["items"].get(k, 0) for x in view["equip_daily"]))
                  for k in sorted(equip_totals)])
    else:
        doc.add_paragraph("（无设备荷载数据）")

    # 场地级设备（塔吊/施工电梯）：**台数逐日取 max**、来源与配员必须一起写出来。
    _site_txt = _site_equipment_sentence(plan)
    if _site_txt:
        _site_s = _site_equipment_summary(plan)
        doc.add_paragraph("场地级常驻设备（垂直运输）：" + _site_txt)
        _site_rows = "、".join(
            "任务 %s（%s，台数来源：%s，配员来源：%s）"
            % (i["hit_tasks"], i["name"], i["source_text"],
               "kb:Equipment_Crew_Mapping" if i["crew_source"] == "kb" else "代码兜底默认")
            for i in _site_s["items"])
        doc.add_paragraph("逐设备命中：" + _site_rows + "。")

    # D5：设备清单的**三态**（第 44 轮）—— 用户申报了却被排程当没看见的限额必须显形；
    # 模型补的估算必须点明"非用户输入、且未作为限额使用"；清单为空时**不许**写成
    # "用户没申报"（那是把系统的缺口说成用户的缺失）。
    _eb = _equipment_binding_rows(plan)
    _eq_state, _eq_items, _eq_src = equipment_declared_state(plan)
    if _eb and _eq_state in (None, "user"):
        # 老计划 / 用户申报且排程端给了对账结果 → 既有逐字文案与表格（既有回归门钉着）
        doc.add_paragraph("用户申报设备限额对账（是否在排程里生效）：")
        add_grid(["用户申报设备", "数量", "是否生效", "说明"],
                 [(r["name"],
                   "—" if r["quantity"] is None else r["quantity"],
                   "生效" if r["effective"] else "⚠ 未生效",
                   r["note"]) for r in _eb])
        if any(not r["effective"] for r in _eb):
            doc.add_paragraph("未匹配的设备限额没有参与排程，如需生效请在计划里给它们安排工序。")
    elif _eq_state == "user":
        doc.add_paragraph(
            "用户申报设备限额对账：边界条件里用户申报了 %s；排程端没有返回逐条对账结果"
            "（equipment_binding 为空），因此无法判断这些设备限额有没有生效。"
            % (_equipment_items_text(_eq_items) or "（清单为空）"))
    else:
        _eq_s, _eq_text = equipment_declared_sentence(plan)
        if _eq_text:
            doc.add_paragraph("%s：%s" % (EQUIPMENT_SECTION_TITLE, _eq_text))
            if _eq_state in ("model", "unmarked") and _eq_items:
                _eq_src_label = (MODEL_EQUIPMENT_LABEL if _eq_state == "model"
                                 else "来源未标注")
                add_grid(["设备", "数量", "单位", "来源"],
                         [(i["name"],
                           "—" if i.get("quantity") in (None, "") else i["quantity"],
                           i.get("unit") or "—", _eq_src_label)
                          for i in _eq_items])

    # 流水组织 / 季节性保障
    add_h("六、流水施工组织与季节性保障")
    doc.add_paragraph("流水组织（确定性摘要）：按阶段/工种串行衔接，主体结构可分段流水；"
                      "详见横道图（甘特）各任务的起止衔接。")
    doc.add_paragraph("季节性保障：")
    for r in plan.get("risks") or []:
        doc.add_paragraph(f"• {r.get('risk_name')}：{r.get('mitigation', '')}")

    # 风险 + 报告
    risks = plan.get("risks") or []
    if risks:
        add_h("七、主要风险与应对")
        for r in risks:
            doc.add_paragraph(f"• {r.get('risk_name')}：{r.get('mitigation', '')}")
    add_h("八、施工监督报告")
    add_md_lines(_report_text(plan))

    # ---- 节点级告警（第 44 轮）：meta 里恒有，交付物却一个字都没印 ----
    # 无告警时整节不出（不空表、不占位）；标题用 level=2，与目录（只镜像 Heading 1）无关。
    add_node_warnings_section(doc, plan)

    # ---- 页脚署名（品牌单一真源）----
    try:
        _sec = doc.sections[0]
        _sec.footer.paragraphs[0].text = branding.footer_text(
            datetime.date.today().isoformat())
        _sec.footer.paragraphs[0].alignment = 1
    except Exception:
        pass

    out = _plan_dir(plan) / ("施工进度计划（草案·未审计）.docx" if draft
                             else "施工进度计划.docx")
    doc.save(str(out))
    # ---- G5：交付物单位清零（报告档，见本文件顶部 G5 说明）----
    _assert_docx_no_cjk_compat_square_metre(out)
    return str(out)


# ============================================================
# T4 · HtmlPageNode（自包含，SVG 甘特 + 人员曲线 + 资源荷载）
# ============================================================

class HtmlPageNode(BaseNode):
    name = "html_page"
    title = "导出 HTML 看板"

    def __init__(self, llm=None):
        super().__init__()
        self.llm = llm          # 非空 → LLM 编排网页；空 → 确定性模板

    def run(self, ctx):
        plan = ctx.get("plan_json")
        if not plan:
            return {"_stop": "无 plan_json，跳过 HTML 导出"}
        try:
            if self.llm is not None:
                path, used_agent = build_plan_html_agent(plan, self.llm, ctx)
            else:
                path, used_agent = build_plan_html(plan), False
            _maintain(Path(path).parent.parent)   # 跟随实际写入目录，勿用常量（同 Word 导出处）
            # ---- 运行末尾用量（第 44 轮）----
            # 本节点是**最后一个节点、也是最后一个 LLM 节点**（见 `USAGE_NOTE` 上方注释与
            # `builder._main_nodes()` 的节点顺序）：此刻 `usage.meter().snapshot()` 才是完整的。
            # 落进 `meta.usage_final` 并写回计划 JSON —— `meta.usage` 是**计划数据定稿时**的
            # 快照，不写回的话用户下次读到的花费就是少报的那个。
            _record_usage_final(plan)
            # D6（合同 §9.2）：关键路径规范依据覆盖率写进 meta 与产物。
            # 只往**既有** `norm_coverage` 里加子键（不新增 meta 顶层键）；没有该结构就不写。
            _record_norm_coverage(plan)
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            ctx.setdefault("wbs_warnings", []).append(f"HTML 导出失败：{msg}")
            self.emit("node_progress", {"node": self.name, "progress": 100,
                                        "message": f"✖ HTML 导出失败（不影响计划）：{msg}"})
            self.done_summary = f"HTML 导出失败：{msg}"
            return {}
        artifacts = dict(ctx.get("artifacts") or {})
        artifacts["html"] = path
        mode = "LLM 编排" if used_agent else "确定性模板"
        self.done_summary = f"已导出 HTML 看板（{mode}）：{path}"
        return {"artifacts": artifacts}


# ============================================================
# LLM 网页编排（确定性数据 + LLM 布局；失败回退确定性模板）
# ============================================================

# ══════════════ 关键路径口径：**条数（个）** 与 **天数（天）** 是两回事 ══════════════
# 真实缺陷（真计划 `plans/plan_run_1789895021.json`：overview.critical_path_length=81、
# total_duration_days=604、cpm_result.total_duration_days=604）：交付物正文写着
#   「**当前计划总工期为604天，关键路径长度为81天**，表明非关键路径任务具有一定的浮动时间。」
# —— 81 是**关键路径任务的条数（个）**，被写成了天数；而同一份交付物的表格里明明写着
# 「关键路径任务数 81 个」「关键路径工期·排程版 604 天」。歧义源是那个老键名里的"长度"。
# 交付侧两道防线（口径与改写规则都在 `plan_assembler`，这里只做交付侧的落点）：
#   ① facts 里这个数**带名字、带单位**（`_facts_overview` / `_critical_path_caliber_fact`）；
#   ② 模型写出来的叙述再过一道**确定性改写**（`_fix_critical_path_narrative`），
#      docx / 看板 / facts 的正文则统一走 `_report_text`。
# plan_json 的契约键（overview.critical_path_length）一个字都不动。
def _facts_overview(ov):
    """facts 里的 `overview`：**去掉有歧义的键名**（facts 只进 prompt、不落盘）。

    `overview.critical_path_length` 装的是关键路径**任务条数（个）**，但"长度"这个词与
    同一个 overview 里的 `total_duration_days`（天）摆在一起，模型实测把它读成了天数
    （交付物正文「关键路径长度为81天」）。这里换名成 `critical_path_task_count` 并附一句
    note；plan_json 的契约键 `overview.critical_path_length` 不动（schemas.Overview、
    看板、契约测试都读它）。
    """
    src = ov if isinstance(ov, dict) else {}
    out = {k: v for k, v in src.items() if k != "critical_path_length"}
    n = src.get("critical_path_length")
    if n is None:
        n = src.get("critical_path_task_count")
    if n is not None:
        out["critical_path_task_count"] = n
        out["critical_path_task_count_note"] = (
            "上键是**条数（个）**，不是天数/长度；关键路径的天数见 critical_path_caliber")
    return out


def _critical_path_caliber_fact(plan):
    """关键路径两个数（条数 / 天数）的带标签版本 —— 与报告侧同一真源、同一规则。"""
    try:
        from . import plan_assembler
        return plan_assembler.critical_path_caliber(plan)
    except Exception:
        return {}


def _fix_critical_path_narrative(html_text, plan):
    """关键路径口径的结构性守卫：模型页面把**条数**写成天数 → 确定性改写。

    与 `_ensure_org_section` / `_ensure_confidence_section` 同一套路（判据独立、
    宁缺勿造、失败保持原页面不动）：返回 ``(html_text, 改写说明列表)``。
    页面上没有「关键路径长度」这个词 → **一行都不动**；数字与计划真值对不上 →
    那一处**一个字都不动**（绝不猜、绝不拿别的数顶上）。
    调用方把改写痕迹写进节点状态 / `ctx["wbs_warnings"]`。
    """
    if not html_text:
        return html_text, []
    try:
        from . import plan_assembler
    except Exception:
        return html_text, []
    if plan_assembler.CRITICAL_PATH_TERM not in html_text:
        return html_text, []
    try:
        fixed, fixes = plan_assembler.fix_critical_path_wording(html_text, plan)
    except Exception:
        return html_text, []
    if not fixes or fixed == html_text:
        return html_text, []
    return fixed, fixes


def _facts_bundle(plan, view):
    """把确定性的数据与图表打包成"可信资产"，交给 LLM 编排。

    明确不含原始完整 plan_json —— 避免 LLM 编造/网页出现黑色调试块。
    """
    ov = plan.get("overview") or {}
    rp = plan.get("resource_plan") or {}
    meta = plan.get("meta") if isinstance(plan.get("meta"), dict) else {}
    total = ov.get("total_duration_days") if ov.get("total_duration_days") is not None \
        else view["total_day_count"]

    milestones = plan.get("key_milestones") or []
    if len(milestones) < 5:
        seen = {m.get("name") for m in milestones}
        for t in plan.get("critical_path_tasks") or []:
            if len(milestones) >= 5:
                break
            nm = (t.get("task_name") or "") + "完成"
            if nm not in seen:
                milestones.append({"name": nm, "date": t.get("finish_date"),
                                   "task_id": t.get("task_id"), "description": "关键路径里程碑"})
                seen.add(nm)

    # P0-B：两列口径分开喂给模型 —— `duration_days` 是**排程跨度**（与日期同源），
    # `wbs_target_days` 是 WBS 目标。以前只有一个 `duration_days` 且装的是 WBS 目标，
    # 模型页面于是把"字段 15 天 / 日期 6 天"这种自相矛盾原样写出来。
    cp_tasks = [{"task_id": t.get("task_id"), "task_name": t.get("task_name"),
                 "start_date": t.get("start_date"), "finish_date": t.get("finish_date"),
                 "duration_days": t.get("duration_days"),
                 "wbs_target_days": _task_wbs_target(t, _row_date_span(t))}
                for t in (plan.get("critical_path_tasks") or [])]

    wbs_rows = []
    _rolled_facts = rolled_rows(plan)
    if _rolled_facts:
        for g in _rolled_facts:
            _ids = [str(i) for i in (g.get("ids") or [])]
            wbs_rows.append([g.get("phase"), g.get("work_package"), _group_id(_ids),
                             _group_label(g, len(_ids)), g.get("工期"),
                             g.get("工程量"), g.get("单位")])
    else:
        for ph in (plan.get("wbs") or {}).get("phases", []):
            for wp in ph.get("work_packages", []):
                for sub in wp.get("sub_packages", []):
                    wbs_rows.append([ph.get("phase"), wp.get("name"), sub.get("id"),
                                     sub.get("name"), sub.get("duration_days"),
                                     sub.get("quantity"), sub.get("unit")])

    # 任务级依据（依据列的源数据）用同一份 task_id → resource_demand 映射，算一次。
    _rdf = _rd_task_map(plan)
    _leaff = _leaf_bindings(plan)      # 换算参数来源键只挂 WBS 叶子（见 _evidence_core）

    # SVG 图表（确定性）
    lab = view["labor_daily"]
    if len(lab) > 40:
        step = len(lab) // 40
        lab = [x for i, x in enumerate(lab) if i % step == 0] + [view["labor_daily"][-1]]
    facts = {
        "project_name": ov.get("project_name"),
        "key_numbers": {
            "total_duration_days": total,
            "planned_start": ov.get("planned_start_date"),
            "planned_end": ov.get("planned_end_date"),
            # ⚠️ 这是**条数**（个），不是"关键路径长度/天数"（P0-B）。天数在下面几行。
            # 键名也一并去歧义：facts 里**不再出现** `critical_path_length` 这个名字
            # （真事故：模型把它写成「关键路径长度为81天」，而总工期是 604 天）。
            "critical_path_task_count": ov.get("critical_path_length"),
            "critical_path_task_count_note": (
                "上键是**条数（个）**，不是天数/长度；关键路径的天数见 critical_path_caliber"),
            "task_count": len(_tasks(plan)),
            "peak_manpower": view["peak_total"],
            "peak_trade": view["peak_trade"],
            "plan_id": plan.get("plan_id"),
        },
        # 四个「天数」的语义（P0-B）：模型页面必须照这里的 label 写，不许自己起名字。
        "duration_calibers": [{"label": k, "value": v} for k, v in _duration_caliber_rows(plan)],
        # 「关键路径」的条数与工期分列（P0-B）
        "critical_path_calibers": [{"label": k, "value": v} for k, v in _critical_path_rows(plan)],
        # 审计身份：模型页面也不许把脚本代答写成"已审计"（P0-A）
        "audit": {k: v for k, v in _audit_display(plan).items() if k != "color"},
        # 关键路径口径（**条数 ≠ 天数**）——见 `_facts_overview` 的说明。
        # `overview` 里那个歧义键名换成带单位的条数；这里再给一份两个数各自的
        # 名字 / 单位 / 来源键（与报告侧 `plan_assembler.critical_path_caliber` 同源）。
        "overview": _facts_overview(ov),
        "critical_path_caliber": _critical_path_caliber_fact(plan),
        "milestones": milestones,
        "critical_path_tasks": cp_tasks,
        "wbs_table": wbs_rows,
        "display_granularity_note": granularity_note(plan),
        "resource_plan": {
            "peak_manpower": rp.get("peak_manpower"),
            "total_manpower_days": rp.get("total_manpower_days"),
            "equipment_peak": rp.get("equipment_peak"),
            # 【第 2 批 · 域 2 / 2.6】`material_summary` **不再喂给编排模型**：
            # 交付物已声明「本计划不含材料计划」，facts 里再给一份材料表会让模型
            # 自己又写一节材料清单出来（口径自相矛盾）。
            # ③-a：口径字段必须喂给模型。此前 `_facts_bundle` 只给裸数字
            # （peak_manpower=19），模型于是不知道"19"是逐日曲线峰值而非用户给的限额
            # （`peak_manpower_source=resource_curve`）—— 实测模型页面里这个口径
            # 一个字都没写。缺什么补什么，**只补 facts，不改别的节点契约**。
            # E1（2026-09-21 裁定）：`declared_peak_manpower` /
            # `declared_peak_manpower_source` 两个键**已删除**（模型补的 120 不再透传，
            # 交付物也不再有「申报峰值」展示项）。
            "peak_manpower_source": rp.get("peak_manpower_source"),
            "curve_peak_manpower": rp.get("curve_peak_manpower"),
            "machine_crew_peak": rp.get("machine_crew_peak"),
            "labor_demand": rp.get("labor_demand"),
            "labor_demand_detail": rp.get("labor_demand_detail"),
        },
        # 峰值口径的**一句话**（与看板卡片、Word 同源：`_peak_caliber`）
        "peak_caliber": _peak_caliber(plan, view),
        # 交付侧曲线峰值 vs 调度器 `curve_peak_manpower` 的差额归因（场地级设备配员，
        # 两条独立计算；一致/缺键时是 None → 模型一个字都不许加）
        "peak_curve_diff": _peak_curve_diff(plan, view),
        # 工作面容量口径（`_workface_summary` / `_workface_sentence`）：模型必须能读到
        # "班组人数是按本施工段工程量算出来的、几条顶到上限"，否则它无从写这段。
        "workface_capacity": dict(_workface_summary(plan),
                                  sentence=_workface_sentence(plan),
                                  site_equipment_sentence=_site_equipment_sentence(plan)),
        # 用户申报设备限额对账（`meta.equipment_binding` 的逐条结论）
        "equipment_binding": _equipment_binding_rows(plan),
        # 设备清单的**三态**（第 44 轮）：这一节到底该写什么，判据与结论都由计划数据给出。
        # 真实事故：模型把"本次没取得设备清单"写成了"用户没申报设备限额"（甩锅）。
        # `equipment_binding: []` 确实容易被读成"用户没给"，所以这里连**该写的那句话**
        # 一起给（`sentence`），并要求原样使用。
        "equipment_declared": _equipment_declared_fact(plan),
        # 节点级告警（`meta.node_warnings` / `node_warning_count` / `model_call_failures`）：
        # 计划 JSON 里恒有，交付物却一个字都不印（第 44 轮修）。count == 0 → 本节不写。
        "node_warnings": {
            "count": (node_warnings_model(plan) or {}).get("count"),
            "failure_count": (node_warnings_model(plan) or {}).get("failure_count"),
            "items": (node_warnings_model(plan) or {}).get("items") or [],
            "how_to_write": ("count 为 0（或取不到）→ 本节一个字都不要写，不要空表、"
                             "不要「无告警」占位；> 0 → 摘要行"
                             "「本次运行有 N 条节点级告警，其中 M 条是模型调用失败」"
                             "（N/M 照抄，不许改写）+ 逐条 node / message / detail。"),
        },
        # 用量口径：交付页末尾**由系统确定性追加**运行末尾用量。
        # ⚠ 模型不许自己在正文里写用量数字 —— facts 里没有运行末尾用量，而 `meta.usage`
        #   是"计划数据定稿时"的快照，把它当总量写出来就是少报（实测差一半）。
        "usage_caliber": ("交付页的模型用量由系统在页面末尾确定性追加（运行末尾快照，"
                          "含本节点自身调用）；正文不要自己写用量数字。"),
        # 施工组织层口径（用户最大的疑问：306 工日 ÷ 9 人 = 34 天/层，凭什么）：
        # 逐条 `_organization` + 组织缺口 + 审计提示 + 口径对齐自检。
        # 字段名照抄契约；模型漏写这两段时由 `_ensure_org_section` 追加确定性段落。
        "organization": _org_facts(plan, view),
        # 原始契约字段也照抄一份（模型可直接读，不做二次加工）
        "organization_gaps": meta.get("organization_gaps"),
        "scope_audit": meta.get("scope_audit"),
        # 任务级「依据 / 资源」（依据列的数据源：`_unit_assumed` / `_norm_flagged`）
        "task_evidence": [
            {"task_id": str(t.get("task_id")),
             "task_name": t.get("task_name"),
             "evidence": _evidence_text(_rdf.get(str(t.get("task_id"))),
                                        _leaff.get(str(t.get("task_id"))))}
            for t in _tasks(plan) if isinstance(t, dict)
        ],
        "unit_assumed_tasks": [
            {"task_id": r["task_id"], "task_name": r["task_name"], "assumption": r["reason"],
             # 换算参数的**来源**（唯一真源 `unit_assumption_source`）：模型必须照抄，
             # 不许把定额条件档位推出的参数说成 AI 估算（虚假溯源）。
             "source": r.get("source"), "source_label": r.get("source_label"),
             "source_evidence": r.get("evidence")}
            for r in _norm_assumed_rows(plan, cap=0)[0]
        ],
        "norm_degradations": [
            {"task_id": r["task_id"], "task_name": r["task_name"], "reason": r["reason"]}
            for r in _norm_degradations(plan, cap=0)[0]
        ],
        # E2：施工段表 + 容量字典 + 取小/回分 + 「为什么是 N 人 / N 台」（照抄，不重算）
        "capacity_table": dict(
            _org_facts_capacity(plan),
            how_to_write=("有 `cap_rows` 时**必须**写出施工段与容量表（列照抄 cap_header）；"
                          "`seg_tables`/`basis` 逐条照抄，**不许重算、不许改数**；"
                          "取不到（present=false）→ 这一节一个字都不要写。")),
        # 容量口径两态（域 1.6 收敛）：模型必须按 `capacity_source` 如实分流，不许自己编。
        "capacity_caliber": dict(
            capacity_caliber_model(plan, cap=20),
            how_to_write=("`missing_count > 0` 时**必须**原样写出「工期不随工程量变化」那一句"
                          "和涉及的条数/任务号（别照抄 capacity_basis 长文）；"
                          "两项都为 0（全 mwi）→ 这一节一个字都不要写。")),
        # 域 8.1：AI 补的资源限额被丢弃的披露
        "ignored_model_limits": {
            "items": list(meta.get("_ignored_model_limits") or []),
            "how_to_write": (
                "items 非空时**必须**写一行「以下资源限额由模型按常见做法补齐，"
                "非用户输入，未作为限额使用：X、Y、Z」（照抄 items 里的资源名）；"
                "items 为空 → 这一行一个字都不要写。"),
        },
        # ---- 域 8.8①：未人工核验的 L4 条数 ----
        "l4_review_notice": {
            "text": _l4_review_notice_text(),
            "how_to_write": (
                "text 非空时**必须**原样写出（照抄，不许改数字）；"
                "text 为空 → 这一行一个字都不要写。"),
        },
        # ---- 域 8.3：日级资源账单 ----
        # 7 项：①每道 L4 的资源量 ②每天每种资源需求量 ③用户限额线 ④超限日高亮
        # ⑤分配明细 ⑥迭代轮数 ⑦收敛状态
        "daily_resource_bill": _daily_resource_bill_model(plan),
        "norm_caliber_note": (
            "「已按写明换算参数换算」的任务与「单位与定额降级清单（仅参考）」**互斥**："
            "前者已按写明来源的换算参数算出班组与工日，不得再列进降级清单。"),
        # 政策变更（用户 2026-09-20）：AI 经验估算定额**已照用**。模型编排看板时必须在
        # 正文里说清这件事，否则它会复述已废除的旧口径（"AI 估算只作参考"= 假话）。
        # 条数与逐条标注都不许模型自己编：条数由 `_ai_norm_counts` 给，逐条标注由
        # `task_evidence` 的 `_evidence_text` 给（确定性渲染，模型照抄即可）。
        "ai_norm_policy": {
            "label": AI_NORM_LABEL,
            "state": AI_NORM_STATE,
            "count": (_ai_norm_counts(plan, (meta.get("norm_coverage")
                                             if isinstance(meta.get("norm_coverage"), dict)
                                             else {})) or [None])[0],
            "how_to_write": (
                "AI 经验估算定额（KB AI_ESTIMATE_V1，无规范依据）自 2026-09-20 起与真人定额"
                "同等参与工期与班组计算；正文里**不许**再写「AI 估算只作参考 / 不参与计算」"
                "（已废除的旧口径）。条数只能照抄本字段的 count（None = 数不出来 → 一个字"
                "都不要写，不许写 0）；逐条标注照抄 task_evidence 里的依据文案。"),
        },
        "report": _report_text(plan),
        # 图表交给**确定性注入**，不交给 LLM 画：LLM 只负责版面文案，图表由
        # echarts_page.chart_cards_html 生成后在 _inject_charts 里塞进占位符。
        # 这样 LLM 无法把图表画错，也避免了 1MB 的 ECharts 源码进 prompt。
        "charts": {
            "mode": "echarts" if _echarts_ok() else "svg",
            "placeholder": "<div id=\"dsh-charts\"></div>",
            "note": ("在希望放图表的位置原样输出 <div id=\"dsh-charts\"></div>，"
                     "不要自己写任何图表代码；系统会替换成横道图 + 分工种人员曲线 + 设备峰值三张交互图。"),
        },
        "svg_gantt": _svg_gantt(view),
        "svg_personnel": _svg_line_chart(
            [{"x": x["day"], "y": x["total"]} for x in lab], y_label="人 总数"),
        "svg_equipment": _svg_bars(
            sorted(rp.get("equipment_peak", {}).items(), key=lambda x: -x[1])[:12]),
    }
    return facts


def _inject_charts(html, plan, view):
    """把 ECharts bundle 与图表区注入 LLM 产出的 HTML。

    必须在 `_sanitize_html` **之后**调用 —— sanitize 会把内联 <script> 一并剥光
    （L702 的正则不要求 src= 属性），所以只能在它之后再插。
    顺序讲究：bundle 必须进 <head>，早于图表区里那段 glue <script>。
    所有 re.sub 的替换值都走 lambda，避免图表 HTML 里的反斜杠被当成转义序列。
    """
    if not _echarts_ok():
        return html
    try:
        bundle = echarts_page.echarts_bundle_html()
        cards = echarts_page.chart_cards_html(plan, view)
    except Exception:
        return html
    if not bundle or not cards:
        return html
    ph = re.search(r"<div[^>]*id=(['\"])dsh-charts\1[^>]*>\s*</div>", html, re.I)
    if ph:
        html = html[:ph.start()] + cards + html[ph.end():]
    elif re.search(r"</body>", html, re.I):
        html = re.sub(r"</body>", lambda m: cards + m.group(0), html, count=1, flags=re.I)
    else:
        html += cards
    if re.search(r"</head>", html, re.I):
        html = re.sub(r"</head>", lambda m: bundle + m.group(0), html, count=1, flags=re.I)
    else:
        html = bundle + html
    return html


def _sanitize_html(text):
    """只保留 HTML 文档；剥离外链脚本/图片/iframe 等，保证自包含离线可开。"""
    text = (text or "").strip()
    if not text:
        return ""
    m = re.search(r"<(?:!DOCTYPE[^>]*>|html[^>]*>).*?</html>", text, re.I | re.S)
    if m:
        text = m.group(0)
    else:
        text = f"<!DOCTYPE html><html lang='zh'><meta charset='utf-8'><body>{text}</body></html>"
    text = re.sub(r"<script[^>]*src=[^>]*>.*?</script>", "", text, flags=re.I | re.S)
    text = re.sub(r"<link[^>]*href=(['\"])?https?:", r"<link \1data-disabled-", text, flags=re.I)
    text = re.sub(r"<(script|iframe|object|embed|\s+style[^>]*src)[^>]*>.*?</\1>", "", text,
                  flags=re.I | re.S)
    text = re.sub(r"<img[^>]*src=(['\"])?https?:", r"<span \1", text, flags=re.I)
    return text[:350000]          # 上限防异常，避免撑爆


def build_plan_html_agent(plan, llm, ctx=None):
    """LLM 编排网页：注入确定性 facts（含 SVG）+ 项目资料摘要，LLM 写 HTML；失败回退确定性 build_plan_html。

    返回 (html 路径, 是否为 LLM 编排)。
    """
    view = _compute_view(plan)
    facts = _facts_bundle(plan, view)
    out = _plan_dir(plan) / "计划看板.html"
    try:
        text = llm.chat_text(load("deliver_html.txt"),
                             combine(ctx, json.dumps(facts, ensure_ascii=False, indent=2)),
                             temperature=0.3)
        html = _sanitize_html(text)
        html = _inject_charts(html, plan, view)     # 必须在 sanitize 之后
        if "</html>" in html.lower() and "<" in html:
            # ③-b 结构性保证：模型可以整段不写「依据 / 资源」「工作面容量口径」
            # 「主要机械峰值」（实测就是 0 次），提示词只是"希望"，这里做的是"保证"。
            merged, mode = _ensure_delivery_markers(html, plan, view)
            if mode == "fallback":
                # 连追加都做不到 → 整体回退确定性渲染（它一定带全部标记）
                if isinstance(ctx, dict):
                    ctx.setdefault("wbs_warnings", []).append(
                        "HTML 编排缺关键标记且追加失败，已回退确定性模板")
                return build_plan_html(plan), False
            if mode == "agent+appendix" and isinstance(ctx, dict):
                ctx.setdefault("wbs_warnings", []).append(
                    "HTML 编排缺关键标记（依据 / 工作面容量 / 主要机械峰值），"
                    "已追加确定性口径段")
            # 施工组织层口径的结构性保证（独立判据：见 `_ensure_org_section`）
            merged, _org_added = _ensure_org_section(merged, plan, view)
            if _org_added and isinstance(ctx, dict):
                ctx.setdefault("wbs_warnings", []).append(
                    "HTML 编排缺施工组织口径（作业面数 / 组织缺口），已追加确定性组织层段落")
            # 【第 2 批 · 域 2 / 2.7】材料计划声明的结构性保证（独立判据：见
            # `_ensure_materials_notice`）—— 模型不可能猜到"材料清单已删除"，
            # 少了这句，用户会以为"我申报的材料被漏了"。
            merged, _mat_added = _ensure_materials_notice(merged)
            if _mat_added and isinstance(ctx, dict):
                ctx.setdefault("wbs_warnings", []).append(
                    "HTML 编排缺「本计划不含材料计划」声明，已追加确定性段落")
            # 「数据来源与置信度」的结构性保证（独立判据：见 `_ensure_confidence_section`）
            merged, _conf_added = _ensure_confidence_section(merged, plan, view)
            if _conf_added and isinstance(ctx, dict):
                ctx.setdefault("wbs_warnings", []).append(
                    "HTML 编排缺数据来源与置信度口径（数据来源与置信度 / 定额覆盖率），"
                    "已追加确定性置信度段落")
            # D4 无定额依据的结构性保证（独立判据：计划确有 `_norm_applied` 为空的工序）。
            # 真实缺陷：这些行的工期/人数来自模型，产物上却什么都没有 —— 用户读成"编的"。
            merged, _nm_added = _ensure_norm_missing_section(merged, plan)
            if _nm_added and isinstance(ctx, dict):
                ctx.setdefault("wbs_warnings", []).append(
                    "HTML 编排缺「无定额依据工序」（本次运行确有这类工序），已追加确定性段落")
            # D6 来源档次 / 覆盖率 + 5d 口径换算留痕 + D7 人工覆盖留痕的结构性保证
            # （独立判据：见 `_norm_tier_markers_missing`，逐项判"计划有没有这份数据"）。
            merged, _tier_added = _ensure_norm_tier_section(merged, plan, view)
            if _tier_added and isinstance(ctx, dict):
                ctx.setdefault("wbs_warnings", []).append(
                    "HTML 编排缺定额来源档次 / 关键路径规范依据覆盖率 / 口径换算或人工覆盖留痕，"
                    "已追加确定性段落")
            # 设备清单三态的结构性保证（独立判据：见 `_ensure_equipment_section`）——
            # 真实事故：模型把"本次没取得设备清单"写成了"用户没申报设备限额"。
            merged, _eq_added = _ensure_equipment_section(merged, plan)
            if _eq_added and isinstance(ctx, dict):
                ctx.setdefault("wbs_warnings", []).append(
                    "HTML 编排的设备清单口径与计划数据不符（清单为空被写成用户未申报，"
                    "或模型补的估算没标明非限额），已追加确定性设备清单段落")
            # 节点级告警的结构性保证（有告警而页面没写 → 追加；无告警 → 一个字不加）
            merged, _nw_added = _ensure_node_warnings_section(merged, plan)
            if _nw_added and isinstance(ctx, dict):
                ctx.setdefault("wbs_warnings", []).append(
                    "HTML 编排缺节点级告警（本次运行确有告警），已追加确定性告警段落")
            # 用量口径段：模型不可能写出"运行末尾"的数（那是它这次调用之后才产生的）
            merged, _us_added = _ensure_usage_section(merged, plan)
            if _us_added and isinstance(ctx, dict):
                ctx.setdefault("wbs_warnings", []).append(
                    "HTML 编排页已由系统追加确定性用量段落（运行末尾快照）")
            # 关键路径口径的结构性保证（独立判据：见 `_fix_critical_path_narrative`）——
            # 真实事故：模型正文写「关键路径长度为81天」，而 81 是**任务条数**不是天数。
            merged, _cp_fixes = _fix_critical_path_narrative(merged, plan)
            if _cp_fixes and isinstance(ctx, dict):
                ctx.setdefault("wbs_warnings", []).append(
                    "HTML 编排把关键路径**条数**写成了天数（%d 处），已按计划真值改写：%s"
                    % (len(_cp_fixes), "；".join(_cp_fixes[:3])))
            # G5：交付物单位清零（归一 + 留痕，见本文件顶部 G5 说明）
            merged = _normalize_deliverable_u33a1(merged, "计划看板.html（LLM 编排路径）")
            _report_no_cjk_compat_square_metre(merged, "计划看板.html（LLM 编排路径）")
            out.write_text(merged, encoding="utf-8")
            return str(out), True
    except Exception:
        pass
    return build_plan_html(plan), False        # 确定性兜底


_HTML_CSS = """
*{box-sizing:border-box} body{font-family:"Microsoft YaHei","PingFang SC",system-ui;margin:0;background:#eef1f6;color:#1f2530}
.wrap{max-width:1120px;margin:0 auto;padding:22px}
.card{background:#fff;border-radius:12px;padding:18px 22px;margin-bottom:18px;box-shadow:0 1px 4px rgba(0,0,0,.08)}
h1{font-size:22px;margin:0 0 4px} h2{font-size:15px;margin:6px 0 12px;padding-bottom:6px;border-bottom:2px solid #4a90d9;color:#234}
.kv{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:10px}
.kv div{background:#f7f9fc;border:1px solid #e6ebf2;border-radius:8px;padding:8px 10px}
/* 修复「计划概要字体错位、比方框长」：19px 的 nowrap 长值（如 2026-06-01 → 2028-09-25）
   会撑破 150px 卡片。去掉 nowrap 并允许任意位置断行，格子下限同步提到 180px。 */
.kv b{display:block;font-size:19px;color:#16325c;overflow-wrap:anywhere;line-height:1.3}.kv span{font-size:12px;color:#6b7688}
table{border-collapse:collapse;width:100%;font-size:12.5px} th,td{border:1px solid #e2e8f0;padding:5px 7px;text-align:left}
th{background:#4a90d9;color:#fff} tr:nth-child(even)td{background:#f6f9fd}
.leg span{display:inline-block;margin-right:14px;font-size:12px;white-space:nowrap}
.leg .k{width:12px;height:12px;display:inline-block;margin-right:4px;border-radius:2px;vertical-align:-1px}
pre{background:#f8fafc;color:#2a3441;border:1px solid #e2e8f0;padding:12px;border-radius:6px;font-size:12px;overflow:auto;white-space:pre-wrap;word-break:break-all}
.mil li{font-size:13px;margin:4px 0}
.chart-lbl{font-size:11px;color:#6b7688;margin-bottom:6px}
/* ECharts 图表容器与阶段筛选按钮 */
.dsh-chart{width:100%}
.dsh-btns{margin:8px 0 10px}
.dsh-btns button{font:inherit;font-size:12px;padding:4px 12px;margin-right:6px;border:1px solid #cfd9e6;
  background:#fff;color:#33415c;border-radius:14px;cursor:pointer}
.dsh-btns button.active{background:#4a90d9;border-color:#4a90d9;color:#fff}
details{margin-top:10px} details summary{cursor:pointer;font-size:12px;color:#6b7688}
.rs-row{font-size:13px;line-height:2}
/* 施工监督报告按 markdown 渲染（像 VS Code 预览那样可读），不再是裸文本 pre */
.md-body{font-size:13px;line-height:1.75;color:#2a3441}
.md-body h3,.md-body h4,.md-body h5,.md-body h6{margin:16px 0 8px;color:#16325c;font-weight:600}
.md-body h3{font-size:16px;padding-left:9px;border-left:4px solid #4a90d9}
.md-body h4{font-size:14px;padding-left:8px;border-left:3px solid #9dc0e8}
.md-body h5{font-size:13px;color:#3a4a63}
.md-body p{margin:6px 0}
.md-body ul,.md-body ol{margin:6px 0;padding-left:24px}
.md-body li{margin:3px 0}
.md-body table{width:auto;min-width:52%;margin:10px 0;font-size:12.5px}
.md-body th{background:#f2f6fc;color:#16325c;font-weight:600;border:1px solid #dbe3ef;padding:5px 12px}
.md-body td{border:1px solid #dbe3ef;padding:5px 12px}
.md-body tr:nth-child(even) td{background:#fafcff}
.md-body strong{color:#16325c}
.md-body code{background:#f2f5f9;border:1px solid #e2e8f0;border-radius:3px;padding:0 4px;font-size:12px}
"""


def _svg_line_chart(series, width=1040, height=220, color="#4a90d9", y_label="人", color_fn=None,
                    start_date=None):
    """折线图：series=[{x, y, label:...}]。返回 SVG 字符串。

    `start_date` 非空时横轴显示**日历日期**（用户提供了开工日期就该用），否则退回相对天数。
    """
    if not series:
        return "<p class='chart-lbl'>（无数据）</p>"
    xs = [s["x"] for s in series]
    ys = [float(s["y"]) for s in series]
    xmax = max(1, max(xs) - min(xs))
    ymax = max(1, max(ys))
    pad_l, pad_r, pad_t, pad_b, plot_w, plot_h = 52, 18, 18, 30, width - 70, height - 48
    def px(x): return pad_l + (x - min(xs)) / xmax * plot_w
    def py(y): return pad_t + (1 - y / ymax) * plot_h
    pts = " ".join(f"{px(s['x']):.0f},{py(s['y']):.0f}" for s in series)
    bars = ""
    area = f"{px(series[0]['x']):.0f},{py(0)} {pts} {px(series[-1]['x']):.0f},{py(0)}"
    # 网格线
    grid = ""
    for i in range(5):
        gy = pad_t + i * plot_h / 4
        grid += f'<line x1="{pad_l}" y1="{gy:.0f}" x2="{pad_l+plot_w}" y2="{gy:.0f}" stroke="#edf0f5" stroke-width="1"/>'
        grid += f'<text x="{pad_l-6}" y="{gy+3:.0f}" font-size="10" fill="#9aa4b5" text-anchor="end">{ymax*(4-i)/4:.0f}</text>'
    xstep = max(1, xmax // 8)
    _fmt = "%y-%m" if xmax > 365 else "%m-%d"
    for x in range(int(min(xs)), int(max(xs)) + 1, xstep):
        lbl = str(x)
        if start_date is not None:
            try:
                lbl = (start_date + datetime.timedelta(days=int(x))).strftime(_fmt)
            except Exception:
                lbl = str(x)
        grid += f'<text x="{px(x):.0f}" y="{height-8}" font-size="10" fill="#9aa4b5" text-anchor="middle">{lbl}</text>'
    x_cap = "日历工期" if start_date is not None else "相对天数"
    return f"""<svg viewBox="0 0 {width} {height}" width="100%" height="{height}">
<line x1="{pad_l}" y1="{pad_t}" x2="{pad_l}" y2="{pad_t+plot_h}" stroke="#ccd4e0"/>
<line x1="{pad_l}" y1="{pad_t+plot_h}" x2="{pad_l+plot_w}" y2="{pad_t+plot_h}" stroke="#ccd4e0"/>
{grid}
<text x="{pad_l}" y="{pad_t-3}" font-size="11" fill="#6b7688">{y_label}</text>
<text x="{pad_l+plot_w}" y="{height-8}" font-size="11" fill="#6b7688" text-anchor="end">{x_cap}</text>
<polygon points="{area}" fill="{color}" opacity="0.12"/>
<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2.2" stroke-linejoin="round"/>
{''.join(f'<circle cx="{px(s["x"]):.0f}" cy="{py(s["y"]):.0f}" r="{2.4 if i%max(1,len(series)//30)==0 else 0}" fill="{color}"/>' for i,s in enumerate(series))}
</svg>"""


# 多折线配色（与 ECharts 默认主题同源的近似色，保证两条路径观感一致）
_TRADE_COLORS = ["#5470c6", "#91cc75", "#fac858", "#ee6666", "#73c0de",
                 "#3ba272", "#fc8452", "#9a60b4", "#ea7ccc", "#48b3bd"]


def _svg_multi_line(groups, width=1040, height=260, y_label="人", start_date=None):
    """多折线（分工种人员曲线）—— ECharts 不可用时的兜底，避免回退后丢失「分工种」能力。

    groups: [(名称, [(day, y), ...]), ...]，按峰值降序传入，最多画 10 条。
    """
    groups = [(n, s) for n, s in (groups or []) if s][:10]
    if not groups:
        return "<p class='chart-lbl'>（无数据）</p>"
    all_x = [x for _, s in groups for x, _ in s]
    xmin, xmax = min(all_x), max(all_x)
    span = max(1, xmax - xmin)
    ymax = max(1, max(y for _, s in groups for _, y in s))
    pad_l, pad_r, pad_t, plot_w, plot_h = 52, 18, 18, width - 70, height - 78
    def px(x): return pad_l + (x - xmin) / span * plot_w
    def py(y): return pad_t + (1 - y / ymax) * plot_h
    grid = ""
    for i in range(5):
        gy = pad_t + i * plot_h / 4
        grid += f'<line x1="{pad_l}" y1="{gy:.0f}" x2="{pad_l+plot_w}" y2="{gy:.0f}" stroke="#edf0f5"/>'
        grid += f'<text x="{pad_l-6}" y="{gy+3:.0f}" font-size="10" fill="#9aa4b5" text-anchor="end">{ymax*(4-i)/4:.0f}</text>'
    xstep = max(1, span // 8)
    _fmt = "%y-%m" if span > 365 else "%m-%d"
    for x in range(int(xmin), int(xmax) + 1, xstep):
        lbl = str(x)
        if start_date is not None:
            try:
                lbl = (start_date + datetime.timedelta(days=int(x))).strftime(_fmt)
            except Exception:
                lbl = str(x)
        grid += f'<text x="{px(x):.0f}" y="{pad_t+plot_h+14}" font-size="10" fill="#9aa4b5" text-anchor="middle">{lbl}</text>'
    lines = ""
    for i, (nm, s) in enumerate(groups):
        c = _TRADE_COLORS[i % len(_TRADE_COLORS)]
        pts = " ".join(f"{px(x):.0f},{py(y):.0f}" for x, y in s)
        lines += f'<polyline points="{pts}" fill="none" stroke="{c}" stroke-width="1.8" stroke-linejoin="round"/>'
    leg = "".join(
        f"<span><span class='k' style='background:{_TRADE_COLORS[i % len(_TRADE_COLORS)]}'></span>"
        f"{_html.escape(str(nm))}</span>" for i, (nm, _) in enumerate(groups))
    return (f'<div class="leg" style="margin-bottom:6px">{leg}</div>'
            f"""<svg viewBox="0 0 {width} {height}" width="100%" height="{height}">
<line x1="{pad_l}" y1="{pad_t}" x2="{pad_l}" y2="{pad_t+plot_h}" stroke="#ccd4e0"/>
<line x1="{pad_l}" y1="{pad_t+plot_h}" x2="{pad_l+plot_w}" y2="{pad_t+plot_h}" stroke="#ccd4e0"/>
{grid}
<text x="{pad_l}" y="{pad_t-3}" font-size="11" fill="#6b7688">{y_label}</text>
{lines}
</svg>""")


def _svg_gantt(view, width=1040, start_date=None):
    """甘特：每任务一行 SVG 条，横轴=日历工期；关键路径红色。

    ⚠️ 这是 **ECharts 不可用时的兜底路径**（echarts_page 缺失 / vendor 文件被裁掉）。
    历史上这里有三个缺陷，用户都已明确指出，兜底路径必须一并修掉，否则两条路径表现不一致：

      ① 工期文字画在 x=label_w+4，而条形 rect 的 x 起点也是 label_w（start_day=0 时），
         且 **rect 在 text 之后写入 → 后绘制的矩形盖住文字**。短工序条宽只有 3~6px、
         比文字还窄，于是只看得见 "→12"。
         → 修法：工期文字移到**左栏内**（x=label_w-6，右对齐），与条形物理隔离，永不重叠。
      ② `str(name)[:20]` 硬截断且无省略号 → 「…（1 项」的右括号 `）` 被吃掉。
         → 修法：按 label_w 反算可容纳字数，超长补 `…`。
      ③ 横轴写相对天数，用户明明提供了开工日期却不用。
         → 修法：`start_date` 非空时显示日历日期（跨年显示 年-月，否则 月-日）。

    `label_w` 从 230 提到 300 是为了容纳新增的**工序编号**前缀。
    """
    g = view["gantt"]
    if not g:
        return "<p class='chart-lbl'>（无排程数据）</p>"
    total = max(view["total_days"], 1)
    label_w, row_h, pad = 300, 20, 6
    dur_w = 54                     # 左栏右侧给"起→止"工期文字预留的宽度
    H = pad * 2 + len(g) * (row_h + 2) + 26
    plot_w = width - label_w - 40
    # 名称可用字数：11px 字号下汉字宽约 11px，再留 8px 余量，避免与工期文字相撞
    max_chars = max(8, int((label_w - dur_w - 8) / 11))

    def _fit(s):
        s = _html.escape(str(s))
        return s if len(s) <= max_chars else s[:max_chars - 1] + "…"

    ents = []
    # 横轴
    ent = f"<rect x='{label_w}' y='{pad+2}' width='{plot_w}' height='{13}' fill='#f2f6fb'/>"
    ents.append(ent)
    xstep = max(1, total // 12)
    _fmt = "%y-%m" if total > 365 else "%m-%d"
    for d in range(0, total + 1, xstep):
        x = label_w + d / total * plot_w
        if start_date is not None:
            try:
                lbl = (start_date + datetime.timedelta(days=d)).strftime(_fmt)
            except Exception:
                lbl = str(d)
        else:
            lbl = str(d)
        ents.append(f"<text x='{x:.0f}' y='{pad+6}' font-size='9' fill='#6b7688' text-anchor='middle'>{lbl}</text>")
    idx = 0
    for t in g:
        y = pad + 22 + idx * (row_h + 2)
        x = label_w + t["start_day"] / total * plot_w
        w = max(3, (max(t["end_day"], t["start_day"] + 1) - t["start_day"]) / total * plot_w)
        color = "#e0524d" if t["crit"] else "#4a90d9"
        # 工序编号 + 名称（同左栏，按可用宽度截断并补省略号）
        tid = str(t.get("id") or "").strip()
        name = _fit(f"{tid} {t['name']}" if tid else str(t["name"]))
        ents.append(f"<clipPath id='c{idx}'><rect x='{label_w}' y='{y-11}' width='{plot_w}' height='{row_h+2}'/></clipPath>")
        ents.append(f"<text x='0' y='{y}' font-size='11' fill='#334'>{name}</text>")
        # 工期文字：左栏内右对齐 —— 与条形物理隔离（修复重叠）
        ents.append(f"<text x='{label_w-6}' y='{y}' font-size='10' fill='#6b7688' "
                    f"text-anchor='end'>{t['start_day']}→{t['end_day']}</text>")
        ents.append(f"<rect x='{x:.0f}' y='{y-9}' width='{w:.0f}' height='{12}' rx='2' fill='{color}' clip-path='url(#c{idx})'/>")
        idx += 1
    return f"""<svg viewBox="0 0 {width} {pad*2 + idx*(row_h+2) + 26}" width="100%" height="{pad*2 + idx*(row_h+2) + 26}">{''.join(ents)}</svg>"""


def _svg_bars(items, width=1040, color="#e8963c"):
    """横向条图：设备/工种峰值。items=[(name, value)]。"""
    if not items:
        return "<p class='chart-lbl'>（无数据）</p>"
    vmax = max(v for _, v in items) or 1
    row_h, label_w = 22, 200
    H = 30 + len(items) * row_h
    ents = []
    for i, (name, v) in enumerate(items):
        y = 24 + i * row_h
        w = max(4, v / vmax * (width - label_w - 70))
        ents.append(f"<text x='0' y='{y+10}' font-size='12' fill='#334'>{_html.escape(str(name))}</text>")
        ents.append(f"<rect x='{label_w}' y='{y}' width='{w:.0f}' height='{14}' rx='2' fill='{color}'/>")
        ents.append(f"<text x='{label_w+w+6:.0f}' y='{y+11}' font-size='11' fill='#6b7688'>{v}</text>")
    return f"""<svg viewBox="0 0 {width} {H}" width="100%" height="{H}">{''.join(ents)}</svg>"""


def _trade_totals(view, extra_demand=None, top=16):
    """按资源名汇总：峰值人数 + 总工日。

    数据源就是 `_compute_view` 的 `labor_daily[].trades`（分工种明细一直都有，过去只是没画出来）。
    这张表直接回应用户的质问——「我怎么没看到最重要的混凝土工、钢筋工这些工种出现在资源计划中」。
    `trades` 里同时含**工种**与**机械配员**（泵工/辅助/操作工/司机），用 kind 列区分，不再混为一谈。

    `extra_demand`（= `resource_plan["labor_demand"]`，来自 `meta.machine_labor_demand`）是
    **台班定额反算**的工种人工需求：有总量、但**没有逐日分布**，所以进不了人员曲线，只能进这张表。
    这类行 peak 返回 None（表格渲染成「—」），并标成「台班定额」，避免与「按每日在场人数叠加」
    的峰值混为一谈 —— 例如混凝土工 2633.6 是**工日总量**，不是某一天的人头数。
    """
    peak, total = {}, {}
    for x in view.get("labor_daily") or []:
        for k, v in (x.get("trades") or {}).items():
            total[k] = total.get(k, 0) + v
            if v > peak.get(k, 0):
                peak[k] = v
    rows = [(k, peak.get(k, 0), v, "机械配员" if k in MACHINE_CREW else "工种")
            for k, v in total.items()]
    for k, v in (extra_demand or {}).items():
        if k not in total:
            try:
                rows.append((k, None, float(v), "台班定额"))
            except (TypeError, ValueError):
                continue
    # 有人头峰值的排前面，其余按工日总量降序
    rows.sort(key=lambda r: (-(r[1] or 0), -r[2]))
    return rows[:top]


def _svg_chart_cards(plan, view, start_date=None):
    """ECharts 不可用时的兜底图表区：甘特 + 分工种人员曲线 + 设备峰值。

    与 ECharts 路径保持同样的三块内容，避免 vendor 缺失时静默降级成"少了两张图"。
    """
    rp = plan.get("resource_plan") or {}
    lab = view.get("labor_daily") or []
    peak_day = max(range(len(lab)), key=lambda i: lab[i]["total"]) if lab else 0
    step = max(1, len(lab) // 400)
    groups = []
    for name in {k for x in lab for k in (x.get("trades") or {})}:
        ser = [(x["day"], (x.get("trades") or {}).get(name, 0)) for i, x in enumerate(lab)
               if i % step == 0 or i == peak_day]
        if any(y for _, y in ser):
            groups.append((name, ser))
    groups.sort(key=lambda g: -max(y for _, y in g[1]))
    n_crit = sum(1 for g in view.get("gantt") or [] if g.get("crit"))
    equip = sorted((rp.get("equipment_peak") or {}).items(), key=lambda x: -x[1])[:12]
    _sd = (plan.get("overview") or {}).get("planned_start_date") or "相对天数"
    return (
        f"<div class='card'><h2>横道图（甘特 · ★=关键路径 {n_crit} 项）</h2>"
        f"<div class='leg'><span><span class='k' style='background:#4a90d9'></span>普通任务</span>"
        f"<span><span class='k' style='background:#e0524d'></span>关键路径</span></div>"
        f"<div class='chart-lbl'>横轴=日历工期（{_html.escape(str(_sd))} 起）</div>"
        f"{_svg_gantt(view, start_date=start_date)}</div>"
        f"<div class='card'><h2>主要工种人员配置曲线</h2><div class='chart-lbl'>峰值 "
        f"{view['peak_total']} 人 · 峰值工种 {view['peak_trade'] or '—'}</div>"
        f"{_svg_multi_line(groups, start_date=start_date)}</div>"
        f"<div class='card'><h2>设备峰值需求统计</h2>"
        f"<div class='chart-lbl'>仅机械（机械配员已归入人工）</div>"
        f"{_svg_bars(equip, color='#7a6bd4')}</div>"
    )


# 【第 2 批 · 域 2 / 2.6】材料名中文化（`_MATERIAL_CN`）与「材料汇总 → 一行人话」的
# `_material_text()` **已整体删除**：系统不再展示材料清单，看板上不再有「主要材料」一行
# （原 `<div class='rs-row'><b>主要材料：…` 已随之一并删除）。
# 交付物改为一句显式声明 —— 见下面的 `MATERIALS_EXCLUDED_NOTICE`。
# ⚠️ 删掉的只有这一处**展示**逻辑；`material_transport`（材料运输**工序**）与
# `_materialize_unit_assumption`（"落实假设值"）与材料清单无关，**都没动**。
#
# 单位归一（`_normalized_unit_text`）本身**保留**：它不只服务 material_summary，
# 还被「施工组织」一节的多处（segment_rule_note / basis_lines / allocation_steps /
# mwi_unit）使用，删掉会把那些地方的单位残留 U+33A1 放出去（G5 断言会红）。
def _normalized_unit_text(unit):
    """单位串里**只**把 CJK 兼容方块字 U+33A1 归一为 `m²`（G5）。

    刻意**不做全量 `kb_units.normalize_unit`**：那会把既有的 `吨` 改成 `t`，
    属用户可见写法漂移，不是 G5 的要求（G5 只清零 U+33A1 这一个字符）。
    """
    from . import plan_assembler as _pa
    if not unit or _pa.CJK_COMPAT_SQUARE_METRE not in str(unit):
        return unit
    return _pa.normalize_cjk_compat_square_metre_in_text(str(unit))[0]


_RE_ZONE_PREFIX = re.compile(r"^(Ⅰ区|Ⅱ区|Ⅲ区|Ⅳ区|Ⅴ区|第[一二三四五六七八九十]+区)\s+")


def _strip_single_zone_prefix(plan):
    """展示层兜底：全计划只出现**一个**分区前缀时，把它从名字里剥掉。

    为什么需要：`layer_engine` 已保证**新生成**的计划在单区时不带「Ⅰ区」，但**旧计划 JSON**
    里的名字是生成时就写死的，复看时仍会满屏「Ⅰ区 1-0.5层 钢筋绑扎」。这正是用户问的
    「既然只有一个区，为什么还要叫一区？」。

    保守规则：只有**所有**带区前缀的名字都指向同一个区时才剥；出现 ≥2 个不同前缀
    （真·多区工程）就原样返回 —— 否则会把区分栋号/分区的关键信息抹掉。

    只动展示用的名字字段，不碰 id / 日期 / 资源，也不回写磁盘上的 plan_json。
    """
    sched = plan.get("all_tasks_schedule") or []
    names = [str(t.get("task_name") or "") for t in sched if isinstance(t, dict)]
    for ph in (plan.get("wbs") or {}).get("phases") or []:
        for wp in ph.get("work_packages") or []:
            names.append(str(wp.get("name") or ""))
            for sub in wp.get("sub_packages") or []:
                names.append(str(sub.get("name") or ""))
    for m in plan.get("key_milestones") or []:
        if isinstance(m, dict):
            names.append(str(m.get("name") or ""))
    prefixes = {mm.group(0) for n in names if (mm := _RE_ZONE_PREFIX.match(n))}
    if len(prefixes) != 1:
        return plan                      # 0 个（本来就干净）或 ≥2 个（真多区）都不动
    prefix = prefixes.pop()

    def _cut(s):
        s = str(s) if s is not None else ""
        return s[len(prefix):] if s.startswith(prefix) else s

    def _cut_obj(o, key):
        return {**o, key: _cut(o.get(key))} if isinstance(o, dict) and o.get(key) else o

    def _cut_leaf(s):
        """叶子同时带 name 与 location，两个都要去前缀（Word/WBS 表都会读到）。"""
        if not isinstance(s, dict):
            return s
        out_leaf = dict(s)
        for k in ("name", "location"):
            if out_leaf.get(k):
                out_leaf[k] = _cut(out_leaf[k])
        return out_leaf

    out = dict(plan)
    out["all_tasks_schedule"] = [_cut_obj(t, "task_name") for t in sched]
    wbs = dict(plan.get("wbs") or {})
    wbs["phases"] = [
        {**ph, "work_packages": [
            {**wp, "name": _cut(wp.get("name")),
             "sub_packages": [_cut_leaf(s) for s in (wp.get("sub_packages") or [])]}
            for wp in (ph.get("work_packages") or [])]}
        for ph in (wbs.get("phases") or [])
    ]
    out["wbs"] = wbs
    out["key_milestones"] = [_cut_obj(m, "name") for m in (plan.get("key_milestones") or [])]
    cpt = plan.get("critical_path_tasks")
    if isinstance(cpt, list):
        out["critical_path_tasks"] = [
            x if isinstance(x, (int, float)) else
            (_cut(x) if isinstance(x, str) else _cut_obj(x, "task_name"))
            for x in cpt
        ]
    rd = plan.get("resource_demand")
    if isinstance(rd, dict) and rd.get("tasks"):
        out["resource_demand"] = {**rd,
                                  "tasks": [_cut_obj(t, "task_name") for t in rd["tasks"]]}
    # 监督报告是自由文本，里面同样嵌着任务名（「**Ⅰ区 1-0.5层 钢筋绑扎** (4.1.1.1, 2天)」），
    # 逐个字段剥不到，整篇替换掉这个前缀即可 —— 单区工程里它不携带任何信息。
    if out.get("report"):
        out["report"] = str(plan["report"]).replace(prefix, "")
    return out


def _md_inline(text):
    """行内 markdown：**粗体** / `代码` / *斜体*。入参必须**已经是转义过的**文本。"""
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", text)
    text = re.sub(r"(?<![*\w])\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", text)
    return text


def _md_to_html(text):
    """把施工监督报告的 markdown 渲染成 HTML —— 像 VS Code 的 md 预览那样可读。

    为什么不用第三方库：交付包要求单文件、离线、零新增依赖；而这份报告是我们自己按固定
    模板生成的，语法子集很小（# 标题 / * 无序表 / 1. 有序表 / | 表格 / **粗体**）。

    顺序很关键：**先 _html.escape 再套标签**，否则任务名里的 < & 会破坏结构。
    """
    src = _html.escape(str(text or ""))
    lines = src.split("\n")
    n, i, out = len(lines), 0, []
    while i < n:
        s = lines[i].strip()
        if not s:
            i += 1
            continue
        # 表格：本行以 | 开头且下一行是 | :--- | 分隔行
        if s.startswith("|") and i + 1 < n and re.match(r"^\|[\s:|-]+\|$", lines[i + 1].strip()):
            head = [c.strip() for c in s.strip("|").split("|")]
            i += 2
            rows = []
            while i < n and lines[i].strip().startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            th = "".join(f"<th>{_md_inline(c)}</th>" for c in head)
            tb = "".join("<tr>" + "".join(f"<td>{_md_inline(c)}</td>" for c in r) + "</tr>"
                         for r in rows)
            out.append("<table><thead><tr>" + th + "</tr></thead><tbody>" + tb + "</tbody></table>")
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", s)
        if m:
            # 卡片本身已有 <h2>施工监督报告</h2>，报告首行的「# 监督报告」是重复的，丢掉
            if not (len(m.group(1)) == 1 and "监督报告" in m.group(2)):
                lvl = min(len(m.group(1)) + 2, 6)     # 整体降 2 级，接到卡片 h2 之下
                body = _md_inline(m.group(2))
                out.append(f"<h{lvl}>{body}</h{lvl}>")
            i += 1
            continue
        if re.match(r"^[*+-]\s+", s):
            items = []
            while i < n and re.match(r"^[*+-]\s+", lines[i].strip()):
                items.append(re.sub(r"^[*+-]\s+", "", lines[i].strip()))
                i += 1
            out.append("<ul>" + "".join(f"<li>{_md_inline(x)}</li>" for x in items) + "</ul>")
            continue
        if re.match(r"^\d+[.)]\s+", s):
            items = []
            while i < n and re.match(r"^\d+[.)]\s+", lines[i].strip()):
                items.append(re.sub(r"^\d+[.)]\s+", "", lines[i].strip()))
                i += 1
            out.append("<ol>" + "".join(f"<li>{_md_inline(x)}</li>" for x in items) + "</ol>")
            continue
        out.append(f"<p>{_md_inline(s)}</p>")
        i += 1
    return "".join(out)


def _resource_plan_of(plan):
    """计划里的 `resource_plan`（并做单区前缀兜底，与 `build_plan_html` 同源）。"""
    plan = _strip_single_zone_prefix(plan)
    rp = plan.get("resource_plan")
    return rp if isinstance(rp, dict) else {}


# ============================================================
# 施工组织层口径（`_organization` / `meta.organization_gaps` / `meta.scope_audit`）
# ------------------------------------------------------------
# 用户最大的疑问是「306 工日 ÷ 9 人 = 34 天/层，凭什么」。定额、工日、人数、设备都印
# 出来了，**为什么是这个工期**却一个字没有。组织层给每条排程行挂 `_organization`
# （契约字段名照抄，不许改）：
#   {"cadence_days","n_faces","crew_per_face","crew_total","shifts",
#    "duration_days","feasible","t_min_days","source","person_days"}
# 本段把它翻成人话：主体节拍（**仅对比参考**，C11）/ 逐条工序「工日 · 作业面数 ·
# 每面人数 · 班次 → 工期」/ 组织缺口报告 / 审计提示（待人工确认）。
# 三条硬规矩：
#   ① 字段名照抄契约，缺字段一律写「来源未记录」，**绝不猜数**（没有的工序不编行）；
#   ② 审计类内容只写「提示与待审」，不下「系统已经知道错了」的结论；
#   ③ 同一份文档里口径不同的人数必须各自写明是哪一个（历史上踩过"两个峰值"的坑）。
#
# ⚠ 旧口径（效率折减 η / 有效班组 / `工期 = 工日 ÷ (作业面数 × 每面人数 × 班次 × 效率折减)`）
#   已按 C8① / 重构方案 §2 裁定 3 **整条删除**，交付物一个字都不再印 ——
#   新口径的展示文案属 E2/C11，**尚未落地**，这里绝不自己发明一句公式。
# ============================================================

ORG_UNRECORDED = "来源未记录"
ORG_NO_CADENCE = "未提供节拍，按推荐班组配置"
ORG_NO_GAP = "本次无组织缺口"
# `_organization.source` 的取值 → 人话（**字段名不许改**，只是翻译展示）
ORG_SOURCE_LABELS = {
    "cadence": "用户输入节拍",
    "preferred": "推荐班组配置",
    "legacy": "旧口径（无组织层）",
    "measure_item": "清单项工程量",
}
# 逐条工序表的列（HTML 与 Word 同一份表头，8 列）
# ⚠ 去掉旧口径的两列「效率折减 η」「有效班组(人)」（C8① 删除对象）。
ORG_TABLE_HEADER = ("工序 ID", "工序", "工日", "作业面数", "每面人数", "班次",
                    "工期(天)", "组织来源")
# 工日列的**唯一真源**：`_organization.person_days` = 该工序**工种自己**的工日
# （不含机械台日、不含机械配员）。混进配员会把工期算虚
# （实测 6.1.1.1：瓦工 270 + 司机 60 + 信号工 30 = 360 ≠ 工种工日 306）。
ORG_PERSON_DAYS_ORG = "组织层「_organization.person_days」（本工序工种工日，不含机械配员）"
ORG_PERSON_DAYS_FALLBACK = ("交付侧「resource_demand.resources 人工工种 total_days 之和」"
                            "（可能含机械配员，与组织层口径可能不同）")
# 节拍落点 = `meta.boundary_conditions.cadence_days`；来源读同容器 `_source.cadence_days`
ORG_CADENCE_DEFAULT_SCOPE = "标准层"
ORG_CADENCE_SOURCE_LABELS = {"user": "用户输入", "model": "模型估算，非用户输入"}
ORG_TITLE = "施工组织口径（工日 → 工期 是怎么来的）"
ORG_GAP_TITLE = "组织缺口报告"
ORG_AUDIT_TITLE = "审计提示（提示与待审，不是系统结论）"


def _org_dict(v):
    return v if isinstance(v, dict) else {}


def _org_row_dicts(plan):
    """计划里所有可能挂 `_organization` 的行容器（按优先级：资源行 → 排程行 → …）。"""
    rd = _org_dict(plan.get("resource_demand"))
    return [rd.get("tasks") or [],
            plan.get("all_tasks_schedule") or [],
            plan.get("critical_path_tasks") or [],
            plan.get("schedule") or []]


def _org_map(plan):
    """task_id → `_organization`（按容器优先级，第一个带该字段的行胜出）。"""
    out = {}
    for rows in _org_row_dicts(plan):
        for r in rows:
            if not isinstance(r, dict):
                continue
            org = r.get("_organization")
            if isinstance(org, dict) and org:
                out.setdefault(str(r.get("task_id")), org)
    return out


def _org_task_names(plan):
    """task_id → 任务名（排程行/资源行优先，WBS 叶子兜底）。"""
    out = {}
    for rows in _org_row_dicts(plan):
        for r in rows:
            if isinstance(r, dict) and r.get("task_id") is not None and r.get("task_name"):
                out.setdefault(str(r.get("task_id")), r.get("task_name"))
    for ph in (_org_dict(plan.get("wbs")).get("phases") or []):
        for wp in (_org_dict(ph).get("work_packages") or []):
            for sub in (_org_dict(wp).get("sub_packages") or []):
                if isinstance(sub, dict) and sub.get("id") is not None and sub.get("name"):
                    out.setdefault(str(sub.get("id")), sub.get("name"))
    return out


def _org_labor_person_days(plan):
    """task_id → 工日（**人工工种** total_days 之和）—— **兜底口径**。

    唯一真源是 `_organization.person_days`（工种的工日，不含机械配员，见 `_org_row`）；
    只有契约没给该字段时才用这里的累计，且渲染时必须标注「可能含机械配员，与组织层
    口径可能不同」（司机/信号工等配员在 `_is_labor` 里算人工，但会把工期算虚）。
    机械台班（塔吊/施工电梯…）一律不算工日。
    """
    out = {}
    for r in (_org_dict(plan.get("resource_demand")).get("tasks") or []):
        if not isinstance(r, dict):
            continue
        res = _org_dict(r.get("resources"))
        if not res:
            continue
        total = 0.0
        for name, v in res.items():
            if not _is_labor(name):
                continue
            try:
                total += float(_org_dict(v).get("total_days") or 0.0)
            except (TypeError, ValueError):
                continue
        out[str(r.get("task_id"))] = total
    return out


def _org_num_text(v, nd=2):
    """数值 → 显示文本；None / 空串 →「来源未记录」（缺字段不许猜）。"""
    if v is None:
        return ORG_UNRECORDED
    if isinstance(v, str):
        s = v.strip()
        return s or ORG_UNRECORDED
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    s = ("%." + str(int(nd)) + "f") % f
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def _org_evidence_text(v, cap=400):
    """`scope_audit` 的 evidence 原文 → 一行文本（dict/list 也照实带出来，不概括）。"""
    if v is None:
        return ORG_UNRECORDED
    if isinstance(v, str):
        s = v.strip()
    elif isinstance(v, (list, tuple)):
        parts = []
        for x in v:
            parts.append(json.dumps(x, ensure_ascii=False)
                         if isinstance(x, (dict, list)) else str(x))
        s = "；".join(parts)
    elif isinstance(v, dict):
        s = json.dumps(v, ensure_ascii=False)
    else:
        s = str(v)
    s = s or ORG_UNRECORDED
    return s if len(s) <= cap else s[:cap] + "…"


def _org_row(plan, tid, org, name, pdays):
    """把一行 `_organization` 翻成可渲染的 dict（**原值照抄**，缺的留 None）。

    ⚠ C8①：`eta` / `effective_crew_total` 是**已删除的旧口径**，本函数一个字都不再取、
    不再印（W2-C 已从 `org_plan` 删掉这两个键），因此这里也**不再**做
    `crew_total×eta == effective_crew_total` 的自检。
    """
    pd = org.get("person_days")
    pd_from_org = pd is not None
    if pd_from_org:
        pd_src = ORG_PERSON_DAYS_ORG
    else:
        # 契约缺 `person_days` 才退回交付侧累计 —— 且**必须标注**这个数是人工工种
        # 之和（可能含机械配员），与组织层口径可能不同，不能装作同一个数。
        pd = pdays.get(tid)
        pd_src = ORG_PERSON_DAYS_FALLBACK if tid in pdays else None
    contract_notes = []
    n_faces, cpf = org.get("n_faces"), org.get("crew_per_face")
    shifts, crew_total = org.get("shifts"), org.get("crew_total")
    try:
        calc = float(n_faces) * float(cpf) * float(shifts)
    except (TypeError, ValueError):
        calc = None
    if calc is not None and crew_total is not None:
        try:
            if abs(float(crew_total) - calc) > 1e-6:
                contract_notes.append(
                    "n_faces×crew_per_face×shifts=%g，而 crew_total=%s" % (calc, crew_total))
        except (TypeError, ValueError):
            pass
    src = org.get("source")
    src_label = ORG_SOURCE_LABELS.get(str(src)) if src is not None else None
    if src_label is None:
        src_label = (("%s（%s）" % (ORG_UNRECORDED, src)) if src is not None
                     else ORG_UNRECORDED)
    return {
        "task_id": tid, "task_name": name or ORG_UNRECORDED,
        "person_days": pd, "person_days_source": pd_src,
        "person_days_from_org": pd_from_org,
        # 按当前组织**实际投入**的工日（crew_total × duration_days）；键缺失就是 None，一个字都不显示
        "planned_person_days": org.get("planned_person_days"),
        "n_faces": n_faces, "crew_per_face": cpf, "shifts": shifts,
        "crew_total": crew_total,
        "duration_days": org.get("duration_days"), "t_min_days": org.get("t_min_days"),
        "feasible": org.get("feasible"), "source": src, "source_label": src_label,
        "cadence_days": org.get("cadence_days"),
        # ---- E2（施工段表 + 容量字典 + 取小/回分 + 为什么是 N 人/N 台）----
        # 字段契约由 W2-C 交付（`org_plan.plan_capacity_chain`），这里**只照抄不重算**。
        "capacity_source": org.get("capacity_source"),
        "resource_kind": org.get("resource_kind"),
        "resource_name": org.get("resource_name"),
        "resource_mobility": org.get("resource_mobility"),
        "floor_area": org.get("floor_area"),
        "segment_rule_note": _normalized_unit_text(org.get("segment_rule_note") or ""),
        "segment_count": org.get("segment_count"),
        "segment_ids": list(org.get("segment_ids") or []),
        "segment_areas": list(org.get("segment_areas") or []),
        "segments": list(org.get("segments") or []),
        "capacity_rollup": org.get("capacity_rollup"),
        "capacity_effective": org.get("capacity_effective"),
        "rollup_kind": org.get("rollup_kind"),
        "user_cap": org.get("user_cap"),
        "user_cap_source": org.get("user_cap_source"),
        "discarded_caps": list(org.get("discarded_caps") or []),
        "basis_lines": [_normalized_unit_text(x) for x in (org.get("basis_lines") or [])
                        if str(x).strip()],
        "allocation_steps": ([_normalized_unit_text(x)
                              for x in (org.get("allocation") or {}).get("steps") or []]
                             if isinstance(org.get("allocation"), dict) else []),
        "contract_notes": contract_notes,
        "cells": [tid, name or ORG_UNRECORDED,
                  _org_num_text(pd, 2), _org_num_text(n_faces, 2),
                  _org_num_text(cpf, 2), _org_num_text(shifts, 2),
                  _org_num_text(org.get("duration_days"), 2), src_label],
    }


def _org_rows(plan):
    """逐条工序的施工组织口径行（**只列真正带 `_organization` 的行**）。

    为什么不为没有 `_organization` 的工序编一行：那等于把「没有数据」画成「有数据」。
    未携带的行由「本段覆盖 N / M 行」如实交代（`rows_note`）。
    """
    orgs, names, pdays = _org_map(plan), _org_task_names(plan), _org_labor_person_days(plan)
    return [_org_row(plan, tid, org, names.get(tid), pdays)
            for tid, org in sorted(orgs.items())]


def _org_planned_person_days_lines(rows, gaps):
    """防御式显示：`planned_person_days`（**实际投入**工日）与 `person_days`（需求）不同才说话。

    为什么必须有这一段：措施项（`source == "measure_item"`，如"爬架提升"按固定操作时长 1 天）
    会出现 `person_days=99` 而 `planned_person_days=10`；只印 99，读者会自己除出
    `99 ÷ (1 面 × 10 人 × η) ≈ 11 天`，而工期写 1 天 —— 又是一个「同名两个数」。
    按契约把两个口径同时摆出来并写清关系。

    **键缺失 / 与 `person_days` 相等 → 一个字都不加**（不推算、不出现"两者相同"的噪声）。
    """
    gap_ids = {str(g.get("task_id")) for g in (gaps.get("items") or [])}
    out = []
    for r in rows:
        pd, planned = r.get("person_days"), r.get("planned_person_days")
        if pd is None or planned is None:
            continue
        try:
            diff = float(pd) - float(planned)
        except (TypeError, ValueError):
            continue
        if abs(diff) < 1e-9:
            continue
        why = ("（_organization.planned_person_days = crew_total × duration_days，"
               "按当前组织「实际投入」）")
        if str(r.get("source") or "") == "measure_item":
            c, d = r.get("crew_total"), r.get("duration_days")
            if c is not None and d is not None:
                why = ("（措施项按固定操作时长 %s 天 × %s 人 = 投入工日）"
                       % (_org_num_text(d, 2), _org_num_text(c, 2)))
        if str(r.get("task_id")) in gap_ids:
            tail = "；差额 %s 工日见「%s」留痕" % (_org_num_text(diff, 2), ORG_GAP_TITLE)
        else:
            tail = "；差额 %s 工日的成因未记录，请人工确认" % _org_num_text(diff, 2)
        out.append("%s（%s）—— 工日：需求 %s（组织层 person_days）→ 投入 %s%s%s"
                   % (r["task_id"], r.get("task_name") or ORG_UNRECORDED,
                      _org_num_text(pd, 2), _org_num_text(planned, 2), why, tail))
    return out


def _org_cadence_source_label(raw):
    """`meta.boundary_conditions._source.cadence_days` → 人话来源。

    ``user`` → 「用户输入」；``model`` → 「模型估算，非用户输入」；其它原样回显
    （显式中文来源不吞掉）；空 → ``None``（交给证据链兜底）。
    """
    if not isinstance(raw, str):
        return None
    key = raw.strip()
    if not key:
        return None
    return ORG_CADENCE_SOURCE_LABELS.get(key.lower(), key)


def _org_cadence(plan):
    """主体节拍 → ``(节拍文本, 来源文本)``；拿不到 → ``(None, None)``。

    **契约落点**：``meta.boundary_conditions.cadence_days``（连带 ``cadence_scope``
    与 ``_source.cadence_days``）—— 排在探测链最前，优先于逐条
    `_organization.source == "cadence"` 的兜底。之后的几个 ``meta.*`` 键只是历史
    兼容探测（契约里并不存在）。**探测不到不猜**：宁可写「未提供节拍，按推荐班组配置」。
    """
    meta = _org_dict(plan.get("meta"))
    bc = _org_dict(meta.get("boundary_conditions"))
    cand = _org_dict(meta.get("organization"))
    src = _org_dict(bc.get("_source"))
    val, label, scope = None, None, None
    if bc.get("cadence_days") is not None:
        val = bc["cadence_days"]
        raw = src.get("cadence_days")
        if isinstance(raw, str) and raw.strip():
            label = _org_cadence_source_label(raw.strip())
        scope = str(bc.get("cadence_scope") or "").strip() or ORG_CADENCE_DEFAULT_SCOPE
    if val is None:
        for probe in (cand.get("cadence_days"), meta.get("organization_cadence_days"),
                      meta.get("cadence_days"), meta.get("standard_floor_cadence_days")):
            if probe is not None:
                val = probe
                break
    if label is None and isinstance(cand.get("cadence_source"), str) and cand.get("cadence_source").strip():
        label = cand.get("cadence_source").strip()
    if label is None and isinstance(cand.get("source"), str) and cand["source"].strip():
        label = ORG_SOURCE_LABELS.get(cand["source"].strip(), cand["source"].strip())
    scope = scope or ORG_CADENCE_DEFAULT_SCOPE
    if val is not None:
        try:
            num = float(val)
        except (TypeError, ValueError):
            return ("%s %s 天/层" % (scope, val), label or ORG_UNRECORDED)
        txt = "%s %.4g 天/层" % (scope, num)
        if label is None:
            # 没有显式来源字段时**用证据定来源**：plan 级节拍与某条
            # `source == "cadence"` 的行上的节拍相等 → 那就是用户输入的节拍。
            for org in _org_map(plan).values():
                if str(org.get("source") or "") != "cadence" or org.get("cadence_days") is None:
                    continue
                try:
                    if abs(float(org["cadence_days"]) - num) < 1e-9:
                        label = "用户输入"
                        break
                except (TypeError, ValueError):
                    continue
        return (txt, label or ORG_UNRECORDED)
    cnt = {}
    for org in _org_map(plan).values():
        if str(org.get("source") or "") != "cadence" or org.get("cadence_days") is None:
            continue
        try:
            f = float(org["cadence_days"])
        except (TypeError, ValueError):
            continue
        cnt[f] = cnt.get(f, 0) + 1
    if cnt:
        best = sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        return ("%s %.4g 天/层" % (scope, best), "用户输入")
    for org in _org_map(plan).values():          # 非 cadence 口径的节拍也如实标来源
        if org.get("cadence_days") is None:
            continue
        try:
            txt = "%s %.4g 天/层" % (scope, float(org["cadence_days"]))
        except (TypeError, ValueError):
            txt = "%s %s 天/层" % (scope, org["cadence_days"])
        return (txt, ORG_SOURCE_LABELS.get(str(org.get("source") or ""), ORG_UNRECORDED))
    return (None, None)


def _org_gap_model(plan):
    """`meta.organization_gaps` → 缺口逐条文字（空/缺字段都说实话）。

    **三种状态必须分开**（"空列表"绝不允许冒充"没有缺口"）：
      ① 非空列表 → 逐条列缺口（n_needed / n_max / levers）；
      ② 空列表 + 有节拍（`_org_cadence` 能取到）→ 真的校核过且通过了；
      ③ 空列表 + 没节拍 → **没校核**，不是"校核通过"（没校核 ≠ 无缺口）；
      ④ 键缺失 / 格式不是列表 → 没有可核对的数据，**不猜**。
    """
    meta = _org_dict(plan.get("meta"))
    if "organization_gaps" not in meta:
        return {"present": False, "state": "missing", "items": [], "lines": [
            ORG_NO_GAP + "（计划 meta 未带 organization_gaps 字段：本次没有可核对的组织缺口"
            "数据，「不代表已核对」）。"]}
    gaps = meta.get("organization_gaps")
    if not isinstance(gaps, list):
        return {"present": False, "state": "malformed", "items": [], "lines": [
            ORG_NO_GAP + "（organization_gaps 字段格式不是列表：本段不猜其含义）。"]}
    items = [g for g in gaps if isinstance(g, dict)]
    if not items:
        cad_txt, _cad_label = _org_cadence(plan)
        if cad_txt:
            # ② 有节拍 + 空缺口 = 真的按这个节拍校核过、没有做不到的工序
            return {"present": True, "state": "checked_none", "items": [], "lines": [
                ORG_NO_GAP + "：已按主体节拍（%s）逐条校核，没有做不到的工序。" % cad_txt]}
        # ③ 没节拍 → 组织层根本没跑过校核（"空"是没算，不是"算过没问题"）
        return {"present": True, "state": "unchecked", "items": [], "lines": [
            ORG_NO_GAP + "（但" + ORG_NO_CADENCE + "：本次未做组织层校核，"
            "「未校核」不等于「校核通过」）。"]}
    lines = []
    for g in items:
        tid = g.get("task_id")
        nm = g.get("task_name") or tid or ORG_UNRECORDED
        trade = g.get("trade") or ORG_UNRECORDED
        pd = _org_num_text(g.get("person_days"), 2)
        head = "%s%s（%s，%s 工日）" % (nm, ("（%s）" % tid) if tid else "", trade, pd)
        body = ("该工序在 %s 天节拍下需要 %s 个作业面，而结构/组织上限只允许 %s 个面"
                "（每面人数上限 %s 人，可达最短 %s 天）。"
                "C11：节拍在这里仅作对比参考，不参与工期与人数计算。"
                % (_org_num_text(g.get("cadence_days"), 2), _org_num_text(g.get("n_needed"), 2),
                   _org_num_text(g.get("n_max"), 2), _org_num_text(g.get("c_max"), 2),
                   _org_num_text(g.get("t_min_days"), 2)))
        levers = [str(x).strip() for x in (g.get("levers") or []) if str(x).strip()]
        lever_txt = ("可动杠杆：" + "；".join(levers) + "。") if levers \
            else ("可动杠杆：" + ORG_UNRECORDED + "。")
        lines.append(head + "：" + body + lever_txt)
    return {"present": True, "state": "gaps", "items": items, "lines": lines}


def _org_spread_entries(entries):
    """选行离散：只把工日/单位相差 **> 1.5 倍**的组列成待审条目。"""
    big, maxr = [], None
    for s in entries or []:
        if not isinstance(s, dict):
            continue
        try:
            r = float(s.get("ratio"))
        except (TypeError, ValueError):
            r = None
        if r is not None:
            maxr = r if maxr is None else max(maxr, r)
            if r > 1.5:
                big.append((s, r))
    return big, maxr


def _scope_audit_model(plan):
    """`meta.scope_audit` → 三段待审提示（重复建项 / 上限待审 / 选行离散）。

    措辞铁律：全部是「疑似 / 待审 / 请人工确认」，**不是**「系统已经知道错了」。
    """
    meta = _org_dict(plan.get("meta"))
    out = {"present": False, "duplicates": [], "cmax_sample": [], "cmax_header": [],
           "cmax_count": None, "cmax_line": "", "spread_lines": [], "note": ""}
    sa = meta.get("scope_audit")
    if not isinstance(sa, dict):
        out["note"] = ("本次未提供审计核对数据（计划 meta 无 scope_audit）："
                       "重复计量 / 单面人数上限 / 选行离散三项「均未核对」，"
                       "本段不做「没有问题」的结论。")
        return out
    out["present"] = True
    for d in (sa.get("duplicate_scopes") or []):
        if not isinstance(d, dict):
            continue
        ids = [str(x) for x in (d.get("task_ids") or [])]
        out["duplicates"].append(
            "疑似重复计量，请在人工门确认：活动族 %s 的「%s」被 %s 共同占用"
            "（%d 条工序指向同一批工程量）。证据：%s"
            % (d.get("kb_activity_id") or ORG_UNRECORDED, d.get("location") or ORG_UNRECORDED,
               "、".join(ids) or ORG_UNRECORDED, len(ids),
               _org_evidence_text(d.get("evidence"))))
    cmax = _org_dict(sa.get("cmax_review"))
    sample = [s for s in (cmax.get("sample") or [])]
    out["cmax_count"] = cmax.get("count")
    if cmax:
        out["cmax_line"] = (
            "上限待审：有 %s 行的单面人数上限存在两个互相矛盾的值"
            "（v1 常数 vs v2 同族最大），本次沿用现行口径，建议人工审定。"
            % _org_num_text(cmax.get("count"), 0))
        for s in sample[:12]:
            if isinstance(s, dict):
                if not out["cmax_header"]:
                    out["cmax_header"] = [str(k) for k in s.keys()]
                out["cmax_sample"].append([_org_evidence_text(s.get(k), cap=120) for k in s.keys()])
            else:
                out["cmax_sample"].append([_org_evidence_text(s, cap=120)])
    else:
        out["cmax_line"] = ("上限待审：本次未提供单面人数上限的复核数据"
                            "（scope_audit.cmax_review 缺失）：该项未核对。")
    spread = [s for s in (sa.get("norm_row_spread") or []) if isinstance(s, dict)]
    big, maxr = _org_spread_entries(spread)
    for s, r in big:
        out["spread_lines"].append(
            "同一活动族 %s（单位 %s）里工日/单位相差 %.4g 倍（> 1.5 倍阈值）："
            "选行规则可能不一致，请在人工门确认。样本：%s"
            % (s.get("kb_activity_id") or ORG_UNRECORDED, s.get("unit") or ORG_UNRECORDED,
               r, _org_evidence_text(s.get("samples"))))
    if not big:
        if spread:
            out["spread_lines"].append(
                "本次未发现工日/单位相差 > 1.5 倍的组（已核对 %d 组，最大 %s 倍）。"
                % (len(spread), _org_num_text(maxr, 4)))
        else:
            out["spread_lines"].append(
                "本次未提供选行离散数据（scope_audit.norm_row_spread 缺失/为空）：该项未核对。")
    return out


def _org_consistency_lines(plan, view, rows):
    """守恒/一致性自检：同一份文档里口径不同的人数/工期必须各自写明是哪一个。

    历史缺陷：同一份交付物里两个「峰值人数」含义不同却不解释。
    组织层引入的「名义班组 / 逐日曲线峰值」两个数，**必须一次说清**。
    （C8① 删除的「有效班组 = 名义班组 × η」已不再有，故这里只剩两个口径。）
    """
    peak = view.get("peak_total")
    lines = []
    if not rows:
        lines.append(
            "本计划没有任何排程行携带「_organization」（施工组织层数据）：逐条"
            "「工日 / 作业面数 / 每面人数 / 班次 → 工期」一律为「来源未记录」，系统不猜。")
        lines.append(
            "本页出现的「峰值人数 %s 人」是逐日人员曲线峰值（按任务叠加、含机械配员），"
            "与组织层的「名义班组」不是同一个数；本次没有组织层数据，"
            "因此不存在两个口径打架的问题。" % _org_num_text(peak, 2))
        return lines
    def _mx(key):
        vals = []
        for r in rows:
            try:
                vals.append(float(r.get(key)))
            except (TypeError, ValueError):
                continue
        return max(vals) if vals else None
    lines.append("本页有两个「人数」，口径不同、互不替代：")
    gaps = _org_gap_model(plan)
    lines.append("①「峰值人数 %s 人」= 逐日人员曲线峰值（按任务叠加、含机械配员），口径见「资源计划」。"
                 % _org_num_text(peak, 2))
    lines.append("②「名义班组 = 作业面数 × 每面人数 × 班次」本段最大 %s 人"
                 "（工序层面配置的在场人数，「不按日叠加」）。" % _org_num_text(_mx("crew_total"), 2))
    span = _schedule_span(plan)
    diff = []
    for r in rows:
        d, s = r.get("duration_days"), span.get(str(r.get("task_id")))
        try:
            if d is not None and s is not None and int(d) != int(s):
                diff.append("%s（组织层 %s 天 / 排程跨度 %s 天）"
                            % (r["task_id"], _org_num_text(d, 2), _org_num_text(s, 2)))
        except (TypeError, ValueError):
            continue
    if diff:
        lines.append("组织层工期与横道排程跨度不一致的行 %d 条（示例：%s）："
                     "「工期(天·排程)」是排程结果，「_organization.duration_days」是组织层"
                     "给出的工期 —— 两个数都列出、不合并，差异请人工复核。"
                     % (len(diff), "；".join(diff[:3])))
    bad = [r for r in rows if r.get("contract_notes")]
    if bad:
        lines.append("契约字段自检：%d 条行的「_organization」内部不自洽（示例：%s —— %s）："
                     "本页照抄原值，未擅自统一。"
                     % (len(bad), bad[0]["task_id"], "；".join(bad[0]["contract_notes"])))
    # 契约要求 `feasible == true ⟺ 无缺口条目`：同一任务既在逐条表里自称可行/带工期，
    # 又被缺口报告列为「面数不够、不可达」，就是上游口径打架 —— 必须写进自检，不替它取舍。
    # **例外**：措施项（`source == "measure_item"`）按固定操作时长排期，工期本就不由
    # 工日 ÷ 班组推得，属预期口径差异，报它等于每次重跑都出假告警。
    measure = [r["task_id"] for r in rows if str(r.get("source") or "") == "measure_item"]
    if measure:
        lines.append("其中 %d 条是措施项（source = measure_item，按固定操作时长/整台班排期，"
                     "示例：%s）：它们的工期不由「工日 ÷ 班组」推得，工期与公式推算值不同属"
                     "「预期口径差异」，本自检不对这类行报「口径不一致」。"
                     % (len(measure), "、".join(measure[:3])))
    conflicts = []
    by_id = {str(r.get("task_id")): r for r in rows}
    for g in (gaps.get("items") or []):
        r = by_id.get(str(g.get("task_id")))
        if r is None:
            continue
        if str(r.get("source") or "") == "measure_item":
            continue
        claimed = (r.get("feasible") is True) or (r.get("duration_days") is not None)
        if not claimed:
            continue
        detail = "需要 %s 个作业面、上限 %s 个面、可达最短 %s 天" % (
            _org_num_text(g.get("n_needed"), 2), _org_num_text(g.get("n_max"), 2),
            _org_num_text(g.get("t_min_days"), 2))
        conflicts.append("%s（行内 feasible=%s、工期 %s 天，而缺口报告说 %s）"
                         % (r["task_id"], r.get("feasible"),
                            _org_num_text(r.get("duration_days"), 2), detail))
    if conflicts:
        lines.append("口径对齐自检：%d 条任务「同时」出现在逐条表（自称可行/已给工期）与组织缺口"
                     "报告（面数不够、不可达）里 —— 「疑似上游口径不一致」（契约要求"
                     "「feasible = true」当且仅当无缺口条目）：%s。本页按原值并列，未擅自取舍，"
                     "请人工确认。" % (len(conflicts), "；".join(conflicts[:3])))
    return lines


def _org_capacity_model(rows, cap=50):
    """E2：**施工段表 + 容量字典 + 取小 / 回分过程 + 「为什么是 N 人 / N 台」**。

    全部字段**照抄** `_organization`（W2-C 的 `org_plan.plan_capacity_chain` 契约），
    一个数都不重算、一个字段都不新造；取不到的列写「来源未记录」。
    没有 `segments` 的行（旧计划 / 非 MWI 链路）**不进**这里 —— 不编行。
    """
    det = [r for r in rows if r.get("segments")]
    seg_header = ["段号", "段面积(m²)", "固定型(人/台)", "移动型(人/台)", "实际分配",
                  "MWI", "MWI 单位", "段级需求", "批次"]
    cap_header = ["工序 ID", "工序", "资源", "段数", "汇总容量", "用户同类限额",
                  "有效容量", "工期(天)"]
    cap_rows, seg_tables, basis = [], [], []
    for r in det[:cap]:
        cap_rows.append([
            r["task_id"], r["task_name"], r.get("resource_name") or ORG_UNRECORDED,
            _org_num_text(r.get("segment_count"), 0),
            _org_num_text(r.get("capacity_rollup"), 0),
            (_org_num_text(r.get("user_cap"), 0) if r.get("user_cap") is not None
             else ORG_UNRECORDED),
            _org_num_text(r.get("capacity_effective"), 0),
            _org_num_text(r.get("duration_days"), 0)])
        srows = []
        for s in (r.get("segments") or []):
            if not isinstance(s, dict):
                continue
            srows.append([
                str(s.get("segment_id") or ORG_UNRECORDED),
                _org_num_text(s.get("segment_area"), 2),
                _org_num_text(s.get("capacity_fixed"), 0),
                _org_num_text(s.get("capacity_mobile"), 0),
                (_org_num_text(s.get("capacity_allocated"), 0)
                 if s.get("capacity_allocated") is not None else ORG_UNRECORDED),
                _org_num_text(s.get("mwi"), 2),
                _normalized_unit_text(s.get("mwi_unit") or ORG_UNRECORDED),
                _org_num_text(s.get("segment_demand"), 0),
                (_org_num_text(s.get("batch"), 0) if s.get("batch") is not None
                 else ORG_UNRECORDED)])
        seg_tables.append({"task_id": r["task_id"], "task_name": r["task_name"],
                           "rows": srows})
        # 「为什么是 N 人 / N 台」：上游写好的依据串（含取小与回分过程），逐条照抄
        if r.get("basis_lines"):
            basis.append({"task_id": r["task_id"], "task_name": r["task_name"],
                          "lines": r["basis_lines"]})
        if r.get("allocation_steps"):
            basis.append({"task_id": r["task_id"], "task_name": r["task_name"],
                          "lines": r["allocation_steps"]})
    notes = []
    for r in det[:cap]:
        if r.get("segment_rule_note"):
            notes.append("%s（层面积 %s m²）：%s"
                         % (r["task_id"], _org_num_text(r.get("floor_area"), 2),
                            r["segment_rule_note"]))
        if r.get("user_cap") is not None and str(r.get("user_cap_source") or "").strip() == "":
            notes.append("%s：用户同类限额 %s 已生效，但来源未标注"
                         % (r["task_id"], _org_num_text(r.get("user_cap"), 0)))
    return {
        "present": bool(det),
        "count": len(det),
        "seg_header": seg_header,
        "cap_header": cap_header,
        "cap_rows": cap_rows,
        "seg_tables": seg_tables,
        "basis": basis,
        "notes": notes,
        "row_count": len(rows),
    }


def organization_section_model(plan, view):
    """施工组织层口径的**单一真源**：看板与 Word 都从这一份 model 渲染（口径不可能两处漂移）。

    ⚠ C8①：旧口径的 `formula` / `eta_explain` / `eta_current` 三个键**已删除**
    （效率折减 η 整条删除，交付物不再印旧口径公式）。新口径的展示文案属 E2，尚未落地。
    """
    rows = _org_rows(plan)
    gaps = _org_gap_model(plan)
    cad_text, cad_src = _org_cadence(plan)
    total_tasks = len([t for t in _tasks(plan) if isinstance(t, dict)])
    if rows:
        rows_note = ("本段覆盖 %d / %d 条排程行（其余行未携带「_organization」，"
                     "不编行、不猜数；它们的排程与依据见上方横道图 / WBS 表）。"
                     % (len(rows), total_tasks))
    else:
        rows_note = ("本计划 %d 条排程行「没有任何一行」携带「_organization」（施工组织层尚未"
                     "产出）：本段逐条口径一律为「来源未记录」，系统不猜。" % total_tasks)
    org_pd = [r for r in rows if r.get("person_days_from_org")]
    if rows:
        n_org = len(org_pd)
        if n_org == len(rows):
            person_days_note = ("工日列 = 本工序工种工日，取自组织层「_organization.person_days」"
                                "（不含机械台日、不含机械配员）。")
        elif n_org:
            person_days_note = (
                "工日列：%d 条取自组织层「_organization.person_days」（本工序工种工日，不含机械配员）；"
                "其余 %d 条组织层没给该字段，交付侧「resource_demand.resources 人工工种 total_days "
                "之和」累计（可能含机械配员，与组织层口径可能不同），两条口径都标了来源、未合并。"
                % (n_org, len(rows) - n_org))
        else:
            person_days_note = (
                "工日列：组织层「_organization.person_days」缺失，交付侧按「resource_demand.resources "
                "人工工种 total_days 之和」累计（可能含机械配员，与组织层口径可能不同）；"
                "缺该字段时工期以「来源未记录」为准，不代填。")
    else:
        person_days_note = ""
    return {
        "title": ORG_TITLE,
        "gap_title": ORG_GAP_TITLE,
        "audit_title": ORG_AUDIT_TITLE,
        "cadence_text": ("主体节拍：%s（来源：%s）" % (cad_text, cad_src)) if cad_text
                        else ("主体节拍：" + ORG_NO_CADENCE),
        "cadence_days": cad_text, "cadence_source": cad_src,
        "header": list(ORG_TABLE_HEADER),
        "rows": rows,
        "row_cells": [r["cells"] for r in rows],
        "row_count": len(rows), "task_count": total_tasks, "rows_note": rows_note,
        "person_days_note": person_days_note,
        "person_days_org_count": len(org_pd),
        # 防御式：只有 `planned_person_days` 存在且 != person_days 才有内容，否则空列表
        "planned_person_days_lines": _org_planned_person_days_lines(rows, gaps),
        "gaps": gaps,
        "scope": _scope_audit_model(plan),
        "consistency": _org_consistency_lines(plan, view, rows),
        # E2：施工段表 + 容量字典 + 取小/回分过程 + 「为什么是 N 人 / N 台」
        "capacity": _org_capacity_model(rows),
    }


def _org_section_html(plan, view):
    """看板「施工组织口径 + 组织缺口 + 审计提示」整段 HTML（确定性）。

    与 Word 同源（都读 `organization_section_model`）：一处口径，两个交付物。
    本段**永远**带「施工组织口径」「组织缺口」两个标记 —— `_ensure_org_section`
    据此判断 LLM 编排页有没有把它丢掉。
    """
    m = organization_section_model(plan, view)
    e = _html.escape
    gaps = m["gaps"]
    scope = m["scope"]
    rows_html = "".join(
        "<tr>" + "".join("<td>%s</td>" % e(str(c)) for c in cells) + "</tr>"
        for cells in m["row_cells"]) or "<tr><td colspan=8>（无：没有排程行携带 _organization）</td></tr>"
    table_html = (
        "<table><tr>" + "".join("<th>%s</th>" % e(str(h)) for h in m["header"]) + "</tr>"
        f"{rows_html}</table>")
    dup_html = "".join("<li>%s</li>" % e(x) for x in scope["duplicates"]) \
        or "<li>本次未提供重复建项数据（该项未核对）。</li>"
    spread_html = "".join("<li>%s</li>" % e(x) for x in scope["spread_lines"]) or "<li>（无数据）</li>"
    cmax_extra = ""
    if scope["cmax_sample"]:
        if scope["cmax_header"] and len(scope["cmax_header"]) == len(scope["cmax_sample"][0]):
            head = scope["cmax_header"]
            body = "".join("<tr>" + "".join("<td>%s</td>" % e(str(c)) for c in row) + "</tr>"
                           for row in scope["cmax_sample"])
        else:
            head = ["样本"]
            body = "".join("<tr><td>%s</td></tr>" % e("；".join(str(c) for c in row))
                           for row in scope["cmax_sample"])
        cmax_extra = ("<details open><summary>查看前 %d 行（共 %s 行）</summary>"
                      "<table><tr>%s</tr>%s</table></details>"
                      % (len(scope["cmax_sample"]), e(_org_num_text(scope["cmax_count"], 0)),
                         "".join("<th>%s</th>" % e(str(h)) for h in head), body))
    return (
        f"<div class='card'><h2>{e(m['title'])}</h2>"
        f"<div class='rs-row'><b>{e(m['cadence_text'])}</b>"
        f"（C11：节拍仅作对比参考，不参与任何计算）</div>"
        # ⚠ C8①：旧口径的「口径公式（含效率折减 η）」与「效率折减是什么」两行已删除，
        #   新口径的公式展示属 E2，尚未落地 —— 这里绝不自己发明一句公式。
        f"<div class='chart-lbl'>逐条工序：数据来自排程行「_organization」；"
        f"缺字段写「{e(ORG_UNRECORDED)}」，系统不猜。{e(m['rows_note'])}</div>"
        + (f"<div class='chart-lbl'>{e(m['person_days_note'])}</div>"
           if m.get("person_days_note") else "")
        + f"{table_html}"
        + (("<div class='chart-lbl'>工日两口径（需求 vs 按当前组织实际投入）：</div>"
            "<ul class='mil'>"
            + "".join("<li>%s</li>" % e(x) for x in m["planned_person_days_lines"]) + "</ul>")
           if m.get("planned_person_days_lines") else "")
        + f"<h3>{e(m['gap_title'])}</h3>"
        "<div class='chart-lbl'>逐条：该工序在 X 天节拍下需要 N 个作业面，而结构/组织上限"
        "只允许 M 个面 → 可达最短 Y 天。这是待审提示，不是系统结论。</div>"
        "<ul class='mil'>" + "".join("<li>%s</li>" % e(x) for x in gaps["lines"]) + "</ul>"
        f"<h3>{e(m['audit_title'])}</h3>"
        "<div class='chart-lbl'>以下全部是「提示与待审」：请在人工门确认，"
        "系统没有判定任何一项「错了」。</div>"
        + (f"<div class='chart-lbl'>{e(scope['note'])}</div>" if scope["note"] else "")
        + "<h4>1. 重复建项（疑似重复计量）</h4><ul class='mil'>" + dup_html + "</ul>"
        + "<h4>2. 上限待审（单面人数上限）</h4>"
        + f"<div class='rs-row'>{e(scope['cmax_line'])}</div>" + cmax_extra
        + "<h4>3. 选行离散（同族工日/单位相差 > 1.5 倍）</h4><ul class='mil'>" + spread_html + "</ul>"
        + "<h3>口径对齐自检（同一份文档内）</h3><ul class='mil'>"
        + "".join("<li>%s</li>" % e(x) for x in m["consistency"]) + "</ul>"
        + _org_capacity_html(m.get("capacity"))
        + "</div>")


def _org_capacity_html(cap):
    """E2 的看板 HTML：施工段表 + 容量字典 + 取小/回分依据（取不到 → 空串，不编）。"""
    if not isinstance(cap, dict) or not cap.get("present"):
        return ""
    e = _html.escape

    def _table(header, rows):
        return ("<table><tr>" + "".join("<th>%s</th>" % e(str(h)) for h in header) + "</tr>"
                + "".join("<tr>" + "".join("<td>%s</td>" % e(str(c)) for c in row) + "</tr>"
                          for row in rows) + "</table>")

    out = ["<h3>施工段与容量（E2：段数 / 汇总容量 / 用户限额 / 有效容量 / 工期）</h3>",
           _table(cap["cap_header"], cap["cap_rows"])]
    for t in cap["seg_tables"][:5]:
        out.append("<details><summary>%s %s —— 施工段表（%d 段）</summary>%s</details>"
                   % (e(t["task_id"]), e(t["task_name"]), len(t["rows"]),
                      _table(cap["seg_header"], t["rows"])))
    for b in cap["basis"][:5]:
        out.append("<details><summary>%s %s —— 为什么是 N 人 / N 台（容量依据）</summary>"
                   "<ul class='mil'>%s</ul></details>"
                   % (e(b["task_id"]), e(b["task_name"]),
                      "".join("<li>%s</li>" % e(x) for x in b["lines"])))
    for n in cap["notes"]:
        out.append("<div class='chart-lbl'>%s</div>" % e(n))
    return "".join(out)


def add_organization_section(doc, plan, view, add_grid):
    """把施工组织层口径写进 Word（§三 横道图之后）。

    标题用**加粗正文段**而不是 Heading 样式：`audit_gate.draft_outline_payload` 的目录
    逐字镜像它自己写死的章节清单（那份文件本轮不许改），用 Heading 会让
    「目录 = 真产物」的既有回归门失配。加粗副标题既醒目又不进目录。

    与看板同源（`organization_section_model`）：一处口径，两个交付物。
    """
    from docx.shared import Pt

    m = organization_section_model(plan, view)

    def _bold(text, size=None):
        _p = doc.add_paragraph()
        _r = _p.add_run(str(text))
        _r.bold = True
        if size:
            _r.font.size = Pt(size)
        return _p

    _bold(m["title"], size=13)
    doc.add_paragraph(m["cadence_text"] + "（C11：节拍仅作对比参考，不参与任何计算）")
    # ⚠ C8①：旧口径的「口径公式 / 效率折减是什么 / 当前取值 η」三段已删除。
    #   新口径的公式展示属 E2，尚未落地 —— 这里绝不自己发明一句公式。
    if m["rows_note"]:
        doc.add_paragraph(m["rows_note"])
    if m.get("person_days_note"):
        doc.add_paragraph(m["person_days_note"])
    if m["row_cells"]:
        doc.add_paragraph("逐条工序（数据来自排程行「_organization」；缺字段写「%s」，系统不猜）："
                          % ORG_UNRECORDED)
        add_grid(list(m["header"]), m["row_cells"])
    if m.get("planned_person_days_lines"):
        doc.add_paragraph("工日两口径（需求 vs 按当前组织实际投入）：")
        for line in m["planned_person_days_lines"]:
            doc.add_paragraph("• " + line)
    # ---- E2：施工段表 + 容量字典 + 取小/回分依据 +「为什么是 N 人 / N 台」 ----
    _cap = m.get("capacity") or {}
    if _cap.get("present"):
        doc.add_paragraph("施工段与容量（段数 / 汇总容量 / 用户同类限额 / 有效容量 / 工期）：")
        add_grid(list(_cap["cap_header"]), _cap["cap_rows"])
        for i, t in enumerate(_cap["seg_tables"]):
            if i >= 5:
                doc.add_paragraph("（共 %d 条工序的施工段表，此处列出前 5 条；"
                                  "完整逐段明细见计划 JSON 的 "
                                  "_organization.segments）" % len(_cap["seg_tables"]))
                break
            doc.add_paragraph("施工段表 · %s %s（%d 段）："
                              % (t["task_id"], t["task_name"], len(t["rows"])))
            add_grid(list(_cap["seg_header"]), t["rows"])
        for b in _cap["basis"][:5]:
            doc.add_paragraph("%s %s —— 为什么是 N 人 / N 台（容量依据）："
                              % (b["task_id"], b["task_name"]))
            for line in b["lines"]:
                doc.add_paragraph("• " + line)
        for n in _cap["notes"]:
            doc.add_paragraph(n)
    _bold(m["gap_title"], size=12)
    for line in m["gaps"]["lines"]:
        doc.add_paragraph("• " + line)
    _bold(m["audit_title"], size=12)
    scope = m["scope"]
    if scope["note"]:
        doc.add_paragraph(scope["note"])
    doc.add_paragraph("1. 重复建项（疑似重复计量）")
    for line in (scope["duplicates"] or ["本次未提供重复建项数据（该项未核对）。"]):
        doc.add_paragraph("• " + line)
    doc.add_paragraph("2. 上限待审（单面人数上限）")
    doc.add_paragraph("• " + scope["cmax_line"])
    if scope["cmax_sample"]:
        if scope["cmax_header"] and len(scope["cmax_header"]) == len(scope["cmax_sample"][0]):
            add_grid(scope["cmax_header"], scope["cmax_sample"])
        else:
            add_grid(["样本"], scope["cmax_sample"])
        doc.add_paragraph("（以上为前 %d 行样本，共 %s 行）"
                          % (len(scope["cmax_sample"]), _org_num_text(scope["cmax_count"], 0)))
    doc.add_paragraph("3. 选行离散（同族工日/单位相差 > 1.5 倍）")
    for line in (scope["spread_lines"] or ["本次未提供选行离散数据（该项未核对）。"]):
        doc.add_paragraph("• " + line)
    _bold("口径对齐自检（同一份文档内）", size=12)
    for line in m["consistency"]:
        doc.add_paragraph("• " + line)
    return True


def _org_facts(plan, view):
    """喂给 LLM 编排的施工组织层事实（照抄 model，字段名与契约一致）。"""
    m = organization_section_model(plan, view)
    return {
        "title": m["title"],
        "cadence_text": m["cadence_text"],
        # ⚠ C8①：`formula` / `eta_explain` / `eta_current` 三个键**已删除**
        # （效率折减 η 整条删除，旧口径公式不再印）。新口径展示属 E2，尚未落地。
        "cadence_caliber": "节拍仅作对比参考，不参与工期与人数计算（C11）。",
        "header": m["header"],
        "row_count": m["row_count"], "task_count": m["task_count"], "rows_note": m["rows_note"],
        "person_days_note": m.get("person_days_note", ""),
        "person_days_org_count": m.get("person_days_org_count", 0),
        "planned_person_days_lines": m.get("planned_person_days_lines", []),
        # 逐条行只给"合同字段 + 已渲染好的单元格"：模型照抄即可，不许重算
        "rows": [{"task_id": r["task_id"], "task_name": r["task_name"],
                  "person_days": r["person_days"], "n_faces": r["n_faces"],
                  "crew_per_face": r["crew_per_face"], "shifts": r["shifts"],
                  "crew_total": r["crew_total"],
                  "duration_days": r["duration_days"], "t_min_days": r["t_min_days"],
                  "feasible": r["feasible"], "source": r["source"],
                  "person_days_from_org": r["person_days_from_org"],
                  "planned_person_days": r["planned_person_days"],
                  "source_label": r["source_label"], "cells": r["cells"]}
                 for r in m["rows"]],
        "gaps": {"present": m["gaps"]["present"], "state": m["gaps"].get("state"),
                 "lines": m["gaps"]["lines"]},
        "scope_audit_lines": {
            "note": m["scope"]["note"],
            "duplicates": m["scope"]["duplicates"],
            "cmax": m["scope"]["cmax_line"],
            "spread": m["scope"]["spread_lines"],
        },
        "consistency_lines": m["consistency"],
        "rules": [
            "逐条工序必须照抄 cells（工日 / 作业面数 / 每面人数 / 班次 / 工期 / 来源），"
            "缺字段就是「来源未记录」，「不许补数、不许重算」。",
            "**不许**再写旧口径公式（含「效率折减」/「η」/「有效班组」的任何句子）——"
            "该口径已整条删除；新口径的公式展示尚未落地，正文里一个字都不要编。",
            "节拍只是**对比参考**：照抄 cadence_text，并在同一处写明「不参与工期与人数计算」；"
            "不许说「按节拍推出天数 / 按节拍反算人数」。",
            "工日列口径照抄 person_days_note：优先组织层「_organization.person_days」（本工序工种工日，"
            "不含机械配员）；缺该字段时交付侧累计的数必须标注「可能含机械配员，与组织层口径可能不同」，"
            "「不许把两个口径写成同一个数」。",
            "若 `planned_person_days_lines` 非空，必须原样附上「需求工日 vs 实际投入工日」两口径"
            "（措施项按固定操作时长排期，工期不由「工日 ÷ 班组」推得）；该列表为空时"
            "**一个字都不要提**这两个口径。",
            "组织缺口按 gaps.lines 逐条照抄。注意 gaps.state 的四种状态**不许混写**："
            "`gaps`=逐条列缺口；`checked_none`=已按节拍校核过、没有做不到的工序；"
            "`unchecked`=没有节拍、**未做**校核（绝不能写成「无缺口」）；`missing`/`malformed`="
            "没有可核对的数据（不许猜）。",
            "审计提示必须是「疑似 / 待审 / 请人工确认」的措辞，「不许写成系统已判定的结论」。",
            "同一页出现多个「人数」时（逐日曲线峰值 / 名义班组）必须各自写明口径，"
            "照抄 consistency_lines。",
        ],
    }


# ══════════════════════════════════════════════════════════════════════════════
# 域 8.3：日级资源账单
# ------------------------------------------------------------
# 数据源：`meta.daily_resource_share` / `meta.resource_backpressure`
# （由 scheduler 节点在 `_public_version` 之前抬成独立 ctx 键，
#  plan_assembler.build_meta 白名单搬运进 meta）。
# 7 项：①每道 L4 的资源量 ②每天每种资源需求量 ③用户限额线 ④超限日高亮
#       ⑤分配明细 ⑥迭代轮数 ⑦收敛状态
# ══════════════════════════════════════════════════════════════════════════════


def _daily_resource_bill_model(plan):
    """域 8.3：日级资源账单的数据模型（7 项全覆盖）。

    看板 + Word 共用同一份 facts；两处一字不差。
    """
    meta = plan.get("meta") if isinstance(plan.get("meta"), dict) else {}
    share = meta.get("daily_resource_share") or {}
    bp = meta.get("resource_backpressure") or {}

    # ⑥ 迭代轮数 / ⑦ 收敛状态
    rounds_used = bp.get("rounds_used")
    converged = bp.get("converged")
    cap = bp.get("cap")
    share_source = bp.get("share_source")
    bp_note = bp.get("note") or ""

    # ① 每道 L4 的资源量 + ② 每天每种资源需求量 + ⑤ 分配明细
    # share 形状：{资源名: {task_id: {day_str: count}}}
    resources_summary = []
    for res_name in sorted(share.keys()):
        tasks = share[res_name]
        if not isinstance(tasks, dict):
            continue
        task_summaries = []
        for task_id in sorted(tasks.keys()):
            days = tasks[task_id]
            if not isinstance(days, dict):
                continue
            day_list = sorted(days.items(), key=lambda x: str(x[0]))
            task_summaries.append({
                "task_id": task_id,
                "days": [{"day": str(d), "count": int(c)} for d, c in day_list if int(c) > 0],
            })
        resources_summary.append({
            "resource": str(res_name),
            "tasks": task_summaries,
        })

    # ③ 用户限额线 + ④ 超限日高亮（来自 backpressure trace）
    over_limit_records = []
    for tr in (bp.get("trace") or []):
        round_num = tr.get("round")
        for rec in (tr.get("over") or []):
            over_limit_records.append({
                "round": round_num,
                "resource": rec.get("resource"),
                "limit": rec.get("limit"),
                "peak": rec.get("peak"),
                "breached": rec.get("breached", False),
                "note": rec.get("note") or "",
            })

    present = bool(share or bp)
    return {
        "present": present,
        "resources": resources_summary,
        "over_limit": over_limit_records,
        "rounds_used": rounds_used,
        "converged": converged,
        "cap": cap,
        "share_source": share_source,
        "note": bp_note,
        "how_to_write": (
            "present 为 true 时**必须**写出以下 7 项（一项都不能少）：\n"
            "① 每道 L4 的资源量（resources[*].tasks[*].days 照抄）\n"
            "② 每天每种资源的需求量（resources[*].tasks[*].days 的 count 照抄）\n"
            "③ 用户限额线（over_limit[*].limit 照抄）\n"
            "④ 超限日高亮（over_limit 非空时标出哪些资源超限）\n"
            "⑤ 分配明细（resources[*].tasks[*].days 即分配结果）\n"
            "⑥ 迭代轮数（rounds_used 照抄；None → 「未触发回压」）\n"
            "⑦ 收敛状态（converged=true → 「已收敛」；false → 「未完全收敛」；"
            "None → 「未触发回压」）\n"
            "present 为 false → 这一节一个字都不要写。"
        ),
    }


DAILY_RESOURCE_BILL_TITLE = "日级资源账单（回压分摊明细）"


def _daily_resource_bill_text(drb):
    """域 8.3：日级资源账单的确定性文本（看板 + Word 共用，一字不差）。

    7 项全覆盖：①每道 L4 的资源量 ②每天每种资源需求量 ③用户限额线
    ④超限日高亮 ⑤分配明细 ⑥迭代轮数 ⑦收敛状态
    """
    lines = [DAILY_RESOURCE_BILL_TITLE]

    # ⑥ 迭代轮数 / ⑦ 收敛状态
    rounds = drb.get("rounds_used")
    converged = drb.get("converged")
    cap = drb.get("cap")
    if rounds is not None:
        lines.append("回压迭代：%d 轮（上限 %d 轮）" % (int(rounds), int(cap or 3)))
    else:
        lines.append("回压迭代：未触发回压（用户未给限额或无超限资源）")
    if converged is True:
        lines.append("收敛状态：已收敛")
    elif converged is False:
        lines.append("收敛状态：未完全收敛（仍有超限资源，已采用超限额值）")
    else:
        lines.append("收敛状态：未触发回压")

    # ③ 用户限额线 + ④ 超限日高亮
    over = drb.get("over_limit") or []
    if over:
        # 分两类：普通超限 vs 突破限额（域 8.8③ 要求单列）
        normal_over = [r for r in over if not r.get("breached")]
        breached = [r for r in over if r.get("breached")]
        if normal_over:
            lines.append("")
            lines.append("超限记录（普通超限，共 %d 条）：" % len(normal_over))
            for rec in normal_over:
                lines.append("  第 %s 轮：%s 限额 %s → 峰值 %s"
                             % (str(rec.get("round") or "—"),
                                str(rec.get("resource") or "—"),
                                str(rec.get("limit") or "—"),
                                str(rec.get("peak") or "—")))
        if breached:
            lines.append("")
            lines.append("突破限额记录（共 %d 条）：" % len(breached))
            for rec in breached:
                lines.append("  第 %s 轮：%s 限额 %s → 峰值 %s 【突破限额】"
                             % (str(rec.get("round") or "—"),
                                str(rec.get("resource") or "—"),
                                str(rec.get("limit") or "—"),
                                str(rec.get("peak") or "—")))

    # ① 每道 L4 的资源量 + ② 每天每种资源需求量 + ⑤ 分配明细
    resources = drb.get("resources") or []
    if resources:
        lines.append("")
        lines.append("资源分配明细（共 %d 种资源）：" % len(resources))
        for res in resources[:10]:  # 最多列 10 种，避免刷屏
            lines.append("  【%s】" % str(res.get("resource") or "—"))
            for task in (res.get("tasks") or [])[:5]:  # 每种最多列 5 条任务
                days_str = "；".join("第%s天=%d" % (d["day"], d["count"])
                                    for d in (task.get("days") or [])[:10])
                if len(task.get("days") or []) > 10:
                    days_str += "…（共 %d 天）" % len(task["days"])
                lines.append("    %s：%s" % (str(task.get("task_id") or "—"), days_str))
            if len(res.get("tasks") or []) > 5:
                lines.append("    …（共 %d 条任务）" % len(res["tasks"]))

    bp_note = drb.get("note")
    if bp_note:
        lines.append("")
        lines.append("备注：%s" % str(bp_note))

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# 容量口径**两态**（域 1.6 收敛）：让用户看到"工期为什么不随工程量变"
# ------------------------------------------------------------
# `all_tasks_schedule[*].capacity_source` 是两态之一（计划端写的，交付侧只如实分流）：
#   "mwi"                     → 走 MWI 段容量（**正常态**）→ 一个字都不提；
#   "reported_missing"        → 缺容量数据 → **必须**打那句
#                               「本次工程量变化未反映到工期（缺容量数据）」。
# `capacity_basis` 是计划端写好的整段依据（很长），这里**不照抄**：只取语义 + 任务清单。
# ⚠ 单位一律规范形（m²/m³），绝不许 U+33A1 —— G5 收口断言会红。
# ══════════════════════════════════════════════════════════════════════════════
CAPACITY_SOURCE_MWI = "mwi"
# 域 1.6 已删除 Workface_Capacity_Rule 表（旧兜底态的唯一数据源），新计划只产出
# `mwi` / `reported_missing` 两态。旧计划 JSON 里若残留已删表的旧取值，一律落进
# `capacity_caliber_model` 的 `other` 分支**如实照抄**：既不硬映射成 `missing`、
# 也不设兼容常量 —— 验收判据是「全仓无旧兜底态字面量」，留一个常量就等于把
# 那条判据变成"注释/常量除外"，无法用一条 grep 复现。
# 实测依据：现存冻结档案 plan_run_1789827002 / 1789895021 / 1789911477.json 里
# 带 `capacity_source` 的条目 **0 条**（见 test_delivery_capacity_caliber.py），
# 这条兼容路径没有任何真实产物命中，删除它零成本。
CAPACITY_SOURCE_MISSING = "reported_missing"


def _capacity_source_rows(plan):
    """排程行 → ``{task_id: {task_id, task_name, source, basis}}``（无该字段 → 空表）。"""
    out = {}
    for r in (plan.get("all_tasks_schedule") or []):
        if not isinstance(r, dict) or r.get("task_id") is None:
            continue
        src = str(r.get("capacity_source") or "").strip()
        if not src:
            continue
        out[str(r["task_id"])] = {
            "task_id": str(r["task_id"]),
            "task_name": str(r.get("task_name") or ""),
            "source": src,
            "basis": str(r.get("capacity_basis") or ""),
        }
    return out


def capacity_caliber_model(plan, cap=10):
    """「容量口径」段落的数据模型（**两态分流**；全 `mwi` → 无话可说，返回空 lines）。

    判据只认计划里的 `capacity_source`，**绝不按任务名/关键字猜**。
    域 1.6 后只有 `mwi` / `reported_missing` 两态；旧计划若残留已删表的旧取值，
    落进 `other` 分支如实照抄（不合并进 `missing`、不设兼容常量）。
    """
    rows = _capacity_source_rows(plan)
    missing = [r for r in rows.values() if r["source"] == CAPACITY_SOURCE_MISSING]
    mwi = [r for r in rows.values() if r["source"] == CAPACITY_SOURCE_MWI]
    other = [r for r in rows.values()
             if r["source"] not in (CAPACITY_SOURCE_MWI, CAPACITY_SOURCE_MISSING)]
    missing.sort(key=lambda r: r["task_id"])
    lines = []
    if missing:
        # 那段长文案的**语义**（不照抄）：工期不随工程量变 + 原因缺容量数据 + 怎么办
        lines.append(
            "容量口径：下列 %d 条任务的工期不随工程量变化 —— 改工程量不会让这些任务的"
            "工期变长或变短，原因是本次缺容量数据（缺层面积 / MWI 表缺该资源 / "
            "resource_mobility 缺列 / 无定额锚定 / 用户也没给限额）。"
            "请补 MWI 行、层面积或用户同类限额后再看工期。" % len(missing))
        lines.append("涉及任务（列出前 %d 条）：%s%s"
                     % (len(missing[:cap]),
                        "；".join("%s %s" % (r["task_id"], r["task_name"] or "—")
                                  for r in missing[:cap]),
                        "" if len(missing) <= cap else
                        "（共 %d 条，完整清单见计划 JSON 的 "
                        "all_tasks_schedule[*].capacity_basis）" % len(missing)))
    if other:
        lines.append("另有 %d 条任务的容量来源写作未识别的取值（原样照抄，不硬套口径）：%s"
                     % (len(other), "、".join(sorted({r["source"] for r in other}))))
    return {
        "present": bool(rows),
        "lines": lines,
        "missing_count": len(missing), "fallback_count": 0,
        "mwi_count": len(mwi), "other_count": len(other),
        # 逐条只带身份信息：`capacity_basis` 是上游长文，**不进交付物也不进 facts**
        # （只取语义；照抄它既刷屏、又可能带着下游不需要的旧写法/单位）。
        "missing_tasks": [{"task_id": r["task_id"], "task_name": r["task_name"],
                           "source": r["source"]} for r in missing[:cap]],
        "fallback_tasks": [],  # 域 1.6 后不再有 fallback 态
        "ok": bool(rows) and not (missing or other),
    }


def _org_facts_capacity(plan):
    """E2 进 facts 的容量数据（与看板/Word 同一份 model，条数上限收敛避免刷爆 prompt）。"""
    rows = _org_rows(plan)
    cap = _org_capacity_model(rows, cap=20)
    return {
        "present": cap["present"], "count": cap["count"],
        "cap_header": cap["cap_header"], "cap_rows": cap["cap_rows"],
        "seg_header": cap["seg_header"], "seg_tables": cap["seg_tables"][:3],
        "basis": cap["basis"][:3], "notes": cap["notes"][:5],
    }


def _resource_card_html(plan, view, cal_h):
    """「资源计划」卡片的整段 HTML（确定性）。

    **为什么要抽成函数**（第 40 轮 · ③）：看板是 LLM 整页编排的（`html_page` 节点 →
    `build_plan_html_agent`），模型可以整段不写「依据 / 资源」「工作面容量口径」
    「主要机械峰值」—— 实测 `计划_plan_sample3_after_fix\\计划看板.html`（1128916 字节）
    里这三者的出现次数都是 **0**：`工期(天·排程)` 0、`WBS 目标` 0、`依据 / 资源` 0、
    `工作面容量` 0、`主要机械峰值` 0，关键路径表头只剩 6 列（序号/任务编号/任务名称/
    开始日期/完成日期/工期(天)），用户完全看不出交付物少了"说实话"的那几段。
    现在模型的产出漏了标记时，由 `_ensure_delivery_markers` 把这张卡片**追加**进去，
    复用同一个函数 → 追加段与确定性页面一字不差，不可能两处口径漂移。

    向后兼容：旧落盘的 `equipment_peak` 里混着机械配员（泵工/辅助/操作工/司机/信号工），
    与图表（走 `_compute_view` 的 equip_daily，已剔除配员）会自相矛盾 —— 摘要写"泵工 17 台"、
    图里却没有。展示层按同一口径过滤，并把这些配员并进"机械配员峰值"。
    """
    rp = _resource_plan_of(plan)
    # 设备/配员的分列口径统一走 `_split_equipment_peak`（Word 侧同源）
    _equip_items, _crew_items = _split_equipment_peak(rp)
    _ld = rp.get("labor_demand") or {}
    if not isinstance(_ld, dict) or not _ld:
        # 向后兼容：在 plan_assembler 把 labor_demand 并进 resource_plan **之前**落盘的旧计划，
        # 台班定额人工需求只存在于 meta.machine_labor_demand.demand 里。看板不该因为
        # 计划是旧的，就把「混凝土工」这类工种整个藏起来 —— 那正是用户抱怨的现象。
        _mld = plan.get("meta") if isinstance(plan.get("meta"), dict) else {}
        _mld = (_mld or {}).get("machine_labor_demand") or {}
        _ld = _mld.get("demand") if isinstance(_mld, dict) else {}
        _ld = _ld if isinstance(_ld, dict) else {}
    _trades = _trade_totals(view, _ld if isinstance(_ld, dict) else None)

    trade_table = "".join(
        f"<tr><td>{_html.escape(str(n))}</td><td>{k}</td>"
        f"<td>{'—' if p is None else p}</td><td>{t:g}</td></tr>"
        for n, p, t, k in _trades) or "<tr><td colspan=4>（无）</td></tr>"

    # 资源卡片上的"峰值人数"必须带口径。旧计划（没有 peak_manpower_source 键）退回老写法。
    # E1（用户 2026-09-21 裁定）：**「申报峰值：…」卡片段已整体删除** —— 那是把模型补的
    # 120 摆回用户面前（旧口径「把 120 人改标成『模型估算，非用户输入』」是更早一轮的要求，
    # 本次已重新裁定为"从源头删掉、不再有申报峰值项"）。卡片只留峰值人数的**口径**，
    # 口径名照样标出来（user → 用户给定上限；resource_curve → 资源曲线口径）。
    if cal_h is not None:
        _peak_card = (f"<b>峰值人数：</b>{_fnum(cal_h['peak'])} 人"
                      f"（<b>{_html.escape(cal_h['label'])}</b>）　|　")
    else:
        _peak_card = f"<b>峰值人数：</b>{rp.get('peak_manpower', '—')} 人　|　"

    # D1：班组人数口径。数据全在计划里（`resource_demand.tasks` 的 `_workface_note` /
    # `_workface_capped` / `_organization_crew` / `_peak_shaving_skipped`）—— 两个真源
    # 都要报，且"本该削峰却没削"绝不许静默（见 `_workface_summary` docstring）。
    _wf_html = ""
    _wf_txt = _workface_sentence(plan)
    if _wf_txt:
        _wfs = _workface_summary(plan)
        _wf_ex = "".join(
            f"<tr><td>{_html.escape(str(a))}</td><td>{_html.escape(str(b))}</td>"
            f"<td>{_html.escape(str(c))}</td></tr>" for a, b, c in _wfs["examples"])
        _below = _workface_below_org_lines(_wfs)
        _wf_below = (f"<p>{_html.escape(_workface_below_org_lead(_wfs))}</p>"
                     + "<details><summary>两套「每面上限」不同源的行（%d 条）</summary><ul>%s</ul>"
                     "</details>" % (len(_below),
                                     "".join("<li>%s</li>" % _html.escape(x) for x in _below))
                     ) if _below else ""
        _shave = _workface_peak_shaving_sentence(_wfs)
        _wf_html = (
            f"<div class='rs-row'><b>工作面容量口径：</b>{_html.escape(_wf_txt)}"
            + (f"<details><summary>查看逐条（原始 → 上限）</summary>"
               f"<table><tr><th>任务（资源）</th><th>原始 → 上限</th><th>原因</th></tr>"
               f"{_wf_ex}</table></details>" if _wf_ex else "")
            + _wf_below
            + (f"<p>{_html.escape(_shave)}</p>" if _shave else "")
            + "</div>")

    # 峰值两条路径的差额归因（**独立于"工作面容量"那一段**：有没有工作面容量数据都得写）
    _pcd = _peak_curve_diff(plan, view)
    _pcd_html = (f"<div class='rs-row'>{_html.escape(_pcd['sentence'])}</div>"
                 if _pcd else "")

    # 场地级设备（塔吊/施工电梯）口径：与 Word 同一句话（`_site_equipment_sentence`）。
    _site_html = ""
    _site_txt = _site_equipment_sentence(plan)
    if _site_txt:
        _site_s = _site_equipment_summary(plan)
        _site_rows_html = "".join(
            f"<tr><td>{_html.escape(i['name'])}</td><td>{_fnum(i['quantity'])}</td>"
            f"<td>{_html.escape(i['source_text'])}</td><td>{_html.escape(i['crew'])}</td>"
            f"<td>{'kb:Equipment_Crew_Mapping' if i['crew_source'] == 'kb' else '代码兜底默认'}</td>"
            f"<td>{i['hit_tasks']}</td></tr>" for i in _site_s["items"])
        _site_html = (
            f"<div class='rs-row'><b>场地级常驻设备（垂直运输）：</b>{_html.escape(_site_txt)}"
            "<details><summary>查看逐设备来源</summary>"
            "<table><tr><th>设备</th><th>台数</th><th>台数来源</th><th>配员</th>"
            "<th>配员来源</th><th>命中任务数</th></tr>"
            f"{_site_rows_html}</table></details></div>")

    # D5（看板侧）：设备清单的**三态**（第 44 轮）。
    # 老计划（没有 `boundary_conditions._source`）→ 与修复前一字不差的既有对账块；
    # 用户申报 → 同一段对账块；模型补的估算 / 清单为空 → 由 `_equipment_declared_block_html`
    # 给出如实文案（空清单**不许**写成"用户没申报"，见本节顶部注释）。
    _eb_html = _equipment_declared_block_html(plan)

    return (
        "<div class='card'><h2>资源计划</h2>"
        "<div class='rs-row'>"
        f"<b>总人工日：</b>{rp.get('total_manpower_days', '—')} 人·日　|　"
        f"{_peak_card}"
        f"<b>每日用工峰值（按任务叠加）：</b>{view['peak_total']} 人　|　"
        f"<b>峰值工种：</b>{_html.escape(str(view['peak_trade'] or '—'))}"
        "</div>"
        f"{_wf_html}"
        f"{_pcd_html}"
        f"{_site_html}"
        f"<div class='rs-row'><b>主要机械峰值：</b>"
        f"{_html.escape('，'.join(f'{k} {v}台' for k, v in _equip_items) or '无')}</div>"
        f"<div class='rs-row'><b>机械配员峰值（人）：</b>"
        f"{_html.escape('，'.join(f'{k} {v}人' for k, v in _crew_items) or '无')}</div>"
        # 【第 2 批 · 域 2 / 2.6】原「主要材料」一行已删除（材料清单不再展示）；
        # 交付物改为在最上方印一句 `MATERIALS_EXCLUDED_NOTICE`（与 Word 同源）。
        # 容量口径两态（域 1.6 收敛）：全 mwi 时 `_cc_lines` 为空 → 一个字都不多印
        + _capacity_caliber_html(plan)
        + _daily_resource_bill_html(plan)
        + (f"<div class='rs-row'><b>台班定额人工需求：</b>"
           f"{_html.escape('，'.join(f'{k} {round(float(v), 1)} 工日' for k, v in _ld.items()))}</div>"
           if isinstance(_ld, dict) and _ld else "")
        + _eb_html
        + "<h2 style='margin-top:16px'>分工种人工需求</h2>"
        "<div class='chart-lbl'>「工种 / 机械配员」按每日在场人数叠加；"
        "「台班定额」由台班产量定额反算，只有工日总量、无逐日分布，故峰值列为「—」。</div>"
        "<table><tr><th>资源</th><th>类别</th><th>峰值(人)</th><th>总工日</th></tr>"
        f"{trade_table}</table>"
        f"<details><summary>查看原始 resource_plan JSON</summary>"
        f"<pre>{_html.escape(json.dumps(_normalized_resource_plan(rp), ensure_ascii=False, indent=2))}</pre></details>"
        "</div>"
    )


def _capacity_caliber_html(plan):
    """容量口径两态的看板 HTML（全 `mwi` / 无数据 → 返回空串，一个字都不多印）。"""
    lines = capacity_caliber_model(plan)["lines"]
    if not lines:
        return ""
    return "".join("<div class='rs-row'>%s</div>" % _html.escape(x) for x in lines)


def _daily_resource_bill_html(plan):
    """域 8.3：日级资源账单的看板 HTML（与 Word 共用 `_daily_resource_bill_text`，一字不差）。"""
    drb = _daily_resource_bill_model(plan)
    if not drb.get("present"):
        return ""
    text = _daily_resource_bill_text(drb)
    return ("<div class='card'><h2>%s</h2><pre style='white-space:pre-wrap;"
            "font-size:13px'>%s</pre></div>"
            % (_html.escape(DAILY_RESOURCE_BILL_TITLE, quote=False),
               _html.escape(text, quote=False)))


def _normalized_resource_plan(rp):
    """「原始 JSON」展示块的**单位归一**副本（其余字段逐字不动）。

    为什么必须有这一步：这一块是 `resource_plan` 的**逐字 dump**，旧计划里
    `material_summary[*].unit` 还是 `U+33A1`(U+33A1) —— 直接 dump 就把 CJK 兼容方块字
    印进了看板，G5（验收 §6#5）要求产物里 0 处 U+33A1。这里只把 `unit` 过一道
    `_normalized_unit_text`（**不删字段、不改数字**）。

    【第 2 批 · 域 2 / 2.6】新计划不再产出 `material_summary`，但**这一支保留**：
    历史计划 JSON 里那个键还在，直接 dump 仍会把 U+33A1 印进看板 —— G5 是硬断言，
    与"我们要不要展示材料清单"无关。**不展示**由上面删掉的那一行保证（两件事分开）。
    """
    if not isinstance(rp, dict):
        return rp
    out = dict(rp)
    mats = out.get("material_summary")
    if isinstance(mats, list):
        new = []
        for m in mats:
            if isinstance(m, dict) and m.get("unit"):
                m = dict(m, unit=_normalized_unit_text(m.get("unit")))
            new.append(m)
        out["material_summary"] = new
    return out


def _evidence_table_html(plan):
    """任务级「依据 / 资源」表（确定性，`_evidence_text` 的表格形态）。

    与关键路径明细/WBS 表**同一函数、同一数据源**：`_tasks` + `_schedule_span` +
    `_rd_task_map`。LLM 整页编排时经常会把这列整段省掉（实测就是如此），本表是
    ③ 的追加段里"依据"的落地形态。
    """
    rd_map = _rd_task_map(plan)
    leaf_map = _leaf_bindings(plan)   # 换算参数来源键只挂 WBS 叶子（见 _evidence_core）
    span = _schedule_span(plan)
    rows = []
    for t in _tasks(plan):
        if not isinstance(t, dict):
            continue
        tid = str(t.get("task_id"))
        st, fin = t.get("start_date"), t.get("finish_date")
        rows.append((tid, t.get("task_name") or "—",
                     "—" if not st and not fin else "%s ~ %s" % (st or "—", fin or "—"),
                     span.get(tid),
                     _evidence_text(rd_map.get(tid), leaf_map.get(tid))))
    body = "".join(
        f"<tr><td>{_html.escape(str(a))}</td><td>{_html.escape(str(b))}</td>"
        f"<td>{_html.escape(str(c))}</td><td>{'—' if d is None else _html.escape(str(d))}</td>"
        f"<td>{_html.escape(str(e))}</td></tr>"
        for a, b, c, d, e in rows) or "<tr><td colspan=5>（无）</td></tr>"
    # 政策变更（2026-09-20）：AI 经验估算定额从"只作参考"改为照用，看板上必须同口径
    # 解释这一列的新文案（Word 依据列与这里共用 `_evidence_text`，一字不差）。
    _lead = ("依据 / 资源 来自 resource_demand："
             "「单位换算按<换算参数来源>…」= 单位不一致但已按**写明来源**的换算参数换算并算出"
             "班组与工日（来源逐条标出：定额条件档位 / AI 估算 / 用户给定 / 来源未记录）；"
             "「%s」= 该任务的工期/班组依据取自 AI 经验估算定额（KB AI_ESTIMATE_V1，"
             "无规范依据），已按 2026-09-20 政策照用并逐条标注；"
             "「⚠ 无可用定额…」= 工期沿用 WBS 估算、未计算班组。" % AI_NORM_LABEL)
    return (
        "<div class='card'><h2>任务依据 / 资源（交付口径）</h2>"
        "<div class='chart-lbl'>" + _lead + "</div>"
        "<table><tr><th>ID</th><th>任务</th><th>排程起止</th><th>工期(天·排程)</th>"
        "<th>依据 / 资源</th></tr>"
        f"{body}</table></div>")


def _delivery_appendix_html(plan, view, cal_h=None):
    """LLM 编排页面的**追加确定性段**（③ 的结构性保证，`_ensure_delivery_markers` 用）。

    只在模型漏掉关键标记时才插入；内容是复用既有确定性渲染函数拼出来的
    （资源计划卡 + 依据表 + 降级/假定清单），**不复制粘贴**那段 HTML 源码。
    """
    if cal_h is None:
        cal_h = _peak_caliber(plan, view)
    return (
        "<!-- 追加确定性口径段：LLM 编排页缺「依据 / 资源」「工作面容量」「主要机械峰值」，"
        "由 delivery._ensure_delivery_markers 用确定性渲染补齐 -->\n"
        "<div class='card'><h2>交付口径与依据（确定性渲染，由系统追加）</h2>"
        "<div class='chart-lbl'>本页由模型编排生成，以下内容取自计划数据的确定性渲染，"
        "用于保证资源口径、依据来源与降级清单不被省略。</div></div>\n"
        + _resource_card_html(plan, view, cal_h) + "\n"
        + _evidence_table_html(plan) + "\n"
        + _org_section_html(plan, view) + "\n"
        + _norm_lists_card_html(plan))


def _norm_lists_card_html(plan):
    """降级清单 / 已按 AI 假定换算清单的确定性卡片（与 Word 第 5、5b 节同一口径）。

    终版修改（WS3）追加两段（合同 §9.3 / §9.4）：
      · **口径换算留痕**（`norm_binding.basis_adjust` / `basis_unconfirmed`，由 WS1 写）
        —— 合同要求列在「定额降级/口径」区块里；
      · **人工覆盖留痕**（D7 覆盖文件）。
    两个新数据源都取不到时，本函数与改造前**逐字一致**（连早期返回都一样）。
    """
    degraded, dtotal = _norm_degradations(plan)
    assumed, atotal = _norm_assumed_rows(plan)
    basis_rows, basis_total = _norm_basis_adjust_rows(plan, cap=50)
    ov_rows, ov_src = _norm_overrides(plan)
    if not dtotal and not atotal and not basis_total and not ov_rows:
        return ""
    out = ["<div class='card'><h2>单位与定额口径</h2>"]
    if dtotal:
        rows = "".join(
            f"<tr><td>{_html.escape(r['task_id'])}</td><td>{_html.escape(r['task_name'])}</td>"
            f"<td>{_html.escape(r['reason'])}</td></tr>" for r in degraded)
        out.append("<h3>%s</h3><div class='chart-lbl'>%s</div>"
                   "<table><tr><th>任务 ID</th><th>任务</th><th>原因</th></tr>%s</table>"
                   % (_html.escape(NORM_DEGRADED_TITLE),
                      _html.escape(_degraded_lead(degraded)), rows))
        if dtotal > len(degraded):
            out.append("<div class='chart-lbl'>（共 %d 条，此处列出前 %d 条）</div>"
                       % (dtotal, len(degraded)))
    if atotal:
        rows = "".join(
            f"<tr><td>{_html.escape(r['task_id'])}</td><td>{_html.escape(r['task_name'])}</td>"
            f"<td>{_html.escape(_assumed_source_cell(r))}</td>"
            f"<td>{_html.escape(r['reason'])}</td></tr>" for r in assumed)
        _as_lead = assumed_section_lead(assumed)
        out.append("<h3>%s</h3><div class='chart-lbl'>%s</div>"
                   "<table><tr><th>任务 ID</th><th>任务</th><th>换算参数来源</th>"
                   "<th>换算过程与结果（依据列原文）</th></tr>%s</table>"
                   % (_html.escape(assumed_section_title(assumed)), _html.escape(_as_lead), rows))
        if atotal > len(assumed):
            out.append("<div class='chart-lbl'>（共 %d 条，此处列出前 %d 条）</div>"
                       % (atotal, len(assumed)))
        _as_foot = assumed_section_foot(assumed)
        if _as_foot:
            out.append("<div class='chart-lbl'>%s</div>" % _html.escape(_as_foot))
    if basis_total:
        rows = "".join(
            "<tr><td>%s</td><td>%s</td><td>%s</td></tr>"
            % (_html.escape(str(r["task_id"])), _html.escape(str(r["task_name"])),
               _html.escape(_norm_basis_adjust_text(r))) for r in basis_rows)
        out.append("<h3>%s</h3><div class='chart-lbl'>%s</div>"
                   "<table><tr><th>任务 ID</th><th>任务</th><th>留痕</th></tr>%s</table>"
                   % (_html.escape(NORM_BASIS_TITLE), _html.escape(NORM_BASIS_LEAD), rows))
        if basis_total > len(basis_rows):
            out.append("<div class='chart-lbl'>（共 %d 条，此处列出前 %d 条）</div>"
                       % (basis_total, len(basis_rows)))
    if ov_rows:
        _oh, _orows = _norm_override_grid_rows(ov_rows)
        rows = "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % _html.escape(str(c))
                                               for c in r) for r in _orows)
        out.append("<h3>%s</h3><div class='chart-lbl'>%s</div>"
                   "<table><tr>%s</tr>%s</table>"
                   % (_html.escape(NORM_OVERRIDE_TITLE),
                      _html.escape(NORM_OVERRIDE_LEAD % ov_src),
                      "".join("<th>%s</th>" % _html.escape(str(h)) for h in _oh), rows))
    out.append("</div>")
    return "".join(out)


def _norm_missing_card_html(plan, cap=50):
    """D4 无定额依据工序的确定性卡片（看板追加用；与 Word 第 7 节同一份行数据）。"""
    rows, total = _norm_missing_rows(plan, cap=cap)
    if not total:
        return ""
    e = _html.escape
    body = "".join(
        "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
        % (e(str(r["task_id"])), e(str(r["task_name"])), e(str(r["tier"])), e(r["sentence"]))
        for r in rows) or "<tr><td colspan=4>（无）</td></tr>"
    more = ("<div class='chart-lbl'>（共 %d 条，此处列出前 %d 条）</div>"
            % (total, len(rows))) if total > len(rows) else ""
    return ("<div class='card'><h2>%s</h2><div class='chart-lbl'>%s</div>"
            "<table><tr><th>任务 ID</th><th>任务</th><th>来源档次</th>"
            "<th>无定额依据说明</th></tr>%s</table>%s"
            "<div class='chart-lbl'>%s</div></div>"
            % (e(NORM_MISSING_TITLE), e(NORM_MISSING_LEAD), body, more,
               e(NORM_MISSING_FOOT)))


def _norm_tier_card_html(plan, view=None, cap=50):
    """D6 来源档次 + 覆盖率 / 5d 口径换算留痕 / D7 人工覆盖留痕的确定性卡片。"""
    e = _html.escape
    counts, total = _norm_tier_counts(plan)
    cov_txt, cov_live = _norm_coverage_display(plan, None, view)
    basis_rows, basis_total = _norm_basis_adjust_rows(plan, cap=cap)
    ov_rows, ov_src = _norm_overrides(plan)
    if not (total or cov_txt or basis_total or ov_rows):
        return ""
    out = ["<div class='card'><h2>%s</h2>" % e(NORM_TIER_TITLE)]
    if total:
        _tiers = [t for t in NORM_TIER_ORDER if counts.get(t)]
        rows = "".join("<tr><td>%s</td><td>%d</td><td>%.1f%%</td></tr>"
                       % (e(t), counts[t], 100.0 * counts[t] / total) for t in _tiers)
        out.append("<h3>定额来源档次（逐条见进度计划表「依据 / 资源」列的档次前缀）</h3>"
                   "<table><tr><th>来源档次</th><th>工序数</th><th>占比</th></tr>%s</table>"
                   "<div class='chart-lbl'>%s</div>" % (rows, e(_norm_tier_lead(counts))))
    if cov_txt:
        out.append("<div class='rs-row'><b>%s：</b>%s</div>"
                   % (e(NORM_COVERAGE_LABEL), e(cov_txt)))
        if cov_live and cov_live.get("critical_days") is not None:
            _ai_note = ("；「%s」无规范依据，不计入分子。" % NORM_TIER_AI
                        if counts.get(NORM_TIER_AI) else "")
            out.append("<div class='chart-lbl'>%s%s</div>"
                       % (e(_norm_coverage_caliber_note(cov_live)), e(_ai_note)))
    if basis_total:
        rows = "".join("<tr><td>%s</td><td>%s</td><td>%s</td></tr>"
                       % (e(str(r["task_id"])), e(str(r["task_name"])),
                          e(_norm_basis_adjust_text(r))) for r in basis_rows)
        out.append("<h3>%s</h3><div class='chart-lbl'>%s</div>"
                   "<table><tr><th>任务 ID</th><th>任务</th><th>留痕</th></tr>%s</table>"
                   % (e(NORM_BASIS_TITLE), e(NORM_BASIS_LEAD), rows))
    if ov_rows:
        _oh, _orows = _norm_override_grid_rows(ov_rows)
        rows = "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % e(str(c)) for c in r)
                       for r in _orows)
        out.append("<h3>%s</h3><div class='chart-lbl'>%s</div><table><tr>%s</tr>%s</table>"
                   % (e(NORM_OVERRIDE_TITLE), e(NORM_OVERRIDE_LEAD % ov_src),
                      "".join("<th>%s</th>" % e(str(h)) for h in _oh), rows))
    out.append("</div>")
    return "".join(out)


# D4 / D6 的**独立**结构性保证（合同 §9：每个保证有独立判据，不往 DELIVERY_MARKERS /
# CONFIDENCE_MARKERS 里加东西 —— 那两个元组都有既有回归门锁着）。
NORM_MISSING_FALLBACK_COMMENT = "<!-- 追加无定额依据段"
NORM_TIER_FALLBACK_COMMENT = "<!-- 追加定额来源档次段"


def _norm_tier_markers_missing(html_text, plan, view=None):
    """缺哪些档次/覆盖率/口径留痕/覆盖留痕标记 —— **只对"计划确实有这份数据"的项判**。

    与 `_confidence_markers` 同一套做法：某份数据本身不存在（如 WS1 还没写 `basis_adjust`）
    就不把它算作缺失，否则任何页面都永远"缺标记"，保证函数会一直追加。
    """
    low = html_text or ""
    missing = []
    _counts, total = _norm_tier_counts(plan)
    if total and "定额来源档次" not in low:
        missing.append("定额来源档次")
    cov_txt, _live = _norm_coverage_display(plan, None, view)
    if cov_txt and NORM_COVERAGE_LABEL not in low:
        missing.append(NORM_COVERAGE_LABEL)
    _basis, basis_total = _norm_basis_adjust_rows(plan)
    if basis_total and NORM_BASIS_TITLE not in low:
        missing.append(NORM_BASIS_TITLE)
    _ov, _src = _norm_overrides(plan)
    if _ov and NORM_OVERRIDE_TITLE not in low:
        missing.append(NORM_OVERRIDE_TITLE)
    return tuple(missing)


def _ensure_norm_missing_section(html_text, plan):
    """D4 的结构性保证：计划里确有"无定额依据"的工序，而页面没写 → 追加确定性卡片。

    判据是**数据**（`_norm_missing_rows` 的条数 > 0）+ 页面缺 `NORM_MISSING_MARKER`
    （"本行无定额依据"）。一条都没有 → 什么都不做、也不算"追加过"。
    返回 ``(html_text, 是否追加)``；追加失败保持原页面不动（宁缺勿造）。
    """
    try:
        _rows, total = _norm_missing_rows(plan)
    except Exception:
        return html_text, False
    if not total:
        return html_text, False
    if NORM_MISSING_MARKER in (html_text or ""):
        return html_text, False
    try:
        card = _norm_missing_card_html(plan)
        if not card or NORM_MISSING_MARKER not in card:
            return html_text, False
        low = html_text.lower()
        pos = low.rfind("</body>")
        merged = (html_text[:pos] + NORM_MISSING_FALLBACK_COMMENT + " -->\n" + card + "\n"
                  + html_text[pos:]) if pos >= 0 else (html_text + "\n" + card)
        if NORM_MISSING_MARKER not in merged:
            raise ValueError("追加后仍缺标记")
        return merged, True
    except Exception:
        return html_text, False


def _ensure_norm_tier_section(html_text, plan, view=None):
    """D6（来源档次 + 覆盖率）/ 5d（口径换算留痕）/ D7（人工覆盖留痕）的结构性保证。

    这些内容模型不可能自己编对（档次判据在 `_norm_tier_of`，覆盖率要按排程日期算），
    所以按**独立判据**逐项检查：计划有该项数据、页面又没有该标记 → 追加确定性卡片。
    返回 ``(html_text, 是否追加)``。
    """
    try:
        missing = _norm_tier_markers_missing(html_text, plan, view)
    except Exception:
        return html_text, False
    if not missing:
        return html_text, False
    try:
        card = _norm_tier_card_html(plan, view)
        if not card:
            return html_text, False
        if _norm_tier_markers_missing(card, plan, view):
            raise ValueError("确定性段落仍缺标记：%s" % (missing,))
        low = html_text.lower()
        pos = low.rfind("</body>")
        merged = (html_text[:pos] + NORM_TIER_FALLBACK_COMMENT + " -->\n" + card + "\n"
                  + html_text[pos:]) if pos >= 0 else (html_text + "\n" + card)
        if _norm_tier_markers_missing(merged, plan, view):
            raise ValueError("追加后仍缺标记：%s" % (missing,))
        return merged, True
    except Exception:
        return html_text, False


# LLM 编排页面**必须**出现的三个关键标记（③ 的校验判据）。
# 为什么是这三个：它们分别代表"班组/资源从哪来"（依据）、"人数为什么这么少"（工作面容量）、
# "设备峰值口径"（机械峰值）—— 实测模型产出这三者的出现次数都是 0。
DELIVERY_MARKERS = ("依据", "工作面容量", "主要机械峰值")
DELIVERY_FALLBACK_COMMENT = "<!-- 追加确定性口径段"

# 施工组织层口径的**独立**结构性保证（等价于把标记加进 DELIVERY_MARKERS，但不破坏
# 既有回归门「三个旧标记齐 → 页面一字不动」的语义 —— 那条断言钉的就是原元组）。
# 「施工组织口径」「组织缺口」分别代表"工期是怎么来的"和"组织上缺什么"。
ORG_MARKERS = ("施工组织口径", "组织缺口")
ORG_FALLBACK_COMMENT = "<!-- 追加施工组织口径段"


def _org_markers_missing(html_text):
    """模型产出的 HTML 里缺了哪些施工组织层标记（空 = 齐全）。"""
    low = html_text or ""
    return tuple(m for m in ORG_MARKERS if m not in low)


def _ensure_org_section(html_text, plan, view):
    """施工组织层口径的结构性保证：模型页面丢了这段 → 追加确定性段落。

    返回 ``(html_text, 是否追加)``。追加失败时**保持原页面不动**（宁缺勿造），
    由调用方把痕迹写进节点状态。判据与 `DELIVERY_MARKERS` 独立，互不影响。
    """
    missing = _org_markers_missing(html_text)
    if not missing:
        return html_text, False
    try:
        section = _org_section_html(plan, view)
        if _org_markers_missing(section):
            raise ValueError("确定性段落仍缺标记：%s" % (missing,))
        low = html_text.lower()
        pos = low.rfind("</body>")
        merged = (html_text[:pos] + ORG_FALLBACK_COMMENT + " -->\n" + section + "\n"
                  + html_text[pos:]) if pos >= 0 else \
                 (html_text + "\n" + section)
        if _org_markers_missing(merged):
            raise ValueError("追加后仍缺标记：%s" % (missing,))
        return merged, True
    except Exception:
        return html_text, False


def _materials_notice_html():
    """材料计划声明的看板形态（一个 card + 一行文字，与 `materials_badge` 同源）。

    单独一个函数是为了让"结构性保证"能复用它 —— 见 `_ensure_materials_notice`。

    ⚠️ 刻意用 `_html.escape(..., quote=False)`：这段是**我们自己写死的常量**（不含
    `<` / `>` / `&`），而默认 `quote=True` 会把句子里的 `"管够"` 转成 `&quot;管够&quot;`
    —— 于是"这句话在不在产物里"的判据（验收要求：文本存在）在 HTML 上就**搜不到原文**了。
    文本节点里的引号本来也不需要转义（只有属性值才需要）。
    """
    return ("<div class='card' style='padding:10px 16px'>"
            "<span style='color:#3a4a63;font-size:13px'>%s</span></div>"
            % _html.escape(MATERIALS_EXCLUDED_NOTICE, quote=False))


def _materials_notice_missing(html_text):
    """模型产出的 HTML 里有没有那句材料计划声明（True = 缺）。"""
    return MATERIALS_NOTICE_MARKER not in (html_text or "")


def _ensure_materials_notice(html_text):
    """③ 的结构性保证（独立判据）：LLM 编排页面没写材料计划声明 → 追加确定性一段。

    为什么要单独一个保证、而不是把标记加进 `DELIVERY_MARKERS`：那个元组有既有回归门
    锁着「三个旧标记齐 → 页面一字不动」的语义（同 `ORG_MARKERS` / `CONFIDENCE_MARKERS`
    的做法，见那边的说明）。模型不可能"猜到"我们要删材料清单 —— 提示词只是"希望"，
    这里做的是"保证"。

    返回 ``(html_text, 是否追加)``。追加失败**保持原页面不动**（宁缺勿造），
    由调用方把痕迹写进节点状态。
    """
    if not _materials_notice_missing(html_text):
        return html_text, False
    try:
        card = _materials_notice_html()
        if _materials_notice_missing(card):
            raise ValueError("确定性声明仍缺标记：%s" % MATERIALS_NOTICE_MARKER)
        low = html_text.lower()
        pos = low.rfind("</body>")
        merged = (html_text[:pos] + MATERIALS_NOTICE_FALLBACK_COMMENT + " -->\n" + card
                  + "\n" + html_text[pos:]) if pos >= 0 else (html_text + "\n" + card)
        if _materials_notice_missing(merged):
            raise ValueError("追加后仍缺标记：%s" % MATERIALS_NOTICE_MARKER)
        return merged, True
    except Exception:
        return html_text, False


# 「数据来源与置信度」章节的**独立**结构性保证（与 ORG_MARKERS 同一套做法：不往
# DELIVERY_MARKERS 里加东西 —— 那个元组有既有回归门锁着「三个旧标记齐 → 页面一字不动」）。
# 第一个标记是章节标题：模型页面只要真写了这一段就一定带。
# 第二个标记「定额覆盖率」只在**该计划确实有覆盖率数据**时才作为判据：否则一份只有
# 来源构成 / 两版工期的计划永远补不上这一段（Word 里那几节照印，看板也要照印）。
CONFIDENCE_TITLE = "数据来源与置信度"
CONFIDENCE_MARKERS = (CONFIDENCE_TITLE, "定额覆盖率")
CONFIDENCE_FALLBACK_COMMENT = "<!-- 追加置信度口径段"
# 逐条清单（未绑定原因 / 来源代码 / 降级与假定换算清单）行数达到这个量就折进 <details>。
# 关键数字（总数 / 已绑定 / 未绑定 / 其中未审定放行条数）走 kv，永远留在可见处。
CONFIDENCE_DETAIL_MIN_ROWS = 3
CONFIDENCE_CARD_LEAD = ("本节与 Word 交付物同一真源（同一份 confidence_section_blocks）："
                        "所有数字取自计划本身，缺字段就整行不出；逐条清单折进展开区。")


def _confidence_markers(plan):
    """该计划下「置信度段在不在」的判据标记（见上方常量注释）。"""
    marks = [CONFIDENCE_TITLE]
    if not isinstance(plan, dict):
        return tuple(marks)
    meta = plan.get("meta") if isinstance(plan.get("meta"), dict) else {}
    cov = meta.get("norm_coverage") if isinstance(meta.get("norm_coverage"), dict) else {}
    if cov.get("total"):
        marks.append(CONFIDENCE_MARKERS[1])
    return tuple(marks)


def _confidence_markers_missing(html_text, plan):
    """模型产出的 HTML 里缺了哪些置信度章节标记（空 = 齐全）。"""
    low = html_text or ""
    return tuple(m for m in _confidence_markers(plan) if m not in low)


def _conf_md_bold_html(text):
    """源文案里的 **强调** → <b>（其余逐段转义）。只改排版，一个字都不改、不新造。"""
    parts = str(text).split("**")
    return "".join((_html.escape(p) if i % 2 == 0 else "<b>%s</b>" % _html.escape(p))
                   for i, p in enumerate(parts) if p)


def _confidence_kv_table_html(rows):
    """kv 块 → 两列小表（标签 / 数值）。数值可能是长句，所以用表格而不是 19px 的 kv 格子。"""
    e = _html.escape
    body = "".join("<tr><td>%s</td><td><b>%s</b></td></tr>" % (e(str(k)), e(str(v)))
                   for k, v in rows)
    return "<table><tr><th>项目</th><th>数值</th></tr>%s</table>" % body


def _confidence_grid_table_html(headers, rows):
    """grid 块 → 表格（表头 / 行原样来自块本身）。"""
    e = _html.escape
    head = "".join("<th>%s</th>" % e(str(h)) for h in headers)
    body = "".join("<tr>" + "".join("<td>%s</td>" % e(str(c)) for c in row) + "</tr>"
                   for row in rows)
    return "<table><tr>%s</tr>%s</table>" % (head, body)


def _confidence_section_html(plan, view):
    """看板「数据来源与置信度」卡片（确定性）—— 与 Word 章节**同一真源**。

    为什么直接遍历 `confidence_section_blocks`：交付物的数字必须只有一个出处，Word 与
    看板口径漂移正是用户实测抱怨过的事。这里只做**排版**映射，不新造任何内容：
      · h3   → <h3>（小节标题）
      · para → <div class='rs-row'>（** 强调 ** → <b>）
      · kv   → 两列小表（项目 / 数值）—— 关键数字直接可见
      · grid → 表格；行数 ≥ `CONFIDENCE_DETAIL_MIN_ROWS` 的逐条清单折进 <details>
               （summary 写「共 N 条，展开查看」，N 就是块里真实的行数）
    判据与 Word **完全同一判据**：`has_confidence_meta(plan)` 为假、或一块都算不出来 →
    返回 ""（一个字都不加，不留空壳、不留占位）。Word 侧同一判据在
    `add_confidence_section` 里，两边**同一份输入必须同进同出**（政策变更 2026-09-20：
    "有 AI 定额任务但计划没带七项 meta"时，两边都要出这一章，条数才有处可写）。
    """
    if not has_confidence_meta(plan):
        return ""
    try:
        blocks = confidence_section_blocks(plan, view)
    except Exception:
        return ""
    if not blocks:
        return ""
    e = _html.escape
    out = ["<div class='card'><h2>%s</h2>" % e(CONFIDENCE_TITLE),
           "<div class='chart-lbl'>%s</div>" % e(CONFIDENCE_CARD_LEAD)]
    for kind, payload in blocks:
        if kind == "h3":
            out.append("<h3>%s</h3>" % e(str(payload)))
        elif kind == "para":
            out.append("<div class='rs-row'>%s</div>" % _conf_md_bold_html(payload))
        elif kind == "kv":
            out.append(_confidence_kv_table_html(payload))
        elif kind == "grid":
            headers, rows = payload[0], payload[1]
            table = _confidence_grid_table_html(headers, rows)
            if len(rows) >= CONFIDENCE_DETAIL_MIN_ROWS:
                out.append("<details><summary>共 %d 条，展开查看</summary>%s</details>"
                           % (len(rows), table))
            else:
                out.append(table)
    out.append("</div>")
    return "".join(out)


def _ensure_confidence_section(html_text, plan, view):
    """置信度章节的结构性保证：模型页面丢了这段 → 追加确定性卡片。

    返回 ``(html_text, 是否追加)``。计划本身没有置信度元数据（`has_confidence_meta` 为假）
    时**什么都不做**，也不算「追加过」—— 与 Word 同口径：没数据就一个字不加。
    追加失败时保持原页面不动（宁缺勿造），由调用方把痕迹写进节点状态。
    """
    missing = _confidence_markers_missing(html_text, plan)
    if not missing:
        return html_text, False
    try:
        section = _confidence_section_html(plan, view)
        if not section:
            return html_text, False
        if _confidence_markers_missing(section, plan):
            raise ValueError("确定性段落仍缺标记：%s" % (missing,))
        low = html_text.lower()
        pos = low.rfind("</body>")
        merged = (html_text[:pos] + CONFIDENCE_FALLBACK_COMMENT + " -->\n" + section + "\n"
                  + html_text[pos:]) if pos >= 0 else (html_text + "\n" + section)
        if _confidence_markers_missing(merged, plan):
            raise ValueError("追加后仍缺标记：%s" % (missing,))
        return merged, True
    except Exception:
        return html_text, False


def _equipment_marker(plan):
    """设备三态"这句实话在页面上"的判据标记；老计划 → `None`（不判、不动页面）。"""
    state, _items, _src = equipment_declared_state(plan)
    if state is None:
        return None
    if state == "user":
        return EQUIPMENT_SECTION_TITLE
    if state == "model":
        return EQUIPMENT_MODEL_MARKER
    if state == "empty":
        return EQUIPMENT_EMPTY_MARKER
    return EQUIPMENT_UNMARKED_MARKER


def _ensure_equipment_section(html_text, plan):
    """设备清单三态的结构性保证：模型页面缺那句实话 → 追加确定性段落。

    为什么必须保证（真实产物实证）：模型把这一节写成了「本计划无用户申报设备限额
    （equipment_binding 为空），所有设备台数均为 AI 默认口径」—— 把"本次没取得设备清单"
    说成"用户没申报"。只往 facts 里补数据拦不住它（模型照样能自由发挥），所以这里保证
    那句话一定在页面上。返回 ``(html_text, 是否追加)``；老计划（无来源标注）**不动作**。
    """
    marker = _equipment_marker(plan)
    if not marker:
        return html_text, False
    if marker in (html_text or ""):
        return html_text, False
    try:
        block = _equipment_declared_block_html(plan)
        if not block or marker not in block:
            return html_text, False
        low = html_text.lower()
        pos = low.rfind("</body>")
        merged = (html_text[:pos] + EQUIPMENT_FALLBACK_COMMENT + " -->\n" + block + "\n"
                  + html_text[pos:]) if pos >= 0 else (html_text + "\n" + block)
        return merged, True
    except Exception:
        return html_text, False


def _ensure_node_warnings_section(html_text, plan):
    """节点级告警的结构性保证：有告警而模型页面没写 → 追加确定性卡片。

    `count == 0` / 键取不到 → **什么都不做**（与 Word 同口径：没告警就一个字不加）。
    返回 ``(html_text, 是否追加)``。
    """
    model = node_warnings_model(plan)
    if not model or model["count"] <= 0:
        return html_text, False
    if NODE_WARNINGS_TITLE in (html_text or ""):
        return html_text, False
    try:
        card = _node_warnings_card_html(plan)
        if not card:
            return html_text, False
        low = html_text.lower()
        pos = low.rfind("</body>")
        merged = (html_text[:pos] + NODE_WARNINGS_FALLBACK_COMMENT + " -->\n" + card + "\n"
                  + html_text[pos:]) if pos >= 0 else (html_text + "\n" + card)
        return merged, True
    except Exception:
        return html_text, False


def _ensure_usage_section(html_text, plan):
    """用量口径段的结构性保证：**不判标记、直接追加**。

    为什么不能判标记：模型页面里不可能有"运行末尾"的用量 —— 那个数是在它自己这次调用
    **之后**才产生的（facts 里刻意不放用量，见 `_facts_bundle` 的 `usage_caliber`）。
    所以这里只做两件事：页面上已有这一节（确定性页面）就不重复；拿不到任何用量数据就
    一个字不加。返回 ``(html_text, 是否追加)``。
    """
    if USAGE_TITLE in (html_text or ""):
        return html_text, False
    try:
        card = _usage_card_html(plan)
        if not card:
            return html_text, False
        low = html_text.lower()
        pos = low.rfind("</body>")
        merged = (html_text[:pos] + USAGE_FALLBACK_COMMENT + " -->\n" + card + "\n"
                  + html_text[pos:]) if pos >= 0 else (html_text + "\n" + card)
        return merged, True
    except Exception:
        return html_text, False


def _delivery_markers_missing(html_text):
    """模型产出的 HTML 里缺了哪些关键标记（返回缺失的标记元组，空 = 齐全）。"""
    low = html_text or ""
    return tuple(m for m in DELIVERY_MARKERS if m not in low)


def _ensure_delivery_markers(html_text, plan, view, cal_h=None):
    """③ 的结构性保证：模型页面缺关键标记 → 追加确定性段；连追加都做不到 → 整体回退。

    返回 ``(html_text, mode)``，mode ∈ {"agent", "agent+appendix", "fallback"}。
    任何分支都留下可诊断的痕迹：mode 由调用方写进节点状态，HTML 里带 `<!-- 追加确定性口径段`
    注释。**不靠提示词** —— 提示词改了也只是"希望"模型照做，这里保证"不可能被丢"。
    """
    missing = _delivery_markers_missing(html_text)
    if not missing:
        return html_text, "agent"
    try:
        appendix = _delivery_appendix_html(plan, view, cal_h)
        low = html_text.lower()
        pos = low.rfind("</body>")
        if pos >= 0:
            merged = html_text[:pos] + appendix + "\n" + html_text[pos:]
        else:
            # 没有 </body>（模型给了个片段）→ 退而求其次：追加到末尾。
            merged = html_text + "\n" + appendix
        if _delivery_markers_missing(merged):
            raise ValueError("追加后仍缺标记：%s" % (missing,))
        return merged, "agent+appendix"
    except Exception:
        # 保底：整体回退到确定性渲染（它一定带全部标记）。
        return None, "fallback"


def build_plan_html(plan) -> str:
    plan = _strip_single_zone_prefix(plan)   # 旧计划复看时的单区前缀兜底（见函数注释）
    ov = plan.get("overview") or {}
    rp = plan.get("resource_plan") or {}
    view = _compute_view(plan)
    name = _html.escape(str(ov.get("project_name") or "施工进度计划"))

    total = ov.get("total_duration_days") if ov.get("total_duration_days") is not None else view["total_day_count"]
    # 工期方案：成果提交有「甘特图对比模式」，但本项目 plan_json 的 meta.schedule_versions
    # 只有 theory_min_days / resource_ok_days / delta_days **三个数字**，没有备选方案的逐任务
    # 排程，所以双甘特对比无法还原；能还原的是这个数字层面的方案对比。
    _sv = (plan.get("meta") or {}).get("schedule_versions") if isinstance(plan.get("meta"), dict) else {}
    if isinstance(_sv, dict) and _sv.get("theory_min_days") is not None:
        _delta = _sv.get("delta_days")
        schedule_cmp = (f"{_sv.get('theory_min_days')} → {_sv.get('resource_ok_days')} 天"
                        + (f"（+{_delta}）" if _delta else ""))
    else:
        schedule_cmp = "—"
    # 第 39 轮：看板顶部的峰值也要标口径（用户给定上限 / 模型估算，非用户输入 /
    # 资源曲线口径），并**始终并列**逐日曲线峰值 —— 两个数不是一个东西，藏掉哪个都是撒谎。
    # 摘要卡片只印结果（「91 人」/「94 人」），口径由 `_resource_card_html` 与
    # `_peak_caliber` 的说明文字承载（见 `_card_value`；两个数**并列**这一点没有变）。
    _cal_h = _peak_caliber(plan, view)
    if _cal_h is not None:
        _peak_kv = [("峰值人数", _cal_h["text"]),
                    ("每日用工峰值(按任务叠加)", f"{view['peak_total']} 人")]
    else:
        _peak_kv = [("人工峰值", f"{view['peak_total']} 人")]
    # 摘要卡片只印**结果**（数与单位），不印来源键/口径注解 —— 见 `_card_value`。
    # `_card_value` 的第 2 个参数是单位（None = 结果里已自带单位，不再追加）；
    # 标记为 `keep` 的项**整值照印**（部分值里的括号是数据，不是注解）。
    # 口径说明的去处没有变、也不会丢：峰值口径在「资源计划」卡片里、四个天数的语义在
    # 置信度卡与施工组织口径段里、来源键在 Word 的计划总览表里。
    kv = "".join(
        f"<div><b>{value if unit == 'keep' else _card_value(value, unit)}</b><span>{key}</span></div>"
        for key, value, unit in [
            ("总工期(天)", total, "天"),
            ("计划起止", f"{ov.get('planned_start_date','')} → {ov.get('planned_end_date','')}", None),
            # 「关键路径」的两个语义**分列**（P0-B）：条数 ≠ 工期。老写法只印
            # 「关键路径 89 任务」，用户读成"关键路径长度 89 天"。
            # 看板传 `with_source=False`：摘要卡片不印 `overview.critical_path_length`
            # 这类内部字段名（Word 计划总览表仍印，追溯性不变）。
            *[(k, v, "个" if k == "关键路径任务数" else "天")
              for k, v in _critical_path_rows(plan, with_source=False)],
            ("工序总数", len(_tasks(plan)), None),
            *[(k, v, None) for k, v in _peak_kv],
            ("峰值工种", view["peak_trade"] or "—", None),
            # 工期方案卡传 `keep`：这里的「（+170）」是差值**数据**，不是来源/口径注解，
            # 剥掉就等于把"理论最短到资源可行差了多少天"从摘要里删掉。
            ("工期方案(理论→可行)", schedule_cmp, "keep"),
            # 四个「天数」的语义与来源键（与 Word 计划总览同一函数，不可能漂移）
            *[(k, v, "天") for k, v in _duration_caliber_rows(plan, with_source=False)],
            ("计划编号", _html.escape(str(plan.get("plan_id", "—"))), None),
        ]
    )

    # ---- 逐条工序表：工期列 = **排程跨度**（与同行的起止日期一致），WBS 目标另起一列 ----
    # 用户实测：ALC 行日期跨 31 天、"工期"却写 3（WBS 目标），同一行两个数 → 读成"编的"。
    # P0-B：`duration_days` 现在与日期同源，WBS 目标走 `_task_wbs_target`（老计划读旧键）。
    _span = _schedule_span(plan)
    _rd_map = _rd_task_map(plan)
    _leaf_map = _leaf_bindings(plan)   # 换算参数来源键只挂 WBS 叶子（见 _evidence_core）
    crit_table = "".join(
        f"<tr><td>{t.get('task_id','')}</td><td>{_html.escape(str(t.get('task_name','')))}</td>"
        f"<td>{t.get('start_date','')}</td><td>{t.get('finish_date','')}</td>"
        f"<td>{_span.get(str(t.get('task_id')), '—')}</td>"
        f"<td>{_task_wbs_target(t, _span.get(str(t.get('task_id')))) if _task_wbs_target(t, _span.get(str(t.get('task_id')))) is not None else '—'}</td>"
        f"<td>{_html.escape(_evidence_text(_rd_map.get(str(t.get('task_id'))), _leaf_map.get(str(t.get('task_id')))))}</td></tr>"
        for t in (plan.get("critical_path_tasks") or [])
    ) or "<tr><td colspan=7>（无）</td></tr>"

    ms = plan.get("key_milestones") or []
    if len(ms) < 5:
        seen = {m.get("name") for m in ms}
        for t in plan.get("critical_path_tasks") or []:
            if len(ms) >= 5:
                break
            nm = (t.get("task_name") or "") + "完成"
            if nm not in seen:
                ms.append({"name": nm, "date": t.get("finish_date"),
                           "task_id": t.get("task_id"), "description": "关键路径里程碑"})
                seen.add(nm)
    # 里程碑表：还原成果提交的表格式（名称/日期/说明），比 <ul> 更好扫读
    milestone_table = "".join(
        f"<tr><td><b>{_html.escape(str(m.get('name','')))}</b></td>"
        f"<td>{_html.escape(str(m.get('date','') or ''))}</td>"
        f"<td>{_html.escape(str(m.get('description','') or ''))}</td></tr>"
        for m in ms
    ) or "<tr><td colspan=3>（无）</td></tr>"

    risks = "".join(
        f"<li><b>{_html.escape(str(r.get('risk_name','')))}</b>：{_html.escape(str(r.get('mitigation','')))}</li>"
        for r in (plan.get("risks") or [])
    ) or "<li>（无）</li>"

    # ---- 图表：优先 ECharts（内联，离线可用）；不可用时回退确定性的 SVG 图表 ----
    start_date = _to_date(ov.get("planned_start_date"))
    charts_head, charts_html = "", ""
    if _echarts_ok():
        try:
            charts_head = echarts_page.echarts_bundle_html()
            charts_html = echarts_page.chart_cards_html(plan, view)
        except Exception:              # ECharts 出任何问题都不能影响交付，静默回退
            charts_head, charts_html = "", ""
    if not charts_html:
        charts_html = _svg_chart_cards(plan, view, start_date)

    # WBS 汇总表（按展示粒度；未选合并时逐叶子，与旧版一致）
    # 列口径（与关键路径明细表同一套）：
    #   · 工期(天·排程) = 起止日期跨度（含首尾）= 排程真实的占用天数；
    #   · WBS 目标(天)  = WBS 叶子上模型写的目标天数 —— **单独一列**，不与排程跨度混在一格；
    #   · 依据 / 资源   = `resource_demand.tasks[*]` 的定额来源/班组/机械（没算出来就写 ⚠）。
    _dates = {}
    for t in _tasks(plan):
        if isinstance(t, dict):
            d0 = _date_or_none(t.get("start_date"))
            d1 = _date_or_none(t.get("finish_date"))
            if d0 is not None:
                _dates[str(t.get("task_id"))] = (d0, d1 if d1 and d1 >= d0 else d0)

    def _span_of(ids):
        starts = [_dates[i][0] for i in ids if i in _dates]
        finishes = [_dates[i][1] for i in ids if i in _dates]
        if not starts:
            return None
        return max(1, (max(finishes) - min(starts)).days + 1)

    wbs_rows = []
    _rolled = rolled_rows(plan)
    if _rolled:
        for g in _rolled:
            _ids = [str(i) for i in (g.get("ids") or [])]
            wbs_rows.append((g.get("phase"), g.get("work_package"),
                             _group_id(_ids), _group_label(g, len(_ids)),
                             _span_of(_ids) if _span_of(_ids) is not None else g.get("工期"),
                             g.get("工期"), g.get("工程量"), g.get("单位"),
                             _group_evidence(_ids, _rd_map)))
    else:
        for ph in (plan.get("wbs") or {}).get("phases", []):
            for wp in ph.get("work_packages", []):
                for sub in wp.get("sub_packages", []):
                    _tid = str(sub.get("id"))
                    wbs_rows.append((ph.get("phase"), wp.get("name"), sub.get("id"),
                                     sub.get("name"), _span.get(_tid),
                                     sub.get("duration_days"), sub.get("quantity"),
                                     sub.get("unit"),
                                     _evidence_text(_rd_map.get(_tid), sub)))
    wbs_table = "".join(
        f"<tr><td>{_html.escape(str(a))}</td><td>{_html.escape(str(b))}</td><td>{_html.escape(str(c))}</td>"
        f"<td>{_html.escape(str(d))}</td><td>{'—' if e is None else e}</td>"
        f"<td>{'—' if f is None else f}</td><td>{'' if g is None else _html.escape(str(g))}</td>"
        f"<td>{_html.escape(str(h))}</td><td>{_html.escape(str(i))}</td></tr>"
        for a, b, c, d, e, f, g, h, i in wbs_rows
    ) or "<tr><td colspan=9>（无）</td></tr>"

    report = _md_to_html(_report_text(plan))

    # 审计戳：看板也要能一眼看出这份计划审没审过、审了几轮、**是谁审的**。
    # 判据与 Word 页头完全同一份（`_audit_display` → `audit_gate.audit_honesty`）：
    # 脚本代答或缺记录 → 「未审计 · 待人工复审」，并**说明原因**（P0-A）。
    _aud_html = _audit_display(plan)
    _au_color = _aud_html["color"]
    _audit_reason_html = (
        "<span class='chart-lbl' style='margin-left:10px;color:#c0392b'>不能按已审计交付：%s</span>"
        % _html.escape(_aud_html["reason"])) if _aud_html["reason"] else ""
    audit_badge = (
        "<div class='card' style='padding:10px 16px'>"
        "<span style='display:inline-block;padding:2px 10px;border-radius:10px;"
        "background:%s;color:#fff;font-size:12px'>%s</span>"
        "<span class='chart-lbl' style='margin-left:10px'>三轮回审：%s</span>%s</div>"
        % (_au_color, _html.escape(_aud_html["badge"]),
           _html.escape(_aud_html["round_text"]), _audit_reason_html))
    _pb = params_banner(plan)
    params_badge = (
        "<div class='card' style='padding:10px 16px;background:#fff4f4;"
        "border:1px solid #e0524d'><span style='color:#c0392b;font-weight:700'>%s</span>"
        "</div>" % _html.escape(_pb)) if _pb else ""
    # ---- 【第 2 批 · 域 2 / 2.7】材料计划声明（看板侧）----
    # 与 Word 共用 `MATERIALS_EXCLUDED_NOTICE`（一字不差）；HTML 形态复用
    # `_materials_notice_html()`（与 LLM 编排页的结构性保证**同一段 markup**，
    # 避免"确定性页面"与"追加段"两处各写一份而漂移）。
    materials_badge = _materials_notice_html()
    # ---- 域 8.1：AI 补的限额被丢弃的披露（看板侧）----
    # 与 Word 共用 `_ignored_model_limits_text`（一字不差）。
    _iml = _ignored_model_limits_text(plan)
    iml_badge = (
        "<div class='card' style='padding:10px 16px'>"
        "<span style='color:#3a4a63;font-size:13px'>%s</span></div>"
        % _html.escape(_iml, quote=False)) if _iml else ""
    # ---- 域 8.8①：未人工核验的 L4 条数（看板侧）----
    _l4r = _l4_review_notice_text()
    l4r_badge = (
        "<div class='card' style='padding:10px 16px'>"
        "<span style='color:#3a4a63;font-size:13px'>%s</span></div>"
        % _html.escape(_l4r, quote=False)) if _l4r else ""
    # ---- 模型参与度提示条：看板**最顶上**（branding 之后、参数告警之前）----
    # 与 Word 同源（`model_participation_notice`）；level == "ok" 时不出这一段。
    _pnotice = model_participation_notice(plan)
    participation_badge = (
        "<div class='card' style='padding:12px 16px;background:#fff4f4;"
        "border:2px solid #e0524d'><span style='color:#c0392b;font-weight:700;"
        "font-size:14px'>%s</span></div>" % _html.escape(_pnotice)) if _pnotice else ""

    # ---- 资源计划卡片：内容并集 ----
    # 卡片正文由 `_resource_card_html` 生成（与 LLM 编排页面的"追加确定性口径段"共用同一段
    # HTML —— 模型漏写时由 ③ 的结构性保证把它补回去，两处口径因此不可能漂移）。
    resource_card = _resource_card_html(plan, view, _cal_h)
    # 「数据来源与置信度」卡片：紧接计划总览 / 资源卡之后（与 Word 同一真源：
    # `confidence_section_blocks`；没有置信度元数据时是空串，页面一个字都不多）。
    confidence_card = _confidence_section_html(plan, view)
    # 节点级告警卡片（第 44 轮）：`meta.node_warnings` 一直都有，看板却一个字不印。
    # 无告警 / 取不到键 → 空串（**不空表、不占位**）。
    node_warnings_card = _node_warnings_card_html(plan)
    # 用量卡片（第 44 轮）：看板由 `HtmlPageNode`（最后一个 LLM 节点）生成，此处拿到的
    # 就是本次运行末尾用量（含本节点自身调用）；拿不到末尾快照时只报"计划数据定稿时"，
    # 并**明说不是总量**（`meta.usage` 是定稿时快照，实测差一半）。
    usage_card = _usage_card_html(plan)

    html_doc = (
        "<!DOCTYPE html><html lang='zh'><head><meta charset='utf-8'>"
        f"<title>{name} · 计划看板</title><style>{_HTML_CSS}{branding.BRAND_CSS}</style>"
        # ECharts bundle 放 <head>：必须早于下方图表区的 glue <script>，否则 glue 里
        # `window.echarts` 还不存在。vendor 已确认不含 </script>，可直接内联。
        f"{charts_head}</head><body><div class='wrap'>"
        f"{branding.html_brand_head()}"
        f"{participation_badge}"
        f"{params_badge}"
        f"{audit_badge}"
        f"{materials_badge}"
        f"{iml_badge}"
        f"{l4r_badge}"
        f"<div class='card'><h1>📋 {name}</h1><div class='kv'>{kv}</div></div>"
        f"{charts_html}"
        f"{resource_card}"
        f"{confidence_card}"
        f"{node_warnings_card}"
        f"{usage_card}"
        f"<div class='card'><h2>关键路径明细</h2><table><tr><th>ID</th><th>任务</th><th>开始</th>"
        f"<th>完成</th><th>工期(天·排程)</th><th>WBS 目标(天)</th><th>依据 / 资源</th></tr>"
        f"{crit_table}</table></div>"
        f"<div class='card'><h2>关键里程碑</h2>"
        f"<div class='chart-lbl'>关键里程碑为施工进度计划中必须按时完成的重要节点，"
        f"通常对应合同节点或验收节点。</div>"
        f"<table><tr><th>里程碑名称</th><th>日期</th><th>说明</th></tr>{milestone_table}</table></div>"
        f"<div class='card'><h2>工作分解结构（WBS）</h2>"
        f"<div class='chart-lbl'>{_html.escape(granularity_note(plan) or '展示粒度：工序级 × 按层（逐任务，未合并）')}"
        "　工期(天·排程)=起止日期跨度（含首尾）；WBS 目标(天)=叶子上的模型目标天数；"
        "依据 / 资源 来自 resource_demand（⚠ = 无可用定额，工期沿用 WBS 估算、未计算班组）。</div>"
        f"<table><tr><th>阶段</th><th>工作包</th>"
        f"<th>ID</th><th>任务</th><th>工期(天·排程)</th><th>WBS 目标(天)</th><th>工程量</th><th>单位</th>"
        f"<th>依据 / 资源</th></tr>{wbs_table}</table></div>"
        # 施工组织层口径（工日 → 工期 怎么来的）+ 组织缺口 + 审计提示。
        # 放在 WBS 表之后：既有测试按关键字取"第一行"（工期列/依据列），新增表不许抢位。
        f"{_org_section_html(plan, view)}"
        f"{_daily_resource_bill_html(plan)}"
        f"<div class='card'><h2>流水施工组织与季节性保障</h2><ul class='mil'>{risks}</ul>"
        f"<p style='font-size:13px;color:#445'>主体结构按标准层分段流水；工期起止与各任务衔接见上方甘特图。</p></div>"
        f"<div class='card'><h2>施工监督报告</h2><div class='md-body'>{report}</div></div>"
        f"{branding.html_brand_foot(datetime.date.today().isoformat())}"
        "</div></body></html>"
    )
    out = _plan_dir(plan) / "计划看板.html"
    # ---- G5：交付物单位清零（归一 + 留痕，见本文件顶部 G5 说明）----
    html_doc = _normalize_deliverable_u33a1(html_doc, "计划看板.html")
    _report_no_cjk_compat_square_metre(html_doc, "计划看板.html")
    out.write_text(html_doc, encoding="utf-8")
    return str(out)
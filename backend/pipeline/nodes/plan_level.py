"""节点：计划细度选择（PlanLevelNode）—— 生成计划前，让用户拍板 L3 还是 L4。

位置：WBS 与施工段展开**之后**、定额锚定**之前**。
      此时 ctx["wbs"] 已经是完整的三层树，所以行数能**准确数出来**
      （quantity.estimate_row_counts），不是估的 —— 给用户的选项里可以直接写明
      "这个选择会出多少行"。

本节点做三件事：
  ① 判定**参数层级**（param_level）：用户给的参数落在 L4（具体工序工程量）还是只到
     L3（项目级总量）。判不出来就写 "unknown" —— 不瞎猜。
  ② 统计**两个独立维度**的真实行数并推荐档位：
       · 工序拆解深度（`quantity.DEPTH_COMPONENT` 工序级 / `DEPTH_COARSE` 工种级）
       · 楼层分组（`FLOOR_PER_FLOOR` 按层 / `FLOOR_PER_5` 每5层 / `FLOOR_WHOLE` 整栋）
     实测（潭村 12 栋、415 条叶子）的行数矩阵：
         深度 \\ 楼层分组     按层    每5层   整栋
         工序级（现状）        415     116      25
         工种级               338      99      22
     **楼层分组才是行数主杠杆（16 倍）**，"工种级合并"只省 18% —— 所以推荐逻辑以它为主。
     「构件级展开」是产品想要的更细一档，但**当前做不了**（缺分部工程量比例），
     因此明确列为受阻项（`BLOCKED_COMPONENT_NOTE`），不给用户一个选了做不到的选项。
  ③ 发**人工门**（EV_PARAM_REVIEW，purpose="plan_level"）让用户选。门里给的是
      **六个组合选项**（三种楼层分段 × 两种工序细度，各带真实行数），敲一个数字就
      定完整粒度（用户原话：「不要做成"X+X"两轴选项，直接给用户六个选项」）；
      编号：1 按层·工序级 / 2 按层·工种级 / 3 每5层·工序级 / 4 每5层·工种级 /
      5 整栋·工序级 / 6 整栋·工种级，打多个数字以最后一个为准。其余写法照旧宽容：
     深度：L3/l3/三级 → 工种级；L4/l4/四级 → 工序级（**裸数字只当代号**，
     不再是"3=L3、4=L4"——门上印的是六档组合）；
     楼层：整栋/不分层 → 整栋；每5层 → 每5层一组；按层/逐层 → 按层；
     打 Y（passed=True）/空输入 → 推荐值；实在看不懂 → 用推荐值 + 记中文 warning。

**本节点只记录选择，绝不改动 WBS 树。**
  真正的"汇总成 L3 展示"由**交付环节最后**做：L3 行的工期必须用排程结果的
  **时间跨度**才算得准（quantity.rollup_to_l3 的 schedule 参数），而排程发生在本节点
  之后，所以这里只把选择记下来，并在用户选了 L3 时置
  ctx["plan_level_info"]["display_rollup"] = True，供下游交付环节汇总。

终端兼容：terminal/renderer.py 与 terminal/confirmer.py 对 param_review 事件里
purpose != "doc" 的一律按"打 Y 通过 / 直接输入内容"处理，所以打 Y 会走到
passed=True → 采信推荐值 L4，正合预期。终端那一列六个编号由
`terminal/renderer.py::_render_plan_level` 按同一份 `picker` 打印。

健壮性：无 registry（单测/裸调用）时直接用推荐值；任何意外都不抛异常，
只退回推荐值并记一条 warning —— 计划细度不该让整条流水线挂掉。
"""

import re
import uuid

from .. import quantity
from ..base import BaseNode
from ..registry import GATE_TIMEOUT_SECONDS
from ..events import EV_PARAM_REVIEW

# 计划细度取值（与 quantity.LEVEL_L3 / LEVEL_L4 保持同一字面量）
LEVEL_L3 = "L3"
LEVEL_L4 = "L4"

# 默认推荐：L4（工序级）。细的能上卷成粗的，粗的拆不回细的。
RECOMMEND = LEVEL_L4

# 自动分档阈值（方案 C）：明细超过这个行数时，**推荐 L3**。
# 为什么需要：竖向改成"一层一段"后，38 层住宅的明细会到 800 行上下，
# 丢给用户一张 800 行的表并不好用；此时应默认推荐 L3 级总控视图，
# 并把"明细多少行、建议哪一级"明确告诉用户，由用户拍板。
ROW_THRESHOLD_L3 = 400


def recommend_for(l3_rows, l4_rows):
    """按规模自动分档：明细行数超过阈值就推荐 L3，否则推荐 L4。

    返回 (推荐级别, 中文理由)。
    """
    try:
        n = int(l4_rows or 0)
    except (TypeError, ValueError):
        n = 0
    if n > ROW_THRESHOLD_L3:
        return LEVEL_L3, ("明细共 %d 行，超过 %d 行的上限 —— 建议看「工种级」（%d 行）总控视图；"
                          "选「工序级」也可以，但表格会很长"
                          % (n, ROW_THRESHOLD_L3, int(l3_rows or 0)))
    return LEVEL_L4, ("明细共 %d 行，没有超过 %d 行的上限 —— 建议看「工序级」" % (
        n, ROW_THRESHOLD_L3))


def recommend_grouping_for(depth, matrix):
    """推荐**楼层分组**档位：按层会超过阈值就退到每 5 层一组。

    为什么单独推荐这一个轴：实测（潭村 12 栋）行数矩阵是

        深度 \\ 楼层分组     按层    每5层   整栋
        工序级（现状）        415     116      25
        工种级               338      99      22

    —— **楼层分组才是行数主杠杆（415→116→25，16 倍）**，
    而"工种级合并"只省 18%。所以推荐逻辑必须以楼层分组为主。
    """
    try:
        rows = int((matrix or {}).get(depth, {}).get(quantity.FLOOR_PER_FLOOR) or 0)
    except (TypeError, ValueError):
        rows = 0
    if rows > ROW_THRESHOLD_L3:
        return quantity.FLOOR_PER_5, (
            "按层展示是 %d 行（超过 %d 行阈值）→ 建议「每 5 层一组」（%d 行）；"
            "整栋汇总更短（%d 行），但会看不到楼层进展"
            % (rows, ROW_THRESHOLD_L3,
               int((matrix or {}).get(depth, {}).get(quantity.FLOOR_PER_5) or 0),
               int((matrix or {}).get(depth, {}).get(quantity.FLOOR_WHOLE) or 0)))
    return quantity.FLOOR_PER_FLOOR, "按层展示 %d 行，未超过 %d 行阈值 —— 建议「按层」" % (
        rows, ROW_THRESHOLD_L3)


# 构件级展开：产品想要的"更细一档"，但**当前做不了** —— 参数里只有总量，
# 没有"柱/墙/梁/板各占多少"的分部比例，要拆就得替用户编比例（AI 假设）。
# 诚实的做法是把它明确列为受阻项，而不是给用户一个选了也做不到的选项。
BLOCKED_COMPONENT_NOTE = ("「构件级展开」（拆到柱/墙/梁/板）当前不可用："
                          "参数里只有总量，缺分部工程量比例；要启用需你提供各分部占比，"
                          "或明确授权按经验比例拆分（那会被标注为 AI 假设）")


# 参数键名特征：命中说明用户给的是"工序/任务级"明细（L4 口径）
_L4_KEY_HINTS = ("activit", "task", "work_item", "workitem", "sub_package",
                 "subpackage", "procedure", "工序", "任务", "明细")
# 参数键名特征：命中说明用户只给了"项目级总量"（L3 口径）
_L3_KEY_HINTS = ("total", "总量", "overall", "合计")

# 用户文本里的细度写法：L3 / L-3 / l 3 / 三级
_RE_L = re.compile(r"[lL]\s*[-_]?\s*([34])")
_RE_CN = re.compile(r"([三四])\s*级")
# 裸数字：只在短输入里认（防止"4 层楼""共 3 栋"这类句子被误判成细度）
_RE_DIGIT = re.compile(r"(?<!\d)([34])(?!\d)")
# "打代号"的严格形状：整段只有数字和括号/空白（`3`、`[3]`、`（3）`、`2 4`）。
# 有任何一个别的字（`L3`/`选3`/`4 层楼`）就不算代号，交回老的人话解析。
_RE_PICKER = re.compile(r"^[\s\[\]\(\)（）〔〕【】]*\d[\s\d\[\]\(\)（）〔〕【】]*$")
_SHORT_INPUT = 12

# 深度轴的**文字**写法（与 L3/L4 是一回事，用户更可能说人话）
_COARSE_WORDS = ("工种级", "工种", "粗粒度", "粗档", "汇总")
_COMPONENT_WORDS = ("工序级", "工序", "细粒度", "细档", "明细")
# 构件级：产品想要的更细一档，但当前**做不了**（缺分部工程量比例）。
# 单独识别出来，是为了**明确拒绝并说明原因**，而不是悄悄当成"工序级"。
_COMPONENT_LEVEL_WORDS = ("构件级", "构件", "柱墙梁板", "拆到构件")


def _picker_payload(matrix, rec_depth, rec_grouping):
    """把「三种楼层分段 × 两种工序细度」拼成**六个可直接敲数字的组合选项**。

    用户原话：「不要做成"X+X"两轴选项，直接给用户六个选项」——所以每个编号就是
    一个**完整选择**（分段 + 细度一次定死），敲一个数字即可，不用先选轴再选档。
    编号顺序：楼层分段为外层、细度为内层（用户说的"三种施工段 × 细度"）——

        1. 按层 · 工序级（细）      2. 按层 · 工种级（粗）
        3. 每 5 层一组 · 工序级（细） 4. 每 5 层一组 · 工种级（粗）
        5. 整栋 · 工序级（细）      6. 整栋 · 工种级（粗）

    返回 `{"options": [...], "recommend": [no], "max": 6}`。
    """
    matrix = matrix if isinstance(matrix, dict) else {}
    options, n = [], 0
    for g in quantity.FLOOR_GROUPINGS:
        for d in quantity.DEPTHS:
            n += 1
            options.append({
                "axis": "combo",                       # 一个号定一整组（非单轴）
                "no": n,
                "depth": d,
                "floor_grouping": g,
                "label": "%s · %s" % (quantity.FLOOR_LABELS[g],
                                      quantity.DEPTH_LABELS[d]),
                "rows": int(((matrix.get(d) or {}).get(g)) or 0),
                "note": _combo_note(d, g),
            })
    rec = [o["no"] for o in options
           if o["depth"] == rec_depth and o["floor_grouping"] == rec_grouping]
    return {"options": options, "recommend": sorted(rec), "max": n}


def _combo_note(depth, grouping):
    """组合选项那一列的一句人话（分段怎么说 + 细度怎么说）。"""
    return "%s；%s" % (_floor_note(grouping), _depth_note(depth))


def _floor_note(key):
    """每个分段档位一句人话（终端那一列）。"""
    return {
        quantity.FLOOR_PER_FLOOR: "最细，能逐层核对",
        quantity.FLOOR_PER_5: "常用，表不长",
        quantity.FLOOR_WHOLE: "只做总控",
    }.get(key, "")


def _depth_note(key):
    return {
        quantity.DEPTH_COMPONENT: "每条工序都能单独改",
        quantity.DEPTH_COARSE: "按工种合并，行数少",
    }.get(key, "")


class PlanLevelNode(BaseNode):
    name = "plan_level"
    title = "计划细度选择"

    # ---------------- 入口 ----------------
    def run(self, ctx):
        ctx = ctx if isinstance(ctx, dict) else {}

        # ①②：真实行数 + 参数层级（都容错，出错就退化成 0 行 / unknown，不影响选细度）
        wbs = ctx.get("wbs")
        wbs = wbs if isinstance(wbs, dict) else {}
        try:
            l3_rows, l4_rows = quantity.estimate_row_counts(wbs)
        except Exception:
            l3_rows, l4_rows = 0, 0
        try:
            param_level = self._param_level(ctx)
        except Exception:
            param_level = "unknown"

        # ③：按规模算出推荐级别与理由（方案 C：自动分档）
        recommend, rec_reason = recommend_for(l3_rows, l4_rows)

        # ③b：**两个独立维度**的真实行数矩阵（工序拆解深度 × 楼层分组）
        # 这是本节点最该给用户看的东西：粒度的行数影响几乎全来自楼层分组。
        try:
            matrix = quantity.estimate_row_matrix(wbs)
        except Exception:
            matrix = {}
        # 深度：默认工序级（细的能上卷成粗的，反之不行）
        rec_depth = quantity.DEPTH_COARSE if recommend == LEVEL_L3 else quantity.DEPTH_COMPONENT
        rec_grouping, rec_group_reason = recommend_grouping_for(rec_depth, matrix)

        # ④：发人工门并解析决策
        warning = ""
        chosen = recommend
        chosen_depth = rec_depth
        chosen_grouping = rec_grouping
        grouping_rows = dict((g, int((matrix.get(rec_depth) or {}).get(g) or 0))
                             for g in quantity.FLOOR_GROUPINGS)
        depth_rows = dict((d, int(((matrix.get(d) or {}).get(rec_grouping)) or 0))
                          for d in quantity.DEPTHS)
        picker = _picker_payload(matrix, rec_depth, rec_grouping)
        try:
            decision = self._ask(l3_rows, l4_rows, param_level, recommend, rec_reason,
                                 matrix=matrix, rec_depth=rec_depth,
                                 rec_grouping=rec_grouping,
                                 rec_group_reason=rec_group_reason,
                                 picker=picker)
        except Exception as e:                       # 交互层异常不该拖垮流水线
            decision = None
            warning = "计划细度交互异常（%s），已按推荐值 %s 继续" % (e, recommend)

        if isinstance(decision, dict) and decision.get("action") == "abort":
            self.done_summary = "用户在计划细度选择环节取消"
            return {"_stop": "用户在计划细度选择环节取消"}

        if isinstance(decision, dict):
            manual = decision.get("manual_input")
            message = decision.get("message")
            parsed, parsed_group = None, None
            # ① 优先按**编号选项**解析（用户建议：「让用户直接输入数字选择」）——
            #    编号与终端上印出来的那一列是同一份 `picker`。
            #    ⚠️ `picker` 必须**按关键字传**：`_parse_picker(*texts, picker=None)` 是
            #    可变位置参 + 关键字参的签名，位置传进去会被当成第 3 段文本，`opts`
            #    永远是空 → 编号直选静默失效（第 38 轮修的就是这个）。
            chosen_depth, chosen_grouping, picked_any = self._parse_picker(
                manual, message, picker=picker)
            if not picked_any:
                # ② 没打编号 → 老路径：认 L3/L4 与人话（工种级/工序级）或文字描述
                parsed = self._parse_choice(manual, message)
                if parsed is not None:
                    chosen = parsed
                    chosen_depth = (quantity.DEPTH_COARSE if parsed == LEVEL_L3
                                    else quantity.DEPTH_COMPONENT)
                parsed_group = self._parse_floor_grouping(manual, message)
                if parsed_group is not None:
                    chosen_grouping = parsed_group
            # 兜底：解析结果必须是合法档位，否则保持推荐值 ——
            # 不能让 `info["depth_label"]` 之类的字段变成 None（老测试与交付物都读它）。
            if chosen_depth not in quantity.DEPTHS:
                chosen_depth = rec_depth
            if chosen_grouping not in quantity.FLOOR_GROUPINGS:
                chosen_grouping = rec_grouping
            chosen = (LEVEL_L3 if chosen_depth == quantity.DEPTH_COARSE else LEVEL_L4)
            # 点名要「构件级展开」→ 明确告知当前不可用（不悄悄当成工序级放行）
            if self._asks_component_level(manual, message):
                warning = BLOCKED_COMPONENT_NOTE
            elif not picked_any and parsed is None and parsed_group is None:
                shown = str(manual if str(manual or "").strip() else (message or "")).strip()
                if shown:                            # 空输入/passed=True 是"走推荐值"，不算异常
                    warning = ("无法从输入「%s」识别展示粒度，已按推荐值"
                               "（%s + %s）继续"
                               % (shown[:40], quantity.DEPTH_LABELS[chosen_depth],
                                  quantity.FLOOR_LABELS[chosen_grouping]))

        # ⑤：写回 ctx（不碰 WBS）
        try:
            rows_chosen = quantity.estimate_rows_for(wbs, chosen_depth, chosen_grouping)
        except Exception:
            rows_chosen = l4_rows
        info = {
            "param_level": param_level,
            "recommended": recommend,
            "recommend_reason": rec_reason,
            "chosen": chosen,
            "rows_l3": l3_rows,
            "rows_l4": l4_rows,
            # ---- 两个独立维度（产品口径）----
            "depth": chosen_depth,
            "depth_label": quantity.DEPTH_LABELS.get(chosen_depth, chosen_depth),
            "floor_grouping": chosen_grouping,
            "floor_grouping_label": quantity.FLOOR_LABELS.get(chosen_grouping, chosen_grouping),
            "rows_chosen": rows_chosen,
            "row_matrix": matrix,
            "recommend_floor_reason": rec_group_reason,
            "blocked": BLOCKED_COMPONENT_NOTE,
            "note": self._note_of(chosen, chosen_grouping),
        }
        # 需要"汇总渲染"的情形：不是(工序级 + 按层)就都得合并
        if chosen_depth != quantity.DEPTH_COMPONENT or \
                chosen_grouping != quantity.FLOOR_PER_FLOOR:
            # 供下游交付环节做粒度汇总展示（quantity.group_rows）
            info["display_rollup"] = True

        ctx["plan_level"] = chosen
        ctx["plan_level_info"] = info
        ctx["display_granularity"] = {"depth": chosen_depth,
                                      "floor_grouping": chosen_grouping,
                                      "rows": rows_chosen}
        if warning:
            ctx["plan_level_warning"] = warning

        # 括号里给"不合并是多少行"，用户才能判断合并省了多少；
        # 「参数层级 L4」是纯内部概念，不再往用户面前摆。
        self.done_summary = ("展示粒度已定：%s + %s → 计划里合并成 %d 行（不合并是 %d 行）"
                             % (quantity.DEPTH_LABELS.get(chosen_depth, chosen_depth),
                                quantity.FLOOR_LABELS.get(chosen_grouping, chosen_grouping),
                                rows_chosen, l4_rows))
        out = {"plan_level": chosen, "plan_level_info": info,
               "display_granularity": ctx["display_granularity"]}
        if warning:
            out["plan_level_warning"] = warning
        return out

    # ---------------- ①②：参数层级 / 行数 ----------------
    @staticmethod
    def _param_level(ctx):
        """用户给的参数最细到哪一级：L4（工序级）/ L3（项目级总量）/ unknown。

        只看 extracted_params 的**键名特征**，不猜数值：
          · 键名像工序/任务明细、且值确实是清单（list/dict）→ 说明参数到了 L4；
          · 键名像项目级总量（total_* / 总量 / 合计）→ 说明参数只到 L3。
        两者都有时按更细的口径（L4）算 —— 细的能上卷，粗的拆不回细的。
        都没命中（例如只有建筑类型、开工日期）→ "unknown"，不瞎猜。
        """
        params = ctx.get("extracted_params")
        if not isinstance(params, dict) or not params:
            return "unknown"
        has_l4 = False
        has_l3 = False
        for k, v in params.items():
            if v is None or v == "" or v == [] or v == {}:
                continue                                  # 空值不算"给了参数"
            key = str(k or "").strip().lower()
            if isinstance(v, (list, dict)) and any(h in key for h in _L4_KEY_HINTS):
                has_l4 = True
            if any(h in key for h in _L3_KEY_HINTS):
                has_l3 = True
        if has_l4:
            return LEVEL_L4
        if has_l3:
            return LEVEL_L3
        return "unknown"

    @staticmethod
    def _note_of(chosen, grouping=None):
        """选择说明（写进 plan_level_info.note，交付环节与终端都会读）。"""
        base = ("L3 级计划：按分部/工种汇总展示，行数少、适合总控；"
                "选 L3 后将无法逐个修改 L4 的班组与定额（只能改到 L3）"
                if chosen == LEVEL_L3 else
                "L4 级计划：可逐条修改工序的工程量、定额、班组与工期")
        if grouping and grouping != quantity.FLOOR_PER_FLOOR:
            base += ("；楼层按「%s」归组（归组只影响展示，不改 WBS 树、不改排程）"
                     % quantity.FLOOR_LABELS.get(grouping, grouping))
        return base

    # ---------------- ③：人工门 ----------------
    def _ask(self, l3_rows, l4_rows, param_level, recommend=None, reason="",
             matrix=None, rec_depth=None, rec_grouping=None, rec_group_reason="",
             picker=None):
        """发 EV_PARAM_REVIEW 门并等决策；无 registry（单测/裸调用）返回 None。

        决策经 POST /params 上行；超时/取消由 registry.wait 返回 {"action":"abort"}。

        payload 里 `params` **保持原有形状不变**（L3/L4/recommend/param_level/note），
        另外新增 `options` / `matrix` / `blocked` 供终端渲染两个维度 —— 老字段不动，
        是为了不破坏既有终端与测试；新字段是产品要的"两个独立维度"。
        """
        recommend = recommend or RECOMMEND
        registry = getattr(self, "_registry", None)
        if registry is None:
            # 单测/裸调用：没人可问，安静地用推荐值，不发无人应答的门
            self.emit("node_progress", {
                "node": self.name, "progress": 100,
                "message": "未接入交互登记（单测/裸调用），展示粒度按推荐值继续",
            })
            return None

        review_id = "pl_%s_%s" % (getattr(self, "_run_id", "run"), uuid.uuid4().hex[:4])
        registry.register(review_id)
        matrix = matrix or {}
        depth = rec_depth or quantity.DEPTH_COMPONENT
        grouping = rec_grouping or quantity.FLOOR_PER_FLOOR
        grouping_rows = dict((g, int((matrix.get(depth) or {}).get(g) or 0))
                             for g in quantity.FLOOR_GROUPINGS)
        depth_rows = dict((d, int(((matrix.get(d) or {}).get(grouping)) or 0))
                          for d in quantity.DEPTHS)
        picker = picker or _picker_payload(matrix, depth, grouping)
        self.emit(EV_PARAM_REVIEW, {
            "review_id": review_id,
            "purpose": "plan_level",                 # 终端按 purpose 显示不同提示
            "message": "请选择计划展示粒度",
            "params": {                              # ⚠️ 形状保持不变（向后兼容）
                "L3": {"rows": l3_rows, "desc": "工种级，适合管理层与总控计划"},
                "L4": {"rows": l4_rows, "desc": "工序级，适合项目部与排班"},
                "recommend": recommend,
                "recommend_reason": reason,
                "param_level": param_level,
                # 这句会经 params_note() 原样打在门最后一行：L3/L4 两个编号必须带中文，
                # 否则用户不知道"改不到 L4"是什么意思。
                "note": "选「工种级（L3）」后，就不能再逐个调具体工序（L4）的班组与定额了",
            },
            "options": {
                "floor_grouping": [
                    {"key": g, "label": quantity.FLOOR_LABELS[g], "rows": grouping_rows.get(g, 0)}
                    for g in quantity.FLOOR_GROUPINGS],
                "depth": [
                    {"key": d, "label": quantity.DEPTH_LABELS[d], "rows": depth_rows.get(d, 0)}
                    for d in quantity.DEPTHS],
                "recommend": {"depth": depth, "floor_grouping": grouping,
                              "rows": grouping_rows.get(grouping, 0)},
                "recommend_reason": rec_group_reason,
                "blocked": BLOCKED_COMPONENT_NOTE,
            },
            # 第 32 轮：给用户**编号选项**（用户建议：「给选项一个代号，让用户直接输入
            # 数字选择，而非还要自己打字」）；第 38 轮改成**六个组合选项**（用户原话：
            # 「不要做成"X+X"两轴选项，直接给用户六个选项」）—— 一个号 = 一种
            # 楼层分段 + 一种细度，敲一个数字就定完。终端照打，`_parse_picker` 按同一份解析。
            "picker": picker,
            "matrix": matrix,
        })
        decision = registry.wait(
            review_id, cancel_evt=getattr(self, "_cancel_evt", None), timeout=GATE_TIMEOUT_SECONDS)
        return decision

    @classmethod
    def _parse_picker(cls, *texts, picker=None):
        """按**编号选项**解析（终端印出来的那一列编号）。

        用户建议原话：「我建议第十步你给选项一个代号，比如 1. 每层一段+粗粒度
        2. 五层一段+粗粒度。让用户直接输入数字选择，而非还要自己打字」；
        第 38 轮进一步拍板：「不要做成"X+X"两轴选项，直接给用户六个选项」——
        所以每个编号就是一个**完整选择**（楼层分段 + 工序细度都定了）。

        规则：
          · **只认"纯数字"的短输入**（如 `3`、`[3]`、`1 3 6`，≤12 字）——
            `L3`/`选3`/`4 层楼`/`共 12 栋` 这些一律**不算**编号，继续走下面的老解析；
          · 一个数字 → 该号对应的**一整组**（分段 + 细度）；
          · 打了多个数字 → **以最后一个为准**（用户改口），如 `3 5` = 用 5 号；
          · 越界数字**忽略**；一个都没认出来 → 返回 `picked_any=False`，调用方回落到
            老解析（L3/L4、「工种级」「整栋」这些写法继续可用）；
          · 兼容第 32 轮的**单轴**老格式编号（`axis` 为 floor_grouping / depth），
            老编号单子照样能解析。
        返回 `(depth, grouping, picked_any)`。
        """
        parts = [str(t).strip() for t in texts if str(t if t is not None else "").strip()]
        text = " ".join(parts)
        opts = list((picker or {}).get("options") or [])
        depth, grouping = None, None
        if not text or not opts or len(text) > _SHORT_INPUT:
            return depth, grouping, False
        # 纯数字才算"打代号"：允许 [3] / （3） / 2 4 这些写法，但不许掺字。
        if not _RE_PICKER.match(text):
            return depth, grouping, False
        if not re.search(r"\d", text):
            return depth, grouping, False
        by_no = dict((int(o.get("no") or 0), o) for o in opts)
        picked = False
        for ch in re.findall(r"\d+", text):
            opt = by_no.get(int(ch))
            if opt is None:
                continue
            picked = True
            if opt.get("axis") == "floor_grouping":      # 老的单轴编号（向后兼容）
                grouping = opt.get("key")
            elif opt.get("axis") == "depth":
                depth = opt.get("key")
            else:                                        # 新格式：一个号定一整组
                depth = opt.get("depth")
                grouping = opt.get("floor_grouping")
        return depth, grouping, picked

    @classmethod
    def _parse_choice(cls, *texts):
        """从用户输入里解析细度；解析不出返回 None（调用方再用推荐值）。

        宽容规则（按优先级）：
          1. 显式写法 L3 / l-3 / l 3 / 三级 → 取**最后一次**出现的（用户改口以最后为准）；
          2. 短输入里的裸数字 3 / 4（≤12 字）→ 认；长句里的数字不认，避免"4 层楼"误判。
        空输入 / None → None（交给"用推荐值"分支）。
        """
        parts = [str(t).strip() for t in texts if str(t if t is not None else "").strip()]
        text = " ".join(parts)
        if not text:
            return None

        # 人话优先：工种级/粗 → L3；工序级/细 → L4（构件级**不算**：它当前不可用，
        # 由调用方单独提示，不能被当成"工序级"悄悄放行）
        if any(w in text for w in _COMPONENT_LEVEL_WORDS):
            return None
        if any(w in text for w in _COARSE_WORDS):
            return LEVEL_L3
        if any(w in text for w in _COMPONENT_WORDS):
            return LEVEL_L4

        hits = [(m.start(), m.group(1)) for m in _RE_L.finditer(text)]
        hits += [(m.start(), "3" if m.group(1) == "三" else "4")
                 for m in _RE_CN.finditer(text)]
        if hits:
            hits.sort(key=lambda x: x[0])
            return LEVEL_L3 if hits[-1][1] == "3" else LEVEL_L4

        if len(text) <= _SHORT_INPUT:
            digits = _RE_DIGIT.findall(text)
            if digits:
                return LEVEL_L3 if digits[-1] == "3" else LEVEL_L4
        return None

    @staticmethod
    def _asks_component_level(*texts):
        """用户是不是点名要了「构件级展开」（当前不可用的那一档）。"""
        text = " ".join(str(t).strip() for t in texts
                        if str(t if t is not None else "").strip())
        return bool(text) and any(w in text for w in _COMPONENT_LEVEL_WORDS)

    @classmethod
    def _parse_floor_grouping(cls, *texts):
        """从用户输入里解析**楼层分组**；解析不出返回 None（调用方用推荐值）。

        认法（按优先级）：
          1. 明确写法：整栋 / 不分层 / 全栋 → whole；每5层 / 每五层 / 5层一组 → per_5；
             按层 / 逐层 / 分层 → per_floor；
          2. 短输入里的裸数字：`5` → per_5，`1` → per_floor。（`3`/`4` 留给深度轴，
             避免"整栋=3"与"三级=3"打架。）
        不认的输入一律返回 None —— 宁可用推荐值 + 记一条 warning，也不猜。
        """
        parts = [str(t).strip() for t in texts if str(t if t is not None else "").strip()]
        text = " ".join(parts)
        if not text:
            return None
        if any(k in text for k in ("整栋", "全栋", "不分层", "不分组", "整体")):
            return quantity.FLOOR_WHOLE
        if any(k in text for k in ("每5层", "每五层", "5层一组", "五层一组", "每 5 层")):
            return quantity.FLOOR_PER_5
        if any(k in text for k in ("按层", "逐层", "分层展示", "每层")):
            return quantity.FLOOR_PER_FLOOR
        if len(text) <= _SHORT_INPUT:
            if "5" in text:
                return quantity.FLOOR_PER_5
            if "1" in text:
                return quantity.FLOOR_PER_FLOOR
        return None

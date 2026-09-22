"""节点2c：节拍型节点展开 — BeatExpandNode — T-12

把 4 个真实节拍型阶段（地下室结构/地上主体结构/二次结构与砌体/装饰装修）
从「逐相 LLM 一次性总量+拍工期」改造成「代码节拍引擎」：
  - 单层量(qty_per_floor)与资源配置**全部由代码推算**，不走模型；
    `prompts/beat_config.txt` 的 LLM 细化路径**当前不可用**（该提示词文件不在仓库里，
    加载即 OSError），详见文末「配置来源现状」与 `_resolve_config`；
  - 代码切段、算节拍(单段量/日产能)、铺 各区 节拍流水叶子；
    竖向：结构类**一层一段**（floors_per_segment=1），装饰装修 3 层一组，
    地下室 0.5 层一段；竖向段数 = ceil(项目层数 / 每段层数)（见 beat_configs）；
    平面：分区数按标准层面积建议（suggest_zones），面积取不到 → 用配置 zones（AI 默认，可改）；
  - 展开前先做**单层量推算**（beat_configs.derive_beat_quantities，v2.3）：
    单层量 = 项目总量 × 部位占比 ÷ 该部位层数 ÷ 分区数，配置里写死的 qty_per_floor
    只在参数缺失时兜底；每个叶子带 `_qty_source`（参数推算/基线默认）+ `_qty_formula`
    （中文公式），beat_subtrees 里另有 qty_source_summary 计数，供前端展示溯源；
  - 结构搭接(同段串行/跨段/跨相)由 layer_engine.structural_deps 直接产出 → ctx["beat_deps"]；
  - 替换 wbs 中该 4 阶段的 work_packages，叶子 id=p.z.s.k 全数字点分，normalize_wbs 幂等保留。

配置来源现状（⚠️ 已按实现核对，与旧文档不同）：
  ① 代码推算（`beat_configs.derive_beat_quantities`：项目总量 × 部位占比 ÷ 部位层数 ÷ 分区数）
  → ② 代码自动修补（竖向口径/段数/节拍域）→ ③ BASE 基线模板兜底（原因写进 `used_fallback`）。
  **LLM 细化这一级当前不可用**：`prompts/beat_config.txt` 不在提示词目录里，
  `prompts_loader.load` 直接 `read_text` 会抛 `FileNotFoundError`（OSError 的子类）。
  `_resolve_config` 不再静默吞掉它：`except OSError` 发一条节点告警
  （`emit("warning")` → 引擎 `collect_node_warning` → `meta.node_warnings`，终端与计划 JSON
  都有留痕），随后照旧降级到 ②/③。也就是说：**节拍展开的工程量从不来自模型**，
  缺提示词文件属于**必须可见的配置错误**，不是"悄悄不生效"。
依赖合并见 merge_beat_deps（供 DepsGenNode 调用，最小侵入）。
"""

import copy
import json
import re

from ..base import BaseNode
from ..llm import LLMClient, LLMError
from ..prompts_loader import load
from .. import config as _cfg
from .beat_configs import (
    BASE_BEAT_CONFIGS,
    BEAT_PHASE_NAMES,
    GROUPED_SEGMENT_PHASES,
    ONE_FLOOR_PER_SEGMENT_PHASES,
    SOURCE_BASE,
    SOURCE_PARAM,
    SOURCE_RATIO,
    beat_phase_name,
    normalize_vertical_split,
    suggest_zones_from_params,
    validate_l4_candidates,
)
from .. import layer_engine as LE
from .wbs_gen import normalize_wbs


#: 提示词文件读不出来时发的那条节点告警（§14.1.1）。
#: 措辞刻意**不写**「模型调用失败」类字眼：这是**配置错误**（文件缺失），不是模型挂了 ——
#: 免得被 `plan_assembler.MODEL_FAILURE_MARKERS` 误判成一次失败的模型调用。
BEAT_CONFIG_UNUSABLE_MSG = ("节拍展开缺少提示词文件 beat_config.txt（配置错误）："
                            "LLM 细化路径不可用，单层量已按代码推算 / BASE 基线兜底")


def _node_id_by_position(phases, name):
    """按 wbs 阶段顺序取该阶段的 1-based 节点号（与叶子 id 前缀一致）。"""
    for i, ph in enumerate(phases, 1):
        if ph.get("phase") == name:
            return str(i)
    return None


def _last_leaf_id(phase):
    wps = phase.get("work_packages") or []
    for wp in reversed(wps):
        subs = wp.get("sub_packages") or []
        if subs:
            return subs[-1]["id"]
    return None


def _qty_source_summary(leaves):
    """统计该阶段「单层量来源」的工序数：{"参数推算": 3, "占比表拆分": 2, "基线默认": 1}。

    按**工序粒度**计数（不是叶子数：一层一段时同一工序有几十片叶子，数叶子没有意义）。
    口径与 beat_configs.derive_beat_quantities 的 detail 一致：
    `_qty_source == "参数推算"` 表示按项目参数换算，`"占比表拆分"` 表示量来自
    `Component_Ratio`（**B3/B4 的唯一真源**）；其余（含 "基线默认"/缺字段）按基线计。
    """
    seen = {}
    for leaf in leaves or []:
        name = leaf.get("_step_name") or leaf.get("name") or ""
        if name in seen:
            continue
        src = leaf.get("_qty_source") or SOURCE_BASE
        if src == SOURCE_RATIO:
            seen[name] = SOURCE_RATIO
        else:
            seen[name] = SOURCE_PARAM if src == SOURCE_PARAM else SOURCE_BASE
    summary = {}
    for src in seen.values():
        summary[src] = summary.get(src, 0) + 1
    return summary


def _build_phase_leaf_map(phases):
    """node_id(按位置) → 该阶段信息（末叶 + 各分区分段阶梯），供跨相 lead_in 解析。

    E5-a：跨相搭接不再只认"前阶段最后一片叶子"（把 `floors_ahead` 的领先接在最后
    一层之后 = 等于没提前），而是按 `lead_in.floors_ahead` 到**前阶段第 (1+N) 层
    所在那一段**去取挂接点 —— 解析见 `layer_engine._zone_lead_leaves`。

    裁定-1（2026-09-21）：阶梯**按分区各存一份** —— 多分区时每个分区的首段首工序
    都要挂到"前阶段**同分区**的领先段"（同来路、同 lag=0），与 `structural_deps`
    docstring 里「第 2 个分区（Ⅱ区）首段首工序：独立起点，不依赖第 1 个区」一致。

    返回值形状：`{node_id: {"last": 末叶 id, "zone_segments": {分区号: 分段阶梯}}}`。
    `layer_engine.structural_deps` 同时兼容旧的裸字符串形状（那时没有层位信息，
    退回旧口径：末叶 + floors_ahead×2 天，且只挂第 1 个分区）。
    """
    m = {}
    for i, ph in enumerate(phases, 1):
        lid = _last_leaf_id(ph)
        if lid:
            m[str(i)] = {"last": lid, "zone_segments": _zone_ladders(ph)}
    return m


#: 叶子 `location` / `name` 里的楼层区间（由 `layer_engine._fmt_range` 产生，
#: 如 "1-1层" / "1-0.5层" / "1-3层"）。
_FLOOR_RANGE_RE = re.compile(r"(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)层")


def _floor_range_end(text):
    """从 "Ⅰ区 1-3层 内墙抹灰" 里取该段**覆盖到的最后一层**（取不到 → None）。

    `_fmt_range(start, end-1)` 的第二个数字就是"段末层"：一层一段 "1-1层"→1、
    "4-4层"→4；装饰 3 层一组 "1-3层"→3、"4-6层"→6；地下室 0.5 层一段
    "1-0.5层"→0.5、"1.5-1层"→1。是**结构化定位**，不做任何语义猜测。
    """
    m = _FLOOR_RANGE_RE.search(text or "")
    if not m:
        return None
    try:
        return float(m.group(2))
    except (TypeError, ValueError):
        return None


def _zone_ladders(phase):
    """**各分区**的分段阶梯：`{分区号: [{"segment", "end_floor", "leaf"}, ...]}`（段号升序）。

    每个分区按段号升序排列，每段取**最后一道工序**（`_step` 最大者）—— 那就是
    "这一步做完了"的挂接点。非节拍阶段的叶子没有 `_zone`/`_segment` → 返回 `{}`
    （调用方按"无层位信息"处理）。全楼平行专项（`_parallel`，挂在
    `z = len(zones)+pi`）不参与竖向流水，一律跳过。
    """
    by_zone = {}
    for wp in phase.get("work_packages") or []:
        for leaf in wp.get("sub_packages") or []:
            if leaf.get("_parallel"):
                continue
            zone_no = leaf.get("_zone")
            seg_no = leaf.get("_segment")
            if zone_no is None or seg_no is None:
                continue
            step_no = int(leaf.get("_step") or 0)
            segs = by_zone.setdefault(zone_no, {})
            cur = segs.get(seg_no)
            if cur is None or step_no > cur["step"]:
                segs[seg_no] = {
                    "segment": int(seg_no),
                    "step": step_no,
                    "end_floor": _floor_range_end(
                        leaf.get("location") or leaf.get("name") or ""),
                    "leaf": leaf.get("id"),
                }
    out = {}
    for zone_no, segs in by_zone.items():
        items = []
        for seg_no in sorted(segs):
            item = segs[seg_no]
            item.pop("step", None)
            items.append(item)
        out[zone_no] = items
    return out


def _dedupe_deps(deps):
    seen = set()
    out = []
    for d in deps:
        key = (d["predecessor"], d["successor"], d.get("lag_days") or 0)
        if key not in seen:
            seen.add(key)
            out.append(d)
    return out


def merge_beat_deps(deps, beat_deps, beat_leaf_ids, leaves):
    """把节拍引擎的结构搭接并进 deps 节点产生的依赖。

    规则：
      - LLM/顺序链里两端都在节拍叶子上的边 → 删除（节拍搭接由代码独占）
      - 并进 beat_deps → 去重 → 无环则采用；有环 → 回退整棵顺序链（保证 CPM 有解）
    """
    from .deps_gen import has_cycle, default_chain_deps   # 惰性，防循环导入
    beat_ids = set(beat_leaf_ids or [])
    if not beat_deps:
        return _dedupe_deps(deps)
    filtered = [d for d in deps
                if not (d["predecessor"] in beat_ids and d["successor"] in beat_ids)]
    merged = _dedupe_deps(list(beat_deps) + list(filtered))
    if has_cycle(merged, leaves):
        return default_chain_deps(leaves)   # 缓存一致，回退顺序链
    return merged


class BeatExpandNode(BaseNode):
    name = "beat_build"
    title = "节拍流水展开"

    def __init__(self, llm=None, refine=True):
        super().__init__()
        self.llm = llm
        self.refine = refine      # 是否尝试 LLM 细化配置（测试可关）
        self.used_fallback = {}   # node_name -> fallback 层级说明

    def _llm(self):
        if self.llm is None:
            self.llm = LLMClient()
        return self.llm

    @property
    def llm_usable(self):
        try:
            if self.llm is not None:
                return True
            return bool(_cfg.LLM_API_KEY)
        except Exception:
            return False

    # ---------------- 入口 ----------------
    def run(self, ctx):
        wbs = ctx.get("wbs") or {}
        phases = wbs.get("phases") or []
        params = ctx.get("extracted_params") or {}

        beat_leaf_ids = []
        beat_deps = []
        beat_subtrees = {}

        self.emit("node_progress", {"node": self.name, "progress": 10,
                                    "message": "找出适合按节拍流水施工的阶段"})
        # 域 3.4：不再"只认 4 个阶段名" —— 先按分部 **key**（结构标识）认节拍分部，
        # 认不出才退回阶段名（见 `beat_configs.beat_phase_name`）。4 个阶段名只是与
        # 10 个一级分部中的 4 个同名（历史名称复用），不该是查节点的唯一途径。
        beat_phases = [(ph, beat_phase_name(ph)) for ph in phases]
        beat_phases = [(ph, nm) for ph, nm in beat_phases if nm]
        n_beat = len(beat_phases)

        for i, (ph, name) in enumerate(beat_phases, 1):
            node_id = _node_id_by_position(phases, ph["phase"])
            if not node_id:
                continue
            self.emit("node_progress", {"node": self.name,
                                        "progress": 10 + int(80 * i / max(1, n_beat)),
                                        "message": "正在分段铺流水：%s（%d/%d）"
                                                   % (ph['phase'], i, n_beat)})
            cfg = self._resolve_config(ctx, name, node_id, params, ph=ph)
            # ⚠️ 第 7 批（2026-09-21）：**全部工序「量0出局」的节拍阶段必须可见**，不静默。
            # 这种阶段一片叶子都不进树（`expand_node` / `structural_deps` 都按
            # `active_steps` 过滤）。原先它只是"悄悄少了这个阶段"；更糟的是
            # `structural_deps` 会因此在 `steps[0]` 上崩掉整条流水线（已在
            # `layer_engine.structural_deps` 修好，这里补上"为什么会少一个阶段"的留痕）。
            _active = LE.active_steps(cfg.get("cycle") or [],
                                      cfg.get("attach_measures") or [], params)
            if not _active:
                self.emit("warning", {
                    "node": self.name,
                    "message": "该节拍阶段的工序全部「量0出局」，本阶段不进树"
                               "（不会有叶子，也不会有搭接边）",
                    "detail": "阶段：%s；声明工序 %d 道全部被判 missing"
                              "（工种有用户总量，但占比表 `Component_Ratio` 里没有该 L4 的行）"
                              % (name, len(cfg.get("cycle") or [])
                                 + len(cfg.get("attach_measures") or [])),
                })
            phase_dict, ids = LE.expand_node(cfg, params)
            ph["work_packages"] = phase_dict["work_packages"]   # 先落子树，确保前驱相暴露真实叶子
            # 现时重算位置映射：后续相(二次←主体/装修←二次)的跨相 lead_in 解析到真实末叶
            phase_leaf_map = _build_phase_leaf_map(phases)
            deps = LE.structural_deps(cfg, phase_map=phase_leaf_map, params=params)

            beat_leaf_ids.extend(ids)
            beat_deps.extend(deps)
            # 单层量来源计数（各工序用了「参数推算」还是「基线默认」）—— 供前端展示溯源
            qty_summary = _qty_source_summary(
                [l for wp in phase_dict["work_packages"] for l in wp.get("sub_packages") or []])
            leaves_flat = [l for wp in phase_dict["work_packages"]
                           for l in wp.get("sub_packages") or []]
            # ---- 域 4.1b：候选集硬约束（每条叶子的 L4 所属 L3 必须 ∈ 本分部 kb 列表）----
            bad_candidates = validate_l4_candidates(name, leaves_flat)
            if bad_candidates:
                self.emit("warning", {
                    "node": self.name,
                    "message": "叶子 L4 的工种不在该分部的候选集内（违反候选集硬约束）",
                    "detail": "; ".join(
                        "%s → %s（允许 %s）" % (b["leaf_id"], b["work_type_id"],
                                                "、".join(b["allowed"]))
                        for b in bad_candidates[:5]),
                })
            beat_subtrees[name] = {
                "node_id": node_id, "leaf_ids": ids, "cfg": cfg,
                "floors": LE._eff_floors(cfg, params),                     # 项目参数优先
                "floors_per_segment": cfg.get("floors_per_segment"),
                "zones": LE._effective_zones_count(cfg, params),
                # 建议平面段数；None = 标准层面积取不到，已沿用配置 zones（AI 默认，须让用户可改）
                "suggested_zones": suggest_zones_from_params(params),
                "fallback": self.used_fallback.get(name),
                "qty_source_summary": qty_summary,
                # ---- 第 5 批留痕：编号口径 / 候选集硬约束 / L4 未解析 ----
                "numbering": {
                    "scheme": "div.l3.l4.zone.segment",
                    "l3_order": [wp["id"] for wp in phase_dict["work_packages"]],
                    "candidates_bad": bad_candidates,
                    "l4_order": cfg.get("_l4_order"),
                    "l4_unresolved": cfg.get("_l4_unresolved") or [],
                },
                # ---- B3/B4/B5 留痕：占比表驱动了几道工序 / 哪几道量0出局 / 哪几处降级 ----
                "ratio": {
                    "steps": phase_dict.get("ratio_steps"),
                    "exclusions": phase_dict.get("ratio_exclusions") or [],
                    "degradations": phase_dict.get("ratio_degradations") or [],
                },
            }

        # C. 归一化不变量（纯数字点分 id 幂等保留；重编号只作用于非法 id）
        self.emit("node_progress", {"node": self.name, "progress": 96,
                                    "message": "检查分段编号并记下搭接关系"})
        # 域 3.5 收口：非节拍叶子也把「楼层范围」补成**显式的"没有"**（键缺失 ≠ None）
        _stamp_non_beat_layer_fields(wbs)
        wbs2, warns = normalize_wbs(wbs)
        if wbs2 is None:
            wbs2 = wbs
        if warns:
            ctx.setdefault("wbs_warnings", []).extend(warns)
        ctx["wbs"] = wbs2

        ctx["beat_leaf_ids"] = list(dict.fromkeys(beat_leaf_ids))
        ctx["beat_subtrees"] = beat_subtrees
        deps2, dwarn = normalize_deps_self(beat_deps, wbs2)
        if dwarn:
            ctx.setdefault("wbs_warnings", []).extend(dwarn)
        ctx["beat_deps"] = deps2

        fallback_names = {k for k, v in beat_subtrees.items() if v.get("fallback")}
        # 单层量来源合计（各阶段工序数相加）：参数推算 N 项 / 基线默认 M 项
        n_param = sum((v.get("qty_source_summary") or {}).get(SOURCE_PARAM, 0)
                      for v in beat_subtrees.values())
        n_ratio = sum((v.get("qty_source_summary") or {}).get(SOURCE_RATIO, 0)
                      for v in beat_subtrees.values())
        n_base = sum((v.get("qty_source_summary") or {}).get(SOURCE_BASE, 0)
                     for v in beat_subtrees.values())
        self.done_summary = (f"节拍流水已分段：{n_beat} 个阶段、"
                             f"{len(ctx['beat_leaf_ids'])} 条工序、{len(deps2)} 处搭接；"
                             f"每层工程量 {n_ratio} 项按占比表（Component_Ratio）拆分、"
                             f"{n_param} 项按参数推算、{n_base} 项用默认值"
                             + (f"；以下阶段没有配置节拍、按默认节拍处理："
                                f"{'、'.join(sorted(fallback_names))}" if fallback_names else ""))
        self.emit("node_progress", {"node": self.name, "progress": 100,
                                    "message": "分段与搭接都铺好了"})
        return {"wbs": wbs2}

    # ---------------- 配置解析：LLM → 代码修补 → BASE 基线 ----------------
    def _resolve_config(self, ctx, phase_name, node_id, params, ph=None):
        base = self._base_config(phase_name, node_id, ph=ph)
        if self.refine and self.llm_usable:
            user = ("节拍节点名：" + phase_name + "\n项目参数："
                    + json.dumps(params or {}, ensure_ascii=False)
                    + "\n基线配置：\n" + json.dumps(base, ensure_ascii=False))
            try:
                raw = self._llm().chat_json(load("beat_config.txt"), user, temperature=0.2)
                refined = self._refined_config(raw, base, phase_name, params)
                # ① 校验 → ② 代码自动修补（竖向口径/段数/节拍域）→ 失败回 BASE(③)
                phase_dict, ids = LE.expand_node(refined, params)
                errs = LE.common_validate(refined, phase_dict, params)
                if not errs:
                    self.used_fallback[phase_name] = None
                    return refined
                patched = self._auto_patch(refined, errs, params)
                if patched:
                    # 修补后再归一竖向口径（防修补把结构类退回跨层分段）
                    normalize_vertical_split(phase_name, patched,
                                             floors=LE._eff_floors(patched, params))
                    p_ph, p_ids = LE.expand_node(patched, params)
                    if not LE.common_validate(patched, p_ph, params):
                        self.used_fallback[phase_name] = "代码修补(" + ";".join(errs[:2]) + ")"
                        return patched
            except OSError as exc:
                # ⚠️ §14.1.1：提示词文件读不出来 = **配置错误，必须可见**，不许静默吞。
                # 实测事实：`prompts/beat_config.txt` **不在仓库里**，`load()` 走
                # `read_text` 抛 FileNotFoundError（OSError 子类），原来它和别的异常一起被
                # `except (LLMError, Exception): pass` 吞掉 —— 于是「节拍展开的 LLM 细化
                # 路径 100% 不生效、量永远来自 BASE 基线」这件事在终端、计划 JSON、交付物里
                # **一个字都不留**，用户无从知道。
                # 现在走引擎既有的节点告警通道（`emit("warning")` → engine.collect_node_warning
                # → ctx["node_warnings"] → meta.node_warnings），产物里能看到。
                # **行为一字不变**：仍然降级到 BASE 基线（不新建 beat_config.txt、
                # 不激活这条沉睡的 LLM 路径、不改任何工程量）。
                self.emit("warning", {
                    "node": self.name,
                    "message": BEAT_CONFIG_UNUSABLE_MSG,
                    "detail": "%s（节拍阶段：%s）" % (exc, phase_name),
                })
            except (LLMError, Exception):
                # 其它异常（模型不可用 / 返回畸形 / 修补后仍不合法）保持原样：静默降级到
                # BASE 基线 —— 这是既有的失败链语义，本次只把「文件缺失」单独拎出来。
                pass
        # ② 起手/③ 降级：BASE 基线本就被引擎约束在合法域（节拍[2,90]、覆盖全层）
        self.used_fallback[phase_name] = BASE_BEAT_CONFIGS.get(phase_name, {}).get("fallback_reason") or None
        return base

    @staticmethod
    def _base_config(phase_name, node_id, ph=None):
        cfg = copy.deepcopy(BASE_BEAT_CONFIGS.get(phase_name) or {})
        cfg["node_id"] = node_id
        # ---- 域 3.2：工序清单 + 先后顺序由 LLM 产出 ----
        # `l4_order` 是**有序**的 L4 清单（含工程量为 0 的工序），由 `wbs_agent` 从
        # LLM 的 WBS 输出里落库（键名见本批派工单）。它只决定**先后顺序**：
        # 编号（③ L4工序号）由 `layer_engine` 在完整清单上分配，**LLM 不编号**。
        # LLM 没给（离线 / 老计划 / 提示词未生效）→ 不写该键，退回"声明顺序即施工先后"，
        # 行为与第 4 批完全一致（`test_algorithm_parity.py` 钉着这条）。
        if isinstance(ph, dict) and ph.get("l4_order"):
            cfg["l4_order"] = ph["l4_order"]
        return cfg

    @staticmethod
    def _refined_config(raw, base, phase_name=None, params=None):
        """LLM 返回的字段覆盖基线。只接受白名单键，防跑偏。

        竖向分段最后**强制归一**（normalize_vertical_split）：LLM 给的 segments /
        floors_per_segment 只作参考，结构类必须回到「一层一段」，装饰装修「3 层一组」，
        防 LLM 按老模板又输出「5 层一段」把缺陷带回来。
        """
        out = copy.deepcopy(base)
        if not isinstance(raw, dict):
            return normalize_vertical_split(phase_name, out, floors=LE._eff_floors(out, params))
        for key in ("segments", "floors_per_segment"):
            if raw.get(key) is not None:
                try:
                    v = float(raw[key])
                    if v > 0:
                        out[key] = v
                except (TypeError, ValueError):
                    pass
        for key in ("cycle", "attach_measures", "parallel_work"):
            if isinstance(raw.get(key), list):
                out[key] = raw[key]
        if isinstance(raw.get("lead_in"), dict) or raw.get("lead_in") is None:
            pass
        return normalize_vertical_split(phase_name, out, floors=LE._eff_floors(out, params))

    @staticmethod
    def _auto_patch(cfg, errs, params):
        """代码自动修补：层数守恒/节拍域违规时微调参数后重试。失败返回 None。

        只碰「非一层一段」的阶段（结构类的竖向口径由 normalize_vertical_split 兜住，
        一层一段本身必然层数守恒，无需修补）。
        """
        phase = cfg.get("node_name")
        patched = copy.deepcopy(cfg)
        if phase in ONE_FLOOR_PER_SEGMENT_PHASES or phase in GROUPED_SEGMENT_PHASES:
            return patched
        for e in errs:
            if "层数守恒" in e:
                floors = LE._eff_floors(cfg, params)
                if cfg.get("floors_per_segment"):
                    patched["floors_per_segment"] = max(0.5, floors / max(1, int(
                        len(LE.segment_floors(floors, int(cfg.get("segments") or 1),
                                              per=float(cfg.get("floors_per_segment")))) + 1)))
            # 节拍域越界由引擎 clamp 保证不产生，此处仅兜底
        return patched


def _stamp_non_beat_layer_fields(wbs):
    """给**非节拍叶子**补「楼层范围」三键（幂等，只补不改，域 3.5 收口）。

    域 3.5 要求"每个 L4 的楼层范围结构化"：可分层活动给真实范围，
    **不展开的活动明确没有**（`None`，而不是"键缺失"——两者对消费方是两件事）。
    节拍叶子由 `layer_engine._make_leaf` / 平行分支写；非节拍叶子
    （工作包级的 3 段 id 叶子，如 `1.1.1 场地平整`）不是层×区展开出来的，
    在这里统一补成 `None / 0 / False`。用 `setdefault` ⇒ **绝不覆盖**已有值
    （节拍叶子的真实楼层范围、以及任何上游已写的值都不动）。
    """
    for ph in (wbs or {}).get("phases") or []:
        if not isinstance(ph, dict):
            continue
        for wp in ph.get("work_packages") or []:
            if not isinstance(wp, dict):
                continue
            for leaf in wp.get("sub_packages") or []:
                if not isinstance(leaf, dict) or "_beat" in leaf:
                    continue
                leaf.setdefault("floor_range", None)
                leaf.setdefault("floors", 0.0)
                leaf.setdefault("layer_expandable", False)
    return wbs


def normalize_deps_self(deps, wbs):
    """节拍结构搭接在写入前轻校验：只保留两端都存在的叶子；id 已是纯数字。"""
    from .deps_gen import collect_leaf_ids  # 惰性，防循环导入
    leaves = set(collect_leaf_ids(wbs))
    out, warns = [], []
    for d in deps:
        if d["predecessor"] in leaves and d["successor"] in leaves:
            out.append(d)
        else:
            warns.append(f"节拍依赖端点缺失已跳过：{d['predecessor']}→{d['successor']}")
    return out, warns
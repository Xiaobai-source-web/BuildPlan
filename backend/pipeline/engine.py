"""流水线引擎内核 — T-08

能力：
- 顺序执行节点，context 字典在节点间传递
- 每节点执行前 deepcopy 检查点 → 支持 /retry、/edit 节点重入
- 暂停点（pause_point=True）：完成发 node_paused 并阻塞等待 registry
- /cancel：置位 cancel 标志，下一节点入口中断
- 事件顺序强制：node_start → (node emit node_progress) → node_done

节点契约：继承 BaseNode，实现 run(ctx) -> dict（产物合并回 ctx），
可设置 self.done_summary；需要推送进度时 self.emit(...)。
"""

import copy
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path

from . import usage
from .events import (
    EV_DONE,
    EV_ERROR,
    EV_NODE_DONE,
    EV_NODE_PAUSED,
    EV_NODE_START,
)
# 人工门的等待上限（第 35 轮统一到这里，第 36 轮提到默认 60 分钟；见 registry 里的说明）
from .registry import GATE_TIMEOUT_SECONDS  # noqa: F401

# 文案里显示的分钟数。**从常量算出来**，不许再各处手写"30 分钟"——
# 上限改过一次，散落的文案就会说谎（第 35 轮就发生过：常量提到 30 分钟，
# 而终端提示词里还写着 10 分钟，用户按旧数字预期，反而更容易踩坑）。
GATE_TIMEOUT_MINUTES = max(1, int(GATE_TIMEOUT_SECONDS) // 60)


class PipelineCancelled(Exception):
    """用户否决/取消导致的中止。"""


def _new_pause_id(node, run_id):
    return f"pause_{node.name}_{run_id}_{uuid.uuid4().hex[:6]}"


#: 节点级告警在 ctx 里的落点。`plan_assembler.build_meta` 把它**原样透传**进计划
#: `meta["node_warnings"]` —— 两边必须用同一个键名，改这里要同步改那边。
NODE_WARNINGS_CTX_KEY = "node_warnings"


def collect_node_warning(ctx, payload):
    """把一条 `emit("warning", payload)` 记进 `ctx["node_warnings"]`（去重 + 计数）。

    ⚠️ 存在的唯一理由（真实事故，第 42 轮）：
      `boundary.py` 在"补全边界条件时模型调用失败、已退回关键词兜底"时只发了一条
      `warning` 事件。它飘到终端/UI 上就没了 —— **计划 JSON 与交付物里一个字都不留**。
      后果实测：`plan_sample3_after_org_v2`（32 次模型调用、¥0.8）的
      `meta.boundary_conditions` 里 labor / equipment / 材料清单 全空（`peak_total: null`、
      `[]`、`[]`），产物的样子却完全正常，用户无从知道"模型这次没帮上忙"。
      这是**产品级的诚实性缺陷**，不是某一个节点的问题 —— 所以收集点放在**引擎**这条
      所有节点告警的必经之路（`base.BaseNode.emit` → `Pipeline.run` 注入的 `_emit`），
      任何节点（含以后新加的）发 `warning` 都会被留档，不需要节点自己记得写。

    两条口径：
      · **只收 `"warning"`**：`node_progress` 每个节点发好几条（32 次模型调用那类运行会
        产生上百条），收进 ctx 只会把它撑爆，落盘进计划更没有价值。
      · **去重按 `(node, message, detail)`**，重复的只累加 `count`：同一节点在同一处反复
        降级（例如每个子任务都退一次兜底）不该在产物里刷 N 条同样的字。

    任何异常一律吞掉：告警留档是**旁路**，绝不能因为它把流水线弄挂。
    """
    try:
        if not isinstance(ctx, dict) or not isinstance(payload, dict):
            return
        node = str(payload.get("node") or "")
        message = str(payload.get("message") or "")
        detail = str(payload.get("detail") or "")
        items = ctx.get(NODE_WARNINGS_CTX_KEY)
        if not isinstance(items, list):
            # 键缺失 / 被写成别的类型（老 ctx、节点同名键）→ 就地换成一个新列表，
            # 不抛异常、也不去"修"别人的数据。
            items = []
            ctx[NODE_WARNINGS_CTX_KEY] = items
        for it in items:
            if not isinstance(it, dict):
                continue
            if (it.get("node") == node and it.get("message") == message
                    and it.get("detail") == detail):
                it["count"] = int(it.get("count") or 1) + 1
                return
        items.append({"node": node, "message": message, "detail": detail,
                      "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                      "count": 1})
    except Exception:
        pass


def _emit_with_warning_log(emit, ctx):
    """包一层 emit：`"warning"` **先落 ctx**，其余事件原样转发（一字不改）。

    只拦 `"warning"` 一种事件名；转发本身不做 try —— 下游 SSE 队列的异常照旧抛，
    与本次改动之前的行为完全一致（这次只加"留档"这一件事，不碰事件语义）。
    """
    def _emit(event, data):
        if event == "warning":
            collect_node_warning(ctx, data)
        emit(event, data)
    return _emit


def find_wbs_task(wbs, task_id):
    """在 wbs 三层结构中按 id 查叶子任务。"""
    if not isinstance(wbs, dict):
        return None
    for phase in wbs.get("phases", []):
        for wp in phase.get("work_packages", []):
            for sub in wp.get("sub_packages", []):
                if sub.get("id") == task_id:
                    return sub
    return None


def _to_number(value):
    """把 /edit 的数值参数归一为 int/float：10 → 10，10.0 → 10，'10' → 10，'10.5' → 10.5。

    非数值字符串（如日期、名称）原样返回。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return int(value) if isinstance(value, float) and value.is_integer() else value
    if isinstance(value, str):
        s = value.strip()
        try:
            num = float(s)
        except ValueError:
            return value
        return int(num) if num.is_integer() else num
    return value


def apply_edits(ctx, edits):
    """把 /edit 的 edits dict 应用到 ctx（见 §6.3）。

    支持键：
      duration.<task_id>=N   → wbs 叶子任务工期
      quantity.<task_id>=N   → wbs 叶子任务工程量
      total_<param>=N        → extracted_params 参数
      extracted_params.<k>=V → extracted_params 参数
      a.b.c=V                → 通用深路径设置
    """
    for key, value in edits.items():
        parts = [p for p in str(key).split(".") if p]
        if not parts:
            continue

        if parts[0] in ("duration", "quantity") and len(parts) >= 2:
            # 任务 ID 形如 1.1.1，被 split(".") 拆散，需重新 join 还原
            task_id = ".".join(parts[1:])
            task = find_wbs_task(ctx.get("wbs"), task_id)
            if task is not None:
                field = f"{parts[0]}_days" if parts[0] == "duration" else parts[0]
                task[field] = _to_number(value)
            continue

        if (parts[0] == "extracted_params"
                or parts[0].startswith("total_")
                or parts[0] in ("planned_start_date", "project_name")):
            ep = ctx.setdefault("extracted_params", {})
            if isinstance(ep, dict):
                ep[parts[-1]] = value
            continue

        # 通用深路径
        node = ctx
        for p in parts[:-1]:
            node = node.setdefault(p, {})
            if not isinstance(node, dict):
                break
        else:
            node[parts[-1]] = value


class Pipeline:
    """节点流水线：顺序执行 + 检查点 + 暂停点迭代 + 中途取消。"""

    def __init__(self, run_id=None, registry=None):
        self.nodes = []
        self.run_id = run_id or f"run_{int(time.time() * 1000)}"
        self.registry = registry
        self._cancel_evt = threading.Event()
        self._emit = None
        self._checkpoints = {}

    # ---------------- 构建 ----------------
    def add_node(self, node):
        self.nodes.append(node)
        return node

    def add_nodes(self, *nodes):
        for n in nodes:
            self.add_node(n)
        return self

    def cancel(self):
        self._cancel_evt.set()

    @property
    def cancelled(self):
        return self._cancel_evt.is_set()

    # ---------------- 执行 ----------------
    def run(self, ctx, emit=None):
        self._emit = emit or (lambda event, data: None)
        # ---- 唯一收集点（第 42 轮）----
        # 所有节点的事件都经由下面 `node._emit = self._emit` 走这一条路，所以在这里包
        # 一次就能覆盖全部节点；节点自己不必记得留档，也不会漏掉以后新加的节点。
        # 为什么必须留档：节点级告警（尤其"模型调用失败、退回兜底"）原来只发事件，
        # 事件飘到终端就没了 —— 计划 JSON / 交付物里一个字都不留，失败对用户不可见。
        self._emit = _emit_with_warning_log(self._emit, ctx)
        self._checkpoints = {}
        self._pending_edits = None      # 重入时待应用的 edits
        self._editing_node = None
        # 本次运行的**步数表**（第 32 轮）：终端要显示"第 k / N 步"，而它自己不认识
        # 流水线（节点顺序、总数都在后端）。所以开跑先下发一份全链清单，
        # 每个 node_start 再带上"我是第几步" —— 终端不必猜、也不会算错。
        self._emit("run_plan", {
            "run_id": self.run_id,
            "steps": [{"index": i, "node": n.name, "title": n.title}
                      for i, n in enumerate(self.nodes, 1)],
        })
        i = 0
        while i < len(self.nodes):
            node = self.nodes[i]

            if self._cancel_evt.is_set():
                self._emit(EV_DONE, {
                    "status": "cancelled",
                    "note": ("本次运行已取消。\n"
                             "如果是你在某道人工门（WBS 复评 / 审计 / 参数补充）上作答后看到这句，"
                             "多半是那道门**已经等超时关掉了**（现为 %d 分钟），你那一行答的是过期的题；"
                             "直接重新描述项目即可再开始。" % GATE_TIMEOUT_MINUTES),
                    "run_id": self.run_id})
                return

            # 检查点：仅首次执行该节点时记录（重跑复用最初的 pre-node 状态）
            self._checkpoints.setdefault(node.name, copy.deepcopy(ctx))

            # 注入运行时依赖
            node._emit = self._emit
            node._registry = self.registry
            node._run_id = self.run_id
            node._cancel_evt = self._cancel_evt

            self._emit(EV_NODE_START, {"node": node.name, "title": node.title,
                                       "step": i + 1, "steps": len(self.nodes)})
            try:
                usage.set_current_node(node.name)     # 把 LLM 用量归属到当前环节
                result = node.run(ctx)
            except PipelineCancelled:
                self._emit(EV_DONE, {"status": "cancelled", "run_id": self.run_id})
                return
            except Exception as e:
                # ⚠️ 第 7 批（2026-09-21）：**节点异常的堆栈绝不许吞**。
                # 原先这一行只发 `f"{e}"`（如「list index out of range」），异常发生在
                # **哪个文件、哪一行完全丢失**。实测代价：交付包 v3.1 跑
                # `项目样例\示例3_住宅楼_对比版.txt` 时 `beat_build` 失败，
                # 终端与 `backend\_launch.log` 里都只有一句错误文本，只能靠"逐处猜 +
                # 直证"才定位到 `layer_engine.structural_deps:679` 的 `steps[0]`。
                # 现在把堆栈写到**两处**；⚠️ 终端事件载荷 `message` **一个字不改**
                # （不往用户界面里灌多行堆栈，那是另一种噪声）：
                #   ① stderr —— `一键测试.py` 把后端 stderr 重定向进 `backend\_launch.log`；
                #   ② `backend\_last_node_error.log` —— 每次覆盖，找起来最省事。
                tb = traceback.format_exc()
                try:
                    print(tb, file=sys.stderr, flush=True)
                except Exception:
                    pass
                try:
                    (Path(__file__).resolve().parents[1] / "_last_node_error.log").write_text(
                        "节点失败：%s\n错误：%s\n\n%s" % (node.name, e, tb),
                        encoding="utf-8")
                except Exception:
                    pass
                self._emit(EV_ERROR, {"node": node.name, "message": f"{e}"})
                self._emit(EV_DONE, {"status": "error", "run_id": self.run_id})
                return

            if isinstance(result, dict):
                ctx.update(result)
                # 节点要求优雅停止（如闲聊意图跳流水线）
                if "_stop" in result:
                    self._emit(EV_DONE, {"status": "ok", "note": result["_stop"],
                                         "run_id": self.run_id,
                                         "usage": usage.meter().snapshot()})
                    return

            # 重入后：把用户 edits 压回 ctx，确保编辑值覆盖节点输出（下游据此重算）
            if self._editing_node == node.name and self._pending_edits:
                apply_edits(ctx, self._pending_edits)
                self._editing_node = None
                self._pending_edits = None

            summary = node.done_summary or f"{node.title}完成"
            self._emit(EV_NODE_DONE, self._done_payload(node, ctx, summary))

            # 暂停点：发 node_paused 并阻塞等待 /resume
            if node.pause_point and self.registry is not None:
                decision = self._pause(node, ctx)
                action = decision.get("action", "continue")
                if action == "abort":
                    reason = str(decision.get("reason") or "")
                    note = ("这道人工门等待超时（%d 分钟）已自动作废，本次运行结束。\n"
                            "重新描述项目即可再开始；作答前不必着急，门会给足时间。"
                            % (GATE_TIMEOUT_SECONDS // 60)
                            if reason == "timeout" else
                            "本次运行已取消。重新描述项目即可再开始。")
                    self._emit(EV_DONE, {"status": "cancelled", "note": note,
                                         "run_id": self.run_id})
                    return
                if action in ("retry", "edit"):
                    self._reenter(node, ctx, decision)
                    continue  # i 不变 → 重跑当前节点
            i += 1

        self._emit(EV_DONE, {"status": "ok", "run_id": self.run_id,
                             "usage": usage.meter().snapshot()})

    # ---------------- 暂停/重入 ----------------
    def _done_payload(self, node, ctx, summary):
        """组装 node_done 事件载荷：**默认**仍是 `{node, summary}` 两个键。

        为什么会有多出来的键（第 23 轮，真实缺陷的护栏）：节点把警告算完只留下一句
        "警告 27 条"，用户在终端永远看不到内容 —— 而终端 `renderer.render_event`
        本来就会渲染 `data["warnings"]`（前 3 条）+ `data["warnings_note"]`
        （"…其余 N 条同类"），只是**没有任何节点把警告送上来**。

        所以这里加了一条**显式 opt-in**：节点声明 `warning_ctx_key = "<ctx 键名>"`，
        引擎才把那个键里的警告原文（以及节点自己算好的 `warning_note`）附在 node_done 上。
        没声明的节点载荷**一字不变**（不新算、不猜、不影响任何既有节点的行为）。
        """
        payload = {"node": node.name, "summary": summary}
        key = getattr(node, "warning_ctx_key", "")
        if not key:
            return payload
        warnings = ctx.get(key)
        if isinstance(warnings, (list, tuple)) and warnings:
            payload["warnings"] = [str(w) for w in warnings]
        note = str(getattr(node, "warning_note", "") or "").strip()
        if note:
            payload["warnings_note"] = note
        return payload

    def _pause(self, node, ctx):
        pause_id = _new_pause_id(node, self.run_id)
        self.registry.register(pause_id)
        self._emit(EV_NODE_PAUSED, {
            "pause_id": pause_id,
            "node": node.name,
            "output_summary": node.done_summary or f"{node.title}完成",
            "context_summary": self._context_summary(ctx),
        })
        return self.registry.wait(pause_id, cancel_evt=self._cancel_evt,
                                  timeout=GATE_TIMEOUT_SECONDS)

    def _reenter(self, node, ctx, decision):
        """恢复检查点、应用 edits/instruction，准备重跑当前节点。

        - edits 重跑前先应用（节点可读取编辑后的输入），重跑后由 run 循环再压回一次
          （确保编辑值覆盖节点输出，下游据此重算）。
        - instruction 直接写入 ctx，节点运行前即可读取。
        """
        snap = self._checkpoints.get(node.name)
        if snap is not None:
            ctx.clear()
            ctx.update(copy.deepcopy(snap))
        self._editing_node = node.name
        self._pending_edits = None
        if decision.get("action") == "edit" and decision.get("edits"):
            apply_edits(ctx, decision["edits"])        # 前向：节点可读
            self._pending_edits = decision["edits"]    # 后向：覆盖节点输出
            ctx["_last_edit"] = decision["edits"]
        if decision.get("instruction"):
            ctx["_extra_instruction"] = decision["instruction"]
            ctx["_extra_for"] = node.name

    def _context_summary(self, ctx):
        """暂停时给用户看的摘要：总工期 / 关键路径 / 人工峰值 / 风险。"""
        bits = []
        cpm = ctx.get("cpm_result") or {}
        if cpm.get("total_duration_days"):
            bits.append(f"总工期{cpm['total_duration_days']}天")
        cp = cpm.get("critical_path") or []
        if cp:
            bits.append(f"关键路径{len(cp)}任务")
        rd = ctx.get("resource_demand") or {}
        peak = 0
        for t in (rd.get("tasks") or []):
            for rname, q in (t.get("resources") or {}).items():
                if rname in ("普工", "钢筋工", "模板工", "混凝土工", "瓦工", "抹灰工"):
                    peak += q.get("per_day", 0)
        if peak:
            bits.append(f"人工峰值约{peak}人")
        risks = ctx.get("risks") or []
        if risks:
            bits.append(f"主要风险{len(risks)}条")
        return " / ".join(bits) or "（尚无中间结果）"

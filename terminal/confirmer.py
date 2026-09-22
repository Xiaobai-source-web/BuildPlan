"""人工确认与暂停迭代交互 — T-06

配合 client 的可暂停生成器使用（§5.3 时序）：
收到 confirm_required / node_paused 时，调用方先 client.pause()，
进入本模块阻塞等用户输入，完成后 POST /confirm 或 /resume，
再 client.resume() 继续读同一 SSE 连接。

第 16 轮改造（界面外壳）后这里多两件事：
  1. **门的正文**由本模块打印（`renderer.render_audit_gate`）—— 正文和它的输入提示
     是同一次交互，放一起才不会被别的输出插队，也不会重复。
  2. 读取输入统一走 `tui.ask()`：VT 模式在**底部输入框**里读，退化模式是
     「满宽细线 + 同一行提示符」；两条路最终都调用内建 `input()`，
     所以脚本、管道的 stdin、以及测试里 monkeypatch input 都照旧可用。
"""

import re

import renderer
import tui


def _report_delivery(result, what="这一行"):
    """把人工门决策的返回值如实告诉用户 —— 不许静默。

    第 35/36 轮两次真实缺陷（用户实测）：
      · 「我输入 1，流程反而取消了」—— WBS 复评门；
      · 「为什么我输入 Y，却直接退出了计划」—— R2 两版工期审计门。
    两次的机制**完全一样**：那道门已经等超时关掉了，后端 registry 里没有这个 id，
    `POST /resume` 或 `POST /params` 回 `{"ok": false}`，而终端**一个字都不说**。
    用户看到的只有后面那句「⏹ 流程已取消」，于是合理地认为是"我这一下把计划弄没了"。

    更糟的是：`_ask_audit` 走的是 `/params`，而当初只给 `/resume` 补了提示，
    所以"输入 Y 被丢掉"这条路线上**至今没有任何反馈**（本次补上）。

    这里只说事实，不猜测：送达失败 = 门已作废；下一步怎么办由调用方决定。
    """
    ok = None
    if isinstance(result, dict):
        ok = result.get("ok")
    if ok is False:
        tui.out(renderer.color(
            "⚠ %s没有送达：这道门已经结束或超时作废了（单道人工门最多等 %d 分钟）。\n"
            "   如果本次运行已经结束，重新描述一遍项目即可再开始。"
            % (what, _gate_minutes()), "warn"))
    return ok


def _gate_minutes():
    """人工门上限的分钟数 —— 从后端**同一个常量**算，不许手写数字。

    写死过一次就撒过谎：常量从 10 分钟提到 30 分钟后，终端提示里还写着旧数字。
    """
    try:
        import sys
        from pathlib import Path

        backend = str(Path(__file__).resolve().parent.parent / "backend")
        if backend not in sys.path:
            sys.path.insert(0, backend)
        from pipeline.registry import GATE_TIMEOUT_SECONDS
        return max(1, int(GATE_TIMEOUT_SECONDS) // 60)
    except Exception:
        return 60


def _report_resume(result):
    """`/resume`（WBS 复评门等）的送达反馈。"""
    return _report_delivery(result, "这一行")


def _report_params(result):
    """`/params`（三轮回审门 / 参数门）的送达反馈。"""
    return _report_delivery(result, "这一行")


def _ask(hint, rule=True):
    """读一行（走当前 Tui：VT 底部输入框 / 退化模式同屏提示）。"""
    return tui.ask(hint, rule=rule)


def _leave_plan_mode():
    """用户在工作模式门选了"不生成"→ 把终端退回普通模式（第 33 轮）。"""
    try:
        import console as _console
        ctx = _console.current_ctx()
        if ctx is not None and getattr(ctx, "mode", "normal") == "plan":
            _console._set_mode(ctx, "normal", "")
    except Exception:
        pass


def ask_confirm(client, data, run_id=None):
    """confirm_required → [Y/n] → POST /confirm。返回 True=继续 / False=中止。

    strict=True 时（工作模式确认门）：必须输入 Y 才继续，回车/其他视为取消。
    普通确认（原 confirm 节点）保持 enter=Y 默认。
    """
    data = data or {}
    cid = data.get("confirm_id")
    strict = bool(data.get("strict"))
    # 第 33 轮：工作模式确认门**只**在"识别为计划请求"时出现 —— 它是最可靠的
    # 「用户确实要生成计划」信号，所以在这里把终端切到【生成计划】模式
    # （用户要求："每进入一个模式就挂在聊天框上方"）。
    if strict:
        try:
            import console as _console
            ctx = _console.current_ctx()
            if ctx is not None:
                _console._set_mode(ctx, "plan", "")
        except Exception:
            pass
    while True:
        try:
            if strict:
                ans = _ask("  [Y/n，回车=取消] ").strip().lower()
            else:
                ans = _ask("  [Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            # 无法读取输入：按中止处理
            if cid:
                client.post_confirm(cid, False, run_id)
            return False
        if ans in ("y", "yes"):
            if cid:
                client.post_confirm(cid, True, run_id)
            return True
        if strict and not ans:          # 严格模式：回车默认取消
            if cid:
                client.post_confirm(cid, False, run_id)
            _leave_plan_mode()
            return False
        if ans in ("n", "no"):
            if cid:
                client.post_confirm(cid, False, run_id)
            _leave_plan_mode()
            return False
        tui.out("  请输入 Y（继续）或 n（中止）"
                + ("，或直接回车取消" if strict else ""))


def _repairs_of(data):
    """门载荷里的「一键修复」编号选项（缺失/非法一律当空表 → 老界面不变）。"""
    data = data or {}
    return [x for x in (data.get("repairs") or [])
            if isinstance(x, dict) and x.get("key")]


def _pick_repair(text, repairs):
    """门上的编号输入 → 选项 dict。不是编号（或越界）→ None（那就是自由意见）。

    编号是**单个数字**：`1` 选第一个修复，`N+1`（N=修复条数）是"我自己写意见"，
    其余任何输入都按自由修改意见走老路径。
    """
    t = str(text or "").strip()
    if not t.isdigit():
        return None
    idx = int(t)
    if 1 <= idx <= len(repairs):
        return repairs[idx - 1]
    return None


def handle_pause(client, data, run_id=None):
    """node_paused → Y=继续；编号=让系统**真的重做**；其它文本=修改意见 → POST /resume。

    对齐 param_review 门模式：打 Y（或回车）通过；直接输入对 WBS 的修改意见，
    后端主体 LLM 会据此对既有 WBS 开放 2/3级 修改。abort/cancel/quit 取消整次运行。

    规格 §3（第 25 轮）：门上带 `repairs`（编号选择题）时，输入**单个数字**即选择
    "让系统自己修" —— 上行 `action=revise` + `repair_key=<key>` + `instruction=<label>`：
      · 后端认 `repair_key` → 走本节点的真实重做机制（`_expand_phase(retry_req=...)`）；
      · 不认 `repair_key` 的老路径收到的是一条具体的自由意见（label），照样能用、不会崩。
    """
    data = data or {}
    pause_id = data.get("pause_id")
    repairs = _repairs_of(data)
    if repairs:
        hint = ("  [Y=继续 / 输入编号 [1]-[%d] 让系统自己修 / 或直接输入你的WBS修改意见 / abort 取消] "
                % (len(repairs) + 1))
    else:
        hint = "  [Y=继续 / 直接输入WBS修改意见（或 abort 取消）] "
    while True:
        try:
            ans = _ask(hint).strip()
        except (EOFError, KeyboardInterrupt):
            if pause_id:
                _report_resume(client.post_resume(pause_id, "abort", run_id=run_id))
            return
        if ans.lower() in ("", "y", "yes", "continue", "c", "go"):
            if pause_id:
                _report_resume(client.post_resume(pause_id, "continue", run_id=run_id))
            return
        if ans.lower() in ("abort", "cancel", "quit"):
            if pause_id:
                _report_resume(client.post_resume(pause_id, "abort", run_id=run_id))
            return
        if repairs:
            picked = _pick_repair(ans, repairs)
            if picked is not None:
                if pause_id:
                    _report_resume(client.post_resume(pause_id, "revise",
                                       instruction=str(picked.get("label") or picked["key"]),
                                       repair_key=picked["key"], run_id=run_id))
                return
        # 其它任一文本（含越界编号）→ 作为人工修改意见，交给主体 LLM 修订既有 WBS
        if pause_id:
            _report_resume(client.post_resume(pause_id, "revise", instruction=ans, run_id=run_id))
        return


def ask_param_review(client, data, run_id=None):
    """param_review 门 → 打 Y 通过，或输入补充 → POST /params。

    purpose=param（缺省）：打 Y 通过，或输入项目参数作为补充喂给边界条件 LLM。
    purpose=doc：打 Y=输入已是全部数据无需文件，或输入文件路径让 MCP 读取。
    purpose=audit：三轮回审门。先打印本轮**实物内容**（WBS 树 / 两版工期 / 草案目录），
        **打 Y 才算审过**；输入其它任意文字视为审计意见 → 计划保持"未审计"并停在当前阶段。
    回车 / Y / yes → passed=True；其它任一文本 → passed=False, manual_input=该文本；
    中断(Ctrl+C)交给上层取消；EOF → 兜底通过，避免死锁。
    """
    data = data or {}
    review_id = data.get("review_id")
    purpose = data.get("purpose") or "param"

    if purpose == "audit":
        return _ask_audit(client, data, review_id, run_id)

    hint = ("[Y 通过 / 直接输入修正、补充的参数] " if purpose != "doc"
            else "[Y=输入已是全部数据 / 直接输入项目文件路径] ")
    while True:
        try:
            ans = _ask("  " + hint).strip()
        except (EOFError, KeyboardInterrupt):
            if review_id:
                _report_params(client.post_params(review_id, False, run_id=run_id))
            return False
        if ans in ("", "y", "yes"):
            if review_id:
                _report_params(client.post_params(review_id, True, run_id=run_id))
            return True
        # 其它文本：param → 手动补参数交给边界 LLM；doc → 视为文件路径交给 MCP
        if review_id:
            _report_params(client.post_params(review_id, False, manual_input=ans, run_id=run_id))
        return False


def _ask_audit(client, data, review_id, run_id):
    """三轮回审门：打印**实物内容** → 打 Y 通过 / 输入意见退回。

    留空的回车**不算通过**（这一点与其它门相反）：审计是"签字"，手滑回车不能等于
    认可。想通过必须明确打 Y。
    """
    data = data or {}
    # 门与门之间用满宽细线隔开（规格 §B2：「门与门之间…都没有间隔」）
    tui.out(renderer.render_audit_gate(data), rule_before=True)
    # 本轮若是"意见菜单"态（后端在载荷里给了 options_hint），提示语必须跟着换成
    # 「输入 1/2/3 选菜单项」—— 否则提示仍写「直接输入审计意见=退回」，
    # 用户不知道该回编号（实测反馈：菜单态提示语与菜单对不上）。
    options_hint = str(data.get("options_hint") or "").strip()
    prompt = options_hint or "  [Y=审过 / 直接输入审计意见=退回] "
    while True:
        try:
            ans = _ask(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            if review_id:
                _report_params(client.post_params(review_id, False, run_id=run_id))
            return False
        if ans.lower() in ("y", "yes", "通过"):
            if review_id:
                _report_params(client.post_params(review_id, True, run_id=run_id))
            return True
        if ans:
            if review_id:
                _report_params(client.post_params(review_id, False, manual_input=ans, run_id=run_id))
            return False
        tui.out("  本轮审计需要明确打 Y 才算通过（回车不等于认可）。\n"
                "  想退回修改，请直接输入你的审计意见。")


def parse_edits(text):
    """解析 'k=v, k2=v2' 或 'a.b=v' → 嵌套 edits dict。值自动转 int/float。"""
    edits = {}
    for part in re.split(r"[,;\s]+", text.strip()):
        if not part or "=" not in part:
            continue
        k, _, v = part.partition("=")
        k = k.strip()
        v = v.strip()
        if not k:
            continue
        try:
            num = float(v)
            v = int(num) if num.is_integer() else num
        except ValueError:
            pass  # 保留字符串
        keys = k.split(".")
        node = edits
        for kk in keys[:-1]:
            node = node.setdefault(kk, {})
        node[keys[-1]] = v
    return edits

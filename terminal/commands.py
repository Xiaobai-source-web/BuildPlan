"""斜杠命令体系 — T-05

dispatch(ctx, text)：按命令分发，返回 "quit" 表示请求退出终端，否则 None。

命令表（`/help` 也列出）：
  模式（第 34 轮，全英文）：`/mode normal|plan|revise|import`、`/exit`
  常用：`/help` `/show` `/confirm` `/history` `/status` `/switch` `/quit`
  已有计划（第 33 轮）：`/plans` `/open` `/import` `/wbs` `/inputs`
  模型档位（第 35 轮）：`/llm` `/llm-use` `/llm-add` `/llm-del`
  改得动：`/revise` `/versions` `/undo` `/goto` `/sources` `/cost`
  ⚠️ `/retry` `/edit` `/continue` 是**死命令**（主链没有节点声明暂停点，
  引擎的"重跑当前节点"走不到），已从 `_COMMANDS` 与 `/help` 撤下；
  门里中止用 `/abort`（三道门共用 `boundary.is_abort_decision`）。
"""

import json
import os
import subprocess
import webbrowser

import renderer
import switch
import tui


def _cmd_help(ctx, args):
    lines = [
        "/help            显示帮助",
        "/show            浏览器打开当前计划看板",
        "/confirm         确认当前计划",
        "── 模式（输入框左侧一直显示当前模式）──",
        "/mode normal     普通：纯聊天（默认）",
        "/mode plan       生成计划：描述项目即开始编制",
        "/mode revise     修改计划：先选一份计划，再问它 / 改它",
        "/mode import     导入计划：给一个计划 JSON 的路径",
        "/exit            回普通模式（不退出程序）",
        "── 已有计划（第二次打开也能接着改）──",
        "/plans           列出本机已有的计划",
        "/open [计划编号]  打开一份已有计划（不带参数=最新一份），之后就能用 /revise 改",
        "/import <文件>   从一份计划 JSON 导入（建立档案后即可修改）",
        "/wbs             列出已留档的 WBS 树（看清哪棵树来自哪次输入）",
        "/inputs          列出已留档的输入（编号 / 文本或文件路径）",
        "── 改得动（自然语言改计划）──",
        "/revise <一句话>  改计划，改完自动重排并报新旧总工期",
        "                 例：/revise 把 5.1.1.1 的工期改成 20",
        "                     /revise 把 5.1.1.1 的工程量改成 300",
        "/versions        修订链：改过几版、每版改了什么",
        "/undo            回退一轮修改",
        "/goto <版本号>   回到指定版本（/goto 0 = 初版）",
        "── 模型档位（多套 key / 随时换模型，全局当前档）──",
        "/llm             看当前档位 + 列出已保存的档位（key 打码）",
        "/llm-use <编号或名字>  切换档位，下一条消息即生效（不用重启）",
        "/llm-add         新增一套 key / 端点（引导式问答，key 只写本机）",
        "/llm-del <编号>  删除某个档位（不影响已生成的计划）",
        "  说明：完整 key 只存在本机 backend\\llm_profiles.json，界面一律打码显示。",
        "── 其他 ──",
        "/sources [id/关键词] 逐值溯源：每个数字从哪来",
        "/cost            本次运行的 token 用量与费用",
        "/history         查看对话历史",
        "/status          查看后端与运行状态",
        "/switch          切换后端（云 <-> 本地 llama.cpp）",
        "/quit            退出终端",
        "── 系统 ──",
        "!命令            直接执行系统命令",
    ]
    return "\n".join("  " + l for l in lines)


# ======================================================================
# 已有计划：列表 / 打开 / 导入 / WBS 档 / 输入档（第 33 轮）
# ======================================================================
def _cmd_plans(ctx, args):
    """列出已有计划（只读），并在只有一份时提示直接用 /open。"""
    try:
        _status, data = ctx.client.list_plans()
    except Exception as e:
        return renderer.color("读取计划列表失败：%s" % e, "red")
    rows = (data or {}).get("plans") or []
    if not rows:
        return renderer.color("本机还没有已生成的计划。输入一句“生成计划”即可开始。", "gray")
    lines = ["本机已有 %d 份计划（新的在前）：" % len(rows)]
    for i, r in enumerate(rows, 1):
        days = r.get("总工期")
        lines.append("  %2d. %-24s %-18s %s 天  %s%s"
                     % (i, str(r.get("plan_id") or ""), str(r.get("项目") or "—")[:18],
                        days if days is not None else "—",
                        str(r.get("审计状态") or ""),
                        ("  （输入 %s）" % r.get("输入编号")) if r.get("输入编号") else ""))
        if r.get("修改时间"):
            lines.append("      %s ｜ %s" % (r["修改时间"], r.get("版本") or ""))
    lines.append(renderer.color("  打开：/open <计划编号>（不带参数=最新一份）", "gray"))
    return "\n".join(lines)


def _cmd_open(ctx, args):
    """把一份已有计划装进上下文并进入修改模式（之后 /revise 就针对它）。"""
    pid = str(args or "").strip()
    if not pid:
        try:
            _status, data = ctx.client.list_plans()
        except Exception as e:
            return renderer.color("读取计划列表失败：%s" % e, "red")
        rows = (data or {}).get("plans") or []
        if not rows:
            return renderer.color("本机没有可打开的计划。", "gray")
        pid = str(rows[0].get("plan_id") or "")
        if not pid:
            return renderer.color("最新一份计划没有编号，无法打开。", "red")
        print(renderer.color("  未指定编号，打开最新一份：%s" % pid, "gray"))
    try:
        status, plan = ctx.client.get("/plans/%s" % pid)
    except Exception as e:
        return renderer.color("打开失败：%s" % e, "red")
    if status >= 400 or not isinstance(plan, dict) or plan.get("error"):
        return renderer.color("没有这份计划：%s（用 /plans 看可用编号）" % pid, "red")
    ctx.current_plan = plan
    ctx.current_plan_id = plan.get("plan_id") or pid
    # 落一次基线：让 /versions、/undo 从这一版开始有链可走（幂等，已存在不覆盖）
    try:
        ctx.client.post_baseline(ctx.current_plan_id, plan)
    except Exception:
        pass
    _enter_revise_mode(ctx, ctx.current_plan_id)
    ov = plan.get("overview") or {}
    return renderer.color("已打开计划 %s（%s，%s 天）→ 已进入修改模式"
                          % (ctx.current_plan_id, ov.get("project_name") or "—",
                             ov.get("total_duration_days", "—")), "green")


def _cmd_import(ctx, args):
    """从一份计划 JSON 导入：过契约校验 → 落基线 → 进修改模式。"""
    path = str(args or "").strip().strip('"').strip("'")
    if not path:
        return renderer.color(
            "用法：/import <计划 JSON 的完整路径>\n"
            "  例：/import D:\\Desktop\\某项目\\plan_run_1234.json\n"
            "  怎么看路径：在资源管理器里右键文件 → 复制文件地址", "gray")
    if not os.path.isfile(path):
        return renderer.color("找不到这个文件：%s" % path, "red")
    try:
        with open(path, "r", encoding="utf-8") as f:
            plan = json.load(f)
    except Exception as e:
        return renderer.color("这份文件不是可读的 JSON：%s" % e, "red")
    ok, why = _validate_plan(plan)
    if not ok:
        return renderer.color("导入失败：这份 JSON 不像一份计划数据 —— %s\n"
                              "（需要包含 overview 与 wbs/all_tasks_schedule 等字段）" % why, "red")
    pid = str(plan.get("plan_id") or "").strip() or _pid_from_path(path)
    plan["plan_id"] = pid
    try:
        ctx.client.post_baseline(pid, plan)
    except Exception as e:
        return renderer.color("导入失败（无法建立档案）：%s" % e, "red")
    ctx.current_plan = plan
    ctx.current_plan_id = pid
    _enter_revise_mode(ctx, pid)
    return renderer.color("已导入计划 %s（%s，%s 天）→ 已进入修改模式；"
                          "之后用 /revise 一句话就能改"
                          % (pid, (plan.get("overview") or {}).get("project_name") or "—",
                             (plan.get("overview") or {}).get("total_duration_days", "—")),
                          "green")


def _pid_from_path(path):
    base = os.path.splitext(os.path.basename(path))[0]
    safe = "".join(ch for ch in base if ch.isalnum() or ch in "_-")
    return safe or "plan_import"


def _validate_plan(plan):
    """导入前过一遍契约（与生成计划落盘时同一份真源）。返回 (ok, 说明)。"""
    if not isinstance(plan, dict) or not plan:
        return False, "内容为空"
    try:
        from pipeline import schemas
        schemas.PlanJson.model_validate(plan)
        return True, ""
    except Exception as e:
        return False, str(e)[:160]


def _enter_revise_mode(ctx, plan_id):
    """进入修改模式并**持久化**（跨会话记住正在改哪份计划）。"""
    try:
        ctx.mode = "revise"
        ctx.mode_plan_id = str(plan_id or "")
        ctx.client.post_mode("revise", plan_id)
    except Exception:
        pass


def _cmd_wbs(ctx, args):
    """列出已留档的 WBS 树（哪棵树来自哪次输入）。"""
    try:
        _status, data = ctx.client.list_wbs()
    except Exception as e:
        return renderer.color("读取 WBS 档失败：%s" % e, "red")
    rows = (data or {}).get("wbs") or []
    if not rows:
        return renderer.color(
            "还没有留档的 WBS 树。跑一次“生成计划”就会自动存档（跑完即存）。", "gray")
    lines = ["已留档 %d 棵 WBS 树（新的在前）：" % len(rows)]
    for r in rows:
        st = r.get("统计") or {}
        lines.append("  %-22s %s ｜ %s 阶段 / %s 工序"
                     % (str(r.get("run_id") or ""), str(r.get("时间") or ""),
                        st.get("阶段", "—"), st.get("工序", "—")))
        src = "来源：%s" % (r.get("来源环节") or "—")
        if r.get("input_id"):
            src += " ｜ 输入编号：%s" % r.get("input_id")
        lines.append("      " + src)
        p = r.get("参数摘要") or {}
        if p:
            lines.append("      参数：" + "、".join("%s=%s" % (k, v) for k, v in p.items()))
    lines.append(renderer.color(
        "  注：从历史树「接着往下跑」还没做（需要给引擎加「从第 k 步开始」的能力）。", "gray"))
    return "\n".join(lines)


def _cmd_inputs(ctx, args):
    """列出已留档的输入（编号 / 文本或文件路径）。"""
    try:
        _status, data = ctx.client.list_inputs()
    except Exception as e:
        return renderer.color("读取输入档失败：%s" % e, "red")
    rows = (data or {}).get("inputs") or []
    if not rows:
        return renderer.color("还没有留档的输入。每次跑“生成计划”都会自动存一份。", "gray")
    lines = ["已留档 %d 份输入（新的在前）：" % len(rows)]
    for r in rows:
        kind = r.get("类型") or "文本"
        lines.append("  %-28s %s ｜ %s" % (r.get("input_id") or "", r.get("时间") or "", kind))
        if kind == "文件":
            lines.append("      文件：%s" % (r.get("文件路径") or ""))
        else:
            text = str(r.get("文本") or "").replace("\n", " ")
            lines.append("      文本：%s" % (text[:70] + ("…" if len(text) > 70 else "")))
    return "\n".join(lines)


def _cmd_show(ctx, args):
    if not ctx.current_plan:
        return renderer.color("还没有已生成的计划，先输入一句“生成计划”。", "gray")
    try:
        path = renderer.build_show_html(ctx.current_plan)
    except Exception as e:
        return renderer.color(f"生成看板失败：{e}", "red")
    ctx.show_html = path
    url = "file:///" + os.path.abspath(path).replace("\\", "/")
    webbrowser.open(url)
    return renderer.color(f"已打开看板：{url}", "cyan")


def _cmd_confirm(ctx, args):
    if not ctx.current_plan:
        return renderer.color("当前没有计划可确认。", "gray")
    pid = ctx.current_plan.get("plan_id", "?")
    return renderer.color(f"计划 {pid} 已确认（想看板请用 /show）。", "green")


def _cmd_history(ctx, args):
    if not ctx.history:
        return renderer.color("（暂无对话）", "gray")
    return "\n".join(
        renderer.color(f"  你> {t}" if role == "user" else f"  终端> {t}", "cyan" if role == "user" else "green")
        for role, t in ctx.history[-20:]
    )


def _cmd_status(ctx, args):
    backend = switch.describe(ctx.backend)
    run = "运行中" if ctx.running else "空闲"
    plan = ctx.current_plan.get("plan_id", "无") if ctx.current_plan else "无"
    return (f"  后端：{backend}\n"
            f"  状态：{run}\n"
            f"  当前计划：{plan}\n"
            f"  历史记录：{len(ctx.history)} 条")


def _cmd_switch(ctx, args):
    new_key = switch.toggle(ctx.backend)
    if new_key == ctx.backend:
        return renderer.color("无可切换的后端。", "gray")
    ctx.backend = new_key
    ctx.client = _rebuild_client(ctx)
    return renderer.color(f"已切换 → {switch.describe(new_key)}", "cyan")


def _cmd_quit(ctx, args):
    if ctx.running and ctx.run_id:
        # 运行中退出：先问是否取消后端任务
        try:
            ans = input("  流水线仍在运行，退出前是否取消后端任务？[Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = "y"
        if ans in ("y", "yes", ""):
            try:
                ctx.client.post_cancel(ctx.run_id)
            except Exception:
                pass
    return "quit"


def _cmd_pause_only(ctx, args, name):
    # /retry /edit /continue /abort 仅在节点暂停时由 confirmer 处理
    return renderer.color(f"{name} 仅在节点暂停时可用（看到 ⏸ 提示后输入）。", "gray")


# ---------------- 自然语言修改：产品第 2 句标语「改得动」的入口 ----------------
def _plan_id(ctx):
    if ctx.current_plan_id:
        return ctx.current_plan_id
    if ctx.current_plan:
        return ctx.current_plan.get("plan_id")
    return None


def _need_plan(ctx):
    pid = _plan_id(ctx)
    if not pid:
        return None, renderer.color("还没有已生成的计划，先输入一句“生成计划”。", "gray")
    return pid, None


def _cmd_revise(ctx, args):
    """用一句人话改计划：改完会真重排，并把新旧总工期一起报出来。"""
    pid, err = _need_plan(ctx)
    if err:
        return err
    if not args:
        return renderer.color(
            "用法：/revise 把 5.1.1.1 的工期改成 20 ｜ /revise 把 5.1.1.1 的工程量改成 300\n"
            "      /revise 主体结构整体加 3 天 ｜ /revise 把钢筋工班组改成 12 人", "gray")
    # 这一步要几秒到几十秒，先出声：绝不让用户对着空屏猜"是挂了还是在跑"。
    # 走 `tui.out`（与全项目一致）—— 顺序模式落到自带 flush 的写口，VT 模式落进滚动区，
    # 不会像裸 `print` 那样把 ANSI 直接甩到屏幕上、破坏排版。
    tui.out(renderer.color("⏳ 正在把这句话翻译成修改指令并重排受影响任务"
                           "（可能要十几秒）…", "dim"), gap=False)
    try:
        status, data = ctx.client.post_revise(pid, args)
    except Exception as e:
        return renderer.color(f"修改请求失败：{e}", "red")
    if status >= 400 or data.get("error"):
        return renderer.color(f"修改失败：{data.get('error') or status}", "red")
    tui.out(renderer.color("✔ 修改已处理（下面汇报生效/拦下的条目）", "dim"), gap=False)

    lines = [renderer.color("✎ " + (data.get("summary") or "已处理"), "green")]
    for p in (data.get("applied") or [])[:8]:
        lines.append("  生效  %s 的 %s → %s" % (p.get("target"), p.get("field"), p.get("value")))
    for p in (data.get("rejected") or [])[:5]:
        lines.append(renderer.color("  拦下  %s：%s"
                                    % ((p.get("patch") or {}).get("target"), p.get("reason")), "yellow"))
    if data.get("total_duration_days"):
        lines.append("  总工期 %s 天" % data["total_duration_days"])
    for w in (data.get("warnings") or [])[:4]:
        lines.append(renderer.color("  提示  %s" % str(w)[:110], "gray"))
    # 本地缓存同步成新计划，后续 /show 看到的就是改完的版本
    if isinstance(data.get("plan"), dict):
        ctx.current_plan = data["plan"]
    return "\n".join(lines)


def _cmd_versions(ctx, args):
    """修订链：现在有几版、每版改了什么、能不能回退。"""
    pid, err = _need_plan(ctx)
    if err:
        return err
    try:
        status, data = ctx.client.get_versions(pid)
    except Exception as e:
        return renderer.color(f"查询版本失败：{e}", "red")
    if status >= 400 or data.get("error"):
        return renderer.color(f"查询失败：{data.get('error') or status}", "red")
    lines = ["计划 %s 的修订链：" % pid]
    vs = data.get("versions") or []
    for v in vs:
        lines.append("  %-4s %s" % (v.get("版本"), v.get("说明")))
    hist = (data.get("history") or [])[1:]
    if hist:
        lines.append("  用户原话：")
        for h in hist[-5:]:
            lines.append("    [%s] %s" % (h.get("时间", ""), h.get("用户原话", "")))
    if len(vs) <= 1:                     # 只有基线 → 还没改过
        lines.append("  （还没有任何修改，当前就是初版）")
    lines.append(renderer.color("  回退：/undo 退一轮 ｜ /goto 0 回到初版", "gray"))
    return "\n".join(lines)


def _cmd_undo(ctx, args):
    pid, err = _need_plan(ctx)
    if err:
        return err
    try:
        status, data = ctx.client.post_undo(pid)
    except Exception as e:
        return renderer.color(f"回退失败：{e}", "red")
    if status >= 400 or data.get("error"):
        return renderer.color(f"回退失败：{data.get('error') or status}", "red")
    if isinstance(data.get("plan"), dict):
        ctx.current_plan = data["plan"]
    return renderer.color("已回退一轮，当前总工期 %s 天" % data.get("total_duration_days"), "green")


def _cmd_goto(ctx, args):
    pid, err = _need_plan(ctx)
    if err:
        return err
    if not args.strip().isdigit():
        return renderer.color("用法：/goto 0（0 = 初版；不带参数用 /versions 看版本号）", "gray")
    try:
        status, data = ctx.client.post_goto(pid, int(args.strip()))
    except Exception as e:
        return renderer.color(f"跳转失败：{e}", "red")
    if status >= 400 or data.get("error"):
        return renderer.color(f"跳转失败：{data.get('error') or status}", "red")
    if isinstance(data.get("plan"), dict):
        ctx.current_plan = data["plan"]
    return renderer.color("已回到 v%s，当前总工期 %s 天"
                          % (args.strip(), data.get("total_duration_days")), "green")


def _cmd_cost(ctx, args):
    """本次运行的 token 用量与费用（成本模块是可选的，没配价就是 0）。"""
    meta = (ctx.current_plan or {}).get("meta") or {}
    usage = meta.get("usage")
    if not usage:
        return renderer.color("本次运行没有记录到 token 用量（可能未联网或计划还没生成）。", "gray")
    lines = ["本次运行的模型用量与费用："]
    lines.append("  调用次数 %s ｜ 输入 %s tokens ｜ 输出 %s tokens ｜ 合计 %s tokens"
                 % (usage.get("calls"), usage.get("prompt_tokens"),
                    usage.get("completion_tokens"), usage.get("total_tokens")))
    # 字段名以 pipeline/usage.snapshot() 为准：cost_cny（不是 cost_yuan）
    if usage.get("cost_cny") is not None:
        lines.append("  预估费用 %.4f 元" % float(usage.get("cost_cny") or 0.0))
    if usage.get("model"):
        lines.append("  模型：%s" % usage["model"])
    if usage.get("note"):
        lines.append("  计价口径：%s" % usage["note"])
    by_node = usage.get("by_node") or {}
    if by_node:
        lines.append("  按节点：")
        # by_node 是 {环节名: token 数}（整数），不是嵌套字典
        for node, tok in sorted(by_node.items(), key=lambda kv: -(kv[1] or 0))[:8]:
            lines.append("    %-16s %s tokens" % (node, tok))
    return "\n".join(lines)


def _cmd_sources(ctx, args):
    """逐值溯源：计划里每个数字都能说出它是从哪来的（「算得清」的终端入口）。"""
    plan = ctx.current_plan
    if not plan:
        return renderer.color("还没有已生成的计划，先输入一句\u201c生成计划\u201d。", "gray")

    # ---------- 辅助：收集所有叶子任务 ----------
    def _leaves(plan):
        for phase in (plan.get("wbs") or {}).get("phases") or []:
            for wp in phase.get("work_packages") or []:
                for sub in wp.get("sub_packages") or []:
                    yield sub

    # ---------- 无参：汇总视图 ----------
    if not args.strip():
        meta = plan.get("meta") or {}
        cred = meta.get("credibility") or {}
        total = sum(cred.values()) if cred else 0
        lines = ["═══ 溯源汇总（可信度）═══"]
        if total > 0:
            for key, label in [("user", "用户提供"), ("kb", "数据库"), ("ai", "AI 假设")]:
                val = cred.get(key, 0)
                pct = val / total * 100 if total else 0
                lines.append("  %-12s %s" % (label, "%0.1f%%" % pct))
        else:
            lines.append("  （无可信度数据）")

        # 定额覆盖率
        nc = meta.get("norm_coverage") or {}
        if nc:
            lines.append("")
            lines.append("═══ 定额覆盖率 ═══")
            lines.append("  总计 %s 条 ｜ 已锚定 %s 条（%s）｜ 未锚定 %s 条（%s）"
                         % (nc.get("total", "?"), nc.get("bound", "?"),
                            "%0.1f%%" % (nc.get("bound_pct", 0) or 0),
                            nc.get("unbound", "?"),
                            "%0.1f%%" % (nc.get("unbound_pct", 0) or 0)))
            by_reason = nc.get("by_reason") or {}
            if by_reason:
                lines.append("  未锚定原因：")
                for reason, cnt in by_reason.items():
                    lines.append("    %-24s %s 条" % (reason, cnt))

        # 一句话点明
        lines.append("")
        lines.append("═══ 一句话 ═══")
        n = sum(1 for _ in _leaves(plan))
        kb = cred.get("kb", 0)
        ai = cred.get("ai", 0)
        if total > 0:
            kb_pct = kb / total * 100
            ai_pct = ai / total * 100
            lines.append("  共 %s 条叶子任务。其中 %.0f%% 有数据库依据，%.0f%% 为 AI 假设。"
                         % (n, kb_pct, ai_pct))
        else:
            lines.append("  共 %s 条叶子任务。" % n)
        lines.append("  用 /sources <任务id> 可查看任意一条的完整溯源。")
        return "\n".join(lines)

    # ---------- 有参：先尝试精确 id 匹配 ----------
    token = args.strip()
    for leaf in _leaves(plan):
        if leaf.get("id") == token:
            return _render_leaf(leaf)

    # ---------- 模糊匹配 ----------
    matches = []
    for leaf in _leaves(plan):
        if token in (leaf.get("id") or "") or token in (leaf.get("name") or ""):
            matches.append(leaf)
    if not matches:
        return renderer.color(
            "清单里没有 id 或名称含「%s」的任务，试试 /sources 不加参数看汇总。" % token, "gray")
    if len(matches) == 1:
        return _render_leaf(matches[0])
    # 多条匹配：列出前 10 条
    lines = ["找到 %d 条匹配「%s」的任务：" % (len(matches), token)]
    for leaf in matches[:10]:
        nb = leaf.get("norm_binding") or {}
        src = leaf.get("_qty_source") or "—"
        lines.append("  %-12s %-30s 来源=%s  定额=%s"
                     % (leaf.get("id", "?"), (leaf.get("name") or "")[:30],
                        src, nb.get("source_code") or "—"))
    if len(matches) > 10:
        lines.append("  …还有 %d 条，换个更精确的关键词试试。" % (len(matches) - 10))
    lines.append(renderer.color("  想看某一条的完整溯源，用 /sources <id>。", "gray"))
    return "\n".join(lines)


def _render_leaf(leaf):
    """把一条叶子任务的溯源信息渲染成可读文本。"""
    lines = []
    lines.append("═══ %s · %s ═══" % (leaf.get("id", "?"), leaf.get("name", "?")))

    # 工程量
    lines.append("")
    lines.append("【工程量】")
    qty = leaf.get("quantity", "?")
    unit = leaf.get("unit", "")
    lines.append("  值：%s %s" % (qty, unit))
    lines.append("  来源：%s" % (leaf.get("_qty_source") or "未标注"))
    formula = leaf.get("_qty_formula")
    if formula:
        lines.append("  算式：%s" % formula)

    # 定额
    nb = leaf.get("norm_binding") or {}
    if nb:
        lines.append("")
        lines.append("【定额锚定】")
        lines.append("  编号：%s" % (nb.get("source_code") or "—"))
        mode_label = "工日/单位" if nb.get("mode") == "labor" else "单位/工日"
        nv = nb.get("norm_value")
        pv = nb.get("productivity_value")
        nu = nb.get("unit", "")
        if nv is not None:
            lines.append("  口径：%s %s（%s）" % (nv, nu, mode_label))
        elif pv is not None:
            lines.append("  口径：%s %s/工日" % (pv, nu))
        else:
            lines.append("  口径：—")
        match_map = {"exact": "精确匹配", "default": "默认值", "ai": "AI 推断"}
        lines.append("  命中方式：%s" % match_map.get(nb.get("match_type", ""), nb.get("match_type", "—")))
        ct = nb.get("condition_text")
        if ct:
            lines.append("  条件：%s" % ct)
        crew = nb.get("crew") or {}
        if crew:
            lines.append("  班组：%s" % "、".join("%s×%s" % (k, v) for k, v in crew.items()))
        cs = nb.get("crew_source")
        if cs:
            lines.append("  班组来源：%s" % cs)

    # 来源与置信度
    prov = nb.get("provenance") or {}
    if prov:
        lines.append("")
        lines.append("【来源与置信度】")
        origin_map = {"user": "用户提供", "kb": "数据库", "ai": "AI 假设", "default": "默认值"}
        lines.append("  来源：%s" % origin_map.get(prov.get("origin", ""), prov.get("origin", "—")))
        conf = prov.get("confidence")
        if conf is not None:
            lines.append("  置信度：%s" % conf)
        note = prov.get("note")
        if note:
            lines.append("  备注：%s" % note)

    # 工期
    lines.append("")
    lines.append("【工期】")
    dd = leaf.get("duration_days", "?")
    wt = leaf.get("work_type", "—")
    lines.append("  工期：%s 天（%s）" % (dd, wt))

    return "\n".join(lines)


# ======================================================================
# 模型档位：多 key / 随时换模型（第 35 轮）
# ======================================================================
# 为什么做成"档位"而不是"改 .env"：接口是 OpenAI 兼容协议，换厂商只差三个值，
# 但 .env 只有一个槽位，换过去就丢了原来的 key。档位把配置变成一叠名片，
# 记住**当前生效的一张**，切一次之后所有对话都用它（用户选定的语义）。
# 与 `一键测试.py` 的首次配置菜单共用同一份厂商预设，避免两处口径不一致。
_PROVIDER_PRESETS = (
    ("1", "通义千问 / 阿里百炼", "https://dashscope.aliyuncs.com/compatible-mode/v1",
     "qwen-plus"),
    ("2", "DeepSeek", "https://api.deepseek.com/v1", "deepseek-chat"),
    ("3", "月之暗面 Kimi", "https://api.moonshot.cn/v1", "moonshot-v1-8k"),
    ("4", "智谱 GLM", "https://open.bigmodel.cn/api/paas/v4", "glm-4-flash"),
    ("5", "硅基流动 SiliconFlow", "https://api.siliconflow.cn/v1",
     "Qwen/Qwen2.5-7B-Instruct"),
    ("6", "OpenAI", "https://api.openai.com/v1", "gpt-4o-mini"),
    ("7", "本地模型（Ollama / vLLM / LM Studio）", "http://localhost:11434/v1",
     "qwen2.5:7b"),
)
_CUSTOM_CHOICE = "8"


def _ask(prompt, default=""):
    """在命令里问一个问题（回车取默认值）。Ctrl-C / 输入结束都按"取消"处理。"""
    try:
        ans = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return ""
    return ans or default


def _pick_provider():
    """厂商选择菜单。返回 (名字, base_url, model) 或 None（取消）。"""
    print(renderer.color("  选一家厂商（接口是标准 OpenAI 兼容协议，任何一家的 key 都能用）：",
                         "gray"))
    for num, name, url, model in _PROVIDER_PRESETS:
        print("    [%s] %-30s 默认模型 %s" % (num, name, model))
    print("    [%s] 自定义端点（自己填 base_url 与模型名）" % _CUSTOM_CHOICE)
    choice = _ask("  请选择（回车＝取消）：")
    if not choice:
        return None
    if choice == _CUSTOM_CHOICE:
        base = _ask("  base_url（含 /v1，例如 https://api.example.com/v1）：")
        model = _ask("  模型名（例如 my-model）：")
        if not base or not model:
            print(renderer.color("  自定义端点需要 base_url 与模型名都填写，已取消。", "red"))
            return None
        return (model, base, model)
    hit = next((p for p in _PROVIDER_PRESETS if p[0] == choice), None)
    if hit is None:
        print(renderer.color("  没有 [%s] 这个选项，已取消。" % choice, "red"))
        return None
    return (hit[1], hit[2], hit[3])


def _llm_rows(data):
    """把 /llm 响应渲染成给用户看的几行。"""
    rows = (data or {}).get("profiles") or []
    active = (data or {}).get("active") or None
    fb = (data or {}).get("fallback") or {}
    lines = []
    if active:
        lines.append("当前模型档位：[%d] %s" % (
            next((r["index"] for r in rows if r.get("active")), 0), active.get("name") or ""))
        lines.append("  %s · %s · key %s" % (active.get("model") or "（未填模型）",
                                             active.get("base_url") or "",
                                             data.get("active_key_masked") or ""))
    else:
        lines.append("当前没有启用档位，沿用 backend\\.env 的配置：")
        lines.append("  %s · %s · key %s" % (fb.get("model") or "（未设置）",
                                             fb.get("base_url") or "（未设置）",
                                             fb.get("key_masked") or "未填"))
    lines.append("")
    if not rows:
        lines.append(renderer.color("  还没有保存过任何档位；用 /llm-add 保存一个，之后就能随时切。",
                                    "gray"))
        return lines
    lines.append("已保存 %d 个档位（key 只在本机，回显一律打码）：" % len(rows))
    for r in rows:
        lines.append("  [%d] %s%-18s %-26s %-16s %s"
                     % (r["index"], "● " if r.get("active") else "  ",
                        str(r.get("name") or "")[:18], str(r.get("host") or "")[:26],
                        str(r.get("model") or "")[:16], r.get("key_masked") or "未填"))
    return lines


def _cmd_llm(ctx, args):
    """列出模型档位；不认得的参数给提示，不做静默忽略。"""
    arg = str(args or "").strip()
    try:
        _status, data = ctx.client.list_llm_profiles()
    except Exception as e:
        return renderer.color("读取模型档位失败：%s" % e, "red")
    if arg:
        # 顺手当 /llm-use 用：少记一条命令（但 /help 仍把两者都列出来）
        return _cmd_llm_use(ctx, arg)
    lines = _llm_rows(data)
    lines.append(renderer.color("  切换：/llm-use <编号或名字>   新增：/llm-add   删除：/llm-del <编号>",
                                "gray"))
    lines.append(renderer.color("  说明：切档只影响**之后**的运行，已经生成的计划不会被追溯修改。",
                                "gray"))
    return "\n".join(lines)


def _cmd_llm_use(ctx, args):
    """切换到某一档。**立刻回显**新档，让人一眼看出换成功了。"""
    key = str(args or "").strip()
    if not key:
        return renderer.color("用法：/llm-use <编号或名字>（先用 /llm 看有哪些档）", "gray")
    try:
        _status, data = ctx.client.use_llm_profile(key)
    except Exception as e:
        return renderer.color("切换失败：%s" % e, "red")
    if not isinstance(data, dict) or data.get("error"):
        return renderer.color("切不了「%s」：没有这个档位（用 /llm 看编号）。" % key, "red")
    act = data.get("active") or {}
    return "\n".join([
        "已切换到模型档位 [%s] %s" % (act.get("id") or "?", act.get("name") or ""),
        "  %s · %s · key %s" % (act.get("model") or "（未填模型）",
                                act.get("base_url") or "", data.get("key_masked") or ""),
        renderer.color("  下一条消息即生效（不用重启）。已生成的计划不受影响。", "gray"),
    ])


def _cmd_llm_add(ctx, args):
    """交互式新增一档。`/llm-add` 之后一路回车即可；key 只写本机。"""
    picked = _pick_provider()
    if not picked:
        return renderer.color("已取消，没有新增档位。", "gray")
    auto_name, base, model = picked
    name = _ask("  给这一档起个名字（回车＝%s）：" % auto_name, auto_name)
    try:
        key = input("  粘贴 key（回车＝取消）：").strip()
    except (EOFError, KeyboardInterrupt):
        key = ""
    if not key:
        return renderer.color("没有填 key，已取消。", "gray")
    use_now = _ask("  现在就切到这一档？（Y/n）：", "y").lower() not in ("n", "no", "否")
    try:
        _status, data = ctx.client.add_llm_profile(name, base, model, key, use=use_now)
    except Exception as e:
        return renderer.color("保存失败：%s" % e, "red")
    if not isinstance(data, dict) or data.get("error"):
        return renderer.color("保存失败：%s" % (data or {}).get("error", "未知原因"), "red")
    lines = ["已保存模型档位「%s」（key %s），本机现有 %d 个档位。"
             % (data.get("name") or name, data.get("key_masked") or "已打码",
                data.get("total") or 0)]
    if data.get("switched"):
        lines.append(renderer.color("  已切到这一档，下一条消息即生效。", "gray"))
    else:
        lines.append(renderer.color("  想用它：/llm-use %s" % (data.get("name") or name), "gray"))
    return "\n".join(lines)


def _cmd_llm_del(ctx, args):
    """删除一档（只删配置，不动任何已生成的计划）。"""
    key = str(args or "").strip()
    if not key:
        return renderer.color("用法：/llm-del <编号>（先用 /llm 看有哪些档）", "gray")
    try:
        _status, data = ctx.client.remove_llm_profile(key)
    except Exception as e:
        return renderer.color("删除失败：%s" % e, "red")
    if not isinstance(data, dict) or data.get("error"):
        return renderer.color("删不了「%s」：没有这个档位（用 /llm 看编号）。" % key, "red")
    removed = data.get("removed") or {}
    return "已删除模型档位「%s」，还剩 %d 个。%s" % (
        removed.get("name") or key, data.get("total") or 0,
        renderer.color("（若删的是当前档，就退回 backend\\.env 的配置）", "gray"))


_COMMANDS = {
    "/help": (_cmd_help, "显示帮助"),
    "/show": (_cmd_show, "浏览器打开当前计划"),
    "/plans": (_cmd_plans, "列出本机已有的计划"),
    "/open": (_cmd_open, "打开一份已有计划并进入修改模式"),
    "/import": (_cmd_import, "从计划 JSON 导入并进入修改模式"),
    "/wbs": (_cmd_wbs, "列出已留档的 WBS 树"),
    "/inputs": (_cmd_inputs, "列出已留档的输入"),
    "/revise": (_cmd_revise, "用一句人话改计划（改完自动重排）"),
    "/versions": (_cmd_versions, "查看修订链（改过几版、都改了什么）"),
    "/undo": (_cmd_undo, "回退一轮修改"),
    "/goto": (_cmd_goto, "回到指定版本（/goto 0 = 初版）"),
    "/sources": (_cmd_sources, "逐值溯源：每个数字从哪来"),
    "/cost": (_cmd_cost, "本次运行的 token 用量与费用"),
    "/llm": (_cmd_llm, "看当前模型档位 / 列出已保存的多套 key"),
    "/llm-use": (_cmd_llm_use, "切换到某个模型档位（编号或名字）"),
    "/llm-add": (_cmd_llm_add, "新增一套 key / 端点（引导式问答）"),
    "/llm-del": (_cmd_llm_del, "删除某个模型档位"),
    "/confirm": (_cmd_confirm, "确认当前计划"),
    "/history": (_cmd_history, "查看对话历史"),
    "/status": (_cmd_status, "查看后端与运行状态"),
    "/switch": (_cmd_switch, "切换后端"),
    "/quit": (_cmd_quit, "退出终端"),
}


def dispatch(ctx, text):
    """处理一条斜杠命令。返回 'quit' 表示退出终端；否则返回输出文本或 None。"""
    text = text.strip()
    if not text.startswith("/"):
        return None
    cmd, _, args = text.partition(" ")
    cmd = cmd.lower()
    # 宽容别名（第 35 轮）：`/llm add` `/llm use 2` `/llm del 1` 一律按
    # `/llm-add` `/llm-use 2` `/llm-del 1` 处理 —— 用户记不住连字符时不该收到
    # "未知命令"这种没用的回复。只认这四个词，不做更宽的猜测。
    if cmd == "/llm" and args.split(" ")[0].lower() in ("add", "use", "del", "rm", "remove"):
        head, _, rest = args.strip().partition(" ")
        cmd = "/llm-" + {"rm": "del", "remove": "del"}.get(head.lower(), head.lower())
        args = rest
    handler = _COMMANDS.get(cmd)
    if handler is None:
        return renderer.color("未知命令：%s（/help 查看全部）" % (text.partition(" ")[0]), "red")
    out = handler[0](ctx, args.strip())
    return out if out is not None else ""


def run_system_command(text):
    """!cmd 透传系统命令。"""
    cmd = text[1:].strip()
    if not cmd:
        return "（空命令）"
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                                timeout=60, encoding="utf-8", errors="replace")
        tail = (result.stdout + result.stderr).rstrip()
        return tail[-2000:] or f"（exit code {result.returncode}）"
    except subprocess.TimeoutExpired:
        return renderer.color("命令执行超时（60s）。", "red")
    except Exception as e:
        return renderer.color(f"命令执行失败：{e}", "red")


def _rebuild_client(ctx):
    from client import SSEClient
    return SSEClient(base_url=switch.url_of(ctx.backend))

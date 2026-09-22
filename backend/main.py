"""FastAPI 入口 — T-14

端点：
- POST /chat       SSE 流式触发流水线（StreamingResponse）
- POST /confirm    人工确认（confirm_id → decision）
- POST /params     参数人工复核门（review_id → passed / manual_input）
- POST /resume     暂停迭代（pause_id → continue/retry/edit/abort）
- POST /cancel     中途取消（run_id）
- GET  /plans/{plan_id}  只读取回 plan_json
- POST /revise     自然语言修改（一句话 → 改计划 → **真重排** → 落修订链）
- GET  /plans/{plan_id}/versions  修订链（初版 + 每轮修改）
- POST /plans/{plan_id}/undo      回退一轮
- POST /plans/{plan_id}/goto      回退到指定版本
- GET  /healthz     健康检查

运行：cd backend && python -m uvicorn main:app --port 8000

未配置 QWEN_API_KEY 时全链路自动走确定性兜底（正则/模板/顺序链），仍可端到端跑通。
"""

import json
import queue
import threading
import time

from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse

# ⚠️ 不要 `from pipeline.config import PLANS_DIR` —— 那是 **import 时按值绑定**，
# `backend/tests/conftest.py` 的 `monkeypatch.setattr(config, "PLANS_DIR", tmp)` 会失效，
# 测试就会往真实 `backend/plans/` 写档（实测已累积 719 个垃圾输入档）。
# 一律在调用点读 `config.PLANS_DIR`（本文件第 36 行已 `from pipeline import config`）。
from pipeline.config import HOST, PORT
from pipeline.adapter import format_sse
from pipeline.builder import build_pipeline
from pipeline.branding import META as BRAND_META
from pipeline.branding import PRODUCT, VERSION
from pipeline import run_archive
from pipeline import llm_profiles
from pipeline import config
from pipeline.registry import InteractionRegistry

app = FastAPI(title=f"{PRODUCT} —— 施工进度计划生成系统",
              version=VERSION, description=BRAND_META)

# 全局交互登记（confirm/pause，TTL 600s）+ 运行表（run_id → pipeline，供 /cancel）
REGISTRY = InteractionRegistry(ttl=600)
RUNS = {}
_RUNS_LOCK = threading.Lock()
# 模型档位刷新 + 建流水线必须串行（第 35 轮）：refresh 改的是 config 的模块级全局量，
# 两个请求交错会读到"新 key + 旧端点"这种半套配置。
_LLM_LOCK = threading.Lock()


# ---------------- /chat ----------------
@app.post("/chat")
def chat(body: dict):
    prompt = str(body.get("prompt") or "")
    run_id = str(body.get("run_id") or f"run_{int(time.time() * 1000)}")

    # 第 35 轮：**每条消息都先刷新模型档位**，再建流水线 —— 于是用户切档后不必重启
    # 后端，下一条消息即用新模型。顺序不可调换：`LLMClient` 正是在 build_pipeline()
    # 时从 config 现读 base_url/api_key/model 的，晚一步刷新就只对再下一条生效。
    # 用锁串起来是因为 refresh 改的是模块级全局量，两个并发请求交错会读到"半套配置"
    # （新 key 配旧端点）。单用户场景下这把锁不会带来可感知的等待。
    with _LLM_LOCK:
        config.refresh_active_profile()
        pipeline = build_pipeline(run_id=run_id, registry=REGISTRY)
    with _RUNS_LOCK:
        RUNS[run_id] = pipeline

    # 输入留档（第 33 轮）：必须先于流水线落档，这样本次运行产出的 plan 与 WBS 树
    # 都能带上同一个 `input_id`（用户要求："给每一份 plan 和 wbs 都标清楚，
    # 这些内容来自哪份输入"）。留档失败不影响运行。
    input_id = ""
    try:
        file_hint = ""
        try:
            from pipeline.nodes.extractor import detect_local_files
            hits = detect_local_files(prompt) or []
            file_hint = str(hits[0]) if hits else ""
        except Exception:
            file_hint = ""
        input_id = run_archive.save_input(prompt, file_path=file_hint,
                                         run_id=run_id, extra={"通道": "chat"})
    except Exception:
        input_id = ""

    q = queue.Queue()
    done_evt = threading.Event()

    def emit(event, data):
        q.put(format_sse(event, data))

    def worker():
        # 第 34 轮：**模式由终端决定**（用户手选，不再由后端做意图识别）。
        # 优先用请求里带的（终端每次对话都带）；没带就取上次保存的模式；都没有 → 普通。
        mode = str(body.get("mode") or "").strip().lower()
        if mode not in ("normal", "plan", "revise", "import"):
            try:
                mode = run_archive.load_mode().get("mode") or "normal"
            except Exception:
                mode = "normal"
        ctx0 = {"prompt": prompt, "_run_id": run_id, "mode": mode}
        # 第 35 轮：改计划模式下"基于当前计划聊天"会带这个口径 —— 路由据此换成
        # 计划问答的提示词（否则按普通模式口径回答，会答错模式）。
        scope = str(body.get("chat_scope") or "").strip()
        if scope:
            ctx0["chat_scope"] = scope
        if input_id:
            ctx0["input_id"] = input_id
        ctx = ctx0
        try:
            pipeline.run(ctx, emit=emit)
        finally:
            # WBS 树留档（第 33 轮）：跑完把最终树存一份，供 `/wbs` 查看。
            # 只读 ctx、只写档案，失败静默（留档是旁路，绝不影响交付）。
            try:
                run_archive.save_wbs(ctx.get("wbs"), run_id=run_id, input_id=input_id,
                                     params=ctx.get("extracted_params") or {},
                                     source="流水线末（含节拍分段）")
            except Exception:
                pass
            q.put(None)  # 结束哨兵
            done_evt.set()
            with _RUNS_LOCK:
                RUNS.pop(run_id, None)

    threading.Thread(target=worker, daemon=True).start()

    # 心跳：空闲每 15s 发 ping 防代理断连
    def heartbeat():
        while not done_evt.is_set():
            time.sleep(15)
            if not done_evt.is_set():
                q.put(format_sse("ping", {}))

    threading.Thread(target=heartbeat, daemon=True).start()

    def stream():
        while True:
            chunk = q.get()
            if chunk is None:
                break
            yield chunk

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------- 交互端点 ----------------
@app.post("/confirm")
def confirm(body: dict):
    cid = body.get("confirm_id")
    # `answered_by="human"`：这三个端点都是**人在终端/界面上点的**，审计门据此判定
    # "这一轮是人工复核通过"。不声明会被记成 `unknown` → 交付物一律按「未审计」印
    # （宁缺勿假）；自动化脚本要表明身份就传 `"script"`。
    ok = REGISTRY.resolve(cid, {"decision": body.get("decision", True),
                                "answered_by": "human"}) if cid else False
    return {"ok": ok}


@app.post("/params")
def review_params(body: dict):
    """参数人工复核门决策：passed=True 采信提取结果；False 时 manual_input 作为补充。"""
    rid = body.get("review_id")
    decision = {"passed": bool(body.get("passed", True)),
                "manual_input": body.get("manual_input"),
                "answered_by": "human"}
    ok = REGISTRY.resolve(rid, decision) if rid else False
    return {"ok": ok}


@app.post("/resume")
def resume(body: dict):
    pid = body.get("pause_id")
    action = body.get("action", "continue")
    decision = {"action": action, "answered_by": "human"}
    if action == "revise":
        decision["instruction"] = body.get("instruction")        # 人工修改意见 → 主体LLM
    if action == "retry":
        decision["instruction"] = body.get("instruction")
    if action == "edit":
        decision["edits"] = body.get("edits")
    # 一键修复（规格 §3 冻结上行契约）：选了编号时带上 repair_key；
    # manual_input 同时是该选项的 label —— 不认识 repair_key 的老节点收到的
    # 仍是一条具体意见，退化成"自由意见"，不会崩。
    repair_key = body.get("repair_key")
    if repair_key:
        decision["action"] = "repair"
        decision["repair_key"] = str(repair_key)
        decision["instruction"] = body.get("instruction") or body.get("manual_input")
        decision["manual_input"] = body.get("manual_input")
    ok = REGISTRY.resolve(pid, decision) if pid else False
    return {"ok": ok}


@app.post("/cancel")
def cancel(body: dict):
    rid = body.get("run_id")
    with _RUNS_LOCK:
        p = RUNS.get(rid)
    if p is not None:
        p.cancel()
        return {"ok": True, "run_id": rid}
    return {"ok": False, "run_id": rid}


# ---------------- 只读取回 ----------------
# ⚠️ 路由顺序：`/plans` 这条必须排在 `/plans/{plan_id}` 之前吗？FastAPI 里两者路径不同、
# 不冲突（一个无尾段、一个有尾段），但**枚举入口必须存在**，否则"第二次打开想改上次的计划"
# 就没法发现有哪些计划（用户实测提出的情形 ②）。
@app.get("/plans")
def list_all_plans():
    """已有计划概览（新的在前）。**纯只读**：不创建任何目录、不写任何文件。"""
    return {"plans": run_archive.list_plans()}


@app.get("/wbs")
def list_wbs_archives():
    """已留档的 WBS 树概览（不含树本体，避免响应过大）。"""
    return {"wbs": run_archive.list_wbs()}


@app.get("/inputs")
def list_input_archives():
    """已留档的输入（文本或文件路径），带编号，便于追溯"这份计划是哪次输入跑的"。"""
    return {"inputs": run_archive.list_inputs()}


@app.get("/mode")
def get_mode():
    """终端模式（跨会话记住"我在改哪份计划"）。"""
    return run_archive.load_mode()


@app.post("/mode")
def set_mode(body: dict):
    """保存终端模式：{mode: normal|plan|revise|import, plan_id?}。"""
    mode = str(body.get("mode") or "normal")
    if mode not in ("normal", "plan", "revise", "import"):
        return JSONResponse({"error": "unknown mode"}, status_code=400)
    path = run_archive.save_mode(mode, body.get("plan_id") or "")
    return {"ok": bool(path), "mode": mode, "plan_id": body.get("plan_id") or ""}


# ---------------- 模型档位（多 key / 随时换模型，第 35 轮）----------------
# 语义（用户选定）：**全局当前档** —— 切一次，之后所有对话与运行都用它，跨会话保持。
# 为什么能"不重启就换"：LLMClient 在 build_pipeline() 时才从 config 现读三个值，
# 而 /chat 每次都重建流水线；所以只要在建流水线**之前**刷新一次 config 即可。
# key 只在本机流转：GET /llm 一律只回打码后的 key。
@app.get("/llm")
def get_llm_profiles():
    """当前档 + 全部档位（key 打码）。"""
    data = llm_profiles.snapshot()
    data["source"] = _llm_source()
    return data


@app.post("/llm/use")
def use_llm_profile(body: dict):
    """切换到某一档：{key: 序号/名字/id}。切换后立即对**下一次**运行生效。"""
    key = body.get("key")
    key = key if key not in (None, "") else body.get("name")
    hit = llm_profiles.use(key if key is not None else "")
    if hit is None:
        return JSONResponse({"error": "profile not found", "key": key}, status_code=404)
    src = config.refresh_active_profile()
    return {"ok": True, "active": {k: v for k, v in hit.items() if k != "api_key"},
            "key_masked": llm_profiles.mask_key(hit.get("api_key")),
            "source": src or _llm_source(), "provider": config.describe_provider()}


@app.post("/llm/add")
def add_llm_profile(body: dict):
    """新增一档：{name, base_url, model, api_key, note?}；`use=true` 时顺便切过去。"""
    name = str(body.get("name") or "").strip()
    base_url = str(body.get("base_url") or "").strip()
    model = str(body.get("model") or "").strip()
    api_key = str(body.get("api_key") or "").strip()
    if not (base_url and model):
        return JSONResponse({"error": "base_url 与 model 都不能为空"}, status_code=400)
    item = llm_profiles.add(name=name, base_url=base_url, model=model, api_key=api_key,
                            note=body.get("note") or "")
    switched = False
    if body.get("use"):
        switched = llm_profiles.use(item["id"]) is not None
        if switched:
            config.refresh_active_profile()
    return {"ok": True, "id": item["id"], "name": item["name"], "switched": switched,
            "key_masked": llm_profiles.mask_key(item.get("api_key")),
            "total": len(llm_profiles.load()["profiles"])}


@app.post("/llm/remove")
def remove_llm_profile(body: dict):
    """删除一档：{key: 序号/名字/id}。删掉当前档会把当前档归零（退回 .env 基线）。"""
    key = body.get("key")
    key = key if key not in (None, "") else body.get("name")
    hit = llm_profiles.remove(key if key is not None else "")
    if hit is None:
        return JSONResponse({"error": "profile not found", "key": key}, status_code=404)
    config.refresh_active_profile()          # active 可能已归零 → 退回基线
    return {"ok": True, "removed": {k: v for k, v in hit.items() if k != "api_key"},
            "total": len(llm_profiles.load()["profiles"])}


def _llm_source() -> str:
    """当前三个值的来源人话（档位 / 环境变量 / .env）。"""
    try:
        return config.refresh_active_profile() or "backend/.env（未启用档位）"
    except Exception:
        return "backend/.env"


@app.get("/plans/{plan_id}")
def get_plan(plan_id: str):
    path = config.PLANS_DIR / f"{plan_id}.json"
    if not path.exists():
        return JSONResponse({"error": "plan not found"}, status_code=404)
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------- 自然语言修改（「改得动」）----------------
# 设计要点（为什么是这几个端点而不是一个大端点）：
#   · /revise 只做一件事：一句话 → 规范修改指令 → 校验 → 执行 → **真重排** → 落修订链
#   · /plans/{id}/versions 让用户先看清"现在有几版、每版改了什么"再决定回退到哪一版
#   · /undo 与 /goto 走同一条重建路径（PlanStore.rebuild），确定性可复现
# 重算回调用 recompute_after_revision（跑依赖→排程两版→回写日期），
# 而不是 revise 节点那个"只改自身工期、不排程"的默认回调。
def _store():
    from pipeline.plan_store import PlanStore
    return PlanStore()


def _load_plan_for_revise(plan_id):
    """改计划时优先取**修订链的当前版**，没有修订链则回落到初版交付物。"""
    store = _store()
    plan = None
    try:
        plan = store.load_current(plan_id)
    except Exception:
        plan = None
    if not plan:
        path = config.PLANS_DIR / f"{plan_id}.json"
        if path.exists():
            plan = json.loads(path.read_text(encoding="utf-8"))
    return store, plan


@app.post("/revise")
def revise(body: dict):
    """用一句人话改计划：{plan_id, instruction} → 修改结果 + 重排后的计划。

    返回 revision（生效/被拦下的指令、受影响任务、总工期新旧对比、警告）与
    重排后的 plan（日期/工期/峰值都跟着变）。LLM 不可用时自动走规则解析，
    照样能改（只是能听懂的说法少一些），绝不因为没配 Key 就整条功能不可用。
    """
    from pipeline.nodes.revise import ReviseNode
    from pipeline.recompute import recompute_after_revision

    with _LLM_LOCK:
        config.refresh_active_profile()      # 改计划也要用"当前档"（第 35 轮）

    plan_id = str(body.get("plan_id") or "")
    instruction = str(body.get("instruction") or body.get("text") or "").strip()
    if not plan_id:
        return JSONResponse({"error": "缺少 plan_id"}, status_code=400)
    if not instruction:
        return JSONResponse({"error": "缺少 instruction（要改成什么？）"}, status_code=400)

    store, plan = _load_plan_for_revise(plan_id)
    if not plan:
        return JSONResponse({"error": "plan not found: %s" % plan_id}, status_code=404)

    node = ReviseNode(store=store, recompute=recompute_after_revision)
    ctx = {"plan_json": plan, "user_instruction": instruction, "plan_id": plan_id}
    try:
        node.run(ctx)
    except Exception as exc:                     # 改写失败不能让服务 500 掉
        return JSONResponse({"error": "修改失败：%s" % str(exc)[:300]}, status_code=500)

    revision = ctx.get("revision") or {}

    # ---- 第 34 轮：**先看后改**（preview）----
    # 用户要求："不要将任何输入都识别为修改…如果识别到修改意图时，再向用户确认修改项"。
    # `dry_run=True` 时只把"要改什么"算出来给终端看，**不落盘、不写计划、不落修订链**。
    # 终端确认后再用同一条指令真跑一次（同一条代码路径，不分叉）。
    if bool(body.get("dry_run")):
        return {
            "ok": bool(revision.get("applied")),
            "dry_run": True,
            "plan_id": plan_id,
            "summary": revision.get("summary") or "",
            "applied": revision.get("applied") or [],
            "rejected": revision.get("rejected") or [],
            "affected": revision.get("affected") or [],
            "warnings": revision.get("warnings") or [],
            "hint": revision.get("hint") or "",
        }

    updated = ctx.get("plan_json") or plan
    # 把重排后的计划覆盖回交付物目录（否则用户刷新看板还是旧计划）
    try:
        (config.PLANS_DIR / f"{plan_id}.json").write_text(
            json.dumps(updated, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        revision.setdefault("warnings", []).append("计划回写失败：%s" % str(exc)[:120])

    return {
        "ok": bool(revision.get("applied")),
        "plan_id": plan_id,
        "summary": revision.get("summary") or "",
        "applied": revision.get("applied") or [],
        "rejected": revision.get("rejected") or [],
        "affected": revision.get("affected") or [],
        "warnings": revision.get("warnings") or [],
        "hint": revision.get("hint") or "",
        "total_duration_days": (updated.get("overview") or {}).get("total_duration_days"),
        "plan": updated,
    }


@app.post("/plans/{plan_id}/baseline")
def plan_baseline(plan_id: str, body: dict = None):
    """把一份计划落成**基线**（`/open`、`/import` 之后调），幂等：已有基线不覆盖。

    为什么需要：`/open` 打开的是"交付物本体"，而修订链（`/versions`、`/undo`、
    `/revise` 的"当前版"）都建立在 `档案/<id>/基线.json` 之上。没有这一步，
    打开一份旧计划后第一次 `/revise` 会缺基线。
    """
    body = body or {}
    plan = body.get("plan")
    if not isinstance(plan, dict) or not plan:
        return JSONResponse({"error": "缺少 plan"}, status_code=400)
    store = _store()
    try:
        if store.load_baseline(plan_id):
            return {"ok": True, "already": True, "plan_id": plan_id}
        path = store.save_baseline(plan_id, plan)
    except Exception as exc:
        return JSONResponse({"error": str(exc)[:200]}, status_code=500)
    return {"ok": True, "already": False, "plan_id": plan_id, "path": path}


@app.get("/plans/{plan_id}/versions")
def plan_versions(plan_id: str):
    """修订链：初版 + 每一轮修改（谁改的、改了什么、影响多少任务）。"""
    store = _store()
    try:
        return {"plan_id": plan_id,
                "versions": store.versions(plan_id),
                "history": store.history(plan_id),
                "audit": store.audit_log(plan_id)}
    except Exception as exc:
        return JSONResponse({"error": str(exc)[:200]}, status_code=500)


@app.post("/plans/{plan_id}/undo")
def plan_undo(plan_id: str):
    """回退一轮修改（重建 = 从初版重放剩余 patch，确定性）。"""
    return _rebuild_response(plan_id, lambda store: store.undo(plan_id))


@app.post("/plans/{plan_id}/goto")
def plan_goto(plan_id: str, body: dict = None):
    """回退到指定版本号（0 = 初版）。"""
    body = body or {}
    try:
        target = int(body.get("version"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "缺少 version（整数，0 = 初版）"}, status_code=400)
    return _rebuild_response(plan_id, lambda store: store.goto(plan_id, target))


def _rebuild_response(plan_id, action):
    """undo / goto 的公共收尾：重建 → 覆盖交付物 → 返回新总工期。"""
    store = _store()
    try:
        plan = action(store)
    except Exception as exc:
        return JSONResponse({"error": str(exc)[:200]}, status_code=500)
    if not plan:
        return JSONResponse({"error": "没有可回退的修订记录"}, status_code=404)
    try:
        (config.PLANS_DIR / f"{plan_id}.json").write_text(
            json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return {"ok": True, "plan_id": plan_id,
            "total_duration_days": (plan.get("overview") or {}).get("total_duration_days"),
            "versions": store.versions(plan_id),
            "plan": plan}


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


# ---------------- TTL 清理 ----------------
def _cleanup_loop():
    while True:
        time.sleep(60)
        n = REGISTRY.cleanup()
        if n:
            print(f"[registry] 已清理 {n} 个过期交互")


threading.Thread(target=_cleanup_loop, daemon=True).start()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT)

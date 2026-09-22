# `backend/prompts/` — LLM 提示词模板目录

> 本文件是**当前有效**的目录说明（2026-09-21 逐文件核对代码后重写）。
> 各节点在运行时加载这里的 `.txt` 作为 system prompt。

## 1. 目录用途与加载方式

- 加载入口：`backend/pipeline/prompts_loader.py` 的 `load(name)`。
  - 路径固定为 `config.PROMPTS_DIR`（`backend/pipeline/config.py:199`：`PROMPTS_DIR = BACKEND_DIR / "prompts"`）；
  - 只按**文件名**取（如 `load("extract_params.txt")`），UTF-8 读全文；
  - 带进程内缓存 `_CACHE`：同一进程内同名只读一次，**改完文件需重启进程**才生效；
  - `load()` 本身**没有兜底**，文件缺失会抛 `FileNotFoundError`（= `OSError`），由各调用方自行处理。
- 模板里**不使用** `{{变量}}` 之类的占位符（已 grep 全部 `.txt`，无 `{{`）。
  调用方把模型输入（`docctx.combine(ctx, user)` / `json.dumps(...)` 拼好的文本）作为 **user 消息**传入，
  模板只当 system prompt，例如 `self.llm.chat_json(load("extract_params.txt"), user, ...)`。
- 例外：`revise_intent.txt` 由 `backend/pipeline/nodes/revise.py:779-786` 的 `_load_prompt()`
  **手工拼路径读取，不经过 `prompts_loader`**，行为与上一致。
- 主链现为 **27 个节点**，真源是 `backend/pipeline/builder.py` 的
  `_main_nodes()` / `PIPELINE_TITLES` / `pipeline_steps()`：

  `router → work_confirm → doc_load → extractor → param_review → boundary → kb_scope → wbs_agent → beat_build → plan_level → audit_wbs → quantity_fill → norm_bind → crew_bind → deps → cpm → scheduler → audit_schedule → resource → assembler → reporter → confirm → deliver → word_draft → audit_draft → word_export → html_page`

## 2. 文件 → 加载方（逐文件核实）

下表「加载方」按**实际 `load("文件名")` 调用点**核对；节点名以 `builder.py` 的 `name` 为准。

| 文件 | 加载方（节点 / 代码位置） | 用途 |
|---|---|---|
| `boundary_conditions.txt` | `pipeline/nodes/boundary.py:1319` | boundary「补全边界条件」：据已提取参数 + 用户补充 + 常识补全 labor / equipment / materials / 工期 |
| `deliver_html.txt` | `pipeline/nodes/delivery.py:5058` | html_page「导出可视化看板」：把确定性引擎算好的数据编排成自包含 HTML |
| `deps_gen.txt` | `pipeline/nodes/deps_gen.py:716` | deps「编排工序先后」：按叶子清单生成 FS 依赖（失败有顺序链兜底） |
| `extract_params.txt` | `pipeline/nodes/extractor.py:468` | extractor「读取项目参数」：抽取结构化参数 + doc_summary |
| `norm_match.txt` | `pipeline/nodes/norm_bind.py:3619` | norm_bind「匹配消耗量定额」：在候选定额行里选一行 |
| `plan_qa.txt` | `pipeline/nodes/router.py:201` | router：revise 模式下「基于当前计划聊天」的问答口径（`chat_scope == "plan"`） |
| `quantity_fill.txt` | `pipeline/nodes/quantity_agent.py:46`（`PROMPT_NAME`）→ `:624` `load(PROMPT_NAME)` | quantity_fill（全链第 27 个节点）「补全各工序工程量」：闭集逐条表态；读取用 `try/except OSError` 兜底 |
| `report.txt` | `pipeline/nodes/reporter.py:30` | reporter「生成监督报告」：Markdown 监督报告（空/异常 → `plan_assembler.template_report` 确定性模板） |
| `revise_intent.txt` | `pipeline/nodes/revise.py:779-786`（**不经 `prompts_loader`**） | revise（不在主链）「自然语言 → 规范修改指令」翻译器；读不到 → 内置一句兜底提示 |
| `router_intent.txt` | **当前无代码引用（历史遗留）** | 原「意图识别与分流」提示词；第 34 轮取消意图识别后不再调用（详见 §3） |
| `router_reply.txt` | `pipeline/nodes/router.py:248/279`（`_answer()` 默认 `prompt_name`） | router：normal 普通模式聊天回复 |
| `wbs_fusion.txt` | `pipeline/nodes/wbs_agent.py:669` | wbs_agent「跨相融合」：整树展开后开放 2/3 级修改权限、专项归入宿主实体阶段 |
| `wbs_gen.txt` | `pipeline/nodes/wbs_gen.py:249`（类 `WBSGenNode`，name=`"wbs"`） | 整树生成提示词；**但 `WBSGenNode` 在当前仓库没有任何实例化点**（主链 WBS 由 `WBSAgentNode` 承担），因此当前**没有在跑的链路会加载它**；`docs/剩余批次_交接总纲.md` 把它记为「整树/回退链路」 |
| `wbs_overview.txt` | **当前无代码引用（历史遗留）** | 原「WBS 总览」：判断是否需要追加 1 级施工阶段 |
| `wbs_phase.txt` | `pipeline/nodes/wbs_agent.py:764, 802` | wbs_agent「逐相展开」：把一个 1 级阶段展开为 2/3 级 |
| `wbs_review.txt` | `pipeline/nodes/wbs_agent.py:940` | wbs_agent「全局评审」：语义审查候选 WBS |
| `wbs_worker.txt` | **当前无代码引用（历史遗留）** | 仅在 `pipeline/nodes/wbs_agent.py:3` 的 docstring 被提到（「取代原『单一 LLM 一次产整棵』的 wbs_worker 链路」），全仓无任何加载点 |

### 2.1 被代码引用但磁盘上不存在的文件

- `beat_config.txt`：被 `pipeline/nodes/beat_node.py:380` 的 `load("beat_config.txt")` 引用，
  但 `backend/prompts/beat_config.txt` **实测不存在**。这是**已知的静默死路径**（读不到会走
  `except OSError`，源码注释 §14.1.1 正在收口），不是本目录的现役模板。

## 3. 历史沿革

- 本目录最初是把 Dify 导出的工作流 `产品demo/多智能体进度系统工程 (1).yml`（2026-08-22）
  里的 LLM 节点提示词逐条提炼成独立 `.txt`，再随自研 pipeline 落地；因此文件名沿用了
  当时的节点叫法。
- 此后流水线自研、节点多次增删（主链现为 27 个节点）。关键变更：**第 34 轮取消意图识别** ——
  `router` 不再判断用户意图，只按**终端手选的模式**（`normal` / `plan` / `revise` / `import`）分流
  （见 `pipeline/nodes/router.py` 顶部说明与 `_classify` 的「已废弃」注释）。
  于是 `router_intent.txt` 失去调用点，`tests/test_router.py:227-234`
  有护栏测试断言 `router.py` 源码中**不得出现** `router_intent.txt`（防被偷偷接回）。
- 因此目录里同时留着**早期文件名**与**已停用模板**（`router_intent.txt`、`wbs_overview.txt`、
  `wbs_worker.txt`），以及 `wbs_gen.txt` 这类「节点未接线」的中间产物。
  旧版本 README（M0 源码盘点备忘）里的路径与提法（`产品demo/…yml`、`nodes/cpm.py`、
  `nodes/resource.py`、`nodes/plan_assembler.py`、`nodes/extractor.py`、`adapter.py`、
  `资料/CPM算法来源.txt` 等）**已过期**，不应据此找代码或改提示词。

## 4. 改提示词时的约定（据现有代码）

- **加载/缓存**：走 `prompts_loader.load(name)`，文件名即路径；`_CACHE` 进程内缓存，
  改完 `.txt` 后跑测试或重启服务才能看到新内容。
- **无占位符**：模板是纯文本 system prompt，不要把变量写成 `{{x}}` 指望代码替换；
  变量由调用方拼进 user 消息（`docctx.combine(ctx, user)` 或 `json.dumps(..., ensure_ascii=False)`）。
- **输出契约**：多数节点用 `llm.chat_json(load(...), user, temperature=…)`，
  模板里通常已写明「只输出一个 JSON 对象、不要 Markdown 代码块」；`chat_json` 会剥 ``` 围栏
  但不要依赖它。`deliver_html.txt` / `report.txt` / `router_reply.txt` 走 `chat_text`，输出是正文文本。
- **失败兜底不统一，改前先读调用点**，例如：
  - `reporter.py`：报告为空/异常 → `plan_assembler.template_report()` 确定性模板；
  - `quantity_agent.py:30, 615-630`：`quantity_fill.txt` 的 `load()` 用 `try/except OSError` 包住，
    读不到就明确走「模型不可用」并留警告（**明确不复刻** `beat_config.txt` 被静默吞掉的坑）；
  - `revise.py:782-786`：读不到 → 返回内置的一句 JSON 翻译兜底提示；
  - `deps_gen.py` / `router.py` 等各自有代码兜底或模板回退。
- **改 WBS 提示词要两条链路一起看**：`wbs_phase.txt`（现役，`wbs_agent.py:764/802`）
  与 `wbs_gen.txt`（整树/回退链路，仅 `WBSGenNode` 引用）。只改一条可能漏
  （见 `docs/剩余批次_交接总纲.md` 第 7 条，以及第 3 批踩过的同类坑）。

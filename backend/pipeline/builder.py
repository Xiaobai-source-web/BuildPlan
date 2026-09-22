"""流水线装配 — 定义节点序列（M0 提炼的 Dify 工作流拓扑 → 自研引擎）

节点顺序：
  router(闲聊 LLM 入口) → work_confirm → doc_load(项目文件加载门) →
  extractor(双输出:参数+摘要) → param_review(人工复核门) → boundary(边界条件补充) →
  **kb_scope(按建筑类型+结构形式装配 L3/L4 范围；纯代码)** →
  wbs_agent(代码骨架+逐相LLM→组装→跨相融合→复评→人工门) →
  **beat_build(节拍/施工段展开；必须早于复评门与 R1，否则门看到的是占位子树)** →
  plan_level(细度门) →
  **audit_wbs(第 1 轮回审：WBS 结构)** →
  **quantity_fill(补全各工序工程量：闭集逐个表态 + 未入树清单；域 5，全链第 27 个节点)** →
  **norm_bind(定额锚定+来源溯源)** → **crew_bind(机械配员+工作面容量)** →
  deps → cpm → scheduler(两版工期) →
  **audit_schedule(第 2 轮回审：两版工期)** →
  resource(定额路径 / 遗留路径自动分流) →
  assembler → reporter → confirm(确认门) → deliver(plan_final 落盘) →
  word_draft(Word 草案·未审计) → **audit_draft(第 3 轮回审)** →
  word_export(定稿) → html_page(可视化看板)

人工门：doc_load（无文件时问 Y/n）、param_review（打 Y 通过 或 手动输入参数）、
        plan_level（L3/L4 细度）、**三轮回审**（R1 WBS / R2 两版 / R3 Word草案）、
        wbs 复评 HIGH 门、confirm（最终确认）

三轮回审的语义：打 Y 才继续；输入文字视为审计意见 → 记进 meta.audit_comments，
计划保持"未审计"并**停在当前阶段**（不产出最终交付物），提示用户改完再重跑。

可用但**不在主链**的节点：
  revise（自然语言修改，运行后经 /revise 触发，见 nodes/revise.py）
"""

from .engine import Pipeline
from .llm import LLMClient
from .nodes import cpm as _cpm
from .nodes import resource as _resource
from .nodes import deps_gen, extractor, plan_assembler, reporter, wbs_gen
from .nodes.audit_gate import DraftAuditNode, ScheduleAuditNode, WBSAuditNode
from .nodes.boundary import BoundaryNode
from .nodes.delivery import HtmlPageNode, WordExportNode
from .nodes.confirm import ConfirmNode
from .nodes.crew_bind import CrewBindNode
from .nodes.doc_load import DocLoadNode
from .nodes.kb_scope import KBScopeNode
from .nodes.norm_bind import NormBindNode
from .nodes.param_review import ParamReviewNode
from .nodes.plan_level import PlanLevelNode
# ---- 域 5：第 27 个节点（量补全 / 单位换算 / 用户覆盖 / 冻结）----
# 界面名与主链顺序见下方 PIPELINE_TITLES / _main_nodes。
#
# ⚠️ 收口（父代理亲改）：这里原本是 `try: import ... except ImportError:` 的存在性
#   容错 + 一个同名占位类（并行开发期 `nodes/quantity_agent.py` 还没落地时的权宜）。
#   **现在改成裸 import**，理由不是"文件已落地"，而是**留在产品代码里就是一个缺陷**：
#   模块一旦缺失（改名、打包漏文件、循环 import），计划会**照常跑完**，只是
#   **一条工程量都不补**，而没有任何人看得见 —— 这正是本批正在修的
#   `beat_config.txt` 静默死路径（见 `nodes/beat_node.py` 的 `BEAT_CONFIG_UNUSABLE_MSG`
#   与 §14.1.1）的同族缺陷。宁可 import 时炸，也不要静默出一份没补量的计划。
from .nodes.quantity_agent import QuantityAgentNode
from .nodes.router import RouterNode
from .nodes.scheduler import SchedulerNode
from .nodes.wbs_agent import WBSAgentNode
from .nodes.beat_node import BeatExpandNode
from .nodes.work_confirm import WorkConfirmNode
from .registry import InteractionRegistry

# ======================================================================
# 界面名（第 32 轮）：节点名 / 小标题是**内部**叫法，用户在小标题里看到的是下面这张表
# ======================================================================
# 用户实测原话：「'闲聊'这个词本身就不专业」。确实 —— `闲聊 LLM 入口` 是**实现**
# 的写法（这个节点就是"调用大模型判断意图"），不是用户在做的事。整条链上还有一批
# 同类：「节拍流水展开」「关键路径计算」「知识库范围装配」「参数抽取」。
#
# 这里一次收口：节点**代码里的** name/title 一个字节都不动（日志、测试、文档都靠它），
# 只在装配时把界面名盖到 title 上，并随 `run_plan` 事件下发全链（终端的状态行、
# 步数分母、进度行都取这一份 —— 只有**一个**真源）。改标题只需要改这张表。
PIPELINE_TITLES = {
    # ⚠️ 第 34 轮起这个节点**不再做意图识别**（用户要求取消），它只按终端手选的模式
    # 分流。所以界面名必须改 —— 用户实测看到「第 1 步 · 意图识别与分流」后直接质疑
    # 「为什么这里还是在显示跑意图识别」：名字撒谎比名字难懂更伤可信度。
    "router": "识别当前模式",
    "work_confirm": "确认生成计划",
    "doc_load": "读取项目文件",
    "extractor": "读取项目参数",
    "param_review": "确认项目参数",
    "boundary": "补全边界条件",
    "kb_scope": "范围与结构映射",
    "wbs_agent": "编制 WBS 分工",
    "beat_build": "节拍流水分段",
    "plan_level": "选择展示细度",
    "quantity_fill": "补全各工序工程量",
    "norm_bind": "匹配消耗量定额",
    "crew_bind": "配机械与班组",
    "deps": "编排工序先后",
    "cpm": "计算关键路径",
    "scheduler": "排程与两版工期",
    "resource": "算资源与工日",
    "assembler": "组装计划数据",
    "reporter": "生成监督报告",
    "confirm": "确认生成计划",
    "deliver": "落盘计划数据",
    "html_page": "导出可视化看板",
}
# 说明：审计三兄弟（第 N 轮审计：…）与两个 Word 导出节点**本来**就是人话，
# 这里不覆盖 —— 少一条映射就少一处将来对不上的地方。


def pipeline_steps():
    """主链的 (节点名, 界面名) 顺序表 —— 终端步数、分母、进度行的**唯一**真源。

    不建 Pipeline（只列节点），因此没有 key / 没有 llm 也能拿到，供终端与文档引用。
    """
    return [(n.name, PIPELINE_TITLES.get(n.name) or n.title) for n in _main_nodes()]


def _main_nodes(llm=None):
    """主链节点（顺序即执行顺序）。`build_pipeline` 与 `pipeline_steps` 共用这一份。"""
    # 定额锚定节点：调用方注入 llm（测试桩）时用它；未注入时用真实客户端
    # （生产路径；没有 key 时其 llm_usable 为 False，会自动走纯代码策略，不联网）。
    norm_llm = llm if llm is not None else LLMClient()
    nodes = [
        RouterNode(llm=llm),
        WorkConfirmNode(),                # 进工作模式前强制 Y/N 确认
        DocLoadNode(),                    # 项目文件加载门：无文件问 Y/n，读取 doc_content
        extractor.ExtractorNode(llm=llm),
        ParamReviewNode(),                # 参数人工复核门：打Y 或 手动输入参数
        BoundaryNode(llm=llm),            # 边界条件补充（结合用户输入+常识）
        KBScopeNode(),                    # 知识库范围装配：建筑类型+结构形式 → L3/L4 合法范围（纯代码）
        WBSAgentNode(llm=llm),            # WBS 多级分工：代码骨架+逐相LLM→组装→复评→人工门
        # ⚠️ 顺序（第 32 轮修正）：节拍展开必须在 **WBS 复评门之前**。
        #   原来它在 WBSAgentNode 之后，于是复评门看到的是"占位子树"（量=1/单位=项），
        #   脚本自检的 conc_m3 必然是 0 → 评审模型据此报**假 HIGH**，还把用户按在门上问
        #   「是按数据库基线口径继续，还是改成你的项目口径」——一个用户既看不懂、
        #   也答不了的问题（实测原话：「这些是什么意思，作为一个第一次使用的用户，
        #   根本看不懂」）。先把流水段铺开，门再问，看到的才是**真的 WBS**。
        BeatExpandNode(llm=llm),          # 节拍型节点 → 代码节拍引擎层铺流水+搭接
        PlanLevelNode(),                  # 计划细度门：问 L3/L4（带真实行数预估）
        WBSAuditNode(),                   # 【R1】审计门：WBS 结构（用户审过才往下走）
        # 域 5：闭集逐个表态补量 + 单位换算 + 用户值覆盖 + 冻结；必须在 `norm_bind`
        # 之前（定额分母要在量定稿后才选）与 `wbs_audit` 之后（R1 门已过）。
        QuantityAgentNode(llm=llm),
        NormBindNode(llm=norm_llm),       # 定额锚定：每条 L4 定一条定额并记来源
        CrewBindNode(),                   # 机械配员 + 工作面容量（纯代码）
        deps_gen.DepsGenNode(llm=llm),
        _cpm.CPMNode(),                   # 纯 CPM：无资源约束的理想关键路径（作为对照）
        SchedulerNode(),                  # 排程：一次算两版（理论最短 / 资源不超额）
        ScheduleAuditNode(),              # 【R2】审计门：两版工期
        _resource.ResourceNode(),         # 有定额锚定走定额路径，否则走遗留路径
        plan_assembler.PlanAssemblerNode(),
        reporter.ReporterNode(llm=llm),
        # ⚠️ 不许写「最终方案」：这个门在三轮回审**之前**，此刻计划还是「未审计」。
        # （renderer.py 的落盘文案处有同一条禁令 —— 两处说的是同一件事。）
        ConfirmNode(message="要现在整理这份计划数据吗？（后面还有三轮回审）"),
        plan_assembler.PlanDeliverNode(),
        WordExportNode(draft=True),       # 交付：Word **草案**（不含图表，盖"未审计"戳）
        DraftAuditNode(),                 # 【R3】审计门：草案审过才出定稿与看板
        WordExportNode(),                 # 交付：Word 定稿（已审计）
        HtmlPageNode(llm=llm),            # 交付：JSON → HTML 看板（失败兜底确定性模板）
    ]
    # 界面名盖到 title 上（原始 title 仍留在各节点类里，日志/单测取的还是那一份）
    for n in nodes:
        n.title = PIPELINE_TITLES.get(n.name) or n.title
    return nodes


def build_pipeline(run_id=None, registry=None, llm=None) -> Pipeline:
    reg = registry or InteractionRegistry()
    return Pipeline(run_id=run_id, registry=reg).add_nodes(*_main_nodes(llm))

# BuildPlan 施工知识库

面向 AI Agent 的**施工定额知识库**：单文件 SQLite + 5 个只读查询工具 + 本文档。
无第三方依赖（仅 Python 标准库），无网络访问，无写入操作。

```
BuildPlan_KB/
├── README.md          本文档
├── kb.db              知识库（SQLite 3，5.72 MB，19 张业务表）
└── tools/             5 个查询工具，命令行调用
    ├── query_project.py      建筑类型/结构形式 → 适用工种
    ├── query_activities.py   工种 → 施工活动
    ├── query_norms.py        活动 → 定额
    ├── query_duration.py     工程量 + 资源 → 工期
    └── query_source.py       数据来源与质量
```

运行环境：Python 3.8+。工具用 `tools/` 的相对路径定位 `kb.db`，整个文件夹可整体搬移。

> ⚠️ **已知问题（2026-09-21）**：域 1.6 删除了 `Workface_Capacity_Rule` 表之后，
> `query_duration.py` 与 `query_norms.py` 里**仍在查这张表**，因此
> **任何会走到「工作面容量」的调用都会抛**
> `sqlite3.OperationalError: no such table: Workface_Capacity_Rule`
> （例如 `query_duration.py --activity ... --quantity ... --resource-limit ...`）。
> `query_duration.py --demo`、`--list-conditions` 以及 `query_project.py` /
> `query_activities.py` / `query_source.py` 不受影响。
> **产品运行路径不受影响**（后端走 `backend/pipeline/kb.py`，早已改读
> `Resource_Workface_Index` MWI 表）。修法见本文末「已知限制」第 1 条。

---

## 快速开始

```bash
# 1. 看这个库能干什么
python tools/query_activities.py --stats

# 2. 住宅项目适用哪些工种
python tools/query_project.py --l3-for residential --structure frame_shear

# 3. 钢筋工程下有哪些活动
python tools/query_activities.py --l4 rebar --structure frame_shear

# 4. 查一个活动的全部定额条件，并拿到可直接复制的 --match 串
python tools/query_duration.py --activity REBAR_NEW_BEAM --list-conditions

# 5. 算工期（人工主导：5t 梁钢筋，20 人限额，框架梁 ≤25mm）
#    ⚠️ 当前会抛 `sqlite3.OperationalError: no such table: Workface_Capacity_Rule`
#       （query_duration.py 仍在查已删除的容量表，见文首「已知问题」）
python tools/query_duration.py --activity REBAR_NEW_BEAM --quantity 5 \
    --resource-limit 20 --match "框架梁,≤25"

# 5b. 不受上述问题影响的等价用法：先看条件，再不带 --resource-limit 计
python tools/query_duration.py --activity REBAR_NEW_BEAM --list-conditions
python tools/query_duration.py --activity REBAR_NEW_BEAM --quantity 5 --match "框架梁,≤25"

# 6. 算工期（机械主导：660m³ 板浇筑，1 台泵车）
python tools/query_duration.py --activity CONC_NEW_SLAB --quantity 660 \
    --machines 1 --condition "板浇筑"
```

所有工具都支持 `--json`，便于程序化解析。

---

## 数据库构成

**19 张业务表（另有 SQLite 内部表 `sqlite_sequence`），分五层。**

| 层 | 表 | 行数 | 说明 |
|---|---|---:|---|
| **字典** | `L3_Work_Type` | 31 | 工种（钢筋工程 / 混凝土工程 / 桩基工程 …） |
| | `L4_Activity_Dictionary` | 493 | 施工活动（含单位、生产模式；A5 起含 `is_standalone_activity` 判据列） |
| | `Building_Type_Dictionary` | 10 | 住宅 / 办公楼 / 商业 / 学校 / 医院 / 工业厂房 / 仓库 / 酒店 / 公寓 / 综合体 |
| | `Structure_Type_Dictionary` | 7 | 框架 / 剪力墙 / 框剪 / 砖混 / 钢结构 / 排架 / 筒体 |
| | `Condition_Dictionary` | 1 635 | 条件词表（48 类条件维度） |
| | `sources` | 15 | 数据来源登记（含 `AI_ESTIMATE_V1`、`SCAFFOLD_V1`） |
| | `L3_Labor_Type` | 31 | 工种 → 劳力类型（与 L3 工种一一对应） |
| **映射** | `Building_Type_L3_Mapping` | 310 | 建筑类型 → 工种适用性（三档 REQUIRED / OPTIONAL / EXCLUDED） |
| | `Structure_Type_L4_Mapping` | 1 218 | 结构形式 → 活动适用性（三档；含 A2 补砌体 385 行） |
| **定额** | `Norm_Labor_Table` | 3 886 | 人工定额（工日） |
| | `Norm_Equipment_Table` | 245 | 机械定额（台班） |
| | `Equipment_Crew_Mapping` | 26 | 机械配员（含「塔吊 / 施工电梯」两条 user_directive 行） |
| | `L4_Norm_Default` | 493 | **L4 默认定额行**（`review_state` / `confidence` 分档：verified·parsed 参与计算并标注「未经人工审定」；**estimated（AI 经验估算）自 2026-09-20 起同样参与计算**，但逐条标注「AI 经验估算定额（无规范依据，待审）」并在置信度章节计 `released_ai`） |
| **规则** | `Resource_Workface_Index` | 67 | **资源工作面指标 MWI**（域 1.6 起，**工作面容量的唯一来源**；`capacity_mode` area / position / auxiliary / transport / site；`resource_mobility` fixed / mobile / site） |
| | `Activity_Main_Machine` | 66 | 机械主导活动的**主控机械**标注 |
| | `Production_Method_Baseline` | 4 | 生产模式基线（土方 / 桩基 / 混凝土 / 砌筑） |
| | `Resource_Role_Map` | 0 | L4 × 资源 → 主控/伴生 + 配比（**空表**：方案要求主控/伴生结论人工确认后才入库） |
| | `Component_Ratio` | 97 | 构件占比表（结构类型 × L4 → 占比 %）。**97 行全部是 AI 经验估算**（`source_code=ai_estimate_v1`、`confidence=LOW`、`review_state=pending`），用于构件级（柱/墙/梁/板）拆分；**待人工审核**，严禁当成规范数据 |
| **质量** | `data_quality_log` | 505 | 数据质量问题日志（high 95 / medium 359 / low 51；含 `KB_GAP_` 缺口登记），**不许删表** |

> **表删除记录（2026-09-21）**：以下表已删除，不再存在于本库 ——
> `Workface_Capacity_Rule`（域 1.6；容量唯一来源改为 `Resource_Workface_Index` MWI）、
> `Workface_Capacity_Rule_legacy_v1` / `_legacy_v2`（H2）、
> `Unit_Conversion`（H1）、`Norm_Adjustment` / `Norm_Adjustment_Target`（H4/H5）、
> `L4_Labor_Type_Override`（H6）。各自的引用已同步清理
> （`devtools/kb_migrate_ws6.py`、`kb_migrate_ws6_report.py`、`kb_swap_migrated.py`、
> `migrate_v3_workface.py`、`backend/pipeline/kb.py`、`backend/pipeline/nodes/*`、相关测试）。
> ⚠️ **两处遗留未清**：`BuildPlan_KB/tools/query_{norms,duration}.py` 仍查
> `Workface_Capacity_Rule`（见文首「已知问题」）；
> `devtools/verify_kb_invariants.py` 的第 6 条不变量**仍要求这张表存在**，
> 所以现在跑它会报 1 条 FAIL。

**层级关系**：`L3 工种` → `L4 活动` → `条件`。
本项目里 **L5 = 同一活动下的条件组合**（构件类型 × 钢筋直径 × 施工方法…），
存储在定额表的 `condition_combination`（结构化 JSON）与 `condition_text`（可读）字段中，
**没有独立的 L5 表**。

### 覆盖度（实测）

| 检查 | 结果 |
|---|---|
| 31 个工种是否都有活动 | 是，31/31 |
| 493 个活动是否都有定额依据 | **否，11 个空缺**（预制吊装/灌浆、ALC 墙板、爬架、铝模等活动尚无定额） |
| 每个活动是否至少有一种条件 | 是，0 个无条件 |
| 每条定额是否有数值与单位 | 是，0 个空值/空单位 |
| 人工定额覆盖 | 3 886 条，覆盖 433 个 L4 |
| 机械定额覆盖 | 245 条，覆盖 63 个 L4 |
| 机组配置 | 26 条 |

生产模式：**人工主导 430 个，机械主导 63 个**。

---

## 数据来源

`sources` 表实测 **15 行**（`select source_code, source_type, source_category from sources`）：

| 来源代码 | 文档 | 类型 | 提供 |
|---|---|---|---|
| `LD_T72_1_2008` ~ `LD_T72_10_2008`（共 10 行） | 建设工程劳动定额（10 册） | `labor_standard` | 人工工日 |
| `GD_2018_A1_1` | 广东省房屋建筑与装饰工程综合定额（2018）土石方工程 | `regional_quota` | 机械台班 |
| `GD_2018_A1_3` | 同上 · 桩基础工程 | `regional_quota` | 机械台班 |
| `GD_2018_A1_5` | 同上 · 混凝土及钢筋混凝土工程 | `regional_quota` | 机械台班 |
| **`AI_ESTIMATE_V1`** | **AI 经验估算（无规范依据）** | `ai_estimate` | **90 活动 + 90 定额**（**现在会被采用**：照常参与算工期与班组人数，但逐条标注 + 计 `released_ai`） |
| **`SCAFFOLD_V1`** | **WS6 类别占位（无规范来源）** | `placeholder` | 仅 WS6 新增的占位活动（监测 / 预留预埋 / 路面 / 预制构件 / 塔吊电梯）；**数值为估，需整体清退** |

LD/T 十册覆盖：材料运输与加工 / 人工土石方 / 架子 / 砌筑 / 木结构 / 模板 / 钢筋 /
混凝土 / 防水 / 金属结构。

> ⚠️ **AI 数据的使用口径（2026-09-20 用户拍板变更）**：装饰装修 / 机电安装 / 施工准备 / 验收
> 这 4 类共 21 个工种、90 个活动、90 条定额来自 `AI_ESTIMATE_V1`，是按施工经验编造的，
> `status='needs_review'`。**它们目前是这些工序唯一可得的覆盖面来源，因此现在会被采用**——
> 可以像从规范解析出来的定额一样决定工序工期与班组人数；但**必须逐条标注**：交付物
> （Word 与看板）在逐条工序的「依据 / 资源」列写明「**AI 经验估算定额（无规范依据，待审）**」，
> 并在「数据来源与置信度」章节给出条数（状态名 `released_ai`）。
> 仍然拦住的只有四类：人工**否决**（`review_state == "rejected"`，唯一硬开关）、
> **单位不可换算**（如任务 m² vs 定额分母 m³ 且缺厚度参数）、
> **定额口径与任务不符**（method conflict）、**定额值为空**——
> 这四类任务仍然没有班组、工期沿用 WBS 估算。
> **导入真实规范后应逐条清退**（`sources.notes` 里"全部数值由 AI 按施工经验编造，无规范来源。
> 导入真实规范后应整体清退。"的自述保留）：
> ```sql
> DELETE FROM Norm_Labor_Table WHERE source_code='AI_ESTIMATE_V1';
> DELETE FROM L4_Activity_Dictionary WHERE activity_id GLOB '*_AI_*';
> ```

---

## 接口

### query_project.py — 项目上下文

WBS 分解的入口：确定"这个项目有哪些工种"。

| 参数 | 说明 |
|---|---|
| `--types` | 列出所有建筑类型 |
| `--structures` | 列出所有结构形式 |
| `--l3-for <类型>` | 某建筑类型适用的工种（传 ID 或中文名） |
| `--structure <结构>` | 叠加结构形式过滤 |
| `--search <词>` | 模糊搜索 |

指定 `--structure` 后，每个工种会附带 `structure_applicable_l4_count`；
计数为 0 的工种降级为不适用；原本不适用但该结构下有可用活动的工种上调为"通常包含"。

### query_activities.py — 活动

从工种展开到活动。

| 参数 | 说明 |
|---|---|
| `--l3` | 所有工种及活动数 |
| `--l4 <工种ID>` | 某工种下的活动列表 |
| `--structure <结构>` | 过滤该结构体系不适用的活动，并给出 `applicability_level` |
| `--mode <模式>` | 按 `labor_driven` / `equipment_driven` 过滤 |
| `--stats` | 整体统计 |

### query_norms.py — 定额

查某活动的全部定额（人工 + 机械 + 机组 + 调整系数 + 工作面容量 + 主控机械）。

| 参数 | 说明 |
|---|---|
| `--activity <ID>` | 某活动的全部定额 |
| `--work-type <工种ID>` | 某工种下所有活动的定额概览 |
| `--search <词>` | 模糊搜索 |

### query_duration.py — 工期

| 参数 | 说明 |
|---|---|
| `--activity <ID>` | 活动 ID |
| `--quantity <Q>` | 工程量（**必须按定额的真实计量单位传入**） |
| `--resource-limit <N>` | 人工限额（与工作面容量取 min） |
| `--machines <N>` | 主控机械台数（与工作面容量取 min） |
| `--condition <t>` | 按 `condition_text` 精确匹配（须唯一命中一行） |
| `--match "k1,k2"` | 多关键字 AND 匹配（关键字须全部出现在条件里） |
| `--list-conditions` | **列出全部条件 + 可直接复制的 `--match` 串** |
| `--shifts-per-day <N>` | 每天班次（默认 1） |
| `--demo` | 内置案例自检 |

> ⚠️ **已移除**：`--list-adjustments` 与 `--adjustment <ID或名称>` —— 调整系数数据源
> （`Norm_Adjustment` / `Norm_Adjustment_Target` 两张表）已于 H4/H5（2026-09-21）删除，
> 该通道随之整体移除。
>
> ⚠️ **当前不可用**：任何会走到「工作面容量」的调用（典型是带 `--resource-limit` / `--machines`
> 或 `--crew-size` 的用法）会抛
> `sqlite3.OperationalError: no such table: Workface_Capacity_Rule`
> —— 本工具仍在查域 1.6 已删除的那张表。
> **不受影响**：`--demo`、`--list-conditions`、以及不带资源限额的
> `--activity ... --quantity ... --match ...` 用法。

**不确定关键字该写什么时，先跑 `--list-conditions`**，它会对每个条件给出推荐串并标注
是否唯一可寻址（`OK` / `!!`）。

### query_source.py — 来源与质量

| 参数 | 说明 |
|---|---|
| `--list` | 所有数据源 |
| `--for <工种ID>` | 某工种的数据来源 |
| `--quality` | 数据质量概览 |

---

## 核心口径

> ⚠️ **两张定额表口径相反，切勿套用同一公式。**
> ⚠️ 本节的公式来自 `tools/query_duration.py` 的既有实现；工具本身**尚未跟上域 1.6 的容量表删除**，
> 涉及「工作面容量」的一侧目前不可用（见文首「已知问题」）。

### 人工主导 `labor_driven`

```
总工日 D = Q × labor_norm_value / 有效班组
```

- `labor_norm_value` 是**已标准化**到 per 1×`quantity_unit` 的值 → **不再除以 `quantity_basis`**
- `quantity_basis` 仅作出处记录

### 机械主导 `equipment_driven`

```
总工日 D = Q / quantity_basis × machine_shift_norm / 主控机械台数 / 每天班次
```

- `machine_shift_norm` 是**原始值**，按 `basis × quantity_unit` 计 → **必须除以 `quantity_basis`**
- 例：打管桩 Q=500m，定额 0.66 台班/100m → `500 / 100 × 0.66 = 3.3 台班`

### 主控机械

机械活动的工期**只由主控机械决定**；辅助机械（振捣器 / 焊机 / 水泵等）只列出台班与配员。
主控机械来自 `Activity_Main_Machine`（支持按条件分别标注）。
未标注、或同一条件下多行都含主控机械时，工具**报错并列出全部候选，不会任选**。

### 资源投入

```
有效班组 = min(用户资源限额, 工作面容量)
有效机械 = min(用户机械台数, 工作面机械容量)
```

- 用户资源限额：`--resource-limit` / `--machines`
- **工作面容量：唯一来源已改为 `Resource_Workface_Index`（MWI，67 行）**。
  ⚠️ 原来的 `Workface_Capacity_Rule` 表（`legacy_max_labor` / `legacy_max_machine`）**已在域 1.6 删除**；
  产品运行路径（`backend/pipeline/kb.py`、`nodes/scheduler.py`、`nodes/resource.py`）早已改读 MWI
  与叶子自带标定值，取不到容量时按**物理兜底上限**处理并由警告逐条报出。
  **但 KB 侧的 `tools/query_duration.py` / `query_norms.py` 还没跟上**——见文首「已知问题」。
- 两侧都缺 → 报错，不猜测
- 输出中的 `binding_constraint` 指出哪一侧在起约束

### 条件定位

```
--condition "<condition_text>"   精确匹配，须唯一命中一行
--match "框架梁,≤25"             多关键字 AND 匹配
```

实测（2026-09-21 重算）：**人工定额覆盖 433 个活动，其中 77 个活动的 `condition_text` 在同一活动内重复**
（即行数 > 不同 `condition_text` 数），**机械定额 63 个活动中有 9 个同理**——这些活动必须按多维条件匹配
（如 `REBAR_NEW_BEAM` 的 `≤25` 同时对应连系梁/单梁/悬臂梁/斜梁/拱形梁/框架梁 6 种构件）。
> 旧版本这里写的是「172 个不唯一 / 83 个必须多维」，那是**按定额行**统计的旧口径，与本节的**按活动**统计不同，
> 分母也随行数变化（4131 行）而失效——**以本次重算为准**。
命中不唯一时工具**报错并列出候选**，不会盲取首行。

### 单位

工程量 Q 必须按定额的**真实计量单位**传入，即工具输出行 `工程量: <Q> <unit>` 里的单位。
若活动表登记的单位与定额计量单位不一致，工具会给出显式提示。

---

## 已知限制

1. **定额条件可用率未复核** —— 旧版这里写「86.8% —— 543/4120 条」，分母 4120 已随行数变化
   （现在是 **3886 人工 + 245 机械 = 4131 行**）而失效，**本次未重算，旧值不要再引用**。
   已知的确定事实：人工定额覆盖 **433** 个活动（其中 77 个活动条件文本重复）、机械定额覆盖 **63** 个
   （其中 9 个重复）；命中不唯一时工具**报错并列出候选，不会盲取首行**。
   根因是部分来源的定额表在行维度上还有未结构化的区分项。
2. **结构形式映射只覆盖主体结构与桩基** —— 实测 `Structure_Type_L4_Mapping` **1218 行，覆盖 174/493 个活动**
   （旧版写「868 条覆盖 124/489」已过时）。该表只登记 `structure_type_id` / `activity_id`，
   **不含工种列**，所以「覆盖几个工种」无法从本表直算（按活动归属推算约 5/31：混凝土 / 钢筋 / 模板 /
   金属结构 / 桩基），其余工种的活动不区分结构体系。
   对这些工种系统走"无数据 → 保留全部 L4 + 报一条警告"的降级路径（这是刻意的：
   改判"没数据就当作不适用"会一次性误杀大批工种）。缺口已登记在 `data_quality_log`
   （record_id `KB_GAP_structure_mapping_coverage`）。
3. **机械配员不全** —— `Equipment_Crew_Mapping` 实测 **26 行 / 22 种机械名**；
   机械定额表用 `machine_combination_json` 记录机械组合（实测 **53 种不同组合**），
   **两者不是同一口径，覆盖率未精确重算**——旧版「46 种引用 / 只覆盖 20 种」不要再引用。
4. ~~**调整系数的"每 N 单位"基准未结构化**~~ —— **已随表删除而消失**（H4/H5，2026-09-21）：
   `Norm_Adjustment` / `Norm_Adjustment_Target` 两张表已删除（92 + 627 行），
   `query_activities.py` / `query_norms.py` / `query_duration.py` 的调整系数通道
   （含 `--adjustment` / `--list-adjustments`）已整体移除；工期 = `Q × labor_norm_value`。
5. **模板/脚手架/垂直运输的机械定额尚未入库**。
6. **`data_quality_log` 共 505 行，其中 `resolution` 为空的 402 行**（severity：high 95 / medium 359 / low 51）。
   「未裁决」判定用的就是 `resolution IS NULL OR trim(resolution)=''`（该表另有 `resolved_by` / `resolved_at` 列）。
   其中有数条 `KB_GAP_` 缺口登记（结构映射覆盖不全、桩基无人工定额、多个工种只有 AI 估算、装配式活动缺失）。
7. **装饰 / 机电 / 施工准备 / 验收类的活动与定额为 AI 经验估算**，非规范数据（见上）。
   判定口径（2026-09-21 重算）：共 **493** 个活动中，**343 个**带**真实**（非 `AI_ESTIMATE_V1`）人工定额，
   **90 个**带 `source_code = AI_ESTIMATE_V1` 的
   AI 经验估算行（**现在会被采用**：照常算工期与班组人数，交付物逐条标注
   「AI 经验估算定额（无规范依据，待审）」、置信度章节计 `released_ai`），
   11 个连估算行都没有（本轮新增的预制吊装/灌浆、ALC 墙板、爬架、铝模）。
   22/31 个工种没有任何真实劳动定额 —— 已登记在 `data_quality_log`
   （record_id `KB_GAP_ai_estimate_only_work_types`）。

8. **KB 侧的查询工具与不变量脚本仍引用已删除的 `Workface_Capacity_Rule`**（2026-09-21 待修）：
   - `tools/query_duration.py:70` 的 `get_workface_capacity()` 与 `tools/query_norms.py:128`
     仍 `SELECT ... FROM Workface_Capacity_Rule`，所以走到「工作面容量」的调用会抛
     `sqlite3.OperationalError: no such table: Workface_Capacity_Rule`；
   - `devtools/verify_kb_invariants.py` 的第 6 条不变量仍要求「工作面容量只有一张
     `Workface_Capacity_Rule`（478 行）」，与域 1.6 的删除决定相矛盾 ——
     实测 `python devtools\verify_kb_invariants.py` 报 **1 条 FAIL**：
     「工作面容量必须有结构化标定… × 该表已按域 1.6 要求删除，容量唯一来源改为
     `Resource_Workface_Index` MWI 表」。
   **修法**：把这两处工具的容量来源改成 `Resource_Workface_Index`（并删掉对
   `legacy_max_labor` / `legacy_max_machine` 兼容列的依赖），把不变量第 6 条改成
   「容量唯一来源是 `Resource_Workface_Index`，`Workface_Capacity_Rule` **不得**存在」。

---

## 版本

| 项 | 值 |
|---|---|
| 知识库版本 | KB-V1.2 |
| Schema 版本 | DB-V2.0 |
| 工具版本 | Tool-V1.1 |
| 数据截止 | **2026-09-21** |

> 本 README 最近修订：**2026-09-21**。修订原因：业务表由 22 张降为 **19 张**
> （域 1.6 删 `Workface_Capacity_Rule`、H 组删 `Unit_Conversion` / 两张 legacy 归档 /
> `Norm_Adjustment` + `Norm_Adjustment_Target` / `L4_Labor_Type_Override`）、
> 新增 `Component_Ratio` **97 行**（全为 AI 经验估算，`review_state=pending`）、
> `L4_Activity_Dictionary` 与 `L4_Norm_Default` 均 **493 行**、`data_quality_log` **505 行**；
> 同时把各查询工具的现状与遗留问题如实登记进本文档。

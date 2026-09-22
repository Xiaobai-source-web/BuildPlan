"""节点3：关键路径计算 CPM — T-11（纯 Python 确定性算法）

算法直接沿用 `资料/CPM算法来源.txt`（已核对与 Dify 导出 YAML 内嵌代码完全一致）：
提取叶子任务 → 二级 ID 映射 → 解析 FS/SS 依赖 → Kahn 拓扑排序 →
正推 ES/EF → 反推 LS/LF → 关键路径（TF=0）→ 输出排程表。

输入 ctx：wbs（附录 A 三层结构）、dependencies（叶子/二级 ID 依赖）
输出 ctx：cpm_result = {total_duration_days, critical_path, schedule}
"""

import json
from typing import Dict, List, Any

from ..base import BaseNode


def flatten_wbs_phases(wbs_input: Dict) -> Dict[str, Dict]:
    """
    从 WBS 的 phases 结构中提取所有叶子任务（sub_packages）。
    返回: { task_id: { "name": ..., "duration": ... } }
    同时建立 二级ID -> 第一个叶子ID 的映射，用于依赖匹配。
    """
    task_info = {}
    id_mapping = {}  # 二级ID -> 第一个叶子ID

    phases = wbs_input.get("phases", [])
    if not isinstance(phases, list):
        phases = []

    for phase in phases:
        work_packages = phase.get("work_packages", [])
        if not isinstance(work_packages, list):
            continue
        for wp in work_packages:
            sub_packages = wp.get("sub_packages", [])
            if not isinstance(sub_packages, list):
                continue
            # 如果父 ID 还没有映射到子 ID，记录第一个
            parent_id = wp.get("id", "")
            if parent_id and parent_id not in id_mapping and sub_packages:
                first_child = sub_packages[0]
                id_mapping[parent_id] = first_child.get("id", parent_id)

            for sub in sub_packages:
                task_id = sub.get("id")
                if not task_id:
                    continue
                duration = sub.get("duration_days") or sub.get("planned_duration_days") or 1
                try:
                    duration = int(duration)
                except (TypeError, ValueError):
                    duration = 1
                task_info[task_id] = {
                    "name": sub.get("name") or task_id,
                    "duration": duration,
                    "es": 0,
                    "ef": 0,
                    "ls": 0,
                    "lf": 0
                }

    return task_info, id_mapping


def parse_dependencies_with_mapping(deps_input: Dict, id_mapping: Dict) -> tuple:
    """
    解析依赖关系，支持二级ID映射到叶子ID。
    如果 predecessor 或 successor 是二级ID，则尝试映射到第一个叶子ID。
    """
    deps_list = deps_input.get("dependencies", [])
    if not isinstance(deps_list, list):
        deps_list = []

    predecessors = {}
    successors = {}

    for dep in deps_list:
        pred = dep.get("predecessor")
        succ = dep.get("successor")
        if not pred or not succ:
            continue

        # 尝试映射二级ID -> 叶子ID（兜底；正常路径由 deps_gen 保证叶子级）
        pred = id_mapping.get(pred, pred)
        succ = id_mapping.get(succ, succ)

        if not pred or not succ:
            continue

        lag = dep.get("lag_days", 0)
        dep_type = dep.get("type", "FS")

        if dep_type not in ["FS", "SS"]:
            dep_type = "FS"

        predecessors.setdefault(succ, []).append({"task_id": pred, "lag": lag, "type": dep_type})
        successors.setdefault(pred, []).append({"task_id": succ, "lag": lag, "type": dep_type})

    return predecessors, successors


def topological_sort(task_ids: List[str], predecessors: Dict, successors: Dict) -> List[str]:
    """拓扑排序"""
    in_degree = {tid: len(predecessors.get(tid, [])) for tid in task_ids}
    queue = [tid for tid, deg in in_degree.items() if deg == 0]
    sorted_list = []
    while queue:
        queue.sort()
        current = queue.pop(0)
        sorted_list.append(current)
        for succ_info in successors.get(current, []):
            succ = succ_info["task_id"]
            if succ in in_degree:
                in_degree[succ] -= 1
                if in_degree[succ] == 0:
                    queue.append(succ)
    return sorted_list


def calculate_cpm(wbs: Dict, dependencies: Dict) -> Dict[str, Any]:
    """关键路径计算主函数"""
    task_info, id_mapping = flatten_wbs_phases(wbs)

    if not task_info:
        return {"total_duration_days": 0, "critical_path": [], "schedule": []}

    predecessors, successors = parse_dependencies_with_mapping(dependencies, id_mapping)

    for tid in task_info:
        predecessors.setdefault(tid, [])
        successors.setdefault(tid, [])

    sorted_tasks = topological_sort(list(task_info.keys()), predecessors, successors)

    # 正向计算 ES / EF
    for tid in sorted_tasks:
        es = 0
        for pred_info in predecessors.get(tid, []):
            pred_id = pred_info["task_id"]
            lag = pred_info["lag"]
            dep_type = pred_info.get("type", "FS")
            if pred_id in task_info:
                if dep_type == "FS":
                    es = max(es, task_info[pred_id]["ef"] + lag)
                elif dep_type == "SS":
                    es = max(es, task_info[pred_id]["es"] + lag)
        task_info[tid]["es"] = es
        task_info[tid]["ef"] = es + task_info[tid]["duration"]

    total_duration = max(info["ef"] for info in task_info.values()) if task_info else 0

    # 反向计算 LS / LF
    for tid in reversed(sorted_tasks):
        lf = total_duration
        has_successor = False
        for succ_info in successors.get(tid, []):
            succ_id = succ_info["task_id"]
            lag = succ_info["lag"]
            dep_type = succ_info.get("type", "FS")
            if succ_id in task_info:
                if dep_type == "FS":
                    lf = min(lf, task_info[succ_id]["ls"] - lag)
                elif dep_type == "SS":
                    lf = min(lf, task_info[succ_id]["es"] + task_info[succ_id]["duration"] - lag)
                has_successor = True
        if not has_successor:
            lf = total_duration
        task_info[tid]["ls"] = lf - task_info[tid]["duration"]
        task_info[tid]["lf"] = lf

    # 识别关键路径（总时差为0的任务）
    critical_tasks = [tid for tid in sorted_tasks if abs(task_info[tid]["ls"] - task_info[tid]["es"]) < 1e-9]

    def sort_critical_path(path):
        if not path:
            return path
        sorted_path = []
        remaining = set(path)
        while remaining:
            for tid in list(remaining):
                preds = [p["task_id"] for p in predecessors.get(tid, [])]
                if all(p not in remaining for p in preds):
                    sorted_path.append(tid)
                    remaining.remove(tid)
                    break
        return sorted_path

    critical_path = sort_critical_path(critical_tasks)

    schedule = []
    for tid in sorted_tasks:
        schedule.append({
            "task_id": tid,
            "es": task_info[tid]["es"],
            "ef": task_info[tid]["ef"],
            "ls": task_info[tid]["ls"],
            "lf": task_info[tid]["lf"]
        })

    return {
        "total_duration_days": total_duration,
        "critical_path": critical_path,
        "schedule": schedule
    }


class CPMNode(BaseNode):
    name = "cpm"
    title = "关键路径计算"

    def run(self, ctx):
        wbs = ctx.get("wbs") or {}
        dependencies = ctx.get("dependencies") or {"dependencies": []}
        self.emit("node_progress", {"node": self.name, "progress": 40,
                                    "message": "列出全部工序并接上先后关系"})
        result = calculate_cpm(wbs, dependencies)
        self.emit("node_progress", {"node": self.name, "progress": 100,
                                    "message": "关键路径已算出"})
        self.done_summary = (f"总工期 {result['total_duration_days']} 天，"
                             f"关键路径 {len(result['critical_path'])} 个任务")
        return {"cpm_result": result}


def main(wbs: dict, dependencies: dict) -> dict:
    """独立运行入口（与 `资料/CPM算法来源.txt` 的 main 签名一致，供 parity 测试/脚本调用）。"""
    try:
        result = calculate_cpm(wbs, dependencies)
        return {"cpm_result": result}
    except Exception as e:
        return {"cpm_result": {"error": str(e), "total_duration_days": 0,
                               "critical_path": [], "schedule": []}}


if __name__ == "__main__":
    import sys

    if len(sys.argv) >= 3:
        wbs = json.load(open(sys.argv[1], encoding="utf-8"))
        deps = json.load(open(sys.argv[2], encoding="utf-8"))
        print(json.dumps(main(wbs, deps), ensure_ascii=False, indent=2))

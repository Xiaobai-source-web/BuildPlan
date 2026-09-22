"""无节拍路径的**基线快照**（第 41 轮·施工组织层）。

用途：证明"取不到节拍 → 旧口径一字不动"。脚本把真实计划
`terminal/plans/plan_run_1789827002.json` 在**无节拍**边界下的**排程核心产物**
（两版排程行 / 封顶留痕 / 逐日人力 / 峰值 / 总工期 / 是否写了 `_organization`）
序列化并算 sha256，写进 `_org_nocadence_baseline.json`；
`tests/test_org_layer.py::test_无节拍路径与改动前的真实计划快照一致` 复核这个哈希。

⚠️ 只收"排程核心"，**不收 warning 文本与 norm_coverage**：`tests/conftest.py` 的
`_norm_defaults_gate_open_for_synthetic_activities` 会把 `norm_defaults.gate_open` /
`pre_approved` 打开（AI 默认定额行 75 条 → 71 条这类**口径文案**因此不同），
但那是环境差异，不是排程口径差异（实测：排程行/峰值/工期/封顶逐位相同）。

生成（在 backend 目录下）：
    python _org_nocadence_baseline.py
"""
import hashlib
import json
import sys

sys.path.insert(0, '.')
from pipeline.nodes import scheduler          # noqa: E402

PLAN = '../terminal/plans/plan_run_1789827002.json'
SNAP = '_org_nocadence_baseline.json'

#: 参与哈希的键（排程核心；**不含**环境相关的 coverage/warning 文案）
CORE_KEYS = ('theory_total', 'ok_total', 'theory_schedule', 'ok_schedule',
             'theory_capped', 'ok_capped', 'theory_daily_labor', 'ok_daily_labor',
             'peaks', 'rows_with_org')


def snapshot():
    plan = json.load(open(PLAN, encoding='utf-8'))
    out = scheduler.compute_schedules(plan['wbs'], plan.get('dependencies'), {},
                                      {}, plan.get('cpm_result'))
    ver = out['schedule_versions']
    return {
        'theory_total': ver['theory_min']['total_duration_days'],
        'ok_total': ver['resource_ok']['total_duration_days'],
        'theory_schedule': ver['theory_min']['schedule'],
        'ok_schedule': ver['resource_ok']['schedule'],
        'theory_capped': ver['theory_min']['capped'],
        'ok_capped': ver['resource_ok']['capped'],
        'theory_daily_labor': ver['theory_min']['daily_labor'],
        'ok_daily_labor': ver['resource_ok']['daily_labor'],
        'peaks': [ver['theory_min']['peak_labor'], ver['resource_ok']['peak_labor']],
        'rows_with_org': sum(1 for r in ver['resource_ok']['schedule']
                             if '_organization' in r),
    }


def core_of(sub):
    return dict((k, sub[k]) for k in CORE_KEYS)


def dump(core):
    return json.dumps(core, ensure_ascii=False, sort_keys=True, indent=1)


if __name__ == '__main__':
    sub = snapshot()
    core = core_of(sub)
    text = dump(core)
    digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
    json.dump({'sha256': digest, 'core': core},
              open(SNAP, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print('sha256', digest)
    print('chars', len(text), 'theory/ok', sub['theory_total'], sub['ok_total'],
          'peak', sub['peaks'], 'rows', len(sub['ok_schedule']),
          'capped', len(sub['ok_capped']), 'rows_with_org', sub['rows_with_org'])

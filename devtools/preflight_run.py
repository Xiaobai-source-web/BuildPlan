# -*- coding: utf-8 -*-
"""跑计划之前的**预检**：确认模型端点是通的、档位是对的。

为什么需要（实测教训，2026-09-20）：`refresh_active_profile()` 只在 `backend/main.py`
（Web 服务启动）里调用，`backend/pipeline/` 自己**不读档位文件**。若档位没刷到位，
流水线会退回 `backend/.env` 的出厂档（实测那条配额已耗尽 / HTTP 429），
于是**每个节点都静默走确定性兜底**，照样产出一份"看着完整"的 plan_json —— 
但 `meta.usage.calls == 0`、没有边界条件、任务数也不对。**一次都没调用模型却看不出来。**

用法：python devtools/preflight_run.py
退出码：0 = 端点可用；1 = 不可用（别跑）
"""
import io
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from pipeline import config                      # noqa: E402
from pipeline.llm import LLMClient               # noqa: E402


def _mtime(p):
    try:
        return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(os.path.getmtime(str(p))))
    except OSError:
        return '（不存在）'


def main():
    print('=' * 74)
    print('计划运行前预检')
    print('=' * 74)

    prof_file = ROOT / 'backend' / 'llm_profiles.json'
    print('  档位文件     : %s' % prof_file)
    print('  档位文件 mtime: %s' % _mtime(prof_file))

    src = config.refresh_active_profile()
    print('  当前档       : %s' % (src or '（无档位 —— 会退回 backend/.env 的出厂档！）'))
    print('  base_url     : %s' % (config.LLM_BASE_URL or '（空）'))
    print('  model        : %s' % (config.LLM_MODEL or '（空）'))
    print('  Key 是否就位 : %s' % ('是（%d 字符）' % len(config.LLM_API_KEY)
                                  if config.LLM_API_KEY else '否 —— 会全部走兜底！'))

    ok = True
    if not config.LLM_API_KEY:
        ok = False
    if not src:
        print('  ⚠️ 无 active 档位 —— 强烈建议先在 llm_profiles.json 里选定一个。')
        ok = False

    print('  预检最小对话 : ', end='', flush=True)
    try:
        t0 = time.time()
        txt = LLMClient(timeout=60).chat_text('你是测试助手，只回两个字。', '回复：可用')
        print('✅ %r（%.1fs）' % ((txt or '')[:40], time.time() - t0))
    except Exception as exc:
        print('❌ %s: %s' % (type(exc).__name__, str(exc)[:300]))
        print('-' * 74)
        print('模型调用不通 —— 跑下去只会得到一份「0 次调用」的假完整产物。')
        print('请先修端点，或在 backend/llm_profiles.json 里换一个能用的 active 档位。')
        return 1

    # 政策自检：AI 定额与占位定额现在都必须**可用但被标注**
    print('-' * 74)
    print('  定额闸门自检（政策 2026-09-20：不得不用 AI 就用，但必须标出来）')
    try:
        from pipeline import norm_defaults as nd
        for act, kind in (('GD_A11_平整场地', 'machine'),
                          ('CONC_NEW_FOUND', 'machine'),
                          ('FORM_NEW_OTHER', 'labor')):
            opened, label = nd.gate_open(act, kind)
            print('    %-20s kind=%-8s open=%-5s state=%s'
                  % (act, kind, opened, (label or {}).get('state')))
    except Exception as exc:
        print('    （闸门自检跳过：%s: %s）' % (type(exc).__name__, str(exc)[:120]))

    print('=' * 74)
    print('预检结论：%s' % ('✅ 端点可用，可以跑' if ok else '⚠️ 有告警，见上'))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())

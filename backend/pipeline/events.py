"""SSE 事件类型定义（与 产品demo/client.py 保持一致的单一真源）"""

EV_NODE_START = "node_start"
EV_NODE_PROGRESS = "node_progress"
EV_NODE_DONE = "node_done"
EV_NODE_PAUSED = "node_paused"
EV_CONFIRM_REQUIRED = "confirm_required"
EV_PARAM_REVIEW = "param_review"
EV_PLAN_FINAL = "plan_final"
EV_ERROR = "error"
EV_DONE = "done"
EV_PING = "ping"

# 本次运行的步数表（引擎在开跑时下发一次）：终端据此显示"第 k / N 步"。
# 老终端不认识这个事件 —— 渲染层必须返回 None（不打印、不报未知事件）。
EV_RUN_PLAN = "run_plan"

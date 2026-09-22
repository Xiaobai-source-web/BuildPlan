"""backend 兼容转发层：真正的配置在 pipeline/config.py。

保持 `from config import ...` 与 `from pipeline.config import ...` 两种导入均可。
"""

from pipeline.config import *  # noqa: F401,F403

"""运行期设置模块（后台开关）。

显式导出子模块，避免 `from app.modules import settings` 之后 `settings.service`
解析失败 —— 包内的 `__init__.py` 若为空，属性访问依赖导入顺序，容易在测试里偶发失败。
"""
from app.modules.settings import service  # noqa: F401

__all__ = ["service"]

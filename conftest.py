"""让 pytest 从仓库根目录直接运行（pytest tests）时能导入插件模块。

测试文件里 ``import main as router`` 依赖插件根目录在 sys.path 中；
默认的 prepend 导入模式只保证 conftest 所在目录入栈，这里显式补上。
"""

import sys
from pathlib import Path

ROOT = str(Path(__file__).resolve().parent)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

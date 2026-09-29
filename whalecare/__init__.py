"""whalecare —— 自托管个人数据中枢（Python 包壳，只为了 pip 能装）。

★ 设计取舍：这个仓库的**主线分发形式是单文件** `hub.py`（零依赖、可整份通读、可 zipapp）。
PyPI 只是给它加一条"能 `pip install` 的入口"，所以这里**不重写任何逻辑**：
本包只做三件事 —— ① 暴露版本号 ② 提供 `whalecare` 命令 ③ 让 `python -m whalecare` 能跑。

真正跑起来的还是那一份 `hub/hub.py`；片段源码在 `hub/src/whalecare/`，
由 `hub/tools/build_single.py` 合并产出 —— 三者的版本号由
`tests/test_version_consistency.py` 保证一致。
"""

from pathlib import Path

__all__ = ["__version__", "HUB_FILE"]

#: 与 CHANGELOG 首条 `## vX.Y.Z` 保持一致（有门禁盯着，别手改忘了）
__version__ = "0.2.2"


def hub_file() -> Path:
    """定位那份单文件中枢。

    两种场景都要能跑：
      · pip 安装后：包目录里就带着 hub.py（打包时从 hub/hub.py 拷进来）
      · 从仓库源码直接跑：往上一级找 hub/hub.py
    """
    here = Path(__file__).resolve().parent
    for cand in (here / "hub.py", here.parent / "hub" / "hub.py"):
        if cand.is_file():
            return cand
    raise FileNotFoundError(
        "找不到 hub.py —— pip 安装的场景请重新安装；源码场景请确认仓库完整（hub/hub.py 存在）"
    )


HUB_FILE = str(hub_file())

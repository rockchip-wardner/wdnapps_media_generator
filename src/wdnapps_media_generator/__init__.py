"""wdnwebui 媒体生成类应用插件。

目前提供一张卡：**文生图**（``media-generator``），后端是
Qwen-Image-2.1（512×512，RKNN3 / rk1820）。

模型跑在**算力注册中心**启用的 worker 进程里，本包只负责：
把提示词送过去 → 收一张图 → 落盘 → 展示。
"""

__all__ = ["__version__"]

__version__ = "0.1.0"

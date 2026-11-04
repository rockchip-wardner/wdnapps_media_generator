"""图片生成卡片（``media-generator``）。

宿主通过 ``manifest.json`` 的 ``backend.module`` 找到本模块，然后：

* ``register_api(app)`` —— 把本卡的 router 挂到 ``/api/media-generator``；
* ``register_shutdown()`` —— **无参**（宿主就是这么调的！带一个必填参数
  会直接 TypeError，见 ``app/main.py::_on_shutdown``）。

两个函数都只做「接线」，真正的业务在 :mod:`service`。
"""
from __future__ import annotations

import logging

from fastapi import FastAPI

LOG = logging.getLogger("apps.media_generator")

#: 路由前缀。宿主把本 router 挂在 ``/api/media-generator`` 下。
API_PREFIX = "/api/media-generator"


def register_api(app: FastAPI) -> None:
    """把本卡的 API 挂到宿主 app 上。"""
    from wdnapps_media_generator.cards.media_generator.api import router
    app.include_router(router, prefix=API_PREFIX)
    LOG.info("[media-generator] API 已挂载于 %s", API_PREFIX)


def register_shutdown() -> None:
    """宿主关闭时释放本卡持有的资源。

    ⚠️ **必须无参**：宿主在 ``app/main.py::_on_shutdown`` 里是
    ``fn()`` 这样调的 ⇒ 带一个必填参数会直接抛
    ``TypeError: register_shutdown() missing 1 required positional argument``。
    """
    try:
        from wdnapps_media_generator.cards.media_generator.service import (
            get_session,
        )
        get_session().close()
    except Exception:  # noqa: BLE001
        LOG.debug("[media-generator] shutdown 清理失败（忽略）", exc_info=True)

"""文生图卡片（``media-generator``）。

宿主通过 ``manifest.json`` 的 ``backend.module`` 找到本模块，然后调用
``register_api`` / ``register_shutdown``。两个函数都只做「接线」，
真正的业务在 :mod:`service`。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter

LOG = logging.getLogger("apps.media_generator")

#: 路由前缀。宿主把本 router 挂在 ``/api/media-generator`` 下。
API_PREFIX = "/api/media-generator"


def build_router() -> APIRouter:
    """构造本卡的 FastAPI router（延迟 import，避免开机路径拉起依赖）。"""
    from wdnapps_media_generator.cards.media_generator.api import router
    return router


def register_api(app) -> None:
    """把本卡的 API 挂到宿主 app 上。"""
    router = build_router()
    app.include_router(router, prefix=API_PREFIX)
    LOG.info("[media-generator] API 已挂载于 %s", API_PREFIX)


def register_shutdown(app) -> None:
    """宿主关闭时释放本卡持有的资源。"""
    try:
        from wdnapps_media_generator.cards.media_generator.service import (
            get_session,
        )
        get_session().close()
    except Exception:  # noqa: BLE001
        LOG.debug("[media-generator] shutdown 清理失败（忽略）", exc_info=True)

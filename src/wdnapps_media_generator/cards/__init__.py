"""插件卡片集合。

每张卡一个子包，结构与 ``wdnapps_audio_chat.cards.tts`` 一致：

    cards/<card_id>/
    ├── manifest.json      # 插件声明
    ├── api.py             # FastAPI router（挂在 /api/<card_id>）
    ├── service.py         # 业务逻辑 / 状态机
    ├── static/            # template.html + app.js + app.css + wizard.json
    └── __init__.py        # register_api / register_shutdown
"""

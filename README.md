# wdnapps-media-generator

wdnwebui 的**媒体生成类**应用插件。当前提供一张卡：**图片生成**
（`media-generator`），后端是 Qwen-Image-2.1（512×512，RKNN3 / rk1820）。

> 与其它 wdnapps 同结构：卡片目录 + `manifest.json` + `api.py` +
> `service.py` + `static/`，靠 `pyproject.toml` 的
> `[project.entry-points."wdnwebui.cards"]` 注册。

## 卡片

| 项 | 值 |
|---|---|
| 卡片 id | `media-generator` |
| 显示名 | **图片生成** |
| 能力 | `swcap_text_to_image` |
| 出图入口 | 输入一段文字 → 生成 → 展示 PNG |
| 模型 | `qwenimage21`（算力注册中心启用的 worker 进程） |
| 耗时 | worker 首次 load ≈ 90 s；每张图 8 步 ≈ 34 s / 20 步 ≈ 55 s |

## 架构：图是在**别的进程**里出的

```
浏览器
  │  POST /api/media-generator/generate
  ▼
Web UI 进程（本插件的 service.py）
  │  DDS RPC  run_prompt(prompt, output_dir=…)
  ▼
worker 进程（compute_worker.py --model qwenimage21）
  │  cshim → TE(卡0) + DiT(卡1) + VAE(卡1)
  ▼
PNG 落到 <resource_dir>/media_generator/runs/<run_id>/img01.png
```

* 模型**不在 Web UI 进程里跑** —— NPU 资源由算力注册中心统一分配，
  worker 崩了不影响 UI；一个 worker 还能同时服务 LAN 对端。
* 图的字节由 **worker 自己写**：后端把 `output_dir` 传进去，它写完 PNG
  后**原样复制**过去 ⇒ 与运行时基线**逐字节一致**（不重新编码）。
  回退路径（worker 没落盘）由本插件用**同样的 PNG 参数**自己编码，
  字节仍然一致。

## 安装

```bash
cd /home/rockchip/wdnwebui/.venv/src/wdnapps_media_generator
.venv/bin/python -m build --wheel        # 或 python -m build
pip install --force-reinstall dist/*.whl
```

装完 **重启 Web UI**（卡片是开机时扫描 entry points 的）。

### ⚠️ package-data 漏了会怎样

`pyproject.toml` 的 `[tool.setuptools.package-data]` 必须含
`manifest.json` 与 `static/*`。漏了的话：卡片**仍会被发现**（manifest
在包外层），但 `/static/apps/media-generator/` 是空的 ⇒ 前端 404。
editable 装法看不出来（源码就在那儿），只有真 wheel 才暴露。

## 宿主侧还需要两处配合

1. **`app/static/index.html.template` 加卡位**：

   ```html
   {% card_slot "media-generator" %}
   ```

   宿主把每个卡片的 HTML 片段注入到 index.html 的对应占位符；没有占位符
   ⇒ 卡片点开是**空白页**（卡片本身在应用列表里是正常显示的）。

2. **模型目录 + worker manifest**：见下面「模型与 worker」。

## 模型与 worker

worker 的 manifest 放在**模型文件夹**里（一个文件夹 = 一个安装单元）：

```
/mnt/oem/models/qwenimage21/qwenimage21.json     ← manifest
/mnt/oem/models/qwenimage21/...                   ← 模型文件
```

板上 `/mnt/oem` 只有 558 MB 空闲（放不下 9.4 GB 的模型），所以本板用
**软链**把模型包挂进来：

```bash
ln -s /home/rockchip/models/qwenimage21 /mnt/oem/models/qwenimage21
```

manifest 里 `model_dir_env = WDNAI_QWEN_IMAGE_21_DIR` —— worker 启动时
会把它指到 manifest 所在目录，wdnai 认这个环境变量。

manifest 需要 `requires_cards: 2`（TE 在卡 0、DiT+VAE 在卡 1）。

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/media-generator/` | 状态快照 |
| POST | `/api/media-generator/generate` | 发起出图（**立即返回**） |
| POST | `/api/media-generator/stop` | 请求取消当前这次 |
| GET | `/api/media-generator/history` | 生成记录 |
| GET | `/api/media-generator/image/{run_id}` | 取 PNG |
| WS | `/api/media-generator/ws` | 状态推送 |

## 界面

* **提示词框** + 步数（8 / 20 / 28）+ 「固定噪声」开关。
* **进度条**：实时显示
  * 当前阶段与这一步在做什么（`flow-match 第 3/8 步` / `VAE 第 2/4 段`）
  * **这一步**花了多久、**平均每步**多久、已用多久
  * **预计还需**多久（ETA = 剩余步数 × 平均每步 + 后续阶段预估值）
  * 整条流水线的百分比
* **结果图** + 生成记录（点缩略图回看）。

进度是 worker 通过 DDS delta 通道推过来的**结构化事件**
（`qi|stage=flow|step=3|total=8|elapsed_ms=2601.5`）⇒ 卡片的
`service.py` 解析后折成进度条的各个字段。

## 已知边界

* **停止打不断正在跑的那次**：出图是一次 NPU 调用（C 侧不可中断），
  「停止」= 这一张的结果不要了。
* **一次只跑一张**：再点「生成」会返回 409。出图是重能力，排队没有意义。
* **步数**只影响采样步数，不影响模型（模型不用重建）。

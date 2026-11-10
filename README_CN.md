[English](README.md) | [简体中文](README_CN.md)

# wdnapps-media-generator

wdnwebui 的**媒体生成类**应用插件。当前提供一张卡：**图片生成**
（`media-generator`），后端是 Qwen-Image-2.1（512×512，RKNN3 / rk1820）。

> 与其它 wdnapps 同结构：卡片目录 + `manifest.json` + `api.py` +
> `service.py` + `static/`，靠 `pyproject.toml` 的
> `[project.entry-points."wdnwebui.cards"]` 注册。

## 卡片

本仓库有**两张卡**，同一个模型（`qwenimage21` = Qwen-Image-2.1）：

| | `media-generator` | `image-editor` |
|---|---|---|
| 显示名 | **图片生成** | **图片处理** |
| 输入 | 一段文字 | **一张图 + 一段文字** |
| 输出 | 512×512 PNG | 512×512 PNG |
| 能力 | `swcap_text_to_image` | `swcap_text_to_image`（**同一个**） |
| 路由前缀 | `/api/media-generator` | `/api/image-editor` |
| 额外参数 | — | `image`（base64）、`strength`（0~1） |

`image-editor` 排在应用网格里 `media-generator` 的**后面**（`display_order`
66 vs 65），代码在同一个仓、同一个 `cards/` 目录下。

### `image-editor` 的额外依赖：VAE **编码器**

「图 + 文 → 图」比文生图多一步：**输入图要先变成潜变量**。

板端模型包 `Qwen-Image-2.1/` 里原本只有 VAE **解码器**
（`vae_qwenimage21-512.3588.rknn`：输入 `latents` → 输出 `image`），
**没有编码器** ⇒ 图进不了潜空间 ⇒ 图生图做不了。

现在补上了 **`vae_qwenimage21-512.encoder.3588.rknn`**（205 MB，rknn2 /
rk3588 / fp16）：输入 `pixel_values` `[1,4,1,512,512]`（RGBA，[-1,1]）→
输出 `latents` `[1,64,1,32,32]`。它和解码器**同在本机 3588 的 NPU** 上，
实测**能同时常驻**（解码器 2311 MiB + 编码器 ≈ 0.9 GiB < 4 GiB 池）。

> ⚠️ 模型包里**没有**编码器时，图生图会**明确报错**
> （「本机不支持图生图」），**不会**偷偷退化成文生图。
> 文生图完全不受影响（编码器是可选件）。

### 向导里的**拖拽上传**（`type: "image"`）

`image-editor` 的向导第 2 步「原图与改法」带一个**拖拽上传区**：
把图拖进去（或点一下选文件）⇒ 出预览 + 文件名 ⇒ 「下一步」变亮 ⇒
「完成配置」后**自动开跑**（向导页看不到卡片进度条，卡片会提示去哪看）。

用的是宿主向导新加的 **`"type": "image"`** 字段
（`app/static/core/wizard/state.js::_buildImageField`）：

| 项 | 值 |
|---|---|
| 字段值 | **data URL**（`data:image/png;base64,…`） |
| 落盘 | **不落 localStorage**（字段声明 `"transient": true`） |
| 回填 | `applyWizardSnapshot(opts)` 里剥前缀拿裸 base64 |

⚠️ **为什么值是 data URL 而不是路径**：向导原有的「文件字段」那套
（`browse` / `upload`）存的是**服务端路径**，要用 `upload_endpoint` +
`thumbnail_endpoint` 两个后端端点。图片是**浏览器本地文件**，走那条路
要多两个端点；而 data URL 直接能当 `<img src>` 预览、剥前缀就是本卡
`/generate` 收的 base64 ⇒ 卡片侧几乎零改动。

⚠️ **base64 不能落 localStorage**：一张图几百 KB~几 MB，
`localStorage.setItem` 是**整体覆盖** ⇒ 配额爆了会把**其它字段**
（提示词、步数…）一起弄丢。宿主侧用 `"transient": true` 标记 + 落盘前剔除。

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
cd <wdnwebui>/.venv/src
git clone git@github.com:rockchip-wardner/wdnapps_media_generator.git
pip install -e wdnapps_media_generator
```

或者直接装构建好的 wheel：

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
/mnt/oem/models/Qwen-Image-2.1/qwenimage21.json     ← manifest
/mnt/oem/models/Qwen-Image-2.1/...                   ← 模型文件
```

板上 `/mnt/oem` 只有 558 MB 空闲（放不下 9.4 GB 的模型），所以本板用
**软链**把模型包挂进来：

```bash
ln -s /home/rockchip/models/Qwen-Image-2.1 /mnt/oem/models/Qwen-Image-2.1
```

⚠️ **模型目录名 = HF 仓库名**（`Qwen/Qwen-Image-2.1` ⇒ `Qwen-Image-2.1`），
与 `/mnt/oem/models/` 下其它包（`Qwen3-VL-4B` / `Qwen3-TTS` /
`LocateAnything-3B`）同规则；而 **manifest 的文件名 = model_id**
（`qwenimage21.json`）⇒ 它同时是 worker 的 `--model` 参数与 `/proc` 去重
needle，**改目录名不要改它**。改名后同步 manifest 的 `model_dir_name` 即可
（发现规则是「manifest 所在目录 = 模型目录」，代码不依赖目录名）。

manifest 里 `model_dir_env = WDNAI_QWEN_IMAGE_21_DIR` —— worker 启动时
会把它指到 manifest 所在目录，wdnai 认这个环境变量。

manifest 需要 `requires_cards: 2`（TE 在卡 0、DiT+VAE 在卡 1）。

### ⚠️ `infer_timeout_s`：一次推理的预算是**每模型**的

worker 侧的推理预算是「replier pump 最多被一次推理占多久」，超了就回一条
**带原因**的错误（而不是把 pump 焊死）。它**不能是全局常量**：

* 全局 60 s ⇒ 20/28 步出图（实测 103 s / 124 s）必被砍掉，报
  `TimeoutError: inference exceeded 60.0s`；
* 全局调大 ⇒ 一个卡住的对话 worker 会长时间占着 pump。

⇒ 所以它写在 **manifest** 里（文生图 = 300 s）：

```json
"infer_timeout_s": 300
```

worker 用它当 pump 上限，Web UI 主进程用 **它 + 30 s margin** 当客户端
RPC 预算（客户端必须比 worker 活得久，否则先超时的是客户端 ⇒ 报错没有
原因可查）。两边读**同一个 manifest 文件** ⇒ 预算不会漂移。

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

### ⚠️ 点击后**必须立刻**有反馈（2026-11-04 用户反馈后修）

用户反馈「点了生成图片就一直卡在那，进度条要非常久之后才出来，也没提示
在做什么」。根因是三个**反馈空窗**叠在一起：

| # | 空窗 | 修法 |
|---|---|---|
| 1 | worker 的第一条 `qi|` 事件要等它真开始干活（TE 编码 + 首步）才到，最快 1~4 秒 ⇒ 这之前快照里 `progress` 是 `None` ⇒ **前端把进度条整个 `hidden` 了** | ① 后端 `start()` 立刻塞一条 `stage="start"` 事件；② 前端 **busy 时进度条不许消失**（无事件时显示「启动中 · 0%」） |
| 2 | `doGenerate()` 里 `await postJSON()` 之后**直接返回** ⇒ 界面一动不动，要等下一次 WS 帧（最快 1 s）或兜底轮询（最慢 5 s）才知道「点了」 | 点击即刻：按钮禁用 + 变「提交中…」+ meta/hint 说清楚 + 进度条亮起来；POST 成功后**立刻** `pollSnapshot()` 一次 |
| 3 | 向导模式里自动出图时**卡片页是隐藏的** ⇒ 向导界面自己又没有进度显示 ⇒ 用户面对一个「什么都没发生」的结束画面 | 向导完成后明确提示「已开始生成，请到「图片生成」页看实时进度」 |

修完实测（headless chromium，8 步）：

| 时刻 | 界面 |
|---|---|
| **t = 0.22 s** | 进度条已亮：「启动中」0%，按钮禁用，meta「正在生成（8 步）…」 |
| t = 0.34 s | 「编码提示词：文本编码 第 1/1 张，预计还需 31 秒」2% |
| t = 3.3 s | 「生成图像：flow-match 第 1/8 步，预计还需 29 秒」12% |
| … | 每步实时推进 |
| t = 33.4 s | 「完成」99% → 出图（33.3 s） |

复现脚本：`scripts/probe_click_timeline.py`（webui 仓库，headless chromium
点一次「生成图片」，每 100 ms 抓一次 DOM + 记录 WS 帧到达时刻）。

## 已知边界

* **停止打不断正在跑的那次**：出图是一次 NPU 调用（C 侧不可中断），
  「停止」= 这一张的结果不要了。
* **一次只跑一张**：再点「生成」会返回 409。出图是重能力，排队没有意义。
* **步数**只影响采样步数，不影响模型（模型不用重建）。
* **模型没加载完时不能出图**：`generate` 会返回 409 并说明是「正在加载」
  还是「压根没启用」——别去猜，直接看提示。
* **推理超时会翻成人话**：worker 侧报
  `inference exceeded Ns` 时，卡片显示「推理超时：worker 侧的推理预算用完了
  （NPU 可能卡住了）……（原文：…）」，并建议去算力配置页重启该实例。

## License

Apache-2.0。见 [LICENSE](LICENSE)。

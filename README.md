[English](README.md) | [简体中文](README_CN.md)

# wdnapps-media-generator

Media-generation application plugin for wdnwebui. It currently provides one card:
**Image Generation** (`media-generator`), backed by Qwen-Image-2.1 (512×512,
RKNN3 / rk1820).

> Same structure as the other wdnapps packages: a card directory + `manifest.json`
> + `api.py` + `service.py` + `static/`, registered through
> `[project.entry-points."wdnwebui.cards"]` in `pyproject.toml`.

## Cards

This repository ships **two cards**, sharing the same model
(`qwenimage21` = Qwen-Image-2.1):

| | `media-generator` | `image-editor` |
|---|---|---|
| Display name | **Image Generation** | **Image Editing** |
| Input | a piece of text | **an image + a piece of text** |
| Output | 512×512 PNG | 512×512 PNG |
| Capability | `swcap_text_to_image` | `swcap_text_to_image` (**the same one**) |
| Route prefix | `/api/media-generator` | `/api/image-editor` |
| Extra parameters | — | `image` (base64), `strength` (0~1) |

`image-editor` is placed **after** `media-generator` in the app grid
(`display_order` 66 vs 65); the code lives in the same repository, under the same
`cards/` directory.

### The extra dependency of `image-editor`: the VAE **encoder**

"Image + text → image" needs one more step than text-to-image: **the input image has
to become a latent first**.

The board-side model package `Qwen-Image-2.1/` originally shipped only the VAE
**decoder** (`vae_qwenimage21-512.3588.rknn`: input `latents` → output `image`) and
**no encoder** ⇒ the image could not enter the latent space ⇒ image-to-image was
impossible.

Now the **`vae_qwenimage21-512.encoder.3588.rknn`** is there as well (205 MB, rknn2 /
rk3588 / fp16): input `pixel_values` `[1,4,1,512,512]` (RGBA, [-1,1]) → output
`latents` `[1,64,1,32,32]`. It and the decoder **reside together on this board's
3588 NPU**, and are **measured to coexist** (decoder 2311 MiB + encoder ≈ 0.9 GiB
< the 4 GiB pool).

> ⚠️ When the model package has **no** encoder, image-to-image fails **explicitly**
> ("image-to-image is not supported on this board") and **never** silently degrades
> to text-to-image. Text-to-image is entirely unaffected (the encoder is optional).

### The wizard's **drag-and-drop upload** (`type: "image"`)

Step 2 of the `image-editor` wizard, "Original Image and Edits", carries a
**drag-and-drop upload area**: drop the image in (or click to pick a file) ⇒ a
preview plus the file name appear ⇒ "Next" turns active ⇒ after "Finish Setup" it
**starts running automatically** (the wizard page has no progress bar, so the card
tells you where to look).

It uses the **`"type": "image"`** field newly added to the host wizard
(`app/static/core/wizard/state.js::_buildImageField`):

| Item | Value |
|---|---|
| Field value | **data URL** (`data:image/png;base64,…`) |
| Persistence | **not stored in localStorage** (the field declares `"transient": true`) |
| Backfill | `applyWizardSnapshot(opts)` strips the prefix and takes the bare base64 |

⚠️ **Why the value is a data URL and not a path**: the wizard's original "file
field" mechanism (`browse` / `upload`) stores a **server-side path** and needs two
backend endpoints, `upload_endpoint` + `thumbnail_endpoint`. An image is a
**browser-local file**, and going that route means two more endpoints; a data URL
can be previewed directly as an `<img src>` and, once the prefix is stripped, is
exactly the base64 the card's `/generate` expects ⇒ almost no card-side change.

⚠️ **base64 must not be stored in localStorage**: one image runs from a few hundred
KB to a few MB, and `localStorage.setItem` is an **overwrite of the whole item** ⇒
blowing the quota takes the **other fields** (prompt, step count, …) down with it.
The host marks the field `"transient": true` and strips it before persisting.

## Architecture: the image is produced in **another process**

```
browser
  │  POST /api/media-generator/generate
  ▼
Web UI process (this plugin's service.py)
  │  DDS RPC  run_prompt(prompt, output_dir=…)
  ▼
worker process (compute_worker.py --model qwenimage21)
  │  cshim → TE(card 0) + DiT(card 1) + VAE(card 1)
  ▼
PNG lands in <resource_dir>/media_generator/runs/<run_id>/img01.png
```

* The model **does not run inside the Web UI process** — NPU resources are
  allocated centrally by the compute registry, a crashed worker does not take the UI
  down, and one worker can serve a LAN peer at the same time.
* The image bytes are **written by the worker itself**: the backend passes
  `output_dir` in and the worker **copies the PNG over verbatim** ⇒ byte-for-byte
  identical to the runtime baseline (no re-encoding). The fallback path (worker did
  not write to disk) has this plugin encode it with the **same PNG parameters**, so
  the bytes still match.

## Installation

```bash
cd <wdnwebui>/.venv/src
git clone git@github.com:rockchip-wardner/wdnapps_media_generator.git
pip install -e wdnapps_media_generator
```

Or install the pre-built wheel directly:

```bash
cd /home/rockchip/wdnwebui/.venv/src/wdnapps_media_generator
.venv/bin/python -m build --wheel        # or python -m build
pip install --force-reinstall dist/*.whl
```

After installing, **restart the Web UI** (cards are scanned at boot by scanning entry
points).

### ⚠️ What happens when package-data is missing

`[tool.setuptools.package-data]` in `pyproject.toml` must contain `manifest.json` and
`static/*`. Without them the card **is still discovered** (the manifest sits at the
outer package level), but `/static/apps/media-generator/` ends up empty ⇒ the front
end 404s. An editable install hides this (the sources are right there); only a real
wheel exposes it.

## Two more things the host side needs

1. **A card slot in `app/static/index.html.template`**:

   ```html
   {% card_slot "media-generator" %}
   ```

   The host injects each card's HTML fragment into the matching placeholder in
   index.html; without the placeholder, opening the card gives a **blank page**
   (the card itself still shows up normally in the app list).

2. **Model directory + worker manifest**: see "Models and Workers" below.

## Models and Workers

The worker's manifest lives in the **model folder** (one folder = one distribution
unit):

```
/mnt/oem/models/Qwen-Image-2.1/qwenimage21.json     ← manifest
/mnt/oem/models/Qwen-Image-2.1/...                   ← model files
```

On this board `/mnt/oem` has only 558 MB free (not enough for the 9.4 GB model), so
this board mounts the model package in with a **symlink**:

```bash
ln -s /home/rockchip/models/Qwen-Image-2.1 /mnt/oem/models/Qwen-Image-2.1
```

⚠️ **The model directory name = the HF repository name** (`Qwen/Qwen-Image-2.1` ⇒
`Qwen-Image-2.1`), the same rule as the other packages under `/mnt/oem/models/`
(`Qwen3-VL-4B` / `Qwen3-TTS` / `LocateAnything-3B`); and **the manifest's filename =
the model_id** (`qwenimage21.json`) ⇒ it is simultaneously the worker's `--model`
argument and the `/proc` dedup needle, so **do not change it when renaming the
directory**. After renaming, just sync the manifest's `model_dir_name` (the
discovery rule is "the directory the manifest sits in is the model directory"; the
code does not depend on the directory name).

The manifest declares `model_dir_env = WDNAI_QWEN_IMAGE_21_DIR` — the worker points
it at the manifest's own directory at startup, and wdnai honours that env var.

The manifest needs `requires_cards: 2` (TE on card 0, DiT+VAE on card 1).

### ⚠️ `infer_timeout_s`: an inference budget is **per model**

The worker-side inference budget is "how long the replier pump may be occupied by a
single inference"; when it is exceeded the worker returns an error **with a reason**
(rather than wedging the pump). It **must not be a global constant**:

* a global 60 s ⇒ the 20/28-step generation (measured at 103 s / 124 s) always gets
  cut off, reporting `TimeoutError: inference exceeded 60.0s`;
* a global bump ⇒ one stuck chat worker holds the pump for a long time.

⇒ So it lives in the **manifest** (text-to-image = 300 s):

```json
"infer_timeout_s": 300
```

The worker uses it as the pump ceiling, while the Web UI main process uses **it + a
30 s margin** as the client-side RPC budget (the client must outlive the worker, or
the client times out first ⇒ an error with nothing to diagnose). Both sides read
the **same manifest file** ⇒ the budget cannot drift.

## API

| Method | Path | Description |
|---|---|---|
| GET | `/api/media-generator/` | status snapshot |
| POST | `/api/media-generator/generate` | start a generation (**returns immediately**) |
| POST | `/api/media-generator/stop` | request cancellation of the current run |
| GET | `/api/media-generator/history` | generation history |
| GET | `/api/media-generator/image/{run_id}` | fetch the PNG |
| WS | `/api/media-generator/ws` | status push |

## UI

* **Prompt box** + step count (8 / 20 / 28) + a "fixed noise" toggle.
* **Progress bar**: shows in real time
  * the current stage and what this step is doing (`flow-match step 3/8` /
    `VAE segment 2/4`)
  * how long **this step** took, how long **each step averages**, how long in total
  * **how much is left** (ETA = remaining steps × average per step + estimate for the
    later stages)
  * the percentage of the whole pipeline
* **Result image** + generation history (click a thumbnail to look back).

Progress arrives as **structured events** pushed over the DDS delta channel
(`qi|stage=flow|step=3|total=8|elapsed_ms=2601.5`) ⇒ the card's `service.py`
parses them into the fields of the progress bar.

### ⚠️ There must be **immediate** feedback after the click (fixed after the 2026-11-04 user report)

The user reported "I click generate image and it just hangs there; the progress bar
takes a very long time to show up, and there is no hint about what it is doing". The
root cause was three **feedback gaps** stacked on top of each other:

| # | Gap | Fix |
|---|---|---|
| 1 | The worker's first `qi|` event only arrives once it really starts working (TE encoding + first step), at the earliest 1~4 s ⇒ before that `progress` is `None` in the snapshot ⇒ **the front end hid the whole progress bar** | ① the backend's `start()` immediately pushes a `stage="start"` event; ② the progress bar **must not disappear while busy** (with no event yet it shows "starting · 0%") |
| 2 | `doGenerate()` **returns right after** `await postJSON()` ⇒ the UI does not move, and it takes the next WS frame (earliest 1 s) or the fallback poll (latest 5 s) to learn that the click registered | on click: disable the button, turn it into "submitting…", spell it out in meta/hint, light up the progress bar; after the POST succeeds **immediately** call `pollSnapshot()` once |
| 3 | In wizard mode the auto-generation runs **while the card page is hidden** ⇒ the wizard UI itself has no progress display ⇒ the user faces an ending screen where "nothing happened" | after the wizard finishes, clearly state "generation started, go to the 'Image Generation' page for live progress" |

Measured after the fix (headless chromium, 8 steps):

| Moment | UI |
|---|---|
| **t = 0.22 s** | progress bar lit: "starting" 0%, button disabled, meta "generating (8 steps)…" |
| t = 0.34 s | "encoding prompt: text encoding image 1/1, ~31 s remaining" 2% |
| t = 3.3 s | "generating image: flow-match step 1/8, ~29 s remaining" 12% |
| … | advances in real time per step |
| t = 33.4 s | "done" 99% → image out (33.3 s) |

Reproduction script: `scripts/probe_click_timeline.py` (in the webui repository,
headless chromium clicks "generate image" once and grabs the DOM every 100 ms while
recording WS frame arrival times).

## Known boundaries

* **Stop cannot interrupt the run in progress**: generating an image is one NPU call
  (not interruptible from the C side), so "stop" means "do not want the result of
  this one".
* **Only one at a time**: clicking "generate" again returns 409. Generating an image
  is heavy, so queueing makes no sense.
* **Step count** only affects the number of sampling steps, not the model (the model
  is not rebuilt).
* **No generation before the model has finished loading**: `generate` returns 409 and
  says whether it is "still loading" or "not enabled at all" — don't guess, read the
  message.
* **Inference timeouts are translated into plain language**: when the worker reports
  `inference exceeded Ns`, the card shows "inference timed out: the worker-side
  inference budget ran out (the NPU may be stuck)… (original: …)" and suggests
  restarting that instance from the compute configuration page.

## License

Apache-2.0. See [LICENSE](LICENSE).

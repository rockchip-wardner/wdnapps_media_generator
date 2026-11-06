(function () {
  "use strict";

  // 图片处理卡片前端（图生图）。
  //
  // 状态有三个来源：
  //   1. GET  /api/image-editor/          —— 首帧 + 兜底轮询
  //   2. WS   /api/image-editor/ws        —— 状态变化推送（主）
  //   3. POST /api/image-editor/generate  —— 发起出图
  //
  // 出图是重操作（8 步 ≈ 35 s，20 步 ≈ 70 s，28 步 ≈ 95 s，40 步 ≈ 130 s，首次还要等 worker 把模型
  // load 进来 ≈ 90 s）⇒ 界面必须全程给出「在跑 / 还要等多久」的反馈，
  // 而不是让用户对着一张白屏猜。

  const API = "/api/image-editor";
  const PAGE_ID = "image-editor";

  const IE = {
    ws: null,
    wsBackoff: 1.0,
    pollHandle: null,
    lastSnap: null,
    history: [],
    started: false,
    // ⭐ 输入图（base64，**不带** data URL 前缀）
    sourceB64: "",
    sourceName: "",
  };

  // ------------------------------------------------------------------ //
  // 小工具
  // ------------------------------------------------------------------ //
  function $(id) {
    return document.getElementById(id);
  }

  function setText(el, text) {
    if (el) el.textContent = text;
  }

  function fmtTime(epochSec) {
    if (!epochSec) return "--";
    const d = new Date(epochSec * 1000);
    const p = (n) => String(n).padStart(2, "0");
    return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  }

  async function postJSON(path, body) {
    const res = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    let data = null;
    try {
      data = await res.json();
    } catch (e) {
      data = null;
    }
    if (!res.ok) {
      const detail = (data && (data.detail || data.message)) || res.statusText;
      throw new Error(detail || `HTTP ${res.status}`);
    }
    return data || {};
  }

  async function getJSON(path) {
    const res = await fetch(path, { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
  }

  // ------------------------------------------------------------------ //
  // 进度条
  // ------------------------------------------------------------------ //
  // worker 通过 DDS delta 通道把**结构化进度**推过来（后端 service.py
  // 解析后放进快照的 progress 字段）：
  //   stage      —— 当前阶段（生成图像 / 解码图像 / …）
  //   detail     —— 这一步具体在做什么（flow-match 第 3/8 步）
  //   step_ms    —— **这一步**花了多久
  //   step_avg_ms—— 本阶段平均每步
  //   eta        —— 预计还需多久
  //   percent    —— 整条流水线的百分比
  function fmtDur(sec) {
    if (sec === null || sec === undefined) return "--";
    const s = Math.max(0, Math.round(sec));
    if (s < 60) return `${s} 秒`;
    const m = Math.floor(s / 60);
    return `${m} 分 ${String(s % 60).padStart(2, "0")} 秒`;
  }

  function renderProgress(prog, busy) {
    const box = $("ie-progress");
    if (!box) return;
    // ⚠️⚠️ **正在跑的时候进度条必须一直显示**（2026-11-04 实测修）。
    // 之前是「prog 为 null ⇒ 把整个盒子 hidden」⇒ 点完生成、worker 的
    // 第一条进度事件还没到的那几秒里，进度条**消失**了 ⇒ 用户看着像
    // 「点了没反应、什么都没发生」。
    if (!prog && !busy) {
      box.classList.add("hidden");
      return;
    }
    box.classList.remove("hidden");
    if (!prog) {
      // 正在跑、但还没有任何进度事件 ⇒ 显示一个 0% 的空条 + 人话
      setText($("ie-progress-stage"), "启动中");
      setText($("ie-progress-detail"), "正在把请求送到 worker …");
      setText($("ie-progress-pct"), "0%");
      const fill0 = $("ie-progress-fill");
      if (fill0) fill0.style.width = "0%";
      setText($("ie-progress-step"), "--");
      setText($("ie-progress-eta"), "预计还需 --");
      return;
    }
    box.classList.remove("hidden");
    setText($("ie-progress-stage"), prog.stage_label || prog.stage || "--");
    setText($("ie-progress-detail"), prog.detail || "--");
    const pct = Math.max(0, Math.min(100, Number(prog.percent) || 0));
    setText($("ie-progress-pct"), `${Math.round(pct)}%`);
    const fill = $("ie-progress-fill");
    if (fill) fill.style.width = `${pct}%`;

    const stepMs = Number(prog.step_ms) || 0;
    const avgMs = Number(prog.step_avg_ms) || 0;
    const stepTxt = [];
    if (stepMs > 0) stepTxt.push(`这一步 ${fmtDur(stepMs / 1000)}`);
    if (avgMs > 0 && prog.total > 1) stepTxt.push(`平均每步 ${fmtDur(avgMs / 1000)}`);
    if (prog.elapsed) stepTxt.push(`已用 ${fmtDur(prog.elapsed)}`);
    setText($("ie-progress-step"), stepTxt.join(" · ") || "--");

    const eta = Number(prog.eta);
    setText($("ie-progress-eta"),
            eta > 0 ? `预计还需 ${fmtDur(eta)}` : "即将完成");
  }

  // ------------------------------------------------------------------ //
  // 快照渲染
  // ------------------------------------------------------------------ //
  function renderSnapshot(snap) {
    if (!snap) return;
    IE.lastSnap = snap;

    const banner = $("ie-unavailable");
    const reasonEl = $("ie-unavailable-reason");
    const genBtn = $("ie-generate");
    const stopBtn = $("ie-stop");
    const metaEl = $("ie-meta");
    const statusLine = $("ie-status-line");
    const hintEl = $("ie-hint");

    const ready = !!snap.capability_ready;
    const loading = Array.isArray(snap.capability_loading) && snap.capability_loading.length > 0;
    const busy = !!snap.busy;

    // 能力没就绪时的原因文案：**区分「在加载」和「压根没启用」** ——
    // 前者等一等就好，后者要去算力配置页开，文案混了会误导用户。
    let reason = "";
    if (!ready) {
      if (loading) {
        reason = `模型正在加载（${snap.capability_loading.join(", ")}），首次加载约需 90 秒，请稍候。`;
      } else {
        reason = "图片处理所需的模型未启用，请到「算力配置」页启用 qwenimage21 实例（需要两张 NPU 卡）。";
      }
    }
    if (banner && reasonEl) {
      banner.classList.toggle("hidden", !reason);
      setText(reasonEl, reason);
    }

    if (genBtn) {
      genBtn.disabled = busy || !ready;
      genBtn.title = busy ? "上一张还在生成中" : (ready ? "" : reason);
      // 按钮文案随状态走（2026-11-04 实测修）：doGenerate 把它改成
      // 「提交中…」之后必须有人还原 —— 不然「提交中…」会一直挂在那儿，
      // 看着像卡住。
      const want = busy ? "生成中…" : "生成图片";
      if (genBtn.textContent !== want) genBtn.textContent = want;
    }
    if (stopBtn) {
      stopBtn.disabled = !busy;
    }

    const req = snap.request;
    if (metaEl) {
      if (busy && req) {
        const secs = snap.elapsed ? `，已用 ${Math.round(snap.elapsed)} 秒` : "";
        setText(metaEl, `正在生成（${req.steps} 步${secs}）…`);
      } else if (snap.error) {
        setText(metaEl, `失败：${snap.error}`);
      } else if (snap.last) {
        setText(metaEl, `上次生成：${snap.last.steps} 步，耗时 ${snap.last.elapsed} 秒（${fmtTime(snap.last.created_at)}）`);
      } else {
        setText(metaEl, "未开始");
      }
    }
    if (statusLine) {
      setText(statusLine, busy ? "生成中" : (ready ? "就绪" : (loading ? "加载中" : "未启用")));
    }
    if (hintEl) {
      if (busy) {
        // ⚠️ 说清楚**现在在做什么**（2026-11-04 用户反馈：「没提示在做什么」）
        const p = snap.progress || {};
        const lbl = p.stage_label || p.stage || "";
        // detail 与阶段名相同时不重复拼（「启动中：启动中」很丑）
        const what = p.detail && p.detail !== lbl
          ? (lbl ? `${lbl}：${p.detail}` : p.detail)
          : (lbl || "正在准备这次生成…");
        const eta = p.eta ? `，预计还需 ${fmtDur(p.eta)}` : "";
        setText(hintEl, `${what}${eta}`);
      } else if (snap.error) {
        setText(hintEl, snap.error);
      } else {
        setText(hintEl, "");
      }
    }

    const raw = $("data-image-editor");
    if (raw) {
      try {
        raw.textContent = JSON.stringify(snap, null, 2);
      } catch (e) {
        raw.textContent = String(snap);
      }
    }

    // 进度条：跑的时候实时跳；跑完了**留住最后一条**（让人看清
    // 「最后一步花了多久 / 一共用了多久」），下一次生成再重置。
    renderProgress(snap.progress || null, !!snap.busy);

    // DemoGuard：告诉宿主「本应用现在有没有在跑后台任务」。
    if (window.DemoGuard && typeof window.DemoGuard.report === "function") {
      window.DemoGuard.report(PAGE_ID, { session: snap });
    }
  }

  // ------------------------------------------------------------------ //
  // 生成结果
  // ------------------------------------------------------------------ //
  function renderResult(item) {
    const empty = $("ie-result-empty");
    const fig = $("ie-figure");
    const img = $("ie-image");
    const cap = $("ie-caption-text");
    const dl = $("ie-download");
    if (!item || !item.url) {
      if (fig) fig.classList.add("hidden");
      if (empty) empty.classList.remove("hidden");
      return;
    }
    if (img) {
      // 带 run_id 的 URL 是稳定的（同一张图重复取不会 404）
      img.src = item.url;
    }
    if (dl) {
      dl.href = item.url;
      dl.setAttribute("download", `qwenimage21-${item.run_id}.png`);
    }
    if (cap) {
      setText(cap, `${item.prompt}（${item.steps} 步，${item.elapsed} 秒）`);
    }
    if (fig) fig.classList.remove("hidden");
    if (empty) empty.classList.add("hidden");
  }

  function renderHistory(items) {
    IE.history = Array.isArray(items) ? items : [];
    const list = $("ie-history-list");
    const count = $("ie-history-count");
    if (count) setText(count, `${IE.history.length} 张`);
    if (!list) return;
    list.innerHTML = "";
    if (!IE.history.length) {
      const li = document.createElement("li");
      li.className = "vd-event-empty";
      li.textContent = "尚无生成记录，生成后结果显示在这里。";
      list.appendChild(li);
      return;
    }
    IE.history.forEach((item) => {
      const li = document.createElement("li");
      li.className = "vd-event";
      const body = document.createElement("div");
      body.className = "vd-event-body";
      const text = document.createElement("div");
      text.className = "vd-event-text";
      text.textContent = item.prompt;
      const meta = document.createElement("div");
      meta.className = "vd-event-meta";
      meta.textContent = `${fmtTime(item.created_at)} · ${item.steps} 步 · ${item.elapsed} 秒`;
      body.appendChild(text);
      body.appendChild(meta);
      const thumb = document.createElement("img");
      thumb.className = "ie-thumb";
      thumb.src = item.url;
      thumb.alt = item.prompt;
      thumb.loading = "lazy";
      thumb.addEventListener("click", () => renderResult(item));
      li.appendChild(thumb);
      li.appendChild(body);
      list.appendChild(li);
    });
  }

  async function refreshHistory() {
    try {
      const data = await getJSON(`${API}/history`);
      renderHistory(data.items);
    } catch (e) {
      /* 历史拿不到不影响出图 */
    }
  }

  // ------------------------------------------------------------------ //
  // 动作
  // ------------------------------------------------------------------ //
  async function doGenerate() {
    const promptEl = $("ie-prompt");
    const stepsEl = $("ie-steps");
    const fixedEl = $("ie-fixed-noise");
    const strengthEl = $("ie-strength");
    const prompt = promptEl ? promptEl.value.trim() : "";
    if (!prompt) {
      const hintEl = $("ie-hint");
      if (hintEl) setText(hintEl, "请先输入提示词。");
      return false;
    }
    // ⭐ 输入图是**必填**（没有它就退化成文生图了，那是隔壁那张卡的事）
    if (!IE.sourceB64) {
      const hintEl = $("ie-hint");
      if (hintEl) setText(hintEl, "请先上传一张要处理的图片。");
      const box = $("ie-source-drop");
      if (box) box.classList.add("is-error");
      return false;
    }
    const body = {
      prompt,
      steps: stepsEl ? parseInt(stepsEl.value, 10) : 8,
      fixed_noise: fixedEl ? !!fixedEl.checked : false,
      image: IE.sourceB64,
      strength: strengthEl ? parseFloat(strengthEl.value) : 0.75,
    };
    // ⚠️⚠️ **点击后立刻给反馈**（2026-11-04 实测修）。
    // 之前这里是 `await postJSON(...)` 然后直接返回 —— 界面**一动不动**，
    // 要等下一次 WS 帧（最快 1 s）或兜底轮询（最慢 5 s）才知道「点了」。
    // 在这几秒里按钮还是可点的样子（用户会连点几下），而按钮一旦被
    // disabled，浏览器的 click 会被**静默丢弃** ⇒ 连重试都点不动。
    // ⇒ 点击即刻：按钮禁用 + 变「提交中…」+ meta/hint 说清楚 + 进度条亮起来。
    const genBtn = $("ie-generate");
    const stopBtn = $("ie-stop");
    if (genBtn) {
      genBtn.disabled = true;
      genBtn.textContent = "提交中…";
    }
    if (stopBtn) stopBtn.disabled = true;
    setText($("ie-meta"), "正在提交…");
    setText($("ie-hint"), "正在把提示词送到 worker，请稍候。");
    renderProgress(null, true);
    try {
      await postJSON(`${API}/generate`, body);
      // 立刻取一次快照 —— 不等 WS（最快 1 s）或轮询（最慢 5 s）
      await pollSnapshot();
      return true;
    } catch (e) {
      const hintEl = $("ie-hint");
      if (hintEl) setText(hintEl, `无法启动：${e.message || e}`);
      await pollSnapshot();
      return false;
    }
  }

  async function doStop() {
    try {
      await postJSON(`${API}/stop`, {});
    } catch (e) {
      /* 停止失败不致命 */
    }
    await pollSnapshot();
  }

  async function pollSnapshot() {
    try {
      const snap = await getJSON(`${API}/`);
      renderSnapshot(snap);
    } catch (e) {
      /* 忽略 */
    }
  }

  // ------------------------------------------------------------------ //
  // WebSocket
  // ------------------------------------------------------------------ //
  function connectWS() {
    if (IE.ws) return;
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    let ws;
    try {
      ws = new WebSocket(`${proto}//${location.host}${API}/ws`);
    } catch (e) {
      scheduleReconnect();
      return;
    }
    IE.ws = ws;
    ws.onopen = () => {
      IE.wsBackoff = 1.0;
    };
    ws.onmessage = (ev) => {
      let msg = null;
      try {
        msg = JSON.parse(ev.data);
      } catch (e) {
        return;
      }
      if (!msg || msg.type !== "status") return;
      renderSnapshot(msg);
      const last = msg.last;
      if (last && (!IE.lastShownRun || IE.lastShownRun !== last.run_id)) {
        IE.lastShownRun = last.run_id;
        renderResult(last);
        refreshHistory();
      }
    };
    ws.onclose = () => {
      IE.ws = null;
      scheduleReconnect();
    };
    ws.onerror = () => {
      try { ws.close(); } catch (e) { /* ignore */ }
    };
  }

  function scheduleReconnect() {
    if (IE.wsHandle) return;
    const delay = IE.wsBackoff;
    IE.wsBackoff = Math.min(IE.wsBackoff * 1.6, 15);
    IE.wsHandle = setTimeout(() => {
      IE.wsHandle = null;
      connectWS();
    }, delay * 1000);
  }

  // ------------------------------------------------------------------ //
  // ⭐ 输入图：选择 / 拖拽 / 预览 / 清除
  // ------------------------------------------------------------------ //
  const MAX_SOURCE_BYTES = 12 * 1024 * 1024;

  function setSourceImage(b64, name, previewUrl) {
    IE.sourceB64 = b64 || "";
    IE.sourceName = name || "";
    const empty = $("ie-source-empty");
    const wrap = $("ie-source-preview-wrap");
    const img = $("ie-source-preview");
    const nameEl = $("ie-source-name");
    if (empty) empty.classList.toggle("hidden", !!b64);
    if (wrap) wrap.classList.toggle("hidden", !b64);
    if (img && previewUrl) img.src = previewUrl;
    if (nameEl) setText(nameEl, IE.sourceName);
    const drop = $("ie-source-drop");
    if (drop) drop.classList.remove("is-error");
    const hint = $("ie-hint");
    if (hint && b64) setText(hint, "已选择原图，写一句怎么改，点「生成图片」。");
  }

  function clearSourceImage() {
    const fileEl = $("ie-source-file");
    if (fileEl) fileEl.value = "";
    setSourceImage("", "", "");
  }

  function loadSourceFile(file) {
    if (!file) return;
    if (!/^image\//.test(file.type || "")) {
      setText($("ie-hint"), "那不是图片文件（要 PNG / JPEG / WebP）。");
      return;
    }
    if (file.size > MAX_SOURCE_BYTES) {
      setText($("ie-hint"),
        `图片太大（${(file.size / 1048576).toFixed(1)} MB），请用 12 MB 以内的。`);
      return;
    }
    const reader = new FileReader();
    reader.onload = () => {
      const dataUrl = String(reader.result || "");
      const comma = dataUrl.indexOf(",");
      const b64 = comma >= 0 ? dataUrl.slice(comma + 1) : dataUrl;
      setSourceImage(b64, file.name, dataUrl);
    };
    reader.onerror = () => setText($("ie-hint"), "读取图片失败。");
    reader.readAsDataURL(file);
  }

  function initSourceUI() {
    const drop = $("ie-source-drop");
    const fileEl = $("ie-source-file");
    if (fileEl) {
      fileEl.addEventListener("change", () => {
        const f = fileEl.files && fileEl.files[0];
        if (f) loadSourceFile(f);
      });
    }
    if (drop) {
      // 点整块都能选文件（隐藏的 input 仍然可点）
      drop.addEventListener("click", (ev) => {
        if (ev.target && ev.target.id === "ie-source-clear") return;
        if (fileEl) fileEl.click();
      });
      drop.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" || ev.key === " ") {
          ev.preventDefault();
          if (fileEl) fileEl.click();
        }
      });
      ["dragenter", "dragover"].forEach((t) =>
        drop.addEventListener(t, (ev) => {
          ev.preventDefault();
          drop.classList.add("is-drag");
        }));
      ["dragleave", "drop"].forEach((t) =>
        drop.addEventListener(t, (ev) => {
          ev.preventDefault();
          drop.classList.remove("is-drag");
        }));
      drop.addEventListener("drop", (ev) => {
        const dt = ev.dataTransfer;
        if (!dt || !dt.files || !dt.files.length) return;
        loadSourceFile(dt.files[0]);
      });
    }
    const clearBtn = $("ie-source-clear");
    if (clearBtn) {
      clearBtn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        clearSourceImage();
      });
    }
  }

  // ------------------------------------------------------------------ //
  // 启动
  // ------------------------------------------------------------------ //
  function init() {
    if (IE.started) return;
    IE.started = true;

    const genBtn = $("ie-generate");
    if (genBtn) genBtn.addEventListener("click", doGenerate);
    const stopBtn = $("ie-stop");
    if (stopBtn) stopBtn.addEventListener("click", doStop);

    initSourceUI();

    const promptEl = $("ie-prompt");
    if (promptEl) {
      promptEl.addEventListener("keydown", (ev) => {
        if ((ev.ctrlKey || ev.metaKey) && ev.key === "Enter") {
          ev.preventDefault();
          doGenerate();
        }
      });
    }

    pollSnapshot();
    refreshHistory();
    connectWS();

    // 兜底轮询：WS 断了也不至于完全瞎（WS 正常时这条是空转）。
    IE.pollHandle = setInterval(pollSnapshot, 5000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  // ------------------------------------------------------------------ //
  // 向导回填（wizard 框架按字段 id 去掉 "ie-" 前缀后 camelCase 传进来）
  // ------------------------------------------------------------------ //
  window.ImApp_image_editor = window.ImApp_image_editor || {};
  window.ImApp_image_editor.applyWizardSnapshot = async function (opts) {
    const result = { text_filled: false, started: false };
    opts = opts || {};
    if (opts.prompt) {
      const el = $("ie-prompt");
      if (el) {
        el.value = opts.prompt;
        result.text_filled = true;
      }
    }
    if (opts.steps) {
      const el = $("ie-steps");
      if (el) el.value = String(opts.steps);
    }
    if (opts.fixedNoise !== undefined) {
      const el = $("ie-fixed-noise");
      if (el) el.checked = !!opts.fixedNoise;
    }
    // ⭐ 「保留原图的程度」是这张卡**独有**的旋钮 —— 向导里选了却
    // 不回填，用户就会拿到默认值（0.75），向导等于白设。
    if (opts.strength !== undefined && opts.strength !== null && opts.strength !== "") {
      const el = $("ie-strength");
      if (el) el.value = String(opts.strength);
    }
    // ⭐ 原图：向导第 2 步的拖拽上传区（type: "image"）传进来的是
    // **data URL**（`data:image/png;base64,....`）⇒ 剥掉前缀拿裸 base64
    // 给后端，预览直接用 data URL。
    if (opts.original) {
      const dataUrl = String(opts.original);
      const cut = dataUrl.indexOf(",");
      const bare = cut >= 0 ? dataUrl.slice(cut + 1) : dataUrl;
      setSourceImage(bare, "原图（向导上传）", dataUrl);
      result.image_filled = true;
    }
    // 图生图的输入图是**必填**的：向导里没给图 ⇒ 只能落到卡片页自己传。
    // ⚠️ 这里**不能**谎报「已开始生成」—— 没有输入图就什么都没跑。
    const el = $("ie-prompt");
    if (!IE.sourceB64) {
      const hintEl = $("ie-hint");
      if (hintEl) {
        setText(hintEl, "向导已配置完成：请先上传一张要处理的图片，再点「生成图片」。");
      }
      return result;
    }
    if (el && el.value.trim()) {
      const started = await doGenerate();
      result.started = started;
      // ⚠️ 向导页**看不到**卡片的进度条（卡片页此刻是隐藏的）⇒
      // 必须自己说清楚「已经开始生成了，去哪儿看」（2026-11-04 用户
      // 反馈：「点了之后一直卡在那，也没提示在做什么」）。
      if (started) {
        const hintEl = $("ie-hint");
        if (hintEl) {
          setText(hintEl,
            "已开始生成，请到「图片处理」页看实时进度（出图期间请不要关闭页面）。");
        }
      }
    }
    return result;
  };
})();

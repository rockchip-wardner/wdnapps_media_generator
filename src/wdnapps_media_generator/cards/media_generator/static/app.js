(function () {
  "use strict";

  // 图片生成卡片前端。
  //
  // 状态有三个来源：
  //   1. GET  /api/media-generator/          —— 首帧 + 兜底轮询
  //   2. WS   /api/media-generator/ws        —— 状态变化推送（主）
  //   3. POST /api/media-generator/generate  —— 发起出图
  //
  // 出图是重操作（8 步 ≈ 35 s，20 步 ≈ 100 s，28 步 ≈ 125 s，首次还要等 worker 把模型
  // load 进来 ≈ 90 s）⇒ 界面必须全程给出「在跑 / 还要等多久」的反馈，
  // 而不是让用户对着一张白屏猜。

  const API = "/api/media-generator";
  const PAGE_ID = "media-generator";

  const MG = {
    ws: null,
    wsBackoff: 1.0,
    pollHandle: null,
    lastSnap: null,
    history: [],
    started: false,
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
    const box = $("mg-progress");
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
      setText($("mg-progress-stage"), "启动中");
      setText($("mg-progress-detail"), "正在把请求送到 worker …");
      setText($("mg-progress-pct"), "0%");
      const fill0 = $("mg-progress-fill");
      if (fill0) fill0.style.width = "0%";
      setText($("mg-progress-step"), "--");
      setText($("mg-progress-eta"), "预计还需 --");
      return;
    }
    box.classList.remove("hidden");
    setText($("mg-progress-stage"), prog.stage_label || prog.stage || "--");
    setText($("mg-progress-detail"), prog.detail || "--");
    const pct = Math.max(0, Math.min(100, Number(prog.percent) || 0));
    setText($("mg-progress-pct"), `${Math.round(pct)}%`);
    const fill = $("mg-progress-fill");
    if (fill) fill.style.width = `${pct}%`;

    const stepMs = Number(prog.step_ms) || 0;
    const avgMs = Number(prog.step_avg_ms) || 0;
    const stepTxt = [];
    if (stepMs > 0) stepTxt.push(`这一步 ${fmtDur(stepMs / 1000)}`);
    if (avgMs > 0 && prog.total > 1) stepTxt.push(`平均每步 ${fmtDur(avgMs / 1000)}`);
    if (prog.elapsed) stepTxt.push(`已用 ${fmtDur(prog.elapsed)}`);
    setText($("mg-progress-step"), stepTxt.join(" · ") || "--");

    const eta = Number(prog.eta);
    setText($("mg-progress-eta"),
            eta > 0 ? `预计还需 ${fmtDur(eta)}` : "即将完成");
  }

  // ------------------------------------------------------------------ //
  // 快照渲染
  // ------------------------------------------------------------------ //
  function renderSnapshot(snap) {
    if (!snap) return;
    MG.lastSnap = snap;

    const banner = $("mg-unavailable");
    const reasonEl = $("mg-unavailable-reason");
    const genBtn = $("mg-generate");
    const stopBtn = $("mg-stop");
    const metaEl = $("mg-meta");
    const statusLine = $("mg-status-line");
    const hintEl = $("mg-hint");

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
        reason = "图片生成模型未启用，请到「算力配置」页启用 qwenimage21 实例（需要两张 NPU 卡）。";
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

    const raw = $("data-media-generator");
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
    const empty = $("mg-result-empty");
    const fig = $("mg-figure");
    const img = $("mg-image");
    const cap = $("mg-caption-text");
    const dl = $("mg-download");
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
    MG.history = Array.isArray(items) ? items : [];
    const list = $("mg-history-list");
    const count = $("mg-history-count");
    if (count) setText(count, `${MG.history.length} 张`);
    if (!list) return;
    list.innerHTML = "";
    if (!MG.history.length) {
      const li = document.createElement("li");
      li.className = "vd-event-empty";
      li.textContent = "尚无生成记录，生成后结果显示在这里。";
      list.appendChild(li);
      return;
    }
    MG.history.forEach((item) => {
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
      thumb.className = "mg-thumb";
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
    const promptEl = $("mg-prompt");
    const stepsEl = $("mg-steps");
    const fixedEl = $("mg-fixed-noise");
    const prompt = promptEl ? promptEl.value.trim() : "";
    if (!prompt) {
      const hintEl = $("mg-hint");
      if (hintEl) setText(hintEl, "请先输入提示词。");
      return;
    }
    const body = {
      prompt,
      steps: stepsEl ? parseInt(stepsEl.value, 10) : 8,
      fixed_noise: fixedEl ? !!fixedEl.checked : false,
    };
    // ⚠️⚠️ **点击后立刻给反馈**（2026-11-04 实测修）。
    // 之前这里是 `await postJSON(...)` 然后直接返回 —— 界面**一动不动**，
    // 要等下一次 WS 帧（最快 1 s）或兜底轮询（最慢 5 s）才知道「点了」。
    // 在这几秒里按钮还是可点的样子（用户会连点几下），而按钮一旦被
    // disabled，浏览器的 click 会被**静默丢弃** ⇒ 连重试都点不动。
    // ⇒ 点击即刻：按钮禁用 + 变「提交中…」+ meta/hint 说清楚 + 进度条亮起来。
    const genBtn = $("mg-generate");
    const stopBtn = $("mg-stop");
    if (genBtn) {
      genBtn.disabled = true;
      genBtn.textContent = "提交中…";
    }
    if (stopBtn) stopBtn.disabled = true;
    setText($("mg-meta"), "正在提交…");
    setText($("mg-hint"), "正在把提示词送到 worker，请稍候。");
    renderProgress(null, true);
    try {
      await postJSON(`${API}/generate`, body);
      // 立刻取一次快照 —— 不等 WS（最快 1 s）或轮询（最慢 5 s）
      await pollSnapshot();
    } catch (e) {
      const hintEl = $("mg-hint");
      if (hintEl) setText(hintEl, `无法启动：${e.message || e}`);
      await pollSnapshot();
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
    if (MG.ws) return;
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    let ws;
    try {
      ws = new WebSocket(`${proto}//${location.host}${API}/ws`);
    } catch (e) {
      scheduleReconnect();
      return;
    }
    MG.ws = ws;
    ws.onopen = () => {
      MG.wsBackoff = 1.0;
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
      if (last && (!MG.lastShownRun || MG.lastShownRun !== last.run_id)) {
        MG.lastShownRun = last.run_id;
        renderResult(last);
        refreshHistory();
      }
    };
    ws.onclose = () => {
      MG.ws = null;
      scheduleReconnect();
    };
    ws.onerror = () => {
      try { ws.close(); } catch (e) { /* ignore */ }
    };
  }

  function scheduleReconnect() {
    if (MG.wsHandle) return;
    const delay = MG.wsBackoff;
    MG.wsBackoff = Math.min(MG.wsBackoff * 1.6, 15);
    MG.wsHandle = setTimeout(() => {
      MG.wsHandle = null;
      connectWS();
    }, delay * 1000);
  }

  // ------------------------------------------------------------------ //
  // 启动
  // ------------------------------------------------------------------ //
  function init() {
    if (MG.started) return;
    MG.started = true;

    const genBtn = $("mg-generate");
    if (genBtn) genBtn.addEventListener("click", doGenerate);
    const stopBtn = $("mg-stop");
    if (stopBtn) stopBtn.addEventListener("click", doStop);

    const promptEl = $("mg-prompt");
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
    MG.pollHandle = setInterval(pollSnapshot, 5000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  // ------------------------------------------------------------------ //
  // 向导回填（wizard 框架按字段 id 去掉 "mg-" 前缀后 camelCase 传进来）
  // ------------------------------------------------------------------ //
  window.ImApp_media_generator = window.ImApp_media_generator || {};
  window.ImApp_media_generator.applyWizardSnapshot = async function (opts) {
    const result = { text_filled: false, started: false };
    opts = opts || {};
    if (opts.prompt) {
      const el = $("mg-prompt");
      if (el) {
        el.value = opts.prompt;
        result.text_filled = true;
      }
    }
    if (opts.steps) {
      const el = $("mg-steps");
      if (el) el.value = String(opts.steps);
    }
    if (opts.fixedNoise !== undefined) {
      const el = $("mg-fixed-noise");
      if (el) el.checked = !!opts.fixedNoise;
    }
    // 提示词非空 → 直接开跑。
    // ⚠️ 不能 btn.click()：按钮的 disabled 由 capability_ready 决定，
    // 模型刚就绪时按钮还是灰的，而 disabled 按钮的 click() 会被浏览器
    // **静默丢弃**（tts 卡踩过这个坑）⇒ 改成直接调 doGenerate()。
    const el = $("mg-prompt");
    if (el && el.value.trim()) {
      await doGenerate();
      result.started = true;
      // ⚠️ 向导页**看不到**卡片的进度条（卡片页此刻是隐藏的）⇒
      // 必须自己说清楚「已经开始生成了，去哪儿看」（2026-11-04 用户
      // 反馈：「点了之后一直卡在那，也没提示在做什么」）。
      const hintEl = $("mg-hint");
      if (hintEl) {
        setText(hintEl,
          "已开始生成，请到「图片生成」页看实时进度（出图期间请不要关闭页面）。");
      }
    }
    return result;
  };
})();

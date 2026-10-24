(() => {
  "use strict";

  const MAX_FILES = 50;
  const MAX_BYTES = 30 * 1024 * 1024;
  const STORE_KEY = "cutout-studio-params";
  const SUPPORTED_TYPES = new Set([
    "image/jpeg",
    "image/png",
    "image/bmp",
    "image/x-ms-bmp",
    "image/webp",
    "image/gif",
  ]);
  const SUPPORTED_EXT = /\.(jpe?g|png|bmp|webp|gif)$/i;

  // 文件名里不允许出现的字符：路径分隔符、冒号、星号、问号、引号、尖括号、竖线
  const INVALID_CODES = new Set([92, 47, 58, 42, 63, 34, 60, 62, 124]);
  const WINDOWS_RESERVED = new Set([
    "CON",
    "PRN",
    "AUX",
    "NUL",
    ...Array.from({ length: 9 }, (_, i) => `COM${i + 1}`),
    ...Array.from({ length: 9 }, (_, i) => `LPT${i + 1}`),
  ]);

  const els = {
    statusPill: document.getElementById("statusPill"),
    statusText: document.getElementById("statusText"),
    settingsOpen: document.getElementById("settingsOpen"),
    settingsOpen2: document.getElementById("settingsOpen2"),
    dropZone: document.getElementById("dropZone"),
    fileInput: document.getElementById("fileInput"),
    queueList: document.getElementById("queueList"),
    queueEmpty: document.getElementById("queueEmpty"),
    queueCount: document.getElementById("queueCount"),
    startAll: document.getElementById("startAll"),
    clearDone: document.getElementById("clearDone"),
    stageTitle: document.getElementById("stageTitle"),
    stageSub: document.getElementById("stageSub"),
    viewSwitch: document.getElementById("viewSwitch"),
    lightbox: document.getElementById("lightbox"),
    stageEmpty: document.getElementById("stageEmpty"),
    emptyPick: document.getElementById("emptyPick"),
    viewer: document.getElementById("viewer"),
    viewerFrame: document.getElementById("viewerFrame"),
    sourceLayer: document.getElementById("sourceLayer"),
    sourceImg: document.getElementById("sourceImg"),
    resultImg: document.getElementById("resultImg"),
    cut: document.getElementById("cut"),
    tagSource: document.getElementById("tagSource"),
    tagResult: document.getElementById("tagResult"),
    scan: document.getElementById("scan"),
    stageError: document.getElementById("stageError"),
    stageErrorText: document.getElementById("stageErrorText"),
    retryOne: document.getElementById("retryOne"),
    stageMeta: document.getElementById("stageMeta"),
    metaSize: document.getElementById("metaSize"),
    metaFile: document.getElementById("metaFile"),
    metaElapsed: document.getElementById("metaElapsed"),
    metaRequest: document.getElementById("metaRequest"),
    stageNote: document.getElementById("stageNote"),
    downloadOne: document.getElementById("downloadOne"),
    cutOne: document.getElementById("cutOne"),
    nameInput: document.getElementById("nameInput"),
    downloadZip: document.getElementById("downloadZip"),
    zipHint: document.getElementById("zipHint"),
    credentialState: document.getElementById("credentialState"),
    toastRegion: document.getElementById("toastRegion"),
    settingsDialog: document.getElementById("settingsDialog"),
    settingsForm: document.getElementById("settingsForm"),
    settingsClose: document.getElementById("settingsClose"),
    settingsCancel: document.getElementById("settingsCancel"),
    settingsSave: document.getElementById("settingsSave"),
    keyInput: document.getElementById("keyInput"),
    settingsError: document.getElementById("settingsError"),
  };

  const state = {
    items: [],
    selectedId: null,
    configured: false,
    maskedKey: "",
    busy: false,
    batchTotal: 0,
    batchCurrent: 0,
    view: "source",
    comparePos: 50,
    params: { scene: "general", scope: "original" },
  };

  const VIEW_BUTTONS = Array.from(els.viewSwitch.querySelectorAll("button"));

  function newId() {
    if (window.crypto?.randomUUID) return crypto.randomUUID();
    return `id-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }

  function sanitizeName(value) {
    let cleaned = Array.from(value || "")
      .filter((ch) => {
        const code = ch.charCodeAt(0);
        return code >= 32 && code !== 127 && !INVALID_CODES.has(code);
      })
      .join("")
      .replace(/[\p{Cf}]/gu, "")
      .replace(/\.png$/i, "")
      .replace(/\s+/g, " ")
      .replace(/^[.\s]+|[.\s]+$/g, "");

    if (!cleaned) return "";
    if (WINDOWS_RESERVED.has(cleaned.split(".")[0].toUpperCase())) {
      cleaned = "_" + cleaned;
    }
    return cleaned.slice(0, 80);
  }

  function stemOf(filename) {
    const base = (filename || "").split(/[\\/]/).pop() || "";
    const stem = base.replace(/\.[^.]+$/, "");
    return sanitizeName(stem) || "cutout";
  }

  function formatBytes(bytes) {
    if (!Number.isFinite(bytes) || bytes <= 0) return "—";
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
  }

  function formatElapsed(value) {
    if (!value) return "—";
    const seconds = parseFloat(String(value).replace(/s$/i, ""));
    return Number.isFinite(seconds) ? `${seconds.toFixed(2)} s` : String(value);
  }

  function shortToken(token) {
    if (!token) return "—";
    const value = String(token);
    return value.length > 14 ? `${value.slice(0, 9)}…${value.slice(-4)}` : value;
  }

  function selectedItem() {
    return state.items.find((item) => item.localId === state.selectedId) || null;
  }

  function hasFiles(event) {
    const types = event.dataTransfer?.types;
    return Boolean(types && Array.from(types).includes("Files"));
  }

  function setImage(img, src) {
    if (src) {
      if (img.getAttribute("src") !== src) img.src = src;
    } else if (img.hasAttribute("src")) {
      img.removeAttribute("src");
    }
  }

  function toast(message, kind = "info") {
    const node = document.createElement("div");
    node.className = "toast";
    node.dataset.kind = kind;
    node.textContent = message;
    els.toastRegion.replaceChildren(node);
    window.setTimeout(() => {
      node.style.transition = "opacity .2s ease";
      node.style.opacity = "0";
      window.setTimeout(() => node.remove(), 220);
    }, kind === "error" ? 4600 : 3000);
  }

  function saveParams() {
    try {
      localStorage.setItem(STORE_KEY, JSON.stringify(state.params));
    } catch {
      /* 无痕模式下忽略 */
    }
  }

  function restoreParams() {
    try {
      const raw = localStorage.getItem(STORE_KEY);
      if (!raw) return;
      const saved = JSON.parse(raw);
      if (["general", "human", "product"].includes(saved.scene)) {
        state.params.scene = saved.scene;
      }
      if (["original", "crop"].includes(saved.scope)) {
        state.params.scope = saved.scope;
      }
    } catch {
      /* 使用默认值 */
    }
  }

  function syncParamInputs() {
    const scene = document.querySelector(`input[name="scene"][value="${state.params.scene}"]`);
    if (scene) scene.checked = true;
    const scope = document.querySelector(`input[name="scope"][value="${state.params.scope}"]`);
    if (scope) scope.checked = true;
  }

  /* ── 状态与凭证 ───────────────────────────────────── */

  function applyStatus(data) {
    state.configured = Boolean(data.configured);
    state.maskedKey = data.masked_key || "";

    els.statusPill.dataset.state = state.configured ? "ok" : "off";
    els.statusText.textContent = state.configured ? "接口已就绪" : "未配置凭证";

    els.credentialState.dataset.state = state.configured ? "ok" : "off";
    els.credentialState.textContent = state.configured
      ? `已连接 · MediaKit ${state.maskedKey || "Key 已保存"}`
      : "还没有 API Key，抠图前请先填写";

    updateControls();
    renderStage();
  }

  async function loadStatus() {
    try {
      const response = await fetch("/api/status", { cache: "no-store" });
      if (!response.ok) throw new Error("status");
      applyStatus(await response.json());
    } catch {
      els.statusPill.dataset.state = "off";
      els.statusText.textContent = "服务未响应";
      els.credentialState.dataset.state = "off";
      els.credentialState.textContent = "连不上本机服务";
      toast("连不上本机服务，请确认启动窗口还在", "error");
    }
  }

  function openSettings() {
    els.settingsError.hidden = true;
    els.settingsError.textContent = "";
    els.keyInput.value = "";
    els.keyInput.placeholder = state.maskedKey
      ? `已保存 ${state.maskedKey}，更新时请填写完整 Key`
      : "粘贴 AI MediaKit API Key";
    if (!els.settingsDialog.open) els.settingsDialog.showModal();
    window.setTimeout(() => els.keyInput.focus(), 30);
  }

  function closeSettings() {
    if (els.settingsDialog.open) els.settingsDialog.close();
  }

  async function saveSettings(event) {
    event.preventDefault();
    if (els.settingsSave.disabled) return;
    const apiKey = els.keyInput.value.trim();
    els.settingsError.hidden = true;

    if (!apiKey) {
      showSettingsError("请填写 MediaKit API Key。");
      return;
    }

    els.settingsSave.disabled = true;
    els.settingsSave.textContent = "正在保存…";
    try {
      const response = await fetch("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ api_key: apiKey }),
      });
      const data = await response.json().catch(() => null);
      if (!response.ok) {
        throw new Error(data?.error?.message || "保存失败，请稍后再试");
      }
      applyStatus(data);
      closeSettings();
      toast("凭证已保存，可以开始抠图了", "success");
    } catch (error) {
      showSettingsError(error.message || "保存失败，请稍后再试");
    } finally {
      els.settingsSave.disabled = false;
      els.settingsSave.textContent = "保存凭证";
    }
  }

  function showSettingsError(message) {
    els.settingsError.textContent = message;
    els.settingsError.hidden = false;
  }

  /* ── 素材 ─────────────────────────────────────────── */

  function addFiles(fileList) {
    const incoming = Array.from(fileList || []);
    if (!incoming.length) return;

    const accepted = [];
    let tooLarge = 0;
    let wrongType = 0;

    for (const file of incoming) {
      const mime = (file.type || "").toLowerCase();
      const isImage =
        SUPPORTED_TYPES.has(mime) || SUPPORTED_EXT.test(file.name || "");
      if (!isImage) {
        wrongType += 1;
        continue;
      }
      if (file.size > MAX_BYTES) {
        tooLarge += 1;
        continue;
      }
      accepted.push(file);
    }

    const room = MAX_FILES - state.items.length;
    if (accepted.length > room) {
      toast(`一次最多放 ${MAX_FILES} 张，多出的 ${accepted.length - room} 张没有加进来`, "error");
      accepted.length = Math.max(0, room);
    }

    if (wrongType) toast(`跳过了 ${wrongType} 个不是图片的文件`, "error");
    if (tooLarge) toast(`跳过了 ${tooLarge} 张超过 30 MB 的图片`, "error");
    if (!accepted.length) return;

    const created = accepted.map((file) => {
      const objectUrl = URL.createObjectURL(file);
      return {
        localId: newId(),
        file,
        objectUrl,
        name: stemOf(file.name),
        originalName: file.name || "未命名图片",
        status: "pending",
        error: "",
        result: null,
        dims: null,
      };
    });

    state.items.push(...created);

    for (const item of created) {
      const image = new Image();
      image.onload = () => {
        item.dims = { width: image.naturalWidth, height: image.naturalHeight };
        if (state.selectedId === item.localId) {
          renderStage();
        }
      };
      image.src = item.objectUrl;
    }

    if (!state.selectedId) {
      selectItem(created[0].localId);
    } else {
      renderAll();
    }

    if (created.length === 1) {
      toast(`已加入「${created[0].name}」`);
    } else {
      toast(`已加入 ${created.length} 张图片`);
    }
  }

  function removeItem(localId) {
    if (state.busy) return;
    const index = state.items.findIndex((item) => item.localId === localId);
    if (index < 0) return;
    const [removed] = state.items.splice(index, 1);
    if (removed.status === "processing") {
      state.items.splice(index, 0, removed);
      toast("这张正在处理，等它完成后再移除", "error");
      return;
    }
    URL.revokeObjectURL(removed.objectUrl);

    if (state.selectedId === localId) {
      const next = state.items[index] || state.items[index - 1] || null;
      state.selectedId = next ? next.localId : null;
      if (next) {
        state.view = next.result ? "compare" : "source";
      }
    }
    renderAll(true);
  }

  function clearDone() {
    if (state.busy) return;
    const keep = [];
    for (const item of state.items) {
      if (item.result) {
        URL.revokeObjectURL(item.objectUrl);
      } else {
        keep.push(item);
      }
    }
    if (keep.length === state.items.length) return;
    state.items = keep;
    if (!state.items.some((item) => item.localId === state.selectedId)) {
      const next = state.items[0] || null;
      state.selectedId = next ? next.localId : null;
      state.view = next?.result ? "compare" : "source";
    }
    renderAll(true);
    toast("已清空完成的素材（本机 outputs 文件不会被删除）");
  }

  function selectItem(localId) {
    const item = state.items.find((entry) => entry.localId === localId);
    if (!item) return;
    state.selectedId = localId;
    state.view = item.result ? (isAligned(item) ? "compare" : "result") : "source";
    state.comparePos = 50;
    renderAll(true);
    const node = els.queueList.querySelector(`[data-local-id="${localId}"]`);
    if (node) node.scrollIntoView({ block: "nearest" });
  }

  /* ── 渲染队列 ─────────────────────────────────────── */

  const STATUS_LABEL = {
    pending: "待抠图",
    processing: "正在抠图…",
  };

  const SCENE_LABEL = {
    general: "通用",
    human: "人像",
    product: "商品",
  };

  function renderQueue() {
    const fragment = document.createDocumentFragment();

    for (const item of state.items) {
      const li = document.createElement("li");
      li.className = "queue-item";
      li.dataset.status = item.status;
      li.dataset.localId = item.localId;
      if (item.localId === state.selectedId) li.classList.add("is-selected");

      const select = document.createElement("button");
      select.type = "button";
      select.className = "queue-select";
      select.setAttribute("aria-label", `查看 ${item.name}`);
      select.addEventListener("click", () => selectItem(item.localId));

      const thumb = document.createElement("img");
      thumb.className = "queue-thumb";
      thumb.src = item.objectUrl;
      thumb.alt = "";
      thumb.draggable = false;

      const info = document.createElement("span");
      info.className = "queue-info";

      const name = document.createElement("span");
      name.className = "queue-name";
      name.textContent = item.name;

      const status = document.createElement("span");
      status.className = "queue-state";
      if (item.status === "done" && item.result) {
        status.textContent = `已完成 · ${formatBytes(item.result.size)}`;
      } else if (item.status === "error") {
        status.textContent = "失败，点击查看";
      } else {
        status.textContent = STATUS_LABEL[item.status] || "待抠图";
      }

      info.append(name, status);
      select.append(thumb, info);

      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "queue-remove";
      remove.textContent = "×";
      remove.title = "移除这张素材";
      remove.setAttribute("aria-label", `移除 ${item.name}`);
      remove.disabled = state.busy || item.status === "processing";
      remove.addEventListener("click", (event) => {
        event.stopPropagation();
        removeItem(item.localId);
      });

      li.append(select, remove);
      fragment.appendChild(li);
    }

    els.queueList.replaceChildren(fragment);
    els.queueEmpty.hidden = state.items.length > 0;

    const done = state.items.filter((item) => item.result).length;
    els.queueCount.textContent = state.items.length
      ? `${state.items.length} 张${done ? ` · ${done} 完成` : ""}`
      : "0 张";
  }

  /* ── 渲染舞台 ─────────────────────────────────────── */

  function isAligned(item) {
    if (!item?.result) return false;
    if (!item.dims?.width || !item.dims?.height) return true;
    const sourceRatio = item.dims.width / item.dims.height;
    const resultRatio = item.result.width / item.result.height;
    return Math.abs(sourceRatio - resultRatio) / sourceRatio < 0.01;
  }

  function activeDims(item) {
    if (!item) return null;
    if (state.view !== "source" && item.result) {
      return { width: item.result.width, height: item.result.height };
    }
    return item.dims;
  }

  function layoutViewer() {
    const item = selectedItem();
    if (!item || els.viewer.hidden) return;
    const dims = activeDims(item);
    const box = els.lightbox.getBoundingClientRect();
    const maxWidth = Math.max(120, box.width - 56);
    const maxHeight = Math.max(120, box.height - 56);

    if (!dims?.width || !dims?.height) {
      els.viewerFrame.style.width = `${Math.round(maxWidth * 0.72)}px`;
      els.viewerFrame.style.height = `${Math.round(maxHeight * 0.72)}px`;
      return;
    }

    const ratio = dims.width / dims.height;
    let width = maxWidth;
    let height = width / ratio;
    if (height > maxHeight) {
      height = maxHeight;
      width = height * ratio;
    }

    els.viewerFrame.style.width = `${Math.max(120, Math.round(width))}px`;
    els.viewerFrame.style.height = `${Math.max(120, Math.round(height))}px`;
  }

  function setComparePos(percent) {
    const value = Math.min(100, Math.max(0, Number(percent) || 0));
    state.comparePos = value;
    els.viewerFrame.style.setProperty("--pos", `${value}%`);
    els.cut.setAttribute("aria-valuenow", String(Math.round(value)));
  }

  function setView(view) {
    const item = selectedItem();
    if (!item) return;

    let next = view;
    if (!item.result) next = "source";
    if (next === "compare" && !isAligned(item)) next = "result";
    state.view = next;

    els.viewerFrame.dataset.view = next;
    els.sourceLayer.style.display = next === "result" ? "none" : "";
    els.viewerFrame.querySelector(".result-layer").style.display = next === "source" ? "none" : "";
    els.cut.hidden = next !== "compare";
    els.tagSource.hidden = next === "result";
    els.tagResult.hidden = next === "source";

    for (const button of VIEW_BUTTONS) {
      const value = button.dataset.view;
      button.setAttribute("aria-pressed", String(value === next));
    }

    layoutViewer();
    if (next === "compare") setComparePos(state.comparePos);
  }

  function renderStage() {
    const item = selectedItem();

    if (!item) {
      els.stageEmpty.hidden = false;
      els.viewer.hidden = true;
      els.stageError.hidden = true;
      els.stageMeta.hidden = true;
      els.stageTitle.textContent = "预览";
      els.stageSub.textContent = "选中左侧素材后，在这里查看原图与抠图结果";
      els.stageNote.textContent = state.configured
        ? "抠图结果会保存在本机 outputs 文件夹。"
        : "还没有配置凭证：点右上角「凭证设置」填入 MediaKit API Key。";
      document.title = "抠图台 · AI MediaKit 智能抠图";
      updateControls();
      return;
    }

    els.stageEmpty.hidden = true;
    document.title = `${item.name}.png · 抠图台`;

    const dims = item.result
      ? { width: item.result.width, height: item.result.height }
      : item.dims;
    const dimsText = dims?.width ? `${dims.width} × ${dims.height}` : "读取尺寸中";
    const statusText =
      item.status === "processing"
        ? "正在抠图…"
        : item.status === "done"
          ? "已完成"
          : item.status === "error"
            ? "抠图失败"
            : "待抠图";

    els.stageTitle.textContent = item.name || "未命名";
    els.stageSub.textContent = `${item.originalName} · ${dimsText} · ${statusText}`;

    const failed = item.status === "error" && !item.result;
    els.stageError.hidden = !failed;
    if (failed) {
      els.stageErrorText.textContent = item.error || "没有拿到结果，请重新试一次。";
      state.view = "source";
    } else {
      els.stageErrorText.textContent = "";
    }

    setImage(els.sourceImg, item.objectUrl);
    setImage(els.resultImg, item.result?.url || "");
    els.viewer.hidden = false;
    els.scan.hidden = item.status !== "processing";

    const result = item.result;
    els.stageMeta.hidden = !result;
    if (result) {
      els.metaSize.textContent = `${result.width} × ${result.height}`;
      els.metaFile.textContent = `${item.name}.png · ${formatBytes(result.size)}`;
      els.metaElapsed.textContent = formatElapsed(result.elapsed);
      const traceId = result.request_id || result.task_id || "";
      els.metaRequest.textContent = shortToken(traceId);
      els.metaRequest.title = [result.request_id, result.task_id].filter(Boolean).join(" / ");
    }

    const notes = [];
    if (failed) {
      notes.push("原图还留在裁切台上。具体原因见上方红条；调整后点「重新抠图」。");
    } else if (result) {
      if (result.input?.resized) {
        notes.push(
          `为保证接口的尺寸限制，原图 ${result.input.width}×${result.input.height} 已缩到 ${result.input.sent_width}×${result.input.sent_height} 再送接口。`
        );
      } else if (result.input?.transcoded) {
        notes.push("为保证接口格式，上传前已重新编码。");
      }
      if (result.mode === "crop") {
        notes.push("已裁掉透明背景，可在右上角切换原图和抠图查看。");
      } else if (!isAligned(item)) {
        notes.push("结果与原图比例不同，可在右上角切换原图和抠图查看。");
      }
      notes.push(`${SCENE_LABEL[result.scene] || "通用"}场景 · 结果保存在本机 outputs 文件夹。`);
    } else if (item.status === "processing") {
      notes.push("正在上传并抠图，通常需要几秒。");
    } else if (!state.configured) {
      notes.push("还没有配置凭证，点右上角「凭证设置」填入 MediaKit API Key。");
    } else {
      const sceneText = SCENE_LABEL[state.params.scene] || "通用";
      const scopeText = state.params.scope === "crop" ? "只保留主体" : "保留原图尺寸";
      notes.push(`当前设置：${sceneText}场景 · ${scopeText}。`);
    }
    els.stageNote.textContent = notes.join(" ");

    setView(state.view);
    window.requestAnimationFrame(layoutViewer);
    updateControls();
  }

  /* ── 处理 ─────────────────────────────────────────── */

  async function processItem(item) {
    if (!state.configured) {
      openSettings();
      toast("先在凭证设置里填入 MediaKit API Key，再开始抠图", "error");
      return false;
    }
    if (item.status === "processing") return false;

    item.name = sanitizeName(item.name) || stemOf(item.originalName);
    item.status = "processing";
    item.error = "";
    item.result = null;
    if (state.selectedId === item.localId) state.view = "source";
    renderAll();

    const form = new FormData();
    form.append("file", item.file, item.file.name);
    form.append("scene", state.params.scene);
    form.append("output_mode", state.params.scope);
    form.append("name", item.name);

    try {
      const response = await fetch("/api/cutout", {
        method: "POST",
        headers: { "X-Cutout-Client": "1" },
        body: form,
      });
      const data = await response.json().catch(() => null);
      if (!response.ok) {
        const error = new Error(data?.error?.message || `抠图失败（${response.status}）`);
        error.code = data?.error?.code;
        throw error;
      }

      item.result = data;
      item.status = "done";
      if (!sanitizeName(item.name)) item.name = sanitizeName(data.name) || "cutout";
      if (state.selectedId === item.localId) {
        state.view = isAligned(item) ? "compare" : "result";
        state.comparePos = 50;
      }
      return true;
    } catch (error) {
      item.status = "error";
      item.error = error.message || "抠图失败，请稍后重试";
      if (error.code === "NOT_CONFIGURED") {
        state.configured = false;
        loadStatus();
        openSettings();
      }
      if (state.selectedId === item.localId) {
        toast(item.error, "error");
      }
      return false;
    } finally {
      renderAll();
      window.requestAnimationFrame(layoutViewer);
    }
  }

  async function processSelected() {
    const item = selectedItem();
    if (!item || state.busy) return;
    if (item.status === "processing") return;
    await processItem(item);
  }

  async function processAll() {
    if (state.busy) return;
    if (!state.configured) {
      openSettings();
      toast("先在凭证设置里填入 MediaKit API Key，再开始抠图", "error");
      return;
    }
    const queue = state.items.filter(
      (item) => item.status === "pending" || item.status === "error"
    );
    if (!queue.length) {
      toast("没有待处理的素材");
      return;
    }

    state.busy = true;
    state.batchTotal = queue.length;
    state.batchCurrent = 0;
    updateControls();

    let success = 0;
    for (const item of queue) {
      state.batchCurrent += 1;
      updateControls();
      const ok = await processItem(item);
      if (ok) success += 1;
      if (!state.configured) break;
    }

    state.busy = false;
    state.batchTotal = 0;
    renderAll();
    const failed = queue.length - success;
    if (failed === 0) {
      toast(`处理完成，${success} 张都抠好了`, "success");
    } else {
      toast(`处理完成：成功 ${success} 张，失败 ${failed} 张`, "error");
    }
  }

  /* ── 下载 ─────────────────────────────────────────── */

  function downloadItem(item) {
    if (!item?.result) return;
    const name = sanitizeName(item.name) || "cutout";
    const link = document.createElement("a");
    link.href = `/files/${item.result.id}?download=1&name=${encodeURIComponent(name)}`;
    link.download = `${name}.png`;
    document.body.appendChild(link);
    link.click();
    link.remove();
  }

  async function downloadZip() {
    const done = state.items.filter((item) => item.result);
    if (!done.length || state.busy) return;

    const label = els.downloadZip.textContent;
    els.downloadZip.disabled = true;
    els.downloadZip.textContent = "正在打包…";

    try {
      const response = await fetch("/api/zip", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Cutout-Client": "1" },
        body: JSON.stringify({
          items: done.map((item) => ({
            id: item.result.id,
            name: sanitizeName(item.name) || "cutout",
          })),
        }),
      });
      if (!response.ok) {
        const data = await response.json().catch(() => null);
        throw new Error(data?.error?.message || "打包失败，请稍后再试");
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const now = new Date();
      const pad = (value) => String(value).padStart(2, "0");
      const stamp = `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}-${pad(now.getHours())}${pad(now.getMinutes())}`;
      const link = document.createElement("a");
      link.href = url;
      link.download = `抠图结果-${stamp}.zip`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 5000);
      toast(`已打包 ${done.length} 张图片`, "success");
    } catch (error) {
      toast(error.message || "打包失败", "error");
    } finally {
      els.downloadZip.textContent = label;
      updateControls();
    }
  }

  /* ── 控件状态 ─────────────────────────────────────── */

  function updateControls() {
    const item = selectedItem();
    const done = state.items.filter((entry) => entry.result);
    const pending = state.items.filter(
      (entry) => entry.status === "pending" || entry.status === "error"
    );
    const processing = state.items.some((entry) => entry.status === "processing");

    els.startAll.disabled = state.busy || processing || pending.length === 0;
    els.startAll.textContent = state.busy
      ? `正在处理 ${Math.min(state.batchCurrent, state.batchTotal)}/${state.batchTotal}`
      : "全部抠图";

    if (item) {
      els.cutOne.disabled = state.busy || item.status === "processing";
      els.cutOne.textContent =
        item.status === "processing"
          ? "抠图中…"
          : item.status === "done"
            ? "重新抠图"
            : "开始抠图";
      els.downloadOne.disabled = !item.result;
      if (document.activeElement !== els.nameInput) {
        els.nameInput.value = item.name;
      }
      els.nameInput.disabled = false;
    } else {
      els.cutOne.disabled = true;
      els.cutOne.textContent = "开始抠图";
      els.downloadOne.disabled = true;
      els.nameInput.value = "";
      els.nameInput.disabled = true;
    }

    els.downloadZip.disabled = !done.length || state.busy;
    els.clearDone.disabled = !done.length || state.busy;
    els.zipHint.textContent = done.length
      ? `已完成 ${done.length} 张，可打包下载。`
      : "至少完成一张后可用。";

    const hasResult = Boolean(item?.result);
    const aligned = item ? isAligned(item) : false;
    for (const button of VIEW_BUTTONS) {
      const value = button.dataset.view;
      if (value === "source") button.disabled = !item;
      if (value === "result") button.disabled = !hasResult;
      if (value === "compare") button.disabled = !hasResult || !aligned;
    }
  }

  function renderAll(scrollSelection = false) {
    renderQueue();
    renderStage();
    updateControls();
    if (scrollSelection && state.selectedId) {
      const node = els.queueList.querySelector(`[data-local-id="${state.selectedId}"]`);
      if (node) node.scrollIntoView({ block: "nearest" });
    }
  }

  /* ── 对比滑块 ─────────────────────────────────────── */

  function pointerPercent(event) {
    const rect = els.viewerFrame.getBoundingClientRect();
    if (!rect.width) return 50;
    return ((event.clientX - rect.left) / rect.width) * 100;
  }

  function bindCompare() {
    let dragging = false;

    els.viewerFrame.addEventListener("pointerdown", (event) => {
      if (state.view !== "compare" || !selectedItem()?.result) return;
      if (event.button !== 0) return;
      dragging = true;
      els.viewerFrame.setPointerCapture(event.pointerId);
      setComparePos(pointerPercent(event));
      event.preventDefault();
    });

    els.viewerFrame.addEventListener("pointermove", (event) => {
      if (!dragging) return;
      setComparePos(pointerPercent(event));
    });

    const stop = (event) => {
      if (!dragging) return;
      dragging = false;
      try {
        els.viewerFrame.releasePointerCapture(event.pointerId);
      } catch {
        /* 已释放 */
      }
    };
    els.viewerFrame.addEventListener("pointerup", stop);
    els.viewerFrame.addEventListener("pointercancel", stop);

    els.cut.addEventListener("keydown", (event) => {
      if (state.view !== "compare") return;
      const step = event.shiftKey ? 10 : 2;
      if (event.key === "ArrowLeft") {
        setComparePos(state.comparePos - step);
        event.preventDefault();
      } else if (event.key === "ArrowRight") {
        setComparePos(state.comparePos + step);
        event.preventDefault();
      } else if (event.key === "Home") {
        setComparePos(0);
        event.preventDefault();
      } else if (event.key === "End") {
        setComparePos(100);
        event.preventDefault();
      }
    });
  }

  /* ── 事件绑定 ─────────────────────────────────────── */

  function bindEvents() {
    els.dropZone.addEventListener("click", () => els.fileInput.click());
    els.emptyPick.addEventListener("click", () => els.fileInput.click());
    els.fileInput.addEventListener("change", () => {
      addFiles(els.fileInput.files);
      els.fileInput.value = "";
    });

    els.startAll.addEventListener("click", processAll);
    els.cutOne.addEventListener("click", processSelected);
    els.retryOne.addEventListener("click", processSelected);
    els.downloadOne.addEventListener("click", () => {
      const item = selectedItem();
      if (item) downloadItem(item);
    });
    els.downloadZip.addEventListener("click", downloadZip);
    els.clearDone.addEventListener("click", clearDone);

    els.settingsOpen.addEventListener("click", openSettings);
    els.settingsOpen2.addEventListener("click", openSettings);
    els.statusPill.addEventListener("click", openSettings);
    els.settingsClose.addEventListener("click", closeSettings);
    els.settingsCancel.addEventListener("click", () => closeSettings());
    els.settingsForm.addEventListener("submit", saveSettings);
    els.settingsDialog.addEventListener("click", (event) => {
      if (event.target === els.settingsDialog) closeSettings();
    });

    els.nameInput.addEventListener("input", () => {
      const item = selectedItem();
      if (!item) return;
      item.name = els.nameInput.value;
      const nameNode = els.queueList.querySelector(
        `[data-local-id="${item.localId}"] .queue-name`
      );
      if (nameNode) nameNode.textContent = item.name || "未命名";
      els.stageTitle.textContent = item.name || "未命名";
      document.title = `${item.name || "未命名"}.png · 抠图台`;
      els.metaFile.textContent = item.result
        ? `${sanitizeName(item.name) || "cutout"}.png · ${formatBytes(item.result.size)}`
        : "—";
    });

    els.nameInput.addEventListener("blur", () => {
      const item = selectedItem();
      if (!item) return;
      const cleaned = sanitizeName(els.nameInput.value) || stemOf(item.originalName);
      item.name = cleaned;
      els.nameInput.value = cleaned;
      const nameNode = els.queueList.querySelector(
        `[data-local-id="${item.localId}"] .queue-name`
      );
      if (nameNode) nameNode.textContent = cleaned;
      els.stageTitle.textContent = cleaned;
      document.title = `${cleaned}.png · 抠图台`;
    });

    for (const button of VIEW_BUTTONS) {
      button.addEventListener("click", () => setView(button.dataset.view));
    }

    document.querySelectorAll('input[name="scene"]').forEach((input) => {
      input.addEventListener("change", () => {
        if (!input.checked) return;
        state.params.scene = input.value;
        saveParams();
        renderStageNoteOnly();
      });
    });

    document.querySelectorAll('input[name="scope"]').forEach((input) => {
      input.addEventListener("change", () => {
        if (!input.checked) return;
        state.params.scope = input.value;
        saveParams();
        renderStageNoteOnly();
      });
    });

    /* 拖拽：整个窗口都能接图 */
    let dragDepth = 0;
    let fileDrag = false;
    const clearDrag = () => {
      dragDepth = 0;
      fileDrag = false;
      els.dropZone.classList.remove("is-over");
    };
    window.addEventListener("dragenter", (event) => {
      if (!hasFiles(event)) return;
      fileDrag = true;
      dragDepth += 1;
      els.dropZone.classList.add("is-over");
    });
    window.addEventListener("dragover", (event) => {
      if (fileDrag || hasFiles(event)) event.preventDefault();
    });
    window.addEventListener("dragleave", (event) => {
      if (!fileDrag) return;
      dragDepth = Math.max(0, dragDepth - 1);
      if (dragDepth === 0) clearDrag();
    });
    window.addEventListener("drop", (event) => {
      if (!fileDrag && !hasFiles(event)) return;
      event.preventDefault();
      const files = event.dataTransfer?.files;
      clearDrag();
      if (files?.length) addFiles(files);
    });
    window.addEventListener("dragend", clearDrag);

    /* 直接粘贴图片 */
    document.addEventListener("paste", (event) => {
      const files = Array.from(event.clipboardData?.files || []);
      if (!files.length) return;
      event.preventDefault();
      addFiles(files);
    });

    window.addEventListener("keydown", (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
        if (els.settingsDialog.open) return;
        const item = selectedItem();
        if (item && !state.busy) {
          event.preventDefault();
          processItem(item);
        }
      }
    });

    window.addEventListener("beforeunload", () => {
      for (const item of state.items) URL.revokeObjectURL(item.objectUrl);
    });

    if (window.ResizeObserver) {
      new ResizeObserver(() => {
        layoutViewer();
      }).observe(els.lightbox);
    } else {
      window.addEventListener("resize", layoutViewer);
    }
  }

  function renderStageNoteOnly() {
    const item = selectedItem();
    if (!item || item.result || item.status !== "pending") return;
    const sceneText = SCENE_LABEL[state.params.scene] || "通用";
    const scopeText = state.params.scope === "crop" ? "只保留主体" : "保留原图尺寸";
    els.stageNote.textContent = `当前设置：${sceneText}场景 · ${scopeText}。`;
  }

  /* ── 启动 ─────────────────────────────────────────── */

  restoreParams();
  syncParamInputs();
  bindEvents();
  bindCompare();
  setComparePos(50);
  renderAll();
  loadStatus();
})();

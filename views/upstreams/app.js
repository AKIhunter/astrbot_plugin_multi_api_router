const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;

const list = document.querySelector("#upstream-list");
const template = document.querySelector("#upstream-template");
const globalFields = document.querySelector("#global-fields");
const toastElement = document.querySelector("#toast");
const saveBar = document.querySelector("#save-bar");
const helpOverlay = document.querySelector("#help-overlay");
let state = { upstreams: [], global: {}, strategy: {} };
let dirty = false;
let pendingDelete = new Set();
// 页面只展示「已启用 / 配置完整 / 本次新加」的上游，隐藏历史遗留的空槽位。
const addedSlots = new Set();
const expanded = new Set();
let draggingSlot = null;
let lastOperationSnapshot = null;
const MAX_UPSTREAMS = 20;
const API_TYPE_LABELS = {
  chat: "聊天",
  image_generation: "文/图生图",
  image_to_text: "图转文",
  embedding: "Embedding",
  rerank: "Rerank",
  stt: "STT",
  tts: "TTS",
  asr: "ASR",
  multimodal: "多模态",
};
const CHAT_COMPATIBLE_TYPES = new Set(["chat", "image_to_text", "multimodal"]);

// 页面只保留需要经常改的策略项；并发和输出预算等仍在 AstrBot 插件配置里。
const GLOBAL_FIELDS = [
  { key: "enabled", label: "启用路由 Provider", type: "checkbox" },
  { key: "provider_id", label: "整体模式 Provider ID", type: "text" },
  {
    key: "route_timeout_seconds",
    label: "路由超时限制（秒）",
    type: "number",
    min: 5,
    max: 600,
  },
  {
    key: "request_timeout_seconds",
    label: "单次接口超时（秒）",
    type: "number",
    min: 5,
    max: 600,
    hint: "每次调用同一上游的超时限制。",
  },
  {
    key: "max_attempts",
    label: "最多尝试的上游数量",
    type: "number",
    min: 1,
    max: 5,
    hint: "自动路由最多尝试几个上游。",
  },
  {
    key: "upstream_retry_count",
    label: "每个上游异常重试次数",
    type: "number",
    min: 0,
    max: 5,
    hint: "不含首次调用；0 表示不重试。",
  },
  {
    key: "retry_interval_seconds",
    label: "异常重试间隔（秒）",
    type: "number",
    min: 0,
    max: 3600,
    step: "any",
    hint: "支持小数；只暂停本轮重试，等待计入路由总时限。",
  },
  {
    key: "max_input_chars",
    label: "单次请求最大输入字符数",
    type: "number",
    min: 1000,
    max: 2000000,
  },
  { key: "fallback_to_main_model", label: "真实调用失败后回退主模型", type: "checkbox" },
  { key: "fallback_provider_id", label: "回退 Provider ID（留空用默认主模型）", type: "text" },
  { key: "skip_unhealthy_upstreams", label: "自动跳过已确认的配置错误", type: "checkbox" },
];

function escapeText(value) {
  return String(value ?? "");
}

function showToast(message, error = false) {
  toastElement.textContent = message;
  toastElement.className = error ? "toast error" : "toast";
  toastElement.hidden = false;
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => {
    toastElement.hidden = true;
  }, 3200);
}

function formatTime(timestamp) {
  if (!timestamp) return "";
  return new Date(timestamp * 1000).toLocaleString("zh-CN", { hour12: false });
}

function healthClass(health) {
  if (!health) return "unknown";
  return health.ok ? "healthy" : "failed";
}

function visibleUpstreams() {
  return state.upstreams
    .filter((item) => item.route_enabled || item.configured || addedSlots.has(item.slot))
    .sort((a, b) => (a.priority ?? 0) - (b.priority ?? 0) || a.slot - b.slot);
}

function upstreamLabel(slot) {
  // 统一用固定槽位号：页面行号会随排序/删除变化，槽位号才是其他插件引用的值。
  return `上游 #${slot}`;
}

function nextPriority() {
  const visible = visibleUpstreams();
  if (visible.length === 0) return 10;
  return Math.max(...visible.map((item) => Number(item.priority) || 0)) + 10;
}

function cleanupDropStyles() {
  list.querySelectorAll(".upstream").forEach((item) => {
    item.classList.remove("dragging", "drop-before", "drop-after");
  });
}

function applyOrderFromSlots(orderedSlots) {
  const priorityBySlot = new Map(
    orderedSlots.map((slot, index) => [Number(slot), (index + 1) * 10]),
  );
  // 拖拽顺序是新的优先级来源；保存时后端仍读取 priority 字段。
  state.upstreams.forEach((item) => {
    if (priorityBySlot.has(Number(item.slot))) {
      item.priority = priorityBySlot.get(Number(item.slot));
    }
  });
}

function cloneValue(value) {
  return JSON.parse(JSON.stringify(value));
}

function syncStateFromForm() {
  const global = { ...state.global };
  globalFields.querySelectorAll("[data-global-key]").forEach((input) => {
    global[input.dataset.globalKey] =
      input.type === "checkbox" ? input.checked : input.value;
  });
  state.global = global;

  const articles = Array.from(list.querySelectorAll(".upstream"));
  articles.forEach((article, index) => {
    const slot = Number(article.dataset.slot);
    const item = state.upstreams.find((upstream) => Number(upstream.slot) === slot);
    if (!item) return;
    item.route_enabled = article.querySelector(".upstream-enabled").checked;
    item.enabled = item.route_enabled;
    item.api_type = article.querySelector(".api-type").value;
    item.name = article.querySelector(".name").value.trim();
    item.api_base = article.querySelector(".api-base").value.trim();
    const keyInput = article.querySelector(".api-key");
    const keyValue = keyInput.value.trim();
    // 掩码或「显示」出来的明文都视为未编辑：保留原 api_key，避免回写覆盖真 Key。
    const keyUntouched = keyValue.includes("****")
      || (item._keyRevealed && keyValue === (item._revealedApiKey || ""));
    if (!keyUntouched) {
      item.api_key = keyValue;
      item._keyRevealed = false;
      item._revealedApiKey = "";
    }
    item.model = article.querySelector(".model").value.trim();
    item.priority = (index + 1) * 10;
    item.supports_tools = article.querySelector(".tools").checked;
    item.enable_thinking = article.querySelector(".thinking").checked;
    item.configured = Boolean(item.api_base && item.api_key && item.model);
  });
}

function captureOperationSnapshot() {
  syncStateFromForm();
  lastOperationSnapshot = {
    state: cloneValue(state),
    pendingDelete: Array.from(pendingDelete),
    addedSlots: Array.from(addedSlots),
    expanded: Array.from(expanded),
    dirty,
  };
}

function undoLastOperation() {
  if (!lastOperationSnapshot) {
    saveBar.hidden = true;
    return;
  }
  const snapshot = lastOperationSnapshot;
  state = cloneValue(snapshot.state);
  pendingDelete = new Set(snapshot.pendingDelete);
  addedSlots.clear();
  snapshot.addedSlots.forEach((slot) => addedSlots.add(slot));
  expanded.clear();
  snapshot.expanded.forEach((slot) => expanded.add(slot));
  dirty = snapshot.dirty;
  document.querySelector("#save-button").classList.toggle("dirty", dirty);
  renderGlobalFields();
  render();
  saveBar.hidden = true;
  lastOperationSnapshot = null;
  showToast("已撤销上一步操作");
}

function markDirty() {
  dirty = true;
  document.querySelector("#save-button").classList.add("dirty");
  saveBar.hidden = false;
}

function clearDirty() {
  dirty = false;
  document.querySelector("#save-button").classList.remove("dirty");
  saveBar.hidden = true;
}

function renderGlobalFields() {
  globalFields.replaceChildren();
  for (const spec of GLOBAL_FIELDS) {
    if (spec.type === "checkbox") {
      const label = document.createElement("label");
      label.className = "inline-check";
      const input = document.createElement("input");
      input.type = "checkbox";
      input.dataset.globalKey = spec.key;
      input.checked = Boolean(state.global[spec.key]);
      input.addEventListener("change", markDirty);
      label.append(input, document.createTextNode(" " + spec.label));
      globalFields.appendChild(label);
      continue;
    }
    const label = document.createElement("label");
    label.className = "field";
    const span = document.createElement("span");
    span.textContent = spec.label;
    const input = document.createElement("input");
    input.type = spec.type;
    input.dataset.globalKey = spec.key;
    input.id = `global-${spec.key}`;
    span.id = `${input.id}-label`;
    input.setAttribute("aria-labelledby", span.id);
    input.value = state.global[spec.key] ?? "";
    if (spec.min !== undefined) { input.min = spec.min; input.max = spec.max; }
    if (spec.type === "number") {
      input.step = spec.step ?? 1;
      input.required = true;
    }
    input.addEventListener("input", markDirty);
    label.append(span, input);
    if (spec.hint) {
      const hint = document.createElement("small");
      hint.className = "field-hint";
      hint.id = `${input.id}-hint`;
      hint.textContent = spec.hint;
      input.setAttribute("aria-describedby", hint.id);
      label.append(hint);
    }
    globalFields.appendChild(label);
  }
}

function updateSummary() {
  const upstreams = state.upstreams;
  document.querySelector("#enabled-count").textContent = upstreams.filter(
    (item) =>
      item.route_enabled &&
      CHAT_COMPATIBLE_TYPES.has(item.api_type || "chat"),
  ).length;
  document.querySelector("#configured-count").textContent = upstreams.filter(
    (item) => item.configured,
  ).length;
  document.querySelector("#healthy-count").textContent = upstreams.filter(
    (item) => item.health?.ok,
  ).length;
  const strategy = state.strategy || {};
  document.querySelector("#strategy-line").textContent =
    strategy.summary ||
    `${state.provider_id || "multi_api_router"} · ${state.runtime_upstream_count ?? 0} 个运行中上游`;
  document.querySelector("#subtitle").textContent =
    `整体模式：${strategy.overall_provider_id || state.provider_id || "multi_api_router"}` +
    " · 单个上游：按上游名称选择" +
    ` · 运行中 ${state.runtime_upstream_count ?? 0} 个`;
}

function render() {
  list.replaceChildren();
  const ordered = visibleUpstreams();
  for (const upstream of ordered) {
    const fragment = template.content.cloneNode(true);
    const article = fragment.querySelector(".upstream");
    const health = upstream.health;
    const healthState = healthClass(health);

    article.dataset.slot = upstream.slot;
    article.classList.add(healthState);

    // 折叠头部摘要
    const toggle = fragment.querySelector(".upstream-toggle");
    // 显示固定槽位号：其他插件引用此上游时用的就是它，拖拽/删除不会改变。
    const slotLabel = fragment.querySelector(".slot");
    slotLabel.textContent = `#${upstream.slot}`;
    slotLabel.title =
      `槽位号 ${upstream.slot}：其他插件引用时使用 multi_api_router:upstream_${upstream.slot}`;
    fragment.querySelector(".upstream-name").textContent =
      escapeText(upstream.name) || "未命名";
    const toolTag = fragment.querySelector(".tool-tag");
    if (upstream.supports_tools) {
      toolTag.classList.add("on");
    } else {
      toolTag.classList.remove("on");
    }
    toolTag.textContent = upstream.supports_tools ? "工具✓" : "无工具";
    const thinkingTag = fragment.querySelector(".thinking-tag");
    if (upstream.enable_thinking) {
      thinkingTag.classList.add("on");
      thinkingTag.textContent = "思考";
    } else {
      thinkingTag.classList.remove("on");
      thinkingTag.textContent = "直出";
    }
    const typeTag = fragment.querySelector(".type-tag");

    const badge = fragment.querySelector(".state-badge");
    const updateEnabledBadge = () => {
      badge.className = "badge state-badge";
      const chatCompatible = CHAT_COMPATIBLE_TYPES.has(
        upstream.api_type || "chat",
      );
      if (!chatCompatible && upstream.configured) {
        badge.textContent = "协议分类";
        badge.classList.add("muted");
        return;
      }
      if (!upstream.route_enabled) {
        badge.textContent = upstream.configured ? "仅直连" : "未加入路由";
        badge.classList.add("muted");
      } else if (!upstream.configured) {
        badge.textContent = "配置不完整";
        badge.classList.add("muted");
      } else {
        badge.textContent = "路由中";
        badge.classList.add("enabled");
      }
    };
    updateEnabledBadge();

    const headHealth = fragment.querySelector(".head-health");
    if (health) {
      headHealth.textContent = health.ok ? "正常" : health.status;
    } else {
      headHealth.textContent = "未检测";
    }

    // 删除按钮常显（改动需点「保存全部」才生效）。
    const head = fragment.querySelector(".upstream-head");
    const deleteButton = document.createElement("button");
    deleteButton.type = "button";
    deleteButton.className = "button delete-button-inline";
    deleteButton.textContent = "删除";
    deleteButton.addEventListener("click", () => deleteUpstream(upstream.slot));
    head.appendChild(deleteButton);

    // 详情区字段
    const enabledInput = fragment.querySelector(".upstream-enabled");
    const enabledText = enabledInput.closest("label").querySelector("span");
    enabledInput.checked = Boolean(upstream.route_enabled);
    enabledInput.addEventListener("change", () => {
      upstream.route_enabled = enabledInput.checked;
      upstream.enabled = upstream.route_enabled;
      updateEnabledBadge();
      updateSummary();
      markDirty();
    });
    fragment.querySelector(".name").value = escapeText(upstream.name);
    fragment.querySelector(".api-base").value = escapeText(upstream.api_base);
    const apiKeyInput = fragment.querySelector(".api-key");
    const revealKeyButton = fragment.querySelector(".reveal-key-button");
    // 默认只展示掩码（如 sk-a1b2****cdef）；点击「显示」才临时取明文。
    // 明文只存在当前页面内存，不写 localStorage，也不进页面源码。
    if (upstream._keyRevealed && upstream._revealedApiKey) {
      apiKeyInput.value = escapeText(upstream._revealedApiKey);
      apiKeyInput.type = "text";
      revealKeyButton.textContent = "隐藏";
    } else {
      apiKeyInput.value = escapeText(upstream.api_key || "");
      // 掩码用 text 才看得见前后缀；兼容开关返回明文时沿用 password 隐藏。
      apiKeyInput.type = `${upstream.api_key || ""}`.includes("****") ? "text" : "password";
      revealKeyButton.textContent = "显示";
    }
    // 只有确实存在密钥且当前是掩码形态时才提供「显示」。
    revealKeyButton.hidden = !(`${upstream.api_key || ""}`.includes("****"));
    revealKeyButton.addEventListener("click", async () => {
      if (upstream._keyRevealed) {
        upstream._keyRevealed = false;
        apiKeyInput.value = escapeText(upstream.api_key || "");
        apiKeyInput.type = `${upstream.api_key || ""}`.includes("****") ? "text" : "password";
        revealKeyButton.textContent = "显示";
        return;
      }
      revealKeyButton.disabled = true;
      try {
        const response = await bridge.apiPost("upstreams/config/reveal", {
          slot: upstream.slot,
        });
        upstream._revealedApiKey = response.api_key || "";
        upstream._keyRevealed = true;
        apiKeyInput.value = escapeText(upstream._revealedApiKey);
        apiKeyInput.type = "text";
        revealKeyButton.textContent = "隐藏";
        showToast(`${upstreamLabel(upstream.slot)} 已显示明文，本次查看已写入审计日志`);
      } catch (error) {
        showToast(error.message || "无法查看 API Key", true);
      } finally {
        revealKeyButton.disabled = false;
      }
    });
    const modelInput = fragment.querySelector(".model");
    const modelOptions = fragment.querySelector(".model-options");
    const modelListHint = fragment.querySelector(".model-list-hint");
    const fetchModelsButton = fragment.querySelector(".fetch-models-button");
    const modelListId = `model-options-${upstream.slot}`;
    modelOptions.id = modelListId;
    modelInput.setAttribute("list", modelListId);
    modelInput.value = escapeText(upstream.model);
    fragment.querySelector(".tools").checked = Boolean(upstream.supports_tools);
    fragment.querySelector(".thinking").checked = Boolean(upstream.enable_thinking);
    const apiTypeSelect = fragment.querySelector(".api-type");
    apiTypeSelect.value = upstream.api_type || "chat";
    const pinnedHint = fragment.querySelector(".pinned-hint");
    const applyTypeUi = () => {
      const apiType = apiTypeSelect.value;
      const chatCompatible = CHAT_COMPATIBLE_TYPES.has(apiType);
      typeTag.textContent = API_TYPE_LABELS[apiType] || apiType;
      enabledInput.disabled = !chatCompatible;
      if (!chatCompatible) enabledInput.checked = false;
      enabledText.textContent = chatCompatible
        ? "启用路由"
        : "此类型暂不接入自动路由";
      toolTag.hidden = !chatCompatible;
      thinkingTag.hidden = !chatCompatible;
      article.querySelectorAll(".chat-option").forEach((control) => {
        control.hidden = !chatCompatible;
      });
      pinnedHint.textContent = chatCompatible && upstream.provider_label
        ? `可直接指定「${upstream.provider_label}」；关闭“启用路由”不影响直连`
        : `按 ${API_TYPE_LABELS[apiType] || apiType} 协议检测，不会进入聊天 Provider`;
    };
    apiTypeSelect.addEventListener("change", () => {
      upstream.api_type = apiTypeSelect.value;
      upstream.chat_compatible = CHAT_COMPATIBLE_TYPES.has(upstream.api_type);
      enabledInput.checked = upstream.chat_compatible;
      upstream.route_enabled = enabledInput.checked;
      upstream.enabled = upstream.route_enabled;
      applyTypeUi();
      updateEnabledBadge();
      updateSummary();
      markDirty();
    });
    applyTypeUi();

    fetchModelsButton.addEventListener("click", async () => {
      const apiBase = article.querySelector(".api-base").value.trim();
      const apiKey = article.querySelector(".api-key").value.trim();
      if (!apiBase) {
        showToast(`${upstreamLabel(upstream.slot)} 请先填写 API Base`, true);
        return;
      }
      // 未编辑（仍是掩码、加载值或「显示」出来的原文）时不提交 key，
      // 改传 slot 让后端读取已保存密钥。
      const keyUnchanged = apiKey.includes("****")
        || apiKey === (upstream.api_key || "")
        || (upstream._keyRevealed && apiKey === (upstream._revealedApiKey || ""));
      const requestBody = { api_base: apiBase };
      if (keyUnchanged) requestBody.slot = upstream.slot;
      else requestBody.api_key = apiKey;
      fetchModelsButton.disabled = true;
      fetchModelsButton.textContent = "获取中…";
      modelListHint.textContent = "正在读取模型列表";
      try {
        const response = await bridge.apiPost("upstreams/models", requestBody);
        modelOptions.replaceChildren(
          ...response.models.map((model) => {
            const option = document.createElement("option");
            option.value = model;
            return option;
          }),
        );
        const suffix = response.truncated ? "，仅显示前 500 个" : "";
        modelListHint.textContent = response.count
          ? `已获取 ${response.count} 个模型${suffix}；点击输入框选择`
          : "接口返回成功，但没有可选择的模型";
        showToast(
          response.count
            ? `${upstreamLabel(upstream.slot)} 已获取 ${response.count} 个模型`
            : `${upstreamLabel(upstream.slot)} 未返回模型`,
          response.count === 0,
        );
        modelInput.focus();
      } catch (error) {
        modelOptions.replaceChildren();
        modelListHint.textContent = "";
        showToast(`${upstreamLabel(upstream.slot)}：${error.message || "获取模型列表失败"}`, true);
      } finally {
        fetchModelsButton.disabled = false;
        fetchModelsButton.textContent = "获取模型";
      }
    });

    const status = fragment.querySelector(".health-status");
    const detail = fragment.querySelector(".health-detail");
    if (health) {
      status.textContent = health.status;
      detail.textContent = [
        `${health.latency_ms}ms`,
        health.http_status ? `HTTP ${health.http_status}` : "",
        health.protocol || "",
        formatTime(health.checked_at),
      ]
        .filter(Boolean)
        .join(" · ");
    }

    // 只保留头部一个检测按钮，避免重复操作入口。
    const inlineButton = fragment.querySelector(".check-button-inline");
    inlineButton.disabled = upstream.checking;
    inlineButton.textContent = upstream.checking ? "检测中" : "检测";
    inlineButton.addEventListener("click", () => {
      checkUpstream(upstream.slot, inlineButton);
    });

    const dragHandle = fragment.querySelector(".drag-handle");
    dragHandle.addEventListener("dragstart", (event) => {
      captureOperationSnapshot();
      draggingSlot = Number(upstream.slot);
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", String(upstream.slot));
      event.dataTransfer.setDragImage(article, 28, Math.min(article.offsetHeight / 2, 36));
      article.classList.add("dragging");
    });
    dragHandle.addEventListener("dragend", () => {
      draggingSlot = null;
      cleanupDropStyles();
    });
    article.addEventListener("dragover", (event) => {
      if (!draggingSlot || Number(upstream.slot) === draggingSlot) return;
      event.preventDefault();
      const after = event.offsetY > article.offsetHeight / 2;
      cleanupDropStyles();
      article.classList.add(after ? "drop-after" : "drop-before");
    });
    article.addEventListener("drop", (event) => {
      event.preventDefault();
      const sourceSlot = draggingSlot || Number(event.dataTransfer.getData("text/plain"));
      const targetSlot = Number(upstream.slot);
      if (!sourceSlot || sourceSlot === targetSlot) return;
      const after = event.offsetY > article.offsetHeight / 2;
      const slots = ordered.map((item) => Number(item.slot)).filter((slot) => slot !== sourceSlot);
      const targetIndex = slots.indexOf(targetSlot);
      slots.splice(targetIndex + (after ? 1 : 0), 0, sourceSlot);
      applyOrderFromSlots(slots);
      cleanupDropStyles();
      draggingSlot = null;
      render();
      markDirty();
    });

    // 展开/收起
    const body = fragment.querySelector(".upstream-body");
    const applyExpand = () => {
      const open = expanded.has(String(upstream.slot));
      body.hidden = !open;
      toggle.setAttribute("aria-expanded", String(open));
      toggle.classList.toggle("open", open);
    };
    toggle.addEventListener("click", () => {
      const key = String(upstream.slot);
      if (expanded.has(key)) expanded.delete(key);
      else expanded.add(key);
      applyExpand();
    });
    applyExpand();

    // 编辑输入即标记未保存（详情区里的输入，含折叠态）
    article.querySelectorAll("input, select").forEach((input) => {
      if (input === enabledInput || input === apiTypeSelect) return;
      input.addEventListener(input.type === "checkbox" ? "change" : "input", markDirty);
    });

    list.appendChild(fragment);
  }
  updateSummary();
}

async function load() {
  try {
    state = await bridge.apiGet("upstreams/config");
    pendingDelete.clear();
    addedSlots.clear();
    lastOperationSnapshot = null;
    renderGlobalFields();
    render();
    clearDirty();
  } catch (error) {
    showToast(error.message || "读取上游配置失败", true);
  }
}

async function checkUpstream(slot, ...buttons) {
  buttons.forEach((b) => { b.disabled = true; b.textContent = "检测中"; });
  try {
    const response = await bridge.apiPost("upstreams/check", { slot });
    const index = state.upstreams.findIndex((item) => item.slot === slot);
    // 只回写 health，保留 item 里的 api_key/enabled 等编辑态字段。
    // response.upstream 来自 public_data，不含 api_key，整体覆盖会丢密钥并冲掉勾选。
    if (index >= 0) {
      state.upstreams[index].health = response.result;
    }
    render();
    showToast(
      response.result.ok
        ? `${upstreamLabel(slot)} 检测正常`
        : `${upstreamLabel(slot)}：${response.result.status}`,
      !response.result.ok,
    );
  } catch (error) {
    showToast(`${upstreamLabel(slot)}：${error.message || "检测失败"}`, true);
    await load();
  }
}

function addUpstream() {
  // 已删除的槽位是「退役」编号，永不复用：避免别处保存的旧引用静默指向新上游。
  const retired = new Set((state.retired_slots || []).map(Number));
  // 优先复用配置里已存在但页面隐藏的空槽位（历史备份槽位），
  // 避免直接顶到 20 个上限而无法新增。
  const reusable = state.upstreams.find(
    (item) => !item.route_enabled && !item.configured
      && !addedSlots.has(item.slot) && !retired.has(Number(item.slot)),
  );
  if (reusable) {
    captureOperationSnapshot();
    reusable.route_enabled = true;
    reusable.enabled = true;
    reusable.api_type = "chat";
    reusable.supports_tools = true;
    reusable.enable_thinking = false;
    reusable.priority = nextPriority();
    addedSlots.add(reusable.slot);
    expanded.add(String(reusable.slot));
    render();
    markDirty();
    return;
  }
  const used = new Set(state.upstreams.map((item) => item.slot));
  let nextSlot = 0;
  for (let slot = 1; slot <= MAX_UPSTREAMS; slot += 1) {
    if (!used.has(slot) && !retired.has(slot)) {
      nextSlot = slot;
      break;
    }
  }
  if (!nextSlot) {
    showToast(
      `没有可用槽位：${MAX_UPSTREAMS} 个编号均已占用或已退役（删除过的编号不会复用）`,
      true,
    );
    return;
  }
  captureOperationSnapshot();
  state.upstreams.push({
    slot: nextSlot,
    route_enabled: true,
    enabled: true,
    api_type: "chat",
    configured: false,
    name: "",
    api_base: "",
    api_key: "",
    model: "",
    priority: nextPriority(),
    // 新上游默认勾选「支持工具调用」。
    supports_tools: true,
    enable_thinking: false,
    health: null,
    provider_id: "",
  });
  addedSlots.add(nextSlot);
  expanded.add(String(nextSlot));
  render();
  markDirty();
}

function deleteUpstream(slot) {
  const index = state.upstreams.findIndex((item) => item.slot === slot);
  if (index < 0) return;
  const item = state.upstreams[index];
  const label = item.name || upstreamLabel(slot);
  confirmDialog(`确定删除上游「${label}」吗？此操作需点击「保存全部」后生效。`).then(
    (ok) => {
      if (!ok) return;
      captureOperationSnapshot();
      state.upstreams.splice(index, 1);
      expanded.delete(String(slot));
      addedSlots.delete(slot);
      pendingDelete.add(slot);
      render();
      markDirty();
    },
  );
}

// 页面内自绘确认对话框：AstrBot 插件视图运行在 sandbox iframe 里，
// 未授予 allow-modals，window.confirm/alert 会被浏览器静默禁止（恒返回 false）。
function confirmDialog(message) {
  return new Promise((resolve) => {
    let overlay = document.querySelector("#confirm-overlay");
    if (!overlay) {
      overlay = document.createElement("div");
      overlay.id = "confirm-overlay";
      overlay.className = "confirm-overlay";
      overlay.innerHTML =
        '<div class="confirm-box" role="dialog" aria-modal="true">' +
        '<p class="confirm-text"></p>' +
        '<div class="confirm-actions">' +
        '<button type="button" class="button secondary confirm-cancel">取消</button>' +
        '<button type="button" class="button confirm-ok">确认删除</button>' +
        "</div></div>";
      document.body.appendChild(overlay);
    }
    overlay.querySelector(".confirm-text").textContent = message;
    overlay.hidden = false;
    const finish = (result) => {
      overlay.hidden = true;
      overlay.onclick = null;
      resolve(result);
    };
    overlay.querySelector(".confirm-ok").onclick = () => finish(true);
    overlay.querySelector(".confirm-cancel").onclick = () => finish(false);
    overlay.onclick = (event) => {
      if (event.target === overlay) finish(false);
    };
  });
}

function collectPayload() {
  const payload = {};
  const global = {};
  globalFields.querySelectorAll("[data-global-key]").forEach((input) => {
    const key = input.dataset.globalKey;
    global[key] = input.type === "checkbox" ? input.checked : input.value;
  });
  payload.global = global;
  const visibleArticles = Array.from(list.querySelectorAll(".upstream"));
  list.querySelectorAll(".upstream").forEach((article) => {
    const slot = article.dataset.slot;
    const order = visibleArticles.indexOf(article);
    const stateItem = state.upstreams.find((item) => Number(item.slot) === Number(slot));
    const keyValue = article.querySelector(".api-key").value.trim();
    // 未编辑判定：仍是掩码、仍等于加载值、或仍是「显示」出来的原文。
    // 命中时省略 api_key，由后端沿用已保存密钥，避免把真 Key 覆盖成星号。
    const keyUnchanged = Boolean(
      keyValue
      && (
        keyValue.includes("****")
        || keyValue === (stateItem?.api_key || "")
        || (stateItem?._keyRevealed && keyValue === (stateItem._revealedApiKey || ""))
      ),
    );
    const payloadItem = {
      route_enabled: article.querySelector(".upstream-enabled").checked,
      api_type: article.querySelector(".api-type").value,
      name: article.querySelector(".name").value.trim(),
      api_base: article.querySelector(".api-base").value.trim(),
      model: article.querySelector(".model").value.trim(),
      // 页面不再暴露数字优先级，按可见列表拖拽顺序自动写入。
      priority: (order + 1) * 10,
      supports_tools: article.querySelector(".tools").checked,
      enable_thinking: article.querySelector(".thinking").checked,
    };
    if (!keyUnchanged) payloadItem.api_key = keyValue;
    payload[`upstream_${slot}`] = payloadItem;
  });
  if (pendingDelete.size > 0) {
    payload.delete_slots = Array.from(pendingDelete);
  }
  return payload;
}

async function save() {
  const button = document.querySelector("#save-button");
  const bottomButton = document.querySelector("#save-bottom-button");
  // 自定义保存按钮不触发表单原生校验，主动拦截越界、空值和非整数。
  for (const input of globalFields.querySelectorAll('input[type="number"]')) {
    if (!input.checkValidity()) {
      const spec = GLOBAL_FIELDS.find((field) => field.key === input.dataset.globalKey);
      showToast(`${spec.label}：请输入 ${spec.min}–${spec.max} 范围内的${input.step === "any" ? "数字" : "整数"}`, true);
      input.reportValidity();
      return;
    }
  }
  const payload = collectPayload();
  // 保存前前端完整性校验：启用但配置不完整时直接拦截，给友好提示。
  for (const key of Object.keys(payload)) {
    if (!key.startsWith("upstream_")) continue;
    const slot = key.slice("upstream_".length);
    const item = payload[key];
    const stateItem = state.upstreams.find((entry) => Number(entry.slot) === Number(slot));
    // payload 省略 api_key 代表沿用已保存密钥，此时用加载时的存在标记判断完整性。
    const hasKey = Object.prototype.hasOwnProperty.call(item, "api_key")
      ? Boolean(item.api_key)
      : Boolean(stateItem?.api_key_present || stateItem?.api_key);
    if (item.route_enabled && (!item.api_base || !hasKey || !item.model)) {
      showToast(`${upstreamLabel(slot)} 已启用路由但缺少 ${!item.api_base ? "API Base" : !hasKey ? "API Key" : "模型 ID"}，无法保存`, true);
      return;
    }
  }
  button.disabled = true;
  bottomButton.disabled = true;
  button.textContent = "保存中…";
  bottomButton.textContent = "保存中…";
  try {
    const response = await bridge.apiPost("upstreams/save", payload);
    showToast(
      response.ok
        ? `已保存，运行中上游 ${response.runtime_upstream_count} 个`
        : "保存失败",
      !response.ok,
    );
    await load();
  } catch (error) {
    showToast(error.message || "保存失败", true);
  } finally {
    button.disabled = false;
    bottomButton.disabled = false;
    button.textContent = "保存全部";
    bottomButton.textContent = "保存并生效";
  }
}

document.querySelector("#refresh-button").addEventListener("click", load);
document.querySelector("#save-button").addEventListener("click", save);
document.querySelector("#save-bottom-button").addEventListener("click", save);
document.querySelector("#cancel-save-button").addEventListener("click", undoLastOperation);
document.querySelector("#add-upstream").addEventListener("click", addUpstream);
document.querySelector("#help-button").addEventListener("click", () => {
  helpOverlay.hidden = false;
  document.querySelector("#help-close").focus();
});
document.querySelector("#help-close").addEventListener("click", () => {
  helpOverlay.hidden = true;
  document.querySelector("#help-button").focus();
});
helpOverlay.addEventListener("click", (event) => {
  if (event.target === helpOverlay) {
    helpOverlay.hidden = true;
  }
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !helpOverlay.hidden) {
    helpOverlay.hidden = true;
    document.querySelector("#help-button").focus();
  }
});

function editorInputFromTarget(target) {
  if (!(target instanceof Element)) return null;
  const direct = target.closest("input[data-global-key], .upstream input, .upstream select");
  if (direct) return direct;
  const label = target.closest("label");
  return label?.querySelector(
    "input[data-global-key], .upstream input, .upstream select",
  ) || null;
}

// 在控件真正改变前记录快照；鼠标和键盘切换输入框都覆盖。
document.addEventListener("pointerdown", (event) => {
  const editor = editorInputFromTarget(event.target);
  if (editor) {
    captureOperationSnapshot();
    return;
  }
  if (
    !saveBar.hidden
    && !saveBar.contains(event.target)
    && event.target instanceof Element
    && !event.target.closest("button, .upstream, .help-dialog")
  ) {
    undoLastOperation();
  }
});
document.addEventListener("focusin", (event) => {
  if (editorInputFromTarget(event.target)) {
    captureOperationSnapshot();
  }
});
load();

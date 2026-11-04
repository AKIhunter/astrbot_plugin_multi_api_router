const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;

const list = document.querySelector("#upstream-list");
const template = document.querySelector("#upstream-template");
const globalFields = document.querySelector("#global-fields");
const toastElement = document.querySelector("#toast");
let state = { upstreams: [], global: {}, strategy: {} };
let dirty = false;
let pendingDelete = new Set();
// 页面只展示「已启用 / 配置完整 / 本次新加」的上游，隐藏历史遗留的空槽位。
const addedSlots = new Set();
const expanded = new Set();
const MAX_UPSTREAMS = 20;

// 页面只保留需要经常改的策略项；超时、并发、预算等仍在 AstrBot 插件配置里。
const GLOBAL_FIELDS = [
  { key: "enabled", label: "启用路由 Provider", type: "checkbox" },
  { key: "provider_id", label: "整体模式 Provider ID", type: "text" },
  { key: "fallback_to_main_model", label: "真实调用失败后回退主模型", type: "checkbox" },
  { key: "fallback_provider_id", label: "回退 Provider ID（留空用默认主模型）", type: "text" },
  { key: "skip_unhealthy_upstreams", label: "跳过健康检查不通的上游", type: "checkbox" },
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

function markDirty() {
  dirty = true;
  document.querySelector("#save-button").classList.add("dirty");
}

function clearDirty() {
  dirty = false;
  document.querySelector("#save-button").classList.remove("dirty");
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
    input.value = state.global[spec.key] ?? "";
    if (spec.min !== undefined) { input.min = spec.min; input.max = spec.max; }
    input.addEventListener("input", markDirty);
    label.append(span, input);
    globalFields.appendChild(label);
  }
}

function updateSummary() {
  const upstreams = state.upstreams;
  document.querySelector("#enabled-count").textContent = upstreams.filter(
    (item) => item.enabled,
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
    ` · 单个上游：${strategy.pinned_provider_prefix || "multi_api_router:upstream_"}<编号>` +
    ` · 运行中 ${state.runtime_upstream_count ?? 0} 个`;
}

function render() {
  list.replaceChildren();
  // 隐藏既未启用、也没填完整的空槽位，避免历史备份槽位把页面拉得很长；
  // 本次点击「新增 API」的槽位始终显示，方便填写。
  const visible = state.upstreams.filter(
    (item) => item.enabled || item.configured || addedSlots.has(item.slot),
  );
  // 数值越小越优先；页面顺序与真实调用顺序一致。
  const ordered = [...visible].sort(
    (a, b) => (a.priority ?? 0) - (b.priority ?? 0) || a.slot - b.slot,
  );
  for (const upstream of ordered) {
    const fragment = template.content.cloneNode(true);
    const article = fragment.querySelector(".upstream");
    const health = upstream.health;
    const healthState = healthClass(health);

    article.dataset.slot = upstream.slot;
    article.classList.add(healthState);

    // 折叠头部摘要
    const toggle = fragment.querySelector(".upstream-toggle");
    fragment.querySelector(".slot").textContent = `#${upstream.slot}`;
    fragment.querySelector(".upstream-name").textContent =
      escapeText(upstream.name) || "未命名";
    const toolTag = fragment.querySelector(".tool-tag");
    if (upstream.supports_tools) {
      toolTag.classList.add("on");
    } else {
      toolTag.classList.remove("on");
    }
    toolTag.textContent = upstream.supports_tools ? "工具✓" : "无工具";

    const badge = fragment.querySelector(".state-badge");
    if (!upstream.configured) {
      badge.textContent = "配置不完整";
      badge.classList.add("muted");
    } else if (!upstream.enabled) {
      badge.textContent = "未启用";
      badge.classList.add("muted");
    } else {
      badge.textContent = "已启用";
      badge.classList.add("enabled");
    }

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
    fragment.querySelector(".enabled").checked = Boolean(upstream.enabled);
    fragment.querySelector(".name").value = escapeText(upstream.name);
    fragment.querySelector(".priority").value = upstream.priority;
    fragment.querySelector(".api-base").value = escapeText(upstream.api_base);
    fragment.querySelector(".api-key").value = escapeText(upstream.api_key || "");
    fragment.querySelector(".model").value = escapeText(upstream.model);
    fragment.querySelector(".tools").checked = Boolean(upstream.supports_tools);
    fragment.querySelector(".pinned-hint").textContent = upstream.provider_id
      ? `在 AstrBot 中选择此 Provider 可直接使用该上游：${upstream.provider_id}`
      : "";

    const status = fragment.querySelector(".health-status");
    const detail = fragment.querySelector(".health-detail");
    if (health) {
      status.textContent = health.status;
      detail.textContent = [
        `${health.latency_ms}ms`,
        health.http_status ? `HTTP ${health.http_status}` : "",
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
    article.querySelectorAll("input").forEach((input) => {
      input.addEventListener("input", markDirty);
      input.addEventListener("change", markDirty);
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
        ? `上游 ${slot} 检测正常`
        : `上游 ${slot}：${response.result.status}`,
      !response.result.ok,
    );
  } catch (error) {
    showToast(error.message || "检测失败", true);
    await load();
  }
}

function addUpstream() {
  // 优先复用配置里已存在但页面隐藏的空槽位（历史备份槽位），
  // 避免直接顶到 20 个上限而无法新增。
  const reusable = state.upstreams.find(
    (item) => !item.enabled && !item.configured && !addedSlots.has(item.slot),
  );
  if (reusable) {
    addedSlots.add(reusable.slot);
    expanded.add(String(reusable.slot));
    render();
    markDirty();
    return;
  }
  const used = new Set(state.upstreams.map((item) => item.slot));
  let nextSlot = 0;
  for (let slot = 1; slot <= MAX_UPSTREAMS; slot += 1) {
    if (!used.has(slot)) {
      nextSlot = slot;
      break;
    }
  }
  if (!nextSlot) {
    showToast(`最多支持 ${MAX_UPSTREAMS} 个上游`, true);
    return;
  }
  state.upstreams.push({
    slot: nextSlot,
    enabled: false,
    configured: false,
    name: "",
    api_base: "",
    api_key: "",
    model: "",
    priority: nextSlot * 10,
    // 新上游默认勾选「支持工具调用」。
    supports_tools: true,
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
  const label = item.name || `#${slot}`;
  confirmDialog(`确定删除上游「${label}」吗？此操作需点击「保存全部」后生效。`).then(
    (ok) => {
      if (!ok) return;
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
  list.querySelectorAll(".upstream").forEach((article) => {
    const slot = article.dataset.slot;
    payload[`upstream_${slot}`] = {
      enabled: article.querySelector(".enabled").checked,
      name: article.querySelector(".name").value.trim(),
      api_base: article.querySelector(".api-base").value.trim(),
      api_key: article.querySelector(".api-key").value.trim(),
      model: article.querySelector(".model").value.trim(),
      priority: article.querySelector(".priority").value,
      supports_tools: article.querySelector(".tools").checked,
    };
  });
  if (pendingDelete.size > 0) {
    payload.delete_slots = Array.from(pendingDelete);
  }
  return payload;
}

async function save() {
  const button = document.querySelector("#save-button");
  const payload = collectPayload();
  // 保存前前端完整性校验：启用但配置不完整时直接拦截，给友好提示。
  for (const key of Object.keys(payload)) {
    if (!key.startsWith("upstream_")) continue;
    const slot = key.slice("upstream_".length);
    const item = payload[key];
    if (item.enabled && (!item.api_base || !item.api_key || !item.model)) {
      showToast(`上游 ${slot} 已启用但缺少 ${!item.api_base ? "API Base" : !item.api_key ? "API Key" : "模型 ID"}，无法保存`, true);
      return;
    }
  }
  button.disabled = true;
  button.textContent = "保存中…";
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
    button.textContent = "保存全部";
  }
}

document.querySelector("#refresh-button").addEventListener("click", load);
document.querySelector("#save-button").addEventListener("click", save);
document.querySelector("#add-upstream").addEventListener("click", addUpstream);
load();

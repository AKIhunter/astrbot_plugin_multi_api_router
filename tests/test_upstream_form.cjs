// Run with node --test tests/test_upstream_form.cjs; uses no browser or network.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(
  path.join(__dirname, "../views/upstreams/app.js"), "utf8",
);

function editor({ savedKey = "", typedKey = "fixture-new-key", revealedKey = "" } = {}) {
  const nodes = new Map();
  const document = {
    querySelector(selector) {
      if (!nodes.has(selector)) {
        nodes.set(selector, {
          hidden: true,
          addEventListener() {},
          querySelectorAll() { return []; },
          classList: { add() {}, remove() {}, toggle() {} },
        });
      }
      return nodes.get(selector);
    },
    addEventListener() {},
  };
  const fields = {
    ".upstream-enabled": { checked: true },
    ".api-type": { value: "chat" },
    ".name": { value: "fixture" },
    ".api-base": { value: "https://example.invalid/v1" },
    ".api-key": { value: typedKey },
    ".model": { value: "fixture-model" },
    ".tools": { checked: true },
    ".thinking": { checked: false },
  };
  const context = vm.createContext({
    window: { AstrBotPluginView: {} },
    document,
    Element: class {},
    article: {
      dataset: { slot: "16" },
      querySelector: (selector) => fields[selector],
    },
    initial: { savedKey, revealedKey },
    setTimeout() {},
  });
  // Skip only the initial HTTP load; exercise the actual editor/save functions.
  vm.runInContext(source.replace(/\nload\(\);\s*$/, "\n"), context);
  vm.runInContext(`
    state = { global: {}, upstreams: [{
      slot: 16, api_key: initial.savedKey,
      api_key_present: Boolean(initial.savedKey),
      route_enabled: true, configured: false,
      _keyRevealed: Boolean(initial.revealedKey),
      _revealedApiKey: initial.revealedKey,
    }] };
    list.querySelectorAll = () => [article];
    addedSlots.add(16);
  `, context);
  return {
    fields,
    run: (script) => vm.runInContext(script, context),
    payload: () => vm.runInContext("collectPayload().upstream_16", context),
  };
}

test("new key survives the snapshot taken when changing input focus", () => {
  const form = editor();
  assert.equal(form.payload().api_key, "fixture-new-key");
  form.run("captureOperationSnapshot()");
  assert.equal(form.payload().api_key, "fixture-new-key");
});

test("disabling routing still submits the new key", () => {
  const form = editor();
  form.run("captureOperationSnapshot()");
  form.fields[".upstream-enabled"].checked = false;
  const payload = form.payload();
  assert.equal(payload.route_enabled, false);
  assert.equal(payload.api_key, "fixture-new-key");
});

test("replacing an existing masked key survives multiple snapshots", () => {
  const form = editor({ savedKey: "fixture****old1" });
  form.run("captureOperationSnapshot(); captureOperationSnapshot()");
  assert.equal(form.payload().api_key, "fixture-new-key");
});

test("a draft key restored from editor state is still submitted", () => {
  const form = editor();
  form.run("captureOperationSnapshot()");
  form.fields[".api-key"].value = form.run("state.upstreams[0].api_key");
  assert.equal(form.payload().api_key, "fixture-new-key");
});

test("an unchanged mask is omitted so the server retains its key", () => {
  const form = editor({ savedKey: "fixture****old1", typedKey: "fixture****old1" });
  form.run("captureOperationSnapshot()");
  assert.equal(Object.hasOwn(form.payload(), "api_key"), false);
});

test("an unchanged revealed key is omitted", () => {
  const form = editor({
    savedKey: "fixture****old1",
    typedKey: "fixture-saved-key",
    revealedKey: "fixture-saved-key",
  });
  form.run("captureOperationSnapshot()");
  assert.equal(Object.hasOwn(form.payload(), "api_key"), false);
});

test("editing a revealed key submits the replacement", () => {
  const form = editor({
    savedKey: "fixture****old1", revealedKey: "fixture-saved-key",
  });
  form.run("captureOperationSnapshot()");
  assert.equal(form.payload().api_key, "fixture-new-key");
});

test("clearing a key is submitted explicitly", () => {
  const form = editor({ savedKey: "fixture****old1", typedKey: "" });
  form.run("captureOperationSnapshot()");
  assert.equal(form.payload().api_key, "");
});

test("model lookup cannot reuse a key that exists only in the draft", () => {
  const form = editor();
  form.run("captureOperationSnapshot()");
  assert.equal(form.run(
    'canReuseSavedApiKey(article.querySelector(".api-key").value, state.upstreams[0])',
  ), false);
});

test("model lookup can reuse an unchanged mask or revealed key", () => {
  for (const options of [
    { savedKey: "fixture****old1", typedKey: "fixture****old1" },
    {
      savedKey: "fixture****old1",
      typedKey: "fixture-saved-key",
      revealedKey: "fixture-saved-key",
    },
  ]) {
    const form = editor(options);
    form.run("captureOperationSnapshot()");
    assert.equal(form.run(
      'canReuseSavedApiKey(article.querySelector(".api-key").value, state.upstreams[0])',
    ), true);
  }
});

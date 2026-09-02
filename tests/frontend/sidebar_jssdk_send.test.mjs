import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";


const source = await readFile(
  new URL("../../aicrm_next/app/admin_console/static/sidebar_workbench/sidebar_workbench.js", import.meta.url),
  "utf8",
);
const templateSource = await readFile(
  new URL("../../aicrm_next/app/admin_console/templates/sidebar_customer_workbench.html", import.meta.url),
  "utf8",
);
const bootMarker = "  boot();\n})();";
assert.equal(source.includes(bootMarker), true, "workbench boot marker changed; update the focused harness");


function createNode() {
  return {
    className: "",
    dataset: {},
    disabled: false,
    innerHTML: "",
    parentElement: null,
    textContent: "",
    value: "",
    addEventListener() {},
    appendChild(child) { child.parentElement = this; },
    removeChild(child) { child.parentElement = null; },
    closest() { return null; },
    focus() {},
    querySelector() { return null; },
    select() {},
    setAttribute() {},
    classList: {
      add() {},
      remove() {},
      toggle() {},
    },
  };
}


function loadHarness(options = {}) {
  const nodes = new Map();
  const node = (id) => {
    if (!nodes.has(id)) nodes.set(id, createNode());
    return nodes.get(id);
  };
  node("sidebar-workbench-root").dataset = {
    debugEnabled: "false",
    jssdkConfigUrl: "/api/sidebar/jssdk-config",
  };

  let nowMs = 1_000_000;
  let configCalls = 0;
  let agentConfigCalls = 0;
  let sendCalls = 0;
  class HarnessDate extends Date {
    static now() { return nowMs; }
  }
  const window = {
    clearTimeout,
    location: {
      href: "https://www.youcangogogo.com/sidebar/bind-mobile",
      origin: "https://www.youcangogogo.com",
      pathname: "/sidebar/bind-mobile",
      search: "",
    },
    navigator: {},
    setTimeout,
    wx: {
      config() { configCalls += 1; },
      ready(callback) { setTimeout(callback, 10); },
      error() {},
      agentConfig(agentOptions) {
        agentConfigCalls += 1;
        setTimeout(() => {
          if (options.agentConfigShouldFail) agentOptions.fail({ err_msg: "agentConfig:fail" });
          else agentOptions.success({});
        }, 10);
      },
      invoke(method, _payload, callback) {
        if (method === "getCurExternalContact") {
          callback(options.externalContactResponse || { external_userid: "external-a" });
          return;
        }
        assert.equal(method, "sendChatMessage");
        sendCalls += 1;
        callback({ err_msg: "sendChatMessage:ok" });
      },
    },
  };
  const document = {
    body: createNode(),
    createElement: createNode,
    execCommand() { return false; },
    getElementById: node,
  };
  const instrumented = source.replace(
    bootMarker,
    "  globalThis.__sidebarSendTestApi = { initWeComSdk, resolveContextFromWeCom, sendLinkToCurrentChat, state };\n})();",
  );
  const context = {
    AbortController,
    Date: HarnessDate,
    URL,
    URLSearchParams,
    document,
    encodeURIComponent,
    fetch: options.fetchImpl || (async () => ({
      ok: true,
      status: 200,
      async text() {
        return JSON.stringify({
          ok: true,
          corp_id: "ww-test",
          agent_id: "1000001",
          config: { timestamp: 1, nonceStr: "config", signature: "config-signature" },
          agent_config: { timestamp: 1, nonceStr: "agent", signature: "agent-signature" },
        });
      },
    })),
    window,
  };
  vm.runInNewContext(instrumented, context);
  return {
    api: context.__sidebarSendTestApi,
    advanceTime(ms) { nowMs += ms; },
    counts() { return { configCalls, agentConfigCalls, sendCalls }; },
  };
}


test("product send reuses the ready WeCom SDK after the startup budget expires", async () => {
  const harness = loadHarness();
  harness.api.state.bootDeadline = 1_005_000;

  const initial = await harness.api.initWeComSdk();
  assert.equal(initial.ok, true);

  harness.advanceTime(6_000);
  await harness.api.sendLinkToCurrentChat({
    title: "测试商品",
    url: "https://www.youcangogogo.com/p/1",
    imageUrl: "https://www.youcangogogo.com/static/product.png",
  });

  assert.deepEqual(harness.counts(), {
    configCalls: 1,
    agentConfigCalls: 1,
    sendCalls: 1,
  });
});


test("product send reports an authorization failure instead of claiming it is outside WeCom", async () => {
  const harness = loadHarness({ agentConfigShouldFail: true });

  await assert.rejects(
    harness.api.sendLinkToCurrentChat({
      title: "测试商品",
      url: "https://www.youcangogogo.com/p/1",
      imageUrl: "https://www.youcangogogo.com/static/product.png",
    }),
    /企微侧边栏授权失败，请关闭后重新打开/,
  );
});


test("sidebar page invalidates the cached script for the OneID provisioning fix", () => {
  assert.match(templateSource, /sidebar_workbench\.js\?v=20260902-oneid-provisioning/);
});


test("customer-aware JSSDK refresh obtains the OAuth entrypoint without a session-token POST", async () => {
  const jssdkUrls = [];
  const harness = loadHarness({
    fetchImpl: async (url) => {
      jssdkUrls.push(String(url));
      return {
        ok: true,
        status: 200,
        async text() {
          return JSON.stringify({
            ok: true,
            corp_id: "ww-test",
            agent_id: "1000001",
            config: { timestamp: 1, nonceStr: "config", signature: "config-signature" },
            agent_config: { timestamp: 1, nonceStr: "agent", signature: "agent-signature" },
            sidebar_owner_token: "",
            sidebar_owner_token_status: "viewer_session_required",
            sidebar_oauth_url: "/api/sidebar/oauth/start?external_userid=external-a",
            sidebar_owner_context: { external_userid: "external-a" },
          });
        },
      };
    },
  });

  const result = await harness.api.resolveContextFromWeCom();

  assert.equal(result.ok, true);
  assert.equal(jssdkUrls.length, 2);
  assert.equal(new URL(jssdkUrls[0]).searchParams.get("external_userid"), null);
  assert.equal(new URL(jssdkUrls[1]).searchParams.get("external_userid"), "external-a");
  assert.match(harness.api.state.sidebar_oauth_url, /external_userid=external-a/);
});


test("retry explicitly forces a new sidebar OAuth authorization attempt", () => {
  assert.match(source, /boot\(\{ forceSidebarOAuth: true \}\)/);
  assert.match(source, /侧边栏授权未完成，请点击重试重新授权/);
});

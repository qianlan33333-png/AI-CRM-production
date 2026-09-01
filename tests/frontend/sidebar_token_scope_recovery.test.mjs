import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";


const source = await readFile(
  new URL("../../aicrm_next/app/admin_console/static/sidebar_workbench/sidebar_workbench.js", import.meta.url),
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


function jsonResponse(status, payload) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async text() { return JSON.stringify(payload); },
  };
}


function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}


function loadHarness(fetchImpl) {
  const nodes = new Map();
  const node = (id) => {
    if (!nodes.has(id)) nodes.set(id, createNode());
    return nodes.get(id);
  };
  node("sidebar-workbench-root").dataset = {
    contextTokenUrl: "/api/sidebar/context-token",
    debugEnabled: "false",
    jssdkConfigUrl: "/api/sidebar/jssdk-config",
    workbenchUrl: "/api/sidebar/v2/workbench",
  };
  const sessionStorage = new Map();
  const window = {
    clearTimeout,
    location: {
      href: "https://www.xinliushangye.com/sidebar/bind-mobile",
      origin: "https://www.xinliushangye.com",
      pathname: "/sidebar/bind-mobile",
      search: "",
    },
    navigator: {},
    sessionStorage: {
      getItem(key) { return sessionStorage.get(key) || null; },
      removeItem(key) { sessionStorage.delete(key); },
      setItem(key, value) { sessionStorage.set(key, String(value)); },
    },
    setTimeout,
  };
  const assignedUrls = [];
  window.location.assign = (url) => assignedUrls.push(String(url));
  const document = {
    body: createNode(),
    createElement: createNode,
    execCommand() { return false; },
    getElementById: node,
  };
  const instrumented = source.replace(
    bootMarker,
    "  globalThis.__sidebarTokenTestApi = { applySidebarOwnerToken, jssdkConfigUrl, maybeStartSidebarOAuth, refreshSidebarOwnerToken, requestJson, setExternalUserid, state };\n})();",
  );
  const context = {
    AbortController,
    Date,
    URL,
    URLSearchParams,
    document,
    encodeURIComponent,
    fetch: fetchImpl,
    window,
  };
  vm.runInNewContext(instrumented, context);
  const api = context.__sidebarTokenTestApi;
  api.assignedUrls = assignedUrls;
  api.sessionStorage = sessionStorage;
  return api;
}


function tokenPayload(externalUserid, token) {
  return {
    ok: true,
    sidebar_owner_token: token,
    sidebar_owner_token_status: "issued",
    sidebar_owner_context: {
      viewer_userid: "LinKaiYan",
      owner_userid: "LinKaiYan",
      bind_by_userid: "LinKaiYan",
      external_userid: externalUserid,
    },
  };
}


test("a late token response for the previous customer cannot overwrite the current customer token", async () => {
  const tokenA = deferred();
  const tokenB = deferred();
  const api = loadHarness(async (url, options) => {
    assert.equal(url, "/api/sidebar/context-token");
    const externalUserid = JSON.parse(options.body).external_userid;
    return externalUserid === "external-a" ? tokenA.promise : tokenB.promise;
  });

  api.setExternalUserid("external-a");
  const requestA = api.refreshSidebarOwnerToken();
  api.setExternalUserid("external-b");
  const requestB = api.refreshSidebarOwnerToken();

  tokenB.resolve(jsonResponse(200, tokenPayload("external-b", "token-b")));
  assert.equal(await requestB, true);
  assert.equal(api.state.sidebar_owner_token, "token-b");
  assert.equal(api.state.sidebar_owner_token_external_userid, "external-b");

  tokenA.resolve(jsonResponse(200, tokenPayload("external-a", "token-a")));
  assert.equal(await requestA, false);
  assert.equal(api.state.sidebar_owner_token, "token-b");
  assert.equal(api.state.sidebar_owner_token_external_userid, "external-b");
});


test("customer-scope 403 refreshes the current customer token and retries exactly once", async () => {
  const calls = [];
  const api = loadHarness(async (url, options) => {
    calls.push({ url, headers: { ...(options.headers || {}) } });
    if (url === "/api/sidebar/context-token") {
      return jsonResponse(200, tokenPayload("external-b", "token-good"));
    }
    const workbenchCalls = calls.filter((call) => call.url.includes("/api/sidebar/v2/workbench"));
    if (workbenchCalls.length === 1) {
      return jsonResponse(403, { ok: false, error: "sidebar_customer_scope_forbidden" });
    }
    return jsonResponse(200, { ok: true, customer: { external_userid: "external-b" } });
  });
  api.setExternalUserid("external-b");
  api.applySidebarOwnerToken(tokenPayload("external-b", "token-stale"), "external-b");

  const payload = await api.requestJson(
    "/api/sidebar/v2/workbench?external_userid=external-b&owner_userid=LinKaiYan",
    { retryCount: 0 },
  );

  assert.equal(payload.customer.external_userid, "external-b");
  assert.equal(api.state.sidebar_owner_token, "token-good");
  assert.deepEqual(calls.map((call) => call.url), [
    "/api/sidebar/v2/workbench?external_userid=external-b&owner_userid=LinKaiYan",
    "/api/sidebar/context-token",
    "/api/sidebar/v2/workbench?external_userid=external-b&owner_userid=LinKaiYan",
  ]);
  assert.equal(calls[0].headers["X-AICRM-Sidebar-Owner-Token"], "token-stale");
  assert.equal(calls[1].headers["X-AICRM-Sidebar-Owner-Token"], undefined);
  assert.equal(calls[2].headers["X-AICRM-Sidebar-Owner-Token"], "token-good");
});


test("a repeated customer-scope 403 stops after one recovery attempt", async () => {
  let workbenchCalls = 0;
  let tokenCalls = 0;
  const api = loadHarness(async (url) => {
    if (url === "/api/sidebar/context-token") {
      tokenCalls += 1;
      return jsonResponse(200, tokenPayload("external-b", "token-refreshed"));
    }
    workbenchCalls += 1;
    return jsonResponse(403, { ok: false, error: "sidebar_customer_scope_forbidden" });
  });
  api.setExternalUserid("external-b");
  api.applySidebarOwnerToken(tokenPayload("external-b", "token-stale"), "external-b");

  await assert.rejects(
    api.requestJson("/api/sidebar/v2/workbench?external_userid=external-b", { retryCount: 2 }),
    /sidebar_customer_scope_forbidden/,
  );
  assert.equal(tokenCalls, 1);
  assert.equal(workbenchCalls, 2);
});


test("manual retry restarts OAuth after an earlier incomplete authorization attempt", async () => {
  const api = loadHarness(async () => {
    throw new Error("OAuth retry must use the already supplied authorization URL");
  });
  api.setExternalUserid("external-b");
  api.state.sidebar_oauth_url = "/api/sidebar/oauth/start?external_userid=external-b";
  api.state.sidebar_owner_token_status = "viewer_session_required";
  api.sessionStorage.set("aicrm_sidebar_oauth:external-b", "1");

  assert.equal(await api.maybeStartSidebarOAuth("owner_token_missing"), false);
  assert.equal(api.assignedUrls.length, 0);

  assert.equal(await api.maybeStartSidebarOAuth("manual_retry", { force: true }), true);
  assert.equal(api.assignedUrls.length, 1);
  assert.match(api.assignedUrls[0], /\/api\/sidebar\/oauth\/start/);
  assert.match(api.assignedUrls[0], /external_userid=external-b/);
});


test("JSSDK bootstrap declares the identified customer so it can return the OAuth entrypoint", () => {
  const api = loadHarness(async () => {
    throw new Error("JSSDK URL construction must not issue a request");
  });
  api.setExternalUserid("external-b");
  const url = new URL(api.jssdkConfigUrl());
  assert.equal(url.searchParams.get("external_userid"), "external-b");
});


test("a provisioning response keeps the sidebar in identity setup without issuing a customer token", async () => {
  const api = loadHarness(async (url) => {
    assert.equal(url, "/api/sidebar/context-token");
    return jsonResponse(202, {
      ok: true,
      context_status: "provisioning",
      sidebar_owner_token: "",
      sidebar_owner_token_status: "provisioning",
      sync_token: "opaque-sync-token",
      retry_after: 30,
    });
  });
  api.setExternalUserid("external-new");

  assert.equal(await api.refreshSidebarOwnerToken(), false);
  assert.equal(api.state.sidebar_owner_token, "");
  assert.equal(api.state.sidebar_owner_token_status, "provisioning");
  assert.equal(api.state.provisioning_sync_token, "opaque-sync-token");
  assert.notEqual(api.state.provisioning_retry_timer, null);
  clearTimeout(api.state.provisioning_retry_timer);
});

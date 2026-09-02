(function () {
  const root = document.getElementById("sidebar-workbench-root");
  const content = document.getElementById("content");
  const tabsNode = document.getElementById("tabs");
  const toastNode = document.getElementById("toast");
  const debugWrap = document.getElementById("debug-wrap");
  const mobileModal = document.getElementById("mobile-modal");
  const mobileInput = document.getElementById("mobile-input");
  const mobileStatus = document.getElementById("mobile-status");
  const confirmMobileButton = document.getElementById("confirm-mobile-button");

  const tabs = [
    ["profile", "核心画像"],
    ["questionnaires", "问卷"],
    ["products", "商品"],
    ["orders", "订单"],
    ["coupons", "优惠券"],
    ["materials", "素材"],
    ["other_staff_messages", "其他客服聊天"],
  ];
  const materialTabs = [
    ["image", "图片素材"],
    ["radar", "雷达链接"],
  ];
  const productTabs = [
    ["regular", "普通商品"],
    ["service_period", "周期性商品"],
  ];
  const orderTabs = [
    ["regular", "普通订单"],
    ["periodic", "周期订单"],
  ];
  const profileTabs = [
    ["basic", "基础信息"],
    ["timeline", "用户时间线"],
  ];
  const WORKBENCH_STATES = {
    identifying_customer: "identifying_customer",
    sdk_unavailable: "sdk_unavailable",
    context_missing: "context_missing",
    loading_workbench: "loading_workbench",
    ready: "ready",
    degraded_ready: "degraded_ready",
    error: "error",
  };
  const DEFAULT_TIMEOUT_MS = 8000;
  const SDK_TIMEOUT_MS = 5000;
  const STARTUP_BUDGET_MS = 5000;
  const PANEL_TIMEOUT_MS = {
    workbench: 6500,
    questionnaires: 9000,
    products: 9000,
    orders: 12000,
    periodic_orders: 12000,
    coupons: 9000,
    materials: 10000,
    radar_links: 10000,
    timeline: 10000,
    other_staff_messages: 9000,
  };
  const PANEL_CACHE_TTL_MS = 2 * 60 * 1000;
  const JSSDK_CONFIG_CACHE_MAX_TTL_MS = 5 * 60 * 1000;
  const MAX_PROVISIONING_RETRIES = 5;
  const PRODUCT_CARD_IMAGE_PATH = "/static/sidebar_workbench/product-card-cover.png";

  const state = {
    status: WORKBENCH_STATES.identifying_customer,
    external_userid: "",
    owner_userid: "",
    bind_by_userid: "",
    sidebar_owner_token: "",
    sidebar_owner_token_external_userid: "",
    sidebar_owner_token_status: "",
    sidebar_oauth_url: "",
    sidebar_oauth_started: false,
    provisioning_sync_token: "",
    provisioning_retry_timer: null,
    provisioning_retry_attempts: 0,
    activeTab: "profile",
    profileView: "basic",
    materialType: "image",
    materialQuery: "",
    materialQuickKeywords: [],
    productType: "regular",
    orderType: "regular",
    workbench: null,
    loaded: {},
    data: {
      questionnaires: null,
      products: null,
      service_period_products: null,
      orders: null,
      periodic_orders: null,
      coupons: null,
      materials: {},
      radar_links: null,
      timeline: {
        items: [],
        total: 0,
        has_more: false,
        next_offset: 0,
      },
      other_staff_messages: null,
    },
    profileSaveTimer: null,
    periodicRemarkTimers: {},
    toastTimer: null,
    lastError: null,
    panelCache: {},
    panelRequests: new Map(),
    jssdkConfigRequests: new Map(),
    jssdkConfigCache: new Map(),
    contextTokenRequests: new Map(),
    weComSdkReady: null,
    weComSdkInitPromise: null,
    bootDeadline: 0,
    timelineRequestVersion: 0,
    materialRequestVersion: 0,
    materialSearchController: null,
    materialPager: null,
    materialThumbObserver: null,
    materialImageController: null,
  };

  const debugEnabled = root && root.dataset.debugEnabled === "true";
  debugWrap.classList.toggle("hidden", !debugEnabled);

  function endpoint(name) {
    return root.dataset[name] || "";
  }

  function escapeHtml(value) {
    return String(value || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function formatTimelineTime(value) {
    const raw = String(value || "").trim();
    if (!raw) return "—";
    const parsed = new Date(raw);
    if (Number.isNaN(parsed.getTime())) return "—";
    try {
      const parts = new Intl.DateTimeFormat("en-CA", {
        timeZone: "Asia/Shanghai",
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        hourCycle: "h23",
      }).formatToParts(parsed);
      const values = {};
      parts.forEach((part) => {
        if (part.type !== "literal") values[part.type] = part.value;
      });
      if (!values.year || !values.month || !values.day || !values.hour || !values.minute) return "—";
      return values.year + "-" + values.month + "-" + values.day + " " + values.hour + ":" + values.minute;
    } catch (_error) {
      return "—";
    }
  }

  function timelineSummary(item) {
    const summary = String((item && item.summary) || "").trim();
    if (String((item && item.event_type) || "") === "product_enrolled" && summary === "已完成商品报名或支付") {
      return "";
    }
    return summary;
  }

  function huangyoucanMatched(item) {
    return ["matched_unionid", "matched_mobile"].indexOf(String((item && item.huangyoucan_match_status) || "")) >= 0;
  }

  function huangyoucanBoolean(item, key, truthy, falsy) {
    return huangyoucanMatched(item) ? (item[key] ? truthy : falsy) : "—";
  }

  function huangyoucanProgress(item) {
    if (!huangyoucanMatched(item)) return "—";
    const progress = item.huangyoucan_learning_plan_progress;
    return progress ? String(Number(progress.current || 0)) + "/" + String(Number(progress.total || 0)) : "无";
  }

  function huangyoucanLastOpen(item) {
    if (!huangyoucanMatched(item)) return "—";
    const value = item.huangyoucan_last_open_at;
    return value ? String(value).replace("T", " ").slice(0, 16) : "无";
  }

  function safeJsonParse(text) {
    try {
      return JSON.parse(text);
    } catch (_error) {
      return null;
    }
  }

  function writeDebug(label, payload) {
    if (!debugEnabled) return;
    const line = "[" + new Date().toISOString() + "] " + label + (payload === undefined ? "" : " " + JSON.stringify(payload));
    const item = document.createElement("pre");
    item.textContent = line;
    debugWrap.appendChild(item);
  }

  function customerContextQuery() {
    return {
      external_userid: state.external_userid,
      owner_userid: state.owner_userid,
      bind_by_userid: state.bind_by_userid || state.owner_userid,
    };
  }

  function productContextDiagnostics(payload) {
    const products = (payload && payload.products) || [];
    return {
      context_source: payload && payload.diagnostics ? payload.diagnostics.context_source : "",
      context_status: payload && payload.diagnostics ? payload.diagnostics.context_status : "",
      product_url_has_context: products.some((item) => {
        try {
          return new URL(item.product_url || "", window.location.origin).hash.indexOf("aicrm_ctx=") >= 0;
        } catch (_error) {
          return String(item.product_url || "").indexOf("#aicrm_ctx=") !== -1;
        }
      }),
    };
  }

  function clearSidebarOwnerToken(status) {
    state.sidebar_owner_token = "";
    state.sidebar_owner_token_external_userid = "";
    if (status) state.sidebar_owner_token_status = String(status);
  }

  function setExternalUserid(value) {
    const nextExternalUserid = String(value || "").trim();
    if (nextExternalUserid === state.external_userid) return nextExternalUserid;
    window.clearTimeout(state.provisioning_retry_timer);
    state.provisioning_retry_timer = null;
    state.provisioning_retry_attempts = 0;
    state.external_userid = nextExternalUserid;
    if (state.sidebar_owner_token_external_userid !== nextExternalUserid) {
      clearSidebarOwnerToken(nextExternalUserid ? "customer_changed" : "external_userid_missing");
    }
    return nextExternalUserid;
  }

  function applySidebarOwnerToken(payload, expectedExternalUserid) {
    const token = String((payload && payload.sidebar_owner_token) || "").trim();
    const hasTokenField = Boolean(payload && Object.prototype.hasOwnProperty.call(payload, "sidebar_owner_token"));
    const context = (payload && payload.sidebar_owner_context) || {};
    const contextExternalUserid = String(context.external_userid || "").trim();
    const expected = String(expectedExternalUserid || "").trim();
    if (token) {
      const staleResponse = Boolean(
        !contextExternalUserid ||
        (state.external_userid && contextExternalUserid !== state.external_userid) ||
        (expected && contextExternalUserid !== expected) ||
        (expected && state.external_userid !== expected)
      );
      if (staleResponse) {
        writeDebug("stale sidebar owner token discarded", {
          expected_external_userid: expected,
          context_external_userid: contextExternalUserid,
          current_external_userid: state.external_userid,
        });
        return false;
      }
      window.clearTimeout(state.provisioning_retry_timer);
      state.provisioning_retry_timer = null;
      state.provisioning_retry_attempts = 0;
      state.sidebar_owner_token = token;
      state.sidebar_owner_token_external_userid = contextExternalUserid;
    } else if (hasTokenField && (!expected || !state.external_userid || state.external_userid === expected)) {
      clearSidebarOwnerToken((payload && payload.sidebar_owner_token_status) || "missing");
    }
    state.sidebar_owner_token_status = String((payload && payload.sidebar_owner_token_status) || state.sidebar_owner_token_status || "").trim();
    if (payload && Object.prototype.hasOwnProperty.call(payload, "sidebar_oauth_url")) {
      state.sidebar_oauth_url = String(payload.sidebar_oauth_url || "").trim();
    }
    const owner = String(context.owner_userid || context.viewer_userid || "").trim();
    if (owner) state.owner_userid = owner;
    const bindBy = String(context.bind_by_userid || "").trim();
    if (bindBy) state.bind_by_userid = bindBy;
    if (token && state.external_userid && window.sessionStorage) {
      try {
        window.sessionStorage.removeItem(sidebarOAuthAttemptKey());
      } catch (_error) {
        // Ignore storage cleanup failures; the fresh owner token is the source of truth.
      }
    }
    return Boolean(token && state.sidebar_owner_token_external_userid === contextExternalUserid);
  }

  function firstPayloadValue(payload, keys) {
    const queue = [payload];
    const seen = [];
    while (queue.length) {
      const current = queue.shift();
      if (!current || typeof current !== "object" || seen.indexOf(current) >= 0) continue;
      seen.push(current);
      for (const key of keys || []) {
        const value = String(current[key] || "").trim();
        if (value) return value;
      }
      ["data", "context", "user", "member", "currentUser", "current_user"].forEach((key) => {
        if (current[key] && typeof current[key] === "object") queue.push(current[key]);
      });
    }
    return "";
  }

  function extractWeComViewerUserid(payload, options) {
    const keys = [
      "viewer_userid",
      "viewerUserid",
      "viewerUserId",
      "operator_userid",
      "operatorUserid",
      "operatorUserId",
      "owner_userid",
      "ownerUserid",
      "ownerUserId",
      "current_userid",
      "currentUserid",
      "currentUserId",
      "userid",
      "user_id",
      "UserId",
    ];
    if (options && options.allowUserId) keys.push("userId");
    return firstPayloadValue(payload, keys);
  }

  function extractWeComExternalUserid(payload) {
    return firstPayloadValue(payload, [
      "external_userid",
      "externalUserid",
      "external_userId",
      "externalUserId",
      "external_user_id",
      "externalUserID",
      "userId",
      "user_id",
    ]);
  }

  function applyWeComViewerIdentity(payload, source, options) {
    const viewer = extractWeComViewerUserid(payload, options || {});
    if (!viewer) return false;
    const previousOwner = state.owner_userid;
    state.owner_userid = viewer;
    if (!state.bind_by_userid || state.bind_by_userid === previousOwner) state.bind_by_userid = viewer;
    writeDebug("viewer identity resolved", { source: source || "", owner_userid: state.owner_userid, bind_by_userid: state.bind_by_userid });
    return true;
  }

  function jssdkConfigUrl() {
    const signedPageUrl = new URL(window.location.href.split("#")[0]);
    signedPageUrl.searchParams.delete("sidebar_oauth");
    signedPageUrl.searchParams.delete("sidebar_oauth_error");
    const url = new URL(endpoint("jssdkConfigUrl"), window.location.origin);
    url.searchParams.set("url", signedPageUrl.toString());
    if (state.external_userid) url.searchParams.set("external_userid", state.external_userid);
    return url.toString();
  }

  function jssdkStorageKey(url) {
    return "aicrm_sidebar_jssdk_config:" + url;
  }

  function publicJssdkPayload(payload) {
    const copy = Object.assign({}, payload || {});
    delete copy.sidebar_owner_token;
    delete copy.sidebar_owner_token_status;
    delete copy.sidebar_owner_context;
    delete copy.sidebar_oauth_url;
    return copy;
  }

  function readJssdkConfigCache(url) {
    let cached = state.jssdkConfigCache.get(url);
    if (!cached && window.sessionStorage) {
      try {
        cached = safeJsonParse(window.sessionStorage.getItem(jssdkStorageKey(url)) || "");
      } catch (_error) {
        cached = null;
      }
    }
    if (!cached) return null;
    if (cached.expiresAt <= Date.now()) {
      state.jssdkConfigCache.delete(url);
      if (window.sessionStorage) window.sessionStorage.removeItem(jssdkStorageKey(url));
      return null;
    }
    state.jssdkConfigCache.set(url, cached);
    return cached.payload;
  }

  function writeJssdkConfigCache(url, payload) {
    const cached = {
      payload: publicJssdkPayload(payload),
      expiresAt: Date.now() + JSSDK_CONFIG_CACHE_MAX_TTL_MS,
    };
    state.jssdkConfigCache.set(url, cached);
    if (window.sessionStorage) {
      try { window.sessionStorage.setItem(jssdkStorageKey(url), JSON.stringify(cached)); } catch (_error) {}
    }
  }

  function remainingStartupBudget(fallbackMs) {
    if (!state.bootDeadline) return fallbackMs || SDK_TIMEOUT_MS;
    return Math.max(1, Math.min(fallbackMs || SDK_TIMEOUT_MS, state.bootDeadline - Date.now()));
  }

  async function requestJssdkConfig(timeoutMs, options) {
    const url = jssdkConfigUrl();
    const force = Boolean(options && options.force);
    if (!force) {
      const cached = readJssdkConfigCache(url);
      if (cached) return cached;
      const pending = state.jssdkConfigRequests.get(url);
      if (pending) return pending;
    }
    const request = requestJson(url, { timeoutMs: Math.max(1, Number(timeoutMs) || SDK_TIMEOUT_MS), retryCount: 0 })
      .then((payload) => {
        writeJssdkConfigCache(url, payload);
        return payload;
      })
      .finally(() => {
        if (!force) state.jssdkConfigRequests.delete(url);
      });
    if (!force) state.jssdkConfigRequests.set(url, request);
    return request;
  }

  async function refreshSidebarOwnerToken(externalUserid) {
    const key = String(externalUserid || state.external_userid || "").trim();
    if (!key) return false;
    const pending = state.contextTokenRequests.get(key);
    if (pending) return pending;
    const request = (async () => {
    try {
      const payload = await requestJson(endpoint("contextTokenUrl"), {
        method: "POST",
        body: JSON.stringify({ external_userid: key }),
        timeoutMs: remainingStartupBudget(SDK_TIMEOUT_MS),
        retryCount: 0,
        omitSidebarOwnerToken: true,
        skipSidebarTokenRecovery: true,
      });
      const applied = applySidebarOwnerToken(payload, key);
      if (String(payload.context_status || "").trim() === "provisioning") {
        state.provisioning_sync_token = String(payload.sync_token || "").trim();
        scheduleProvisioningRetry(Number(payload.retry_after || 2));
      }
      writeDebug("sidebar owner token refreshed", {
        status: state.sidebar_owner_token_status,
        has_token: Boolean(state.sidebar_owner_token),
        external_userid: key,
        applied,
      });
      return applied;
    } catch (error) {
      if (state.external_userid === key) applySidebarOwnerToken((error && error.payload) || {}, key);
      writeDebug("sidebar owner token refresh failed", { message: error.message || String(error), external_userid: key });
      return false;
    }
    })().finally(() => state.contextTokenRequests.delete(key));
    state.contextTokenRequests.set(key, request);
    return request;
  }

  function sidebarOAuthAttemptKey() {
    return "aicrm_sidebar_oauth:" + String(state.external_userid || "unknown");
  }

  function currentSidebarNextPath() {
    const params = new URLSearchParams(window.location.search);
    params.delete("sidebar_oauth_error");
    const query = params.toString();
    return window.location.pathname + (query ? "?" + query : "");
  }

  function cleanupSidebarOAuthUrl() {
    const url = new URL(window.location.href);
    const hadTransient = url.searchParams.has("sidebar_oauth");
    url.searchParams.delete("sidebar_oauth");
    if (hadTransient && window.history && window.history.replaceState) {
      window.history.replaceState({}, document.title, url.pathname + url.search + url.hash);
    }
  }

  async function maybeStartSidebarOAuth(reason, options) {
    if (state.sidebar_owner_token || state.sidebar_oauth_started || !state.external_userid) return false;
    const force = Boolean(options && options.force);
    const params = new URLSearchParams(window.location.search);
    const oauthError = String(params.get("sidebar_oauth_error") || "").trim();
    if (oauthError) {
      writeDebug("sidebar oauth skipped after callback error", { error: oauthError, reason: reason || "" });
      return false;
    }
    if (!state.sidebar_oauth_url) {
      try {
        const payload = await requestJssdkConfig(remainingStartupBudget(SDK_TIMEOUT_MS), { force: true });
        applySidebarOwnerToken(payload, state.external_userid);
      } catch (error) {
        writeDebug("sidebar oauth bootstrap refresh failed", { message: error.message || String(error) });
      }
    }
    if (state.sidebar_owner_token) return false;
    if (
      !state.sidebar_oauth_url &&
      ["", "viewer_session_required", "context_token_required"].indexOf(state.sidebar_owner_token_status) >= 0
    ) {
      state.sidebar_oauth_url = "/api/sidebar/oauth/start";
    }
    if (!state.sidebar_oauth_url) {
      writeDebug("sidebar oauth unavailable", { reason: reason || "", owner_token_status: state.sidebar_owner_token_status });
      return false;
    }
    if (window.sessionStorage) {
      try {
        const key = sidebarOAuthAttemptKey();
        if (!force && window.sessionStorage.getItem(key) === "1") {
          writeDebug("sidebar oauth skipped after prior attempt", { reason: reason || "" });
          return false;
        }
        window.sessionStorage.setItem(key, "1");
      } catch (_error) {
        // Best-effort loop guard; OAuth can proceed when storage is unavailable.
      }
    }
    const target = new URL(state.sidebar_oauth_url, window.location.origin);
    target.searchParams.set("external_userid", state.external_userid);
    target.searchParams.set("next", currentSidebarNextPath());
    state.sidebar_oauth_started = true;
    writeDebug("sidebar oauth start", { reason: reason || "", target: target.pathname });
    window.location.assign(target.toString());
    return true;
  }

  function showToast(message, tone) {
    window.clearTimeout(state.toastTimer);
    toastNode.textContent = message || "";
    toastNode.className = "toast" + (tone === "error" ? " error" : "");
    toastNode.classList.remove("hidden");
    state.toastTimer = window.setTimeout(() => toastNode.classList.add("hidden"), 1900);
  }

  function sleep(ms) {
    return new Promise((resolve) => window.setTimeout(resolve, ms));
  }

  function scheduleProvisioningRetry(retryAfterSeconds) {
    if (state.provisioning_retry_timer || state.provisioning_retry_attempts >= MAX_PROVISIONING_RETRIES) return false;
    const delay = Math.max(1, Math.min(Number(retryAfterSeconds) || 2, 30)) * 1000;
    state.provisioning_retry_attempts += 1;
    state.provisioning_retry_timer = window.setTimeout(() => {
      state.provisioning_retry_timer = null;
      boot();
    }, delay);
    return true;
  }

  function renderProvisioning() {
    setWorkbenchState(WORKBENCH_STATES.identifying_customer, { context_status: "provisioning" });
    renderTop();
    renderTabs();
    content.innerHTML = panel(
      "核心画像",
      '<div class="status">' + (state.provisioning_retry_attempts >= MAX_PROVISIONING_RETRIES && !state.provisioning_retry_timer
        ? "客户身份暂未建立，请点击重试。"
        : "正在核验企微客户关系，客户身份建立后会自动打开。") + '</div>' +
        '<div class="row-actions"><button class="btn primary" type="button" data-retry-boot>立即重试</button></div>'
    );
  }

  function shouldRetryRequest(error) {
    if (!error) return false;
    if (error.stage === "request_timeout") return true;
    if (!error.status) return true;
    return error.status >= 500;
  }

  function isSidebarCustomerScopeError(error) {
    const payload = (error && error.payload) || {};
    return Number(error && error.status) === 403 && String(payload.error || "").trim() === "sidebar_customer_scope_forbidden";
  }

  function requestExternalUserid(url, options) {
    try {
      const parsed = new URL(url, window.location.origin);
      const queryExternal = ["external_userid", "externalUserid", "externalUserId", "user_id", "userId"]
        .map((key) => String(parsed.searchParams.get(key) || "").trim())
        .find(Boolean);
      if (queryExternal) return queryExternal;
    } catch (_error) {
      // The request wrapper will report malformed URLs; context recovery simply stays disabled.
    }
    const body = options && options.body;
    if (typeof body !== "string" || !body.trim()) return "";
    try {
      const payload = JSON.parse(body);
      return String((payload && (payload.external_userid || payload.user_id)) || "").trim();
    } catch (_error) {
      return "";
    }
  }

  async function requestJson(url, options) {
    const retryCount = Math.max(0, Number((options && options.retryCount) || 0));
    const retryDelayMs = Math.max(0, Number((options && options.retryDelayMs) || 320));
    const targetExternalUserid = requestExternalUserid(url, options) || state.external_userid;
    const allowSidebarTokenRecovery = !(options && options.skipSidebarTokenRecovery);
    let sidebarTokenRecoveryAttempted = false;
    let lastError = null;
    for (let attempt = 0; attempt <= retryCount; attempt += 1) {
      try {
        return await requestJsonOnce(url, options || {});
      } catch (error) {
        lastError = error;
        if (
          allowSidebarTokenRecovery &&
          !sidebarTokenRecoveryAttempted &&
          isSidebarCustomerScopeError(error) &&
          targetExternalUserid &&
          targetExternalUserid === state.external_userid
        ) {
          sidebarTokenRecoveryAttempted = true;
          clearSidebarOwnerToken("scope_refresh_required");
          const refreshed = await refreshSidebarOwnerToken(targetExternalUserid);
          if (refreshed && targetExternalUserid === state.external_userid) {
            try {
              return await requestJsonOnce(url, { ...(options || {}), skipSidebarTokenRecovery: true });
            } catch (retryError) {
              throw retryError;
            }
          }
        }
        if (attempt >= retryCount || !shouldRetryRequest(error)) break;
        await sleep(retryDelayMs * (attempt + 1));
      }
    }
    throw lastError;
  }

  async function requestJsonOnce(url, options) {
    const timeoutMs = Number((options && options.timeoutMs) || DEFAULT_TIMEOUT_MS);
    const providedSignal = options && options.signal;
    const controller = typeof AbortController !== "undefined" ? new AbortController() : null;
    let detachProvidedAbort = null;
    if (controller && providedSignal) {
      const abortFromCaller = () => controller.abort();
      if (providedSignal.aborted) abortFromCaller();
      else {
        providedSignal.addEventListener("abort", abortFromCaller, { once: true });
        detachProvidedAbort = () => providedSignal.removeEventListener("abort", abortFromCaller);
      }
    }
    const timer = controller
      ? window.setTimeout(() => controller.abort(), timeoutMs)
      : null;
    const requestTargetExternalUserid = requestExternalUserid(url, options);
    const canAttachSidebarOwnerToken = Boolean(
      !(options && options.omitSidebarOwnerToken) &&
      state.sidebar_owner_token &&
      state.sidebar_owner_token_external_userid &&
      (!requestTargetExternalUserid || state.sidebar_owner_token_external_userid === requestTargetExternalUserid)
    );
    const finalOptions = {
      cache: "no-store",
      ...(options || {}),
      headers: {
        Accept: "application/json",
        ...(options && options.body ? { "Content-Type": "application/json" } : {}),
        ...(canAttachSidebarOwnerToken ? { "X-AICRM-Sidebar-Owner-Token": state.sidebar_owner_token } : {}),
        ...((options && options.headers) || {}),
      },
      ...(controller ? { signal: controller.signal } : {}),
    };
    delete finalOptions.timeoutMs;
    delete finalOptions.retryCount;
    delete finalOptions.retryDelayMs;
    delete finalOptions.omitSidebarOwnerToken;
    delete finalOptions.skipSidebarTokenRecovery;
    try {
      const response = await fetch(url, finalOptions);
      const text = await response.text();
      const payload = text ? safeJsonParse(text) : null;
      if (!response.ok || (payload && payload.ok === false)) {
        const message = window.AdminApi?.responseErrorMessage
          ? window.AdminApi.responseErrorMessage(response, payload, "请求失败")
          : (typeof (payload && payload.error) === "string" && payload.error) || "请求失败，请稍后重试";
        const error = new Error(message);
        error.status = response.status;
        error.payload = payload || {};
        throw error;
      }
      return payload || { ok: true };
    } catch (error) {
      if (error && error.name === "AbortError") {
        const abortError = new Error(providedSignal && providedSignal.aborted ? "请求已取消" : "请求超时，请重试");
        abortError.stage = providedSignal && providedSignal.aborted ? "request_cancelled" : "request_timeout";
        throw abortError;
      }
      throw error;
    } finally {
      if (timer) window.clearTimeout(timer);
      if (detachProvidedAbort) detachProvidedAbort();
    }
  }

  function queryUrl(baseUrl, params) {
    const url = new URL(baseUrl, window.location.origin);
    Object.entries(params || {}).forEach(([key, value]) => {
      if (value !== undefined && value !== null && String(value).trim() !== "") {
        url.searchParams.set(key, String(value).trim());
      }
    });
    return url.toString();
  }

  function panelCacheKey(tab, url) {
    return [state.external_userid || "", tab || "", url || ""].join("::");
  }

  function readPanelCache(tab, url) {
    const item = state.panelCache[panelCacheKey(tab, url)];
    if (!item || item.expiresAt < Date.now()) return null;
    return item.payload;
  }

  function writePanelCache(tab, url, payload) {
    state.panelCache[panelCacheKey(tab, url)] = {
      payload,
      expiresAt: Date.now() + PANEL_CACHE_TTL_MS,
    };
  }

  function clearPanelCache(tab) {
    Object.keys(state.panelCache || {}).forEach((key) => {
      if (key.indexOf("::" + tab + "::") >= 0) delete state.panelCache[key];
    });
  }

  async function requestPanelJson(tab, url, options) {
    const cached = readPanelCache(tab, url);
    if (cached) return cached;
    const key = panelCacheKey(tab, url);
    const pending = state.panelRequests.get(key);
    if (pending) return pending;
    const request = requestJson(url, {
      timeoutMs: PANEL_TIMEOUT_MS[tab] || DEFAULT_TIMEOUT_MS,
      retryCount: 0,
      ...(options || {}),
    })
      .then((payload) => {
        writePanelCache(tab, url, payload);
        return payload;
      })
      .finally(() => state.panelRequests.delete(key));
    state.panelRequests.set(key, request);
    return request;
  }

  function absoluteUrl(path) {
    const text = String(path || "").trim();
    if (!text) return "";
    try {
      return new URL(text, window.location.origin).toString();
    } catch (_error) {
      return text;
    }
  }

  function openOrderDetail(detailUrl) {
    const link = absoluteUrl(detailUrl);
    if (!link) {
      showToast("暂无订单详情链接", "error");
      return false;
    }
    window.open(link, "_blank", "noopener");
    showToast("已打开订单详情");
    return true;
  }

  function getQueryValue(key) {
    return new URLSearchParams(window.location.search).get(key) || "";
  }

  function firstQueryValue(keys) {
    for (const key of keys || []) {
      const value = getQueryValue(key).trim();
      if (value) return value;
    }
    return "";
  }

  function setPanelLoading(title) {
    content.innerHTML = panel(
      title || "",
      '<div class="skeleton-list" aria-busy="true">' +
        '<div class="skeleton-line strong"></div>' +
        '<div class="skeleton-line"></div>' +
        '<div class="skeleton-line short"></div>' +
      "</div>"
    );
  }

  function stateLabel(status) {
    const labels = {
      identifying_customer: "识别中",
      sdk_unavailable: "未识别到客户",
      context_missing: "未识别到客户",
      loading_workbench: "加载中",
      ready: "",
      degraded_ready: "部分加载",
      error: "加载失败",
    };
    return labels[status] || "加载失败";
  }

  function setWorkbenchState(status, detail) {
    state.status = status;
    state.lastError = detail || null;
    writeDebug("state transition", { status, detail: detail || {} });
    if (status !== WORKBENCH_STATES.ready && status !== WORKBENCH_STATES.degraded_ready) {
      renderTopState(status, detail || {});
    }
  }

  function renderTopState(status, detail) {
    const isLoading = status === WORKBENCH_STATES.identifying_customer || status === WORKBENCH_STATES.loading_workbench;
    document.getElementById("customer-name").textContent = stateLabel(status);
    document.getElementById("customer-mobile").textContent = "";
    document.getElementById("customer-external-userid").textContent = state.external_userid ? "外部联系人 ID " + state.external_userid : "";
    document.getElementById("workflow-title").textContent = "";
    const bindingState = document.getElementById("binding-state");
    bindingState.textContent = isLoading ? (status === WORKBENCH_STATES.identifying_customer ? "识别中" : "加载中...") : stateLabel(status);
    bindingState.classList.remove("hidden");
    bindingState.classList.toggle("loading", isLoading);
    bindingState.classList.toggle("unbound", !isLoading);
    if (detail && detail.message) writeDebug("top state detail", detail);
  }

  function renderRetryPanel(title, message) {
    content.innerHTML = panel(
      title || "",
      '<div class="status error">' + escapeHtml(message || "加载失败，请稍后重试。") + "</div>" +
        '<div class="row-actions"><button class="btn primary" type="button" data-retry-boot>重试</button></div>'
    );
  }

  function panel(title, body) {
    const head = title ? '<div class="head"><h2>' + escapeHtml(title) + "</h2></div>" : "";
    return '<section class="panel">' + head + body + "</section>";
  }

  function empty(message) {
    return '<div class="empty">' + escapeHtml(message) + "</div>";
  }

  function isWorkbenchReady() {
    return Boolean(state.workbench) && (state.status === WORKBENCH_STATES.ready || state.status === WORKBENCH_STATES.degraded_ready);
  }

  function renderTabs() {
    tabsNode.innerHTML = tabs
      .map(([key, label]) => {
        const active = key === state.activeTab ? " active" : "";
        const disabled = key !== "profile" && !isWorkbenchReady() ? ' disabled aria-disabled="true"' : "";
        return '<button class="tab' + active + '" type="button" data-tab="' + key + '"' + disabled + ">" + escapeHtml(label) + "</button>";
      })
      .join("");
  }

  function renderTop() {
    const workbench = state.workbench || {};
    const customer = workbench.customer || {};
    const workflow = workbench.workflow || {};
    const name = String(customer.display_name || "当前客户").trim();
    const mobile = String(customer.mobile || "").trim();
    const externalUserid = String(customer.external_userid || state.external_userid || "").trim();
    const isBound = customer.mobile_bound !== undefined ? Boolean(customer.mobile_bound) : Boolean(customer.is_bound && mobile);
    document.getElementById("customer-name").textContent = name;
    document.getElementById("customer-mobile").textContent = mobile ? "手机号 " + mobile : "";
    document.getElementById("customer-external-userid").textContent = externalUserid ? "外部联系人 ID " + externalUserid : "";
    document.getElementById("workflow-title").textContent = String(workflow.title || "").trim();
    const bindingState = document.getElementById("binding-state");
    const hideBindingState = Boolean(customer.owner_pending);
    bindingState.textContent = hideBindingState ? "" : (isBound ? "手机号已绑定" : "手机号未绑定");
    bindingState.classList.toggle("hidden", hideBindingState);
    bindingState.classList.remove("loading");
    bindingState.classList.toggle("unbound", !isBound);
    const changeButton = document.getElementById("change-mobile-button");
    if (changeButton) changeButton.disabled = Boolean(customer.owner_pending);
  }

  function updateProfileField(key, value) {
    const profile = state.workbench.profile || {};
    profile[key] = value;
    state.workbench.profile = profile;
  }

  function saveProfileSoon() {
    window.clearTimeout(state.profileSaveTimer);
    state.profileSaveTimer = window.setTimeout(saveProfile, 520);
  }

  async function saveProfile() {
    if (!state.workbench || !state.external_userid) return;
    const profile = state.workbench.profile || {};
    try {
      await requestJson(endpoint("profileUrl"), {
        method: "PUT",
        body: JSON.stringify({
          external_userid: state.external_userid,
          source: profile.source || "",
          industry: profile.industry || "",
          industry_description: profile.industry_description || "",
          needs_blockers_followup: profile.needs_blockers_followup || "",
          updated_by: state.bind_by_userid || state.owner_userid || "",
        }),
      });
      showToast("已保存");
    } catch (error) {
      showToast(error.message || "保存失败", "error");
    }
  }

  function updatePeriodicOrderRemark(id, value) {
    const rows = state.data.periodic_orders || [];
    const item = rows.find((entry) => String(entry.id || "") === String(id || ""));
    if (item) item.remark = value;
  }

  function savePeriodicOrderRemarkSoon(id) {
    window.clearTimeout(state.periodicRemarkTimers[id]);
    state.periodicRemarkTimers[id] = window.setTimeout(() => savePeriodicOrderRemark(id), 520);
  }

  async function savePeriodicOrderRemark(id) {
    const rows = state.data.periodic_orders || [];
    const item = rows.find((entry) => String(entry.id || "") === String(id || ""));
    if (!item || !state.external_userid) return;
    try {
      const remarkUrl = queryUrl(endpoint("periodicOrderRemarkUrl") + "/" + encodeURIComponent(id) + "/remark", customerContextQuery());
      const payload = await requestJson(remarkUrl, {
        method: "PUT",
        body: JSON.stringify({
          external_userid: state.external_userid,
          remark: item.remark || "",
        }),
      });
      const updated = payload.periodic_order || {};
      if (Object.prototype.hasOwnProperty.call(updated, "remark")) item.remark = updated.remark || "";
      clearPanelCache("periodic_orders");
      showToast("备注已保存");
    } catch (error) {
      showToast(error.message || "备注保存失败", "error");
    }
  }

  function textAreaField(key, label, value) {
    return (
      '<div class="field">' +
      '<div class="field-title">' + escapeHtml(label) + "</div>" +
      '<textarea class="textarea" data-profile-field="' + key + '">' + escapeHtml(value || "") + "</textarea>" +
      "</div>"
    );
  }

  function segmentedControls(items, activeKey, dataAttribute, extraClass) {
    return '<div class="seg two-column-seg ' + escapeHtml(extraClass || "") + '">' + items.map(([key, label]) => (
      '<button type="button" class="' + (key === activeKey ? "active" : "") + '" ' + dataAttribute + '="' + escapeHtml(key) + '">' + escapeHtml(label) + "</button>"
    )).join("") + "</div>";
  }

  function profileTypeControls() {
    return segmentedControls(profileTabs, state.profileView, "data-profile-view", "profile-seg");
  }

  function renderProfile() {
    if (state.profileView === "timeline") {
      renderProfileTimeline();
      return;
    }
    const profile = (state.workbench && state.workbench.profile) || {};
    content.innerHTML = panel(
      "",
      profileTypeControls() + '<div class="editor">' +
        textAreaField("source", "用户来源", profile.source || "") +
        textAreaField("industry", "行业信息", profile.industry || "") +
        textAreaField("industry_description", "行业具体描述", profile.industry_description || "") +
        textAreaField("needs_blockers_followup", "需求、卡点、跟进状态", profile.needs_blockers_followup || "") +
      "</div>"
    );
  }

  function renderProfileTimeline() {
    const timeline = state.data.timeline || {};
    const rows = timeline.items || [];
    const actions = '<div class="timeline-toolbar"><span class="mini">最新动态在前</span><button class="btn ghost" type="button" data-refresh-timeline>刷新</button></div>';
    const body = rows.length
      ? '<div class="customer-timeline">' + rows.map((item, index) => {
          const summary = timelineSummary(item);
          const sourceAction = item.source_action || {};
          const sourceKind = String(sourceAction.kind || "");
          const canOpenSource = sourceKind === "questionnaire_submission" || sourceKind === "order_detail";
          const sourceButton = canOpenSource
            ? '<div class="timeline-event-actions"><button class="btn ghost" type="button" data-timeline-source="' + escapeHtml(index) + '">查看原纪录</button></div>'
            : "";
          return (
            '<article class="timeline-event"><div class="timeline-marker" aria-hidden="true"></div><div class="timeline-event-main">' +
            '<div class="timeline-event-head"><h3>' + escapeHtml(item.title || "用户动态") + '</h3><time>' + escapeHtml(formatTimelineTime(item.event_time)) + "</time></div>" +
            (summary ? '<div class="timeline-summary">' + escapeHtml(summary) + "</div>" : "") +
            sourceButton + '</div></article>'
          );
        }).join("") + "</div>" +
        (timeline.has_more ? '<div class="row-actions timeline-more"><button class="btn ghost" type="button" data-load-more-timeline>加载更多</button></div>' : "")
      : empty("暂无用户时间线记录");
    content.innerHTML = panel("", profileTypeControls() + actions + body);
  }

  function renderQuestionnaires() {
    const rows = state.data.questionnaires || [];
    if (!rows.length) {
      content.innerHTML = panel("问卷", empty("暂无问卷记录"));
      return;
    }
    content.innerHTML = panel(
      "问卷",
      rows
        .map((item, index) => {
          const answers = item.answers || [];
          const count = String(item.answer_count || answers.length || 0) + "/" + String(item.total_count || item.answer_count || answers.length || 0) + " 题";
          return (
            '<article class="card" tabindex="-1" data-questionnaire-card="' + index + '" data-questionnaire-submission-id="' + escapeHtml(item.submission_id || item.id || "") + '" data-questionnaire-id="' + escapeHtml(item.questionnaire_id || "") + '">' +
            '<div class="card-title"><div><h3>' + escapeHtml(item.title || "未命名问卷") + "</h3>" +
            '<div class="mini">' + escapeHtml([item.submitted_at || "", count].filter(Boolean).join(" · ")) + "</div></div></div>" +
            '<div class="row-actions"><button class="btn primary" type="button" data-toggle-questionnaire="' + index + '">查看答案</button></div>' +
            '<div class="questions">' +
            answers.map((answer) => '<div class="question"><b>' + escapeHtml(answer.question || "未命名问题") + "</b><em>" + escapeHtml(answer.answer || "未填写") + "</em></div>").join("") +
            "</div></article>"
          );
        })
        .join("")
    );
  }

  function renderProducts() {
    const isServicePeriod = state.productType === "service_period";
    const rows = isServicePeriod ? state.data.service_period_products || [] : state.data.products || [];
    const controls = '<div class="seg product-seg">' + productTabs.map(([key, label]) => '<button type="button" class="' + (key === state.productType ? "active" : "") + '" data-product-type="' + key + '">' + escapeHtml(label) + "</button>").join("") + "</div>";
    if (!rows.length) {
      content.innerHTML = panel("商品", controls + empty(isServicePeriod ? "暂无周期性商品" : "暂无普通商品"));
      return;
    }
    content.innerHTML = panel(
      "商品",
      controls +
        rows
        .map((item, index) => {
          const meta = isServicePeriod && item.duration_days ? '<div class="mini">有效期 ' + escapeHtml(String(item.duration_days)) + " 天</div>" : "";
          return (
            '<article class="card"><div class="card-title"><h3>' + escapeHtml(item.title || "未命名商品") + "</h3>" +
            '<div class="price">' + escapeHtml(item.price_label || "") + "</div></div>" + meta +
            '<div class="row-actions"><button class="btn primary" type="button" data-product-send="' + escapeHtml(index) + '" data-product-kind="' + escapeHtml(state.productType) + '">发送商品</button></div></article>'
          );
        })
        .join("")
    );
  }

  function orderTypeControls() {
    return segmentedControls(orderTabs, state.orderType, "data-order-type", "order-seg");
  }

  function regularOrderCards() {
    const rows = state.data.orders || [];
    if (!rows.length) return empty("暂无普通订单");
    return rows
      .map((item) => (
        '<article class="card"><div class="card-title"><div><h3>' + escapeHtml(item.title || "未命名商品") + "</h3>" +
        '<div class="mini">' + escapeHtml(item.id || "") + '</div></div><div class="price">' + escapeHtml(item.amount_label || "") + "</div></div>" +
        '<div class="kv"><span>状态</span><strong>' + escapeHtml(item.status_label || "") + "</strong>" +
        '<span>时间</span><strong>' + escapeHtml(item.paid_at || "") + "</strong></div>" +
        '<div class="row-actions"><button class="btn primary" type="button" data-order-detail-url="' + escapeHtml(item.detail_url || "") + '">查看详情</button></div></article>'
      ))
      .join("");
  }

  function renderOrders() {
    const body = state.orderType === "periodic" ? periodicOrderCards() : regularOrderCards();
    content.innerHTML = panel("", orderTypeControls() + body);
  }

  function periodicOrderCards() {
    const rows = state.data.periodic_orders || [];
    if (!rows.length) return empty("暂无周期订单");
    return rows
        .map((item) => {
          const lastOrder = [item.last_out_trade_no || "", item.last_order_paid_at || ""].filter(Boolean).join(" · ");
          const detailAction = item.detail_url
            ? '<div class="row-actions"><button class="btn primary" type="button" data-order-detail-url="' + escapeHtml(item.detail_url || "") + '">查看详情</button></div>'
            : "";
          return (
            '<article class="card periodic-order-card"><div class="card-title"><div><h3>' + escapeHtml(item.title || "未命名周期商品") + "</h3>" +
            '<div class="mini">' + escapeHtml(lastOrder || item.product_code || "") + '</div></div><div class="price">' + escapeHtml(item.amount_label || "") + "</div></div>" +
            '<div class="kv"><span>剩余有效期</span><strong>' + escapeHtml(String(item.remaining_days || 0)) + " 天</strong>" +
            '<span>周期</span><strong>' + escapeHtml(String(item.duration_days || 0)) + " 天</strong>" +
            '<span>正式登录</span><strong>' + escapeHtml(huangyoucanBoolean(item, "huangyoucan_formally_logged_in", "是", "否")) + "</strong>" +
            '<span>token 消耗</span><strong>' + escapeHtml(huangyoucanBoolean(item, "huangyoucan_has_token_usage", "有", "无")) + "</strong>" +
            '<span>学习计划进度</span><strong>' + escapeHtml(huangyoucanProgress(item)) + "</strong>" +
            '<span>近 7 天打开次数</span><strong>' + escapeHtml(huangyoucanMatched(item) ? String(Number(item.huangyoucan_open_count_7d || 0)) : "—") + "</strong>" +
            '<span>最后打开时间</span><strong>' + escapeHtml(huangyoucanLastOpen(item)) + "</strong></div>" +
            '<div class="field periodic-remark"><div class="field-title">备注</div>' +
            '<textarea class="textarea periodic-remark-textarea" data-periodic-order-remark="' + escapeHtml(item.id || "") + '">' + escapeHtml(item.remark || "") + "</textarea></div>" +
            detailAction + "</article>"
          );
        })
        .join("");
  }

  function renderPeriodicOrders() {
    state.orderType = "periodic";
    renderOrders();
  }

  function renderCoupons() {
    const rows = state.data.coupons || [];
    if (!rows.length) {
      content.innerHTML = panel("", empty("暂无可领取优惠券"));
      return;
    }
    content.innerHTML = panel(
      "",
      rows.map((item) => {
        const products = (item.products || []).map((product) => product.title || "").filter(Boolean).join("、");
        return (
          '<article class="card link-card"><div class="card-title"><div><h3>' + escapeHtml(item.name || "未命名优惠券") + '</h3><div class="mini">' +
          escapeHtml(item.discount_label || "") + '</div></div></div><div class="kv"><span>适用商品</span><strong>' + escapeHtml(products || "全部已配置商品") +
          '</strong><span>领取截止</span><strong>' + escapeHtml(item.claim_ends_at || "") + '</strong></div><div class="row-actions"><button class="btn primary" type="button" data-copy-url="' +
          escapeHtml(item.url || "") + '">复制链接</button></div></article>'
        );
      }).join("")
    );
  }

  function materialTypeControls() {
    return segmentedControls(materialTabs, state.materialType, "data-material-type", "material-seg");
  }

  function materialResultKey(query) {
    return "image:" + String(query || "").trim();
  }

  function materialSearchControls() {
    if (state.materialType !== "image") return "";
    const query = String(state.materialQuery || "").trim();
    const keywords = (state.materialQuickKeywords || []).slice(0, 5);
    const clearButton = query
      ? '<button class="btn ghost material-search-clear" type="button" data-material-search-clear>清空</button>'
      : "";
    const keywordControls = keywords.length
      ? '<div class="material-quick-keywords" aria-label="快捷关键词">' + keywords.map((keyword) => (
          '<button type="button" class="material-keyword' + (keyword === query ? " active" : "") + '" data-material-keyword="' +
          escapeHtml(keyword) + '">' + escapeHtml(keyword) + "</button>"
        )).join("") + "</div>"
      : "";
    return (
      '<form class="material-search" data-material-search-form>' +
      '<label class="material-search-field"><span class="sr-only">搜索图片素材</span><input type="search" maxlength="100" ' +
      'placeholder="搜索名称、描述、分类或标签" value="' + escapeHtml(query) + '" data-material-search-input></label>' +
      '<button class="btn primary material-search-submit" type="submit">搜索</button>' + clearButton + "</form>" + keywordControls
    );
  }

  function renderMaterialLoadError(type, error) {
    destroyMaterialResources();
    content.innerHTML = panel(
      "素材",
      materialTypeControls() +
        materialSearchControls() +
        '<div class="status error">' + escapeHtml((error && error.message) || "加载失败") + "</div>" +
        '<div class="row-actions"><button class="btn primary" type="button" data-retry-material-type="' + escapeHtml(type) + '">重试</button></div>'
    );
  }

  function destroyMaterialResources() {
    if (state.materialPager) state.materialPager.destroy();
    state.materialPager = null;
    if (state.materialThumbObserver) state.materialThumbObserver.disconnect();
    state.materialThumbObserver = null;
    if (state.materialImageController) state.materialImageController.abort();
    state.materialImageController = null;
  }

  function materialCardHtml(item) {
    const thumb = item.thumbnail_url
      ? '<div class="material-thumb thumb image-thumb"><span class="material-thumb-placeholder" data-material-thumb-status>等待加载</span></div>'
      : '<div class="material-thumb thumb image-thumb preview-unavailable">预览不可用</div>';
    return (
      '<article class="card material material--image" data-material-card data-material-id="' + escapeHtml(item.id || "") + '" data-material-thumb-url="' + escapeHtml(item.thumbnail_url || "") + '">' + thumb +
      '<div class="material-main"><div class="material-tags tags">' +
      (item.tags || []).map((tag) => '<span class="tag">' + escapeHtml(tag) + "</span>").join("") +
      '</div></div><button class="btn primary material-send" type="button" data-material-send="' + escapeHtml(item.id || "") + '">发送</button></article>'
    );
  }

  function materialThumbStatus(cell) {
    return cell ? cell.querySelector("[data-material-thumb-status]") : null;
  }

  function resetMaterialThumbForRetry(card, message, retryable) {
    const cell = card && card.querySelector ? card.querySelector(".material-thumb") : null;
    if (!cell) return;
    cell.classList.add("preview-unavailable");
    if (retryable) {
      cell.innerHTML = '<button class="material-thumb-retry" type="button" data-material-thumb-retry>' + escapeHtml(message || "点击重试") + "</button>";
      return;
    }
    cell.textContent = message || "预览不可用";
  }

  function loadMaterialThumbnail(card) {
    if (!card || card.dataset.materialLoading === "true") return;
    const thumbnailUrl = String(card.dataset.materialThumbUrl || "");
    const cell = card.querySelector(".material-thumb");
    if (!thumbnailUrl || !cell) return;
    if (!window.ImageResourceLoader) {
      resetMaterialThumbForRetry(card, "预览不可用", false);
      return;
    }
    card.dataset.materialLoading = "true";
    cell.classList.remove("preview-unavailable");
    cell.innerHTML = '<span class="material-thumb-placeholder" data-material-thumb-status>正在加载</span>';
    const status = materialThumbStatus(cell);
    const image = document.createElement("img");
    image.alt = "图片素材预览";
    image.width = 64;
    image.height = 64;
    image.loading = "lazy";
    image.decoding = "async";
    image.fetchPriority = "low";
    image.style.opacity = "0";
    image.setAttribute("data-material-thumb-img", "");
    cell.appendChild(image);
    window.ImageResourceLoader.loadInto(image, thumbnailUrl, {
      signal: state.materialImageController ? state.materialImageController.signal : undefined,
      cancelOutsideViewport: true,
      onState: function (nextState) {
        if (!card.isConnected || !status.isConnected) return;
        if (nextState === "pending") status.textContent = "正在生成";
        else if (nextState === "retrying") status.textContent = "正在重试";
        else if (nextState === "loading") status.textContent = "正在加载";
      },
    }).then(function () {
      if (!card.isConnected) return;
      image.style.opacity = "";
      if (status.isConnected) status.remove();
    }).catch(function (error) {
      if (!card.isConnected) return;
      const parentAborted = Boolean(state.materialImageController && state.materialImageController.signal.aborted);
      if (error && error.name === "AbortError") {
        if (error.reason === "outside_viewport" && !parentAborted && state.materialThumbObserver) {
          cell.innerHTML = '<span class="material-thumb-placeholder" data-material-thumb-status>等待加载</span>';
          delete card.dataset.materialLoading;
          state.materialThumbObserver.observe(card);
        }
        return;
      }
      resetMaterialThumbForRetry(card, error && error.retryable ? "点击重试" : "预览不可用", Boolean(error && error.retryable));
    }).finally(function () {
      delete card.dataset.materialLoading;
    });
  }

  function bindMaterialThumbnails(items) {
    if (!state.materialImageController && typeof AbortController !== "undefined") {
      state.materialImageController = new AbortController();
    }
    if (!state.materialThumbObserver && typeof IntersectionObserver !== "undefined") {
      state.materialThumbObserver = new IntersectionObserver((entries) => {
        entries.forEach((entry) => {
          if (!entry.isIntersecting) return;
          const card = entry.target;
          state.materialThumbObserver.unobserve(card);
          loadMaterialThumbnail(card);
        });
      }, { rootMargin: "60px 0px" });
    }
    content.querySelectorAll("[data-material-card]:not([data-material-bound])").forEach((card) => {
      card.setAttribute("data-material-bound", "true");
      if (state.materialThumbObserver) state.materialThumbObserver.observe(card);
      else loadMaterialThumbnail(card);
    });
  }

  function setupMaterialPager(entry, query) {
    const list = content.querySelector("[data-material-list]");
    if (!list || !entry.has_more || !window.ImageResourceLoader) return;
    let pager = null;
    pager = window.ImageResourceLoader.createPager({
      container: list,
      pageSize: 5,
      cooldownMs: 300,
      initialOffset: entry.next_offset,
      initialHasMore: entry.has_more,
      sentinelClass: "material-page-sentinel",
      fetchPage: async (page) => {
        const payload = await requestPanelJson(
          "materials:" + page.offset,
          queryUrl(endpoint("materialsUrl"), { type: "image", limit: 5, offset: page.offset, q: query }),
          { signal: page.signal }
        );
        return Object.assign({}, payload, { items: payload.materials || [] });
      },
      onPage: (items, payload) => {
        entry.items = entry.items.concat(items);
        entry.total = Number(payload.total || entry.total || entry.items.length);
        entry.has_more = Boolean(payload.has_more);
        entry.next_offset = Number(payload.next_offset == null ? entry.items.length : payload.next_offset);
        const holder = document.createElement("div");
        holder.innerHTML = items.map(materialCardHtml).join("");
        Array.from(holder.children).forEach((node) => list.insertBefore(node, pager.sentinel));
        bindMaterialThumbnails(items);
      },
    });
    pager.sentinel.style.cssText = "grid-column:1/-1;text-align:center;color:#888;padding:10px 0;font-size:12px;";
    state.materialPager = pager;
  }

  function renderMaterials() {
    destroyMaterialResources();
    const controls = materialTypeControls();
    if (state.materialType === "radar") {
      renderRadarLinks(controls);
      return;
    }
    const searchControls = materialSearchControls();
    const query = String(state.materialQuery || "").trim();
    const entry = state.data.materials[materialResultKey(query)] || { items: [], has_more: false, next_offset: 0 };
    const rows = entry.items || [];
    if (!rows.length) {
      content.innerHTML = panel("", controls + searchControls + empty(state.materialQuery ? "没有匹配的图片素材" : "暂无图片素材"));
      return;
    }
    content.innerHTML = panel(
      "",
      controls + searchControls +
        '<div class="material-list" data-material-list>' + rows.map(materialCardHtml).join("") + "</div>"
    );
    bindMaterialThumbnails(rows);
    setupMaterialPager(entry, query);
  }

  function renderRadarLinks(controls) {
    destroyMaterialResources();
    const rows = state.data.radar_links || [];
    if (!rows.length) {
      content.innerHTML = panel("", controls + empty("暂无启用中的雷达链接"));
      return;
    }
    content.innerHTML = panel(
      "",
      controls + rows.map((item) => (
        '<article class="card material material--radar"><div class="material-thumb thumb radar">' + escapeHtml(item.type_label || "雷达") +
        '</div><div class="material-main"><h3 class="material-title">' + escapeHtml(item.title || "未命名雷达") +
        '</h3><div class="mini">' + escapeHtml(item.type_label || "追踪链接") +
        '</div></div><button class="btn primary material-send" type="button" data-copy-url="' + escapeHtml(item.url || "") + '">复制链接</button></article>'
      )).join("")
    );
  }

  function messageTitle(item) {
    const customer = ((state.workbench || {}).customer || {}).display_name || "客户";
    if (item.scene === "group") {
      return (item.scene_label || "群聊") + " · 客户" + customer;
    }
    return "客户与 " + (item.staff_name || item.staff_userid || "客服") + " 私聊";
  }

  function renderOtherStaffMessages() {
    const rows = state.data.other_staff_messages || [];
    if (!rows.length) {
      if (((state.workbench || {}).customer || {}).owner_pending) {
        content.innerHTML = panel("其他客服的聊天记录", empty("员工身份待确认后可查看其他客服聊天记录"));
        return;
      }
      content.innerHTML = panel("其他客服的聊天记录", empty("暂无其他客服聊天记录"));
      return;
    }
    content.innerHTML = panel(
      "其他客服的聊天记录",
      '<div class="timeline">' +
        rows
          .map((item) => (
            '<article class="msg"><div class="msg-meta"><span>' + escapeHtml(item.send_time || "") + "</span><span>" + escapeHtml(item.scene_label || "") + "</span></div>" +
            '<div class="msg-title">' + escapeHtml(messageTitle(item)) + "</div>" +
            (item.type === "image"
              ? '<div class="imgmsg"><div class="imgph">图</div><div class="txt">' + escapeHtml(item.content || "发送了图片") + "</div></div>"
              : '<div class="txt">' + escapeHtml(item.sender_label || item.staff_name || "") + "：" + escapeHtml(item.content || "") + "</div>") +
            "</article>"
          ))
          .join("") +
      "</div>"
    );
  }

  function renderOwnerPendingWorkbench(message) {
    state.workbench = {
      customer: {
        external_userid: state.external_userid,
        display_name: "当前客户",
        owner_pending: true,
        mobile_bound: false,
        is_bound: false,
      },
      profile: {},
      workflow: {},
      diagnostics: { context_source_status: "owner_pending" },
    };
    setWorkbenchState(WORKBENCH_STATES.degraded_ready, { stage: "owner_pending", message: message || "" });
    renderTop();
    renderTabs();
    content.innerHTML = panel(
      "核心画像",
      '<div class="status error">' + escapeHtml(message || "员工身份待确认，请从企微侧边栏重新打开或稍后重试。") + "</div>" +
        '<div class="row-actions"><button class="btn primary" type="button" data-retry-boot>重试</button></div>'
    );
  }

  async function loadWorkbench() {
    const requestedExternalUserid = state.external_userid;
    setWorkbenchState(WORKBENCH_STATES.loading_workbench, { external_userid: requestedExternalUserid });
    const payload = await requestPanelJson(
      "workbench",
      queryUrl(endpoint("workbenchUrl"), {
        external_userid: requestedExternalUserid,
        owner_userid: state.owner_userid,
      }),
      { timeoutMs: PANEL_TIMEOUT_MS.workbench }
    );
    if (state.external_userid !== requestedExternalUserid) {
      writeDebug("stale workbench response discarded", {
        requested_external_userid: requestedExternalUserid,
        current_external_userid: state.external_userid,
      });
      return;
    }
    writeDebug("workbench response", payload);
    state.workbench = payload;
    const customer = payload.customer || {};
    setExternalUserid(customer.external_userid || state.external_userid);
    state.owner_userid = customer.owner_userid || state.owner_userid;
    if (!state.bind_by_userid) state.bind_by_userid = state.owner_userid;
    setWorkbenchState(payload.diagnostics && payload.diagnostics.context_source_status === "error" ? WORKBENCH_STATES.degraded_ready : WORKBENCH_STATES.ready, payload.diagnostics || {});
    renderTop();
    renderTabs();
    renderActiveTab();
  }

  async function loadTabData(tab) {
    if (tab === "profile") {
      if (state.profileView === "timeline") await loadTimeline({ reset: true, force: true });
      return;
    }
    if (tab === "orders") {
      await loadOrders(state.orderType);
      return;
    }
    if (state.loaded[tab]) return;
    if (tab === "questionnaires") {
      const payload = await requestPanelJson("questionnaires", queryUrl(endpoint("questionnairesUrl"), customerContextQuery()));
      state.data.questionnaires = payload.questionnaires || [];
    } else if (tab === "products") {
      const payload = await requestPanelJson("products", queryUrl(endpoint("productsUrl"), customerContextQuery()));
      writeDebug("products response", productContextDiagnostics(payload));
      state.data.products = payload.products || [];
      state.data.service_period_products = payload.service_period_products || [];
    } else if (tab === "coupons") {
      const payload = await requestPanelJson("coupons", endpoint("couponsUrl"));
      state.data.coupons = payload.items || [];
    } else if (tab === "materials") {
      await loadMaterials(state.materialType);
    } else if (tab === "other_staff_messages") {
      if (((state.workbench || {}).customer || {}).owner_pending) {
        state.data.other_staff_messages = [];
        state.loaded[tab] = true;
        return;
      }
      const payload = await requestPanelJson(
        "other_staff_messages",
        queryUrl(endpoint("otherStaffMessagesUrl"), {
          external_userid: state.external_userid,
          current_userid: state.bind_by_userid || state.owner_userid,
          limit: 20,
        })
      );
      state.data.other_staff_messages = (payload.messages || [])
        .filter((item) => item && (item.type === "text" || item.type === "image"))
        .slice(-20);
    }
    state.loaded[tab] = true;
  }

  async function loadOrders(type) {
    const normalized = type === "periodic" ? "periodic" : "regular";
    const cacheKey = "orders:" + normalized;
    if (state.loaded[cacheKey]) return;
    const panelKey = normalized === "periodic" ? "periodic_orders" : "orders";
    const url = normalized === "periodic" ? endpoint("periodicOrdersUrl") : endpoint("ordersUrl");
    const payload = await requestPanelJson(panelKey, queryUrl(url, customerContextQuery()));
    if (payload.customer) {
      state.workbench.customer = Object.assign({}, state.workbench.customer || {}, payload.customer);
      renderTop();
    }
    if (normalized === "periodic") {
      writeDebug("periodic orders response", payload.diagnostics || {});
      state.data.periodic_orders = payload.periodic_orders || [];
    } else {
      writeDebug("orders response", payload.diagnostics || {});
      state.data.orders = payload.orders || [];
    }
    state.loaded[cacheKey] = true;
  }

  async function loadMaterials(type) {
    if (type === "radar") {
      if (state.data.radar_links) return;
      const payload = await requestPanelJson("radar_links", endpoint("radarLinksUrl"));
      state.data.radar_links = payload.items || [];
      return;
    }
    const query = String(state.materialQuery || "").trim();
    const resultKey = materialResultKey(query);
    if (state.materialSearchController) state.materialSearchController.abort();
    const requestVersion = ++state.materialRequestVersion;
    state.materialSearchController = null;
    if (Object.prototype.hasOwnProperty.call(state.data.materials, resultKey)) return true;
    state.materialSearchController = typeof AbortController !== "undefined" ? new AbortController() : null;
    try {
      const payload = await requestPanelJson(
        "materials",
        queryUrl(endpoint("materialsUrl"), { type: "image", limit: 5, offset: 0, q: query }),
        state.materialSearchController ? { signal: state.materialSearchController.signal } : undefined
      );
      if (requestVersion !== state.materialRequestVersion) return false;
      state.data.materials[resultKey] = {
        items: payload.materials || [],
        total: Number(payload.total || 0),
        has_more: Boolean(payload.has_more),
        next_offset: Number(payload.next_offset == null ? (payload.materials || []).length : payload.next_offset),
      };
      state.materialQuickKeywords = payload.quick_keywords || [];
      return true;
    } catch (error) {
      if (error && error.stage === "request_cancelled") return false;
      throw error;
    } finally {
      if (requestVersion === state.materialRequestVersion) state.materialSearchController = null;
    }
  }

  async function executeMaterialSearch(query, options) {
    if (state.materialType !== "image") return;
    destroyMaterialResources();
    state.materialQuery = String(query || "").trim().slice(0, 100);
    const requestedQuery = state.materialQuery;
    const resultKey = materialResultKey(state.materialQuery);
    if (options && options.force) {
      delete state.data.materials[resultKey];
      clearPanelCache("materials");
    }
    content.innerHTML = panel("", materialTypeControls() + materialSearchControls() + '<div class="status">正在搜索图片素材…</div>');
    try {
      const applied = await loadMaterials("image");
      if (!applied || requestedQuery !== state.materialQuery || state.activeTab !== "materials" || state.materialType !== "image") return;
      renderMaterials();
    } catch (error) {
      if (state.activeTab !== "materials" || state.materialType !== "image") return;
      renderMaterialLoadError("image", error);
    }
  }

  async function switchMaterialType(type) {
    if (!materialTabs.some((item) => item[0] === type)) return;
    state.materialType = type;
    destroyMaterialResources();
    if (state.activeTab !== "materials") return;
    setPanelLoading("素材");
    try {
      await loadMaterials(type);
      if (state.activeTab !== "materials" || state.materialType !== type) return;
      renderMaterials();
    } catch (error) {
      if (state.activeTab !== "materials" || state.materialType !== type) return;
      renderMaterialLoadError(type, error);
    }
  }

  async function switchOrderType(type) {
    if (!orderTabs.some((item) => item[0] === type)) return;
    state.orderType = type;
    if (state.activeTab !== "orders") return;
    setPanelLoading("订单");
    try {
      await loadOrders(type);
      if (state.activeTab !== "orders" || state.orderType !== type) return;
      renderOrders();
    } catch (error) {
      if (state.activeTab !== "orders" || state.orderType !== type) return;
      content.innerHTML = panel(
        "",
        orderTypeControls() + '<div class="status error">' + escapeHtml(error.message || "加载失败") + "</div>" +
          '<div class="row-actions"><button class="btn primary" type="button" data-retry-order-type="' + escapeHtml(type) + '">重试</button></div>'
      );
    }
  }

  async function loadTimeline(options) {
    const reset = Boolean(options && options.reset);
    const force = Boolean(options && options.force);
    const offset = reset ? 0 : Number((state.data.timeline || {}).next_offset || 0);
    const requestVersion = reset ? ++state.timelineRequestVersion : state.timelineRequestVersion;
    const url = queryUrl(endpoint("timelineUrl"), { limit: 20, offset });
    if (force) clearPanelCache("timeline");
    const payload = await requestPanelJson("timeline", url);
    if (requestVersion !== state.timelineRequestVersion) return;
    const current = reset ? [] : (state.data.timeline.items || []);
    state.data.timeline = {
      items: current.concat(payload.items || []),
      total: Number(payload.total || 0),
      has_more: Boolean(payload.has_more),
      next_offset: Number(payload.next_offset || 0),
    };
  }

  async function switchProfileView(view) {
    if (!profileTabs.some((item) => item[0] === view)) return;
    state.profileView = view;
    if (state.activeTab !== "profile") return;
    if (view === "basic") {
      renderProfile();
      return;
    }
    setPanelLoading("用户时间线");
    try {
      await loadTimeline({ reset: true, force: true });
      if (state.activeTab !== "profile" || state.profileView !== "timeline") return;
      renderProfileTimeline();
    } catch (error) {
      if (state.activeTab !== "profile" || state.profileView !== "timeline") return;
      content.innerHTML = panel(
        "",
        profileTypeControls() + '<div class="status error">' + escapeHtml(error.message || "时间线加载失败") + "</div>" +
          '<div class="row-actions"><button class="btn primary" type="button" data-refresh-timeline>重试</button></div>'
      );
    }
  }

  async function refreshTimeline() {
    if (state.activeTab !== "profile" || state.profileView !== "timeline") return;
    setPanelLoading("用户时间线");
    await loadTimeline({ reset: true, force: true });
    if (state.activeTab === "profile" && state.profileView === "timeline") renderProfileTimeline();
  }

  async function loadMoreTimeline() {
    if (!state.data.timeline.has_more) return;
    await loadTimeline({ reset: false, force: false });
    if (state.activeTab === "profile" && state.profileView === "timeline") renderProfileTimeline();
  }

  function renderActiveTab() {
    if (state.activeTab === "profile") renderProfile();
    if (state.activeTab === "questionnaires") renderQuestionnaires();
    if (state.activeTab === "products") renderProducts();
    if (state.activeTab === "orders") renderOrders();
    if (state.activeTab === "coupons") renderCoupons();
    if (state.activeTab === "materials") renderMaterials();
    if (state.activeTab === "other_staff_messages") renderOtherStaffMessages();
  }

  async function switchTab(tab) {
    if (tab !== "profile" && !isWorkbenchReady()) return;
    if (state.activeTab === "materials" && tab !== "materials") destroyMaterialResources();
    state.activeTab = tab;
    renderTabs();
    const label = tabs.find((item) => item[0] === tab)?.[1] || "";
    const materialType = tab === "materials" ? state.materialType : "";
    const orderType = tab === "orders" ? state.orderType : "";
    const profileView = tab === "profile" ? state.profileView : "";
    setPanelLoading(label);
    try {
      await loadTabData(tab);
      if (
        state.activeTab !== tab ||
        (tab === "materials" && state.materialType !== materialType) ||
        (tab === "orders" && state.orderType !== orderType) ||
        (tab === "profile" && state.profileView !== profileView)
      ) return;
      renderActiveTab();
    } catch (error) {
      if (
        state.activeTab !== tab ||
        (tab === "materials" && state.materialType !== materialType) ||
        (tab === "orders" && state.orderType !== orderType) ||
        (tab === "profile" && state.profileView !== profileView)
      ) return;
      content.innerHTML = panel(
        label,
        '<div class="status error">' + escapeHtml(error.message || "加载失败") + "</div>" +
          '<div class="row-actions"><button class="btn primary" type="button" data-retry-tab="' + escapeHtml(tab) + '">重试</button></div>'
      );
    }
  }

  async function openTimelineSource(item) {
    const action = (item && item.source_action) || {};
    if (action.kind === "order_detail") {
      return openOrderDetail(action.detail_url);
    }
    if (action.kind !== "questionnaire_submission") return false;
    const submissionId = String(action.submission_id || "").trim();
    if (!submissionId) {
      showToast("未找到对应问卷原记录", "error");
      return false;
    }
    await switchTab("questionnaires");
    const questionnaireIndex = (state.data.questionnaires || []).findIndex((questionnaire) => (
      String(questionnaire.submission_id || "").trim() === submissionId
    ));
    if (questionnaireIndex < 0) {
      showToast("未找到对应问卷原记录", "error");
      return false;
    }
    const card = content.querySelector('[data-questionnaire-card="' + questionnaireIndex + '"]');
    if (!card) {
      showToast("未找到对应问卷原记录", "error");
      return false;
    }
    card.classList.add("open", "timeline-source-highlight");
    if (typeof card.scrollIntoView === "function") card.scrollIntoView({ behavior: "smooth", block: "start" });
    if (typeof card.focus === "function") card.focus({ preventScroll: true });
    window.setTimeout(() => card.classList.remove("timeline-source-highlight"), 1600);
    return true;
  }

  async function sendMaterial(materialId) {
    if (state.materialType !== "image") {
      showToast("雷达链接请使用复制链接", "error");
      return;
    }
    try {
      const payload = await requestJson(endpoint("materialSendUrl"), {
        method: "POST",
        body: JSON.stringify({
          external_userid: state.external_userid,
          owner_userid: state.owner_userid,
          type: "image",
          material_id: materialId,
          operator: state.bind_by_userid || state.owner_userid || "",
          delivery_mode: "chat_toolbar",
        }),
      });
      if (!payload.media_id) throw new Error("图片素材未取得 media_id");
      await sendImageToCurrentChat(payload.media_id);
      showToast("已发送到当前会话");
    } catch (error) {
      showToast(error.message || "发送失败", "error");
    }
  }

  function fallbackCopyText(value) {
    const textarea = document.createElement("textarea");
    textarea.value = value;
    textarea.setAttribute("readonly", "");
    textarea.style.position = "fixed";
    textarea.style.opacity = "0";
    textarea.style.pointerEvents = "none";
    document.body.appendChild(textarea);
    textarea.focus();
    textarea.select();
    let copied = false;
    try {
      copied = Boolean(document.execCommand && document.execCommand("copy"));
    } finally {
      document.body.removeChild(textarea);
    }
    return copied;
  }

  async function copyLink(value) {
    const link = absoluteUrl(value);
    if (!link) {
      showToast("暂无可复制链接", "error");
      return false;
    }
    try {
      const clipboard = window.navigator && window.navigator.clipboard;
      if (clipboard && typeof clipboard.writeText === "function") {
        await clipboard.writeText(link);
      } else if (!fallbackCopyText(link)) {
        throw new Error("copy unavailable");
      }
      showToast("已复制");
      return true;
    } catch (_error) {
      if (fallbackCopyText(link)) {
        showToast("已复制");
        return true;
      }
      showToast("复制失败，请长按链接复制", "error");
      return false;
    }
  }

  async function sendProduct(productIndex, kind) {
    const rows = kind === "service_period" ? state.data.service_period_products || [] : state.data.products || [];
    const item = rows[Number(productIndex)] || {};
    const fallbackPath = kind === "service_period" ? "" : item.id ? "/p/" + item.id : "";
    const link = absoluteUrl(item.product_url || fallbackPath);
    if (!link) {
      showToast("暂无商品链接", "error");
      return;
    }
    try {
      await sendLinkToCurrentChat({
        title: item.title || "未命名商品",
        url: link,
        imageUrl: absoluteUrl(PRODUCT_CARD_IMAGE_PATH),
      });
      showToast("已发送商品");
    } catch (error) {
      showToast(error.message || "发送失败", "error");
    }
  }

  function assertWeComSendOk(res) {
    const errMsg = String((res || {}).err_msg || "");
    if (!errMsg || errMsg.indexOf(":ok") >= 0) return;
    throw new Error(String((res || {}).errmsg || errMsg || "发送失败"));
  }

  function weComSendUnavailableMessage(sdkReady) {
    const reason = String((sdkReady || {}).reason || "");
    if (reason === "jssdk_config_failed" && sdkReady && sdkReady.error) return String(sdkReady.error);
    if (reason === "wx_config_failed" || reason === "agentConfig_failed") {
      return "企微侧边栏授权失败，请关闭后重新打开";
    }
    if (reason === "sdk_timeout") return "企微侧边栏授权超时，请重试";
    if (reason === "wx_missing" || reason === "agentConfig_missing" || reason === "wx_invoke_missing") {
      return "企微发送能力未加载，请从企微客户侧边栏重新打开";
    }
    return "企微发送能力暂不可用，请重试";
  }

  async function sendLinkToCurrentChat(payload) {
    const sdkReady = await initWeComSdk();
    if (!sdkReady.ok || !window.wx || typeof window.wx.invoke !== "function") {
      throw new Error(weComSendUnavailableMessage(
        sdkReady.ok ? { ok: false, reason: "wx_invoke_missing" } : sdkReady
      ));
    }
    const res = await invokeWeCom("sendChatMessage", {
      msgtype: "news",
      news: {
        link: String(payload.url || ""),
        title: String(payload.title || "未命名商品"),
        desc: "",
        imgUrl: String(payload.imageUrl || ""),
      },
    }, SDK_TIMEOUT_MS);
    writeDebug("sendChatMessage news result", res || {});
    assertWeComSendOk(res);
    return res;
  }

  async function sendImageToCurrentChat(mediaId) {
    const sdkReady = await initWeComSdk();
    if (!sdkReady.ok || !window.wx || typeof window.wx.invoke !== "function") {
      throw new Error(weComSendUnavailableMessage(
        sdkReady.ok ? { ok: false, reason: "wx_invoke_missing" } : sdkReady
      ));
    }
    const res = await invokeWeCom("sendChatMessage", {
      msgtype: "image",
      image: { mediaid: mediaId },
    }, SDK_TIMEOUT_MS);
    writeDebug("sendChatMessage result", res || {});
    assertWeComSendOk(res);
    return res;
  }

  function openMobileModal() {
    mobileInput.value = ((state.workbench || {}).customer || {}).mobile || "";
    mobileStatus.textContent = "";
    mobileStatus.className = "status";
    mobileModal.classList.remove("hidden");
    mobileInput.focus();
  }

  function closeMobileModal() {
    mobileModal.classList.add("hidden");
  }

  async function saveMobile() {
    confirmMobileButton.disabled = true;
    mobileStatus.textContent = "正在保存…";
    try {
      const customer = (state.workbench || {}).customer || {};
      if (customer.owner_pending) {
        throw new Error("请先从企微侧边栏重新打开以确认当前员工身份");
      }
      const payload = await requestJson(endpoint("bindMobileUrl"), {
        method: "POST",
        body: JSON.stringify({
          external_userid: state.external_userid,
          owner_userid: state.owner_userid,
          bind_by_userid: state.bind_by_userid || state.owner_userid,
          mobile: mobileInput.value,
          force_rebind: Boolean(customer.mobile_bound !== undefined ? customer.mobile_bound : customer.is_bound && customer.mobile),
        }),
      });
      const binding = payload.binding || payload;
      state.workbench.customer.mobile = binding.mobile || mobileInput.value;
      state.workbench.customer.is_bound = true;
      state.workbench.customer.mobile_bound = true;
      renderTop();
      closeMobileModal();
      showToast("手机号已保存");
    } catch (error) {
      mobileStatus.textContent = error.message || "保存失败";
      mobileStatus.className = "status error";
      showToast(error.message || "保存失败", "error");
    } finally {
      confirmMobileButton.disabled = false;
    }
  }

  async function resolveContextFromQuery() {
    setExternalUserid(firstQueryValue(["external_userid", "externalUserid", "externalUserId", "user_id", "userId"]));
    writeDebug("query context", {
      has_external_userid: Boolean(state.external_userid),
      has_owner_token: Boolean(state.sidebar_owner_token),
    });
    return Boolean(state.external_userid);
  }

  async function initWeComSdk(options) {
    if (state.weComSdkReady) return state.weComSdkReady;
    if (state.weComSdkInitPromise) return state.weComSdkInitPromise;
    if (!window.wx) return { ok: false, status: WORKBENCH_STATES.sdk_unavailable, reason: "wx_missing" };
    const useStartupBudget = Boolean(options && options.useStartupBudget);
    const timeoutForStage = () => useStartupBudget ? remainingStartupBudget(SDK_TIMEOUT_MS) : SDK_TIMEOUT_MS;
    const initialize = async () => {
      let configPayload;
      try {
        configPayload = await requestJssdkConfig(timeoutForStage());
        applySidebarOwnerToken(configPayload);
        writeDebug("jssdk config response", {
          has_config: Boolean(configPayload && configPayload.config),
          has_agent_config: Boolean(configPayload && configPayload.agent_config),
          owner_token_status: state.sidebar_owner_token_status,
        });
      } catch (error) {
        writeDebug("jssdk config error", { message: error.message || String(error) });
        return { ok: false, status: WORKBENCH_STATES.sdk_unavailable, reason: "jssdk_config_failed", error: error.message || String(error) };
      }
      return await new Promise((resolve) => {
        let resolved = false;
        const timer = window.setTimeout(() => finish(false, "sdk_timeout"), timeoutForStage());
        const finish = (ok, reason) => {
          if (!resolved) {
            resolved = true;
            window.clearTimeout(timer);
            resolve({ ok, status: ok ? WORKBENCH_STATES.identifying_customer : WORKBENCH_STATES.sdk_unavailable, reason: reason || "" });
          }
        };
        window.wx.config({
          beta: true,
          debug: false,
          appId: configPayload.corp_id,
          timestamp: Number(configPayload.config.timestamp),
          nonceStr: configPayload.config.nonceStr,
          signature: configPayload.config.signature,
          jsApiList: ["sendChatMessage"],
        });
        window.wx.ready(function () {
          writeDebug("wx.config success", { url: configPayload.config.url });
          if (typeof window.wx.agentConfig !== "function") {
            finish(false, "agentConfig_missing");
            return;
          }
          window.wx.agentConfig({
            corpid: configPayload.corp_id,
            agentid: String(configPayload.agent_id),
            timestamp: Number(configPayload.agent_config.timestamp),
            nonceStr: configPayload.agent_config.nonceStr,
            signature: configPayload.agent_config.signature,
            jsApiList: ["getCurExternalContact", "sendChatMessage"],
            success: function (res) {
              writeDebug("wx.agentConfig success", res || {});
              applyWeComViewerIdentity(res || {}, "agentConfig", { allowUserId: true });
              finish(true, "");
            },
            fail: function (err) {
              writeDebug("wx.agentConfig fail", err || {});
              finish(false, "agentConfig_failed");
            },
          });
        });
        window.wx.error(function (err) {
          writeDebug("wx.config fail", err || {});
          finish(false, "wx_config_failed");
        });
      });
    };
    state.weComSdkInitPromise = initialize();
    try {
      const result = await state.weComSdkInitPromise;
      if (result.ok) state.weComSdkReady = result;
      return result;
    } finally {
      state.weComSdkInitPromise = null;
    }
  }

  function invokeWeCom(method, payload, timeoutMs) {
    return new Promise((resolve, reject) => {
      if (!window.wx || typeof window.wx.invoke !== "function") {
        reject(new Error("wx.invoke unavailable"));
        return;
      }
      let resolved = false;
      const timer = window.setTimeout(() => {
        if (resolved) return;
        resolved = true;
        const error = new Error(method + " timeout");
        error.stage = method;
        reject(error);
      }, timeoutMs || SDK_TIMEOUT_MS);
      window.wx.invoke(method, payload || {}, function (res) {
        if (resolved) return;
        resolved = true;
        window.clearTimeout(timer);
        resolve(res || {});
      });
    });
  }

  async function resolveContextFromWeCom() {
    const sdkReady = await initWeComSdk({ useStartupBudget: true });
    if (!sdkReady.ok || !window.wx || typeof window.wx.invoke !== "function") return sdkReady;
    try {
      const res = await invokeWeCom("getCurExternalContact", {}, remainingStartupBudget(SDK_TIMEOUT_MS));
      writeDebug("getCurExternalContact result", res || {});
      const externalUserid = extractWeComExternalUserid(res || {});
      if (!externalUserid) {
        return { ok: false, status: WORKBENCH_STATES.context_missing, reason: "external_userid_missing" };
      }
      setExternalUserid(externalUserid);
      applyWeComViewerIdentity(res || {}, "getCurExternalContact");
      if (!state.bind_by_userid) state.bind_by_userid = state.owner_userid;
      try {
        const ownerContextPayload = await requestJssdkConfig(remainingStartupBudget(SDK_TIMEOUT_MS), { force: true });
        applySidebarOwnerToken(ownerContextPayload, state.external_userid);
      } catch (error) {
        writeDebug("customer-aware jssdk config failed", { message: error.message || String(error) });
      }
      writeDebug("getCurExternalContact success", {
        external_userid: state.external_userid,
        owner_userid: state.owner_userid,
        bind_by_userid: state.bind_by_userid,
      });
      return { ok: true, status: WORKBENCH_STATES.identifying_customer };
    } catch (error) {
      writeDebug("getCurExternalContact error", { message: error.message || String(error), stage: error.stage || "" });
      return { ok: false, status: WORKBENCH_STATES.context_missing, reason: error.stage || "getCurExternalContact_failed", error: error.message || String(error) };
    }
  }

  async function boot(options) {
    const forceSidebarOAuth = Boolean(options && options.forceSidebarOAuth);
    cleanupSidebarOAuthUrl();
    state.bootDeadline = Date.now() + STARTUP_BUDGET_MS;
    setWorkbenchState(WORKBENCH_STATES.identifying_customer);
    renderTabs();
    setPanelLoading("");
    try {
      const hasQuery = await resolveContextFromQuery();
      let contextResult = hasQuery ? { ok: true, status: WORKBENCH_STATES.identifying_customer, source: "query" } : await resolveContextFromWeCom();
      if (hasQuery && !state.sidebar_owner_token && !state.owner_userid) {
        const sdkContext = await resolveContextFromWeCom();
        if (sdkContext.ok) contextResult = sdkContext;
      }
      writeDebug("identity result", contextResult);
      if (!contextResult.ok) {
        if (!state.sidebar_owner_token && state.external_userid && await maybeStartSidebarOAuth(contextResult.reason || "context_not_ready", { force: forceSidebarOAuth })) return;
        setWorkbenchState(contextResult.status || WORKBENCH_STATES.context_missing, contextResult);
        renderRetryPanel("", contextResult.status === WORKBENCH_STATES.sdk_unavailable ? "企微 SDK 暂不可用，请确认从企微侧边栏打开，或带 external_userid 参数重试。" : "未识别到客户，请从企微客户侧边栏重新打开。");
        return;
      }
      if (!state.sidebar_owner_token) {
        if (state.sidebar_owner_token_status === "provisioning") {
          renderProvisioning();
          scheduleProvisioningRetry(2);
          return;
        }
        if (await maybeStartSidebarOAuth("owner_token_missing", { force: forceSidebarOAuth })) return;
        setWorkbenchState(WORKBENCH_STATES.error, { message: "sidebar authorization required" });
        renderRetryPanel("", "侧边栏授权未完成，请点击重试重新授权。");
        return;
      }
      await loadWorkbench();
    } catch (error) {
      writeDebug("boot error", { message: error.message || String(error), stage: error.stage || "" });
      if (String(error.message || "").indexOf("owner_userid is required") >= 0) {
        renderOwnerPendingWorkbench(error.message || "owner_userid is required");
        return;
      }
      setWorkbenchState(WORKBENCH_STATES.error, { message: error.message || String(error), stage: error.stage || "" });
      renderRetryPanel("", error.message || "加载失败，请稍后重试。");
    }
  }

  tabsNode.addEventListener("click", (event) => {
    const button = event.target.closest("[data-tab]");
    if (!button || button.disabled) return;
    switchTab(button.dataset.tab);
  });

  content.addEventListener("change", (event) => {
    const field = event.target.dataset.profileField;
    if (!field || event.target.tagName === "TEXTAREA") return;
    updateProfileField(field, event.target.value);
    saveProfile();
  });

  content.addEventListener("input", (event) => {
    const periodicOrderId = event.target.dataset.periodicOrderRemark;
    if (periodicOrderId && event.target.tagName === "TEXTAREA") {
      updatePeriodicOrderRemark(periodicOrderId, event.target.value);
      savePeriodicOrderRemarkSoon(periodicOrderId);
      return;
    }
    const field = event.target.dataset.profileField;
    if (!field || event.target.tagName !== "TEXTAREA") return;
    updateProfileField(field, event.target.value);
    saveProfileSoon();
  });

  content.addEventListener("blur", (event) => {
    const periodicOrderId = event.target.dataset.periodicOrderRemark;
    if (periodicOrderId && event.target.tagName === "TEXTAREA") {
      updatePeriodicOrderRemark(periodicOrderId, event.target.value);
      savePeriodicOrderRemark(periodicOrderId);
      return;
    }
    const field = event.target.dataset.profileField;
    if (!field || event.target.tagName !== "TEXTAREA") return;
    updateProfileField(field, event.target.value);
    saveProfile();
  }, true);

  content.addEventListener("submit", async (event) => {
    const form = event.target.closest ? event.target.closest("[data-material-search-form]") : null;
    if (!form) return;
    event.preventDefault();
    const input = form.querySelector("[data-material-search-input]");
    await executeMaterialSearch(input ? input.value : "");
  });

  content.addEventListener("click", async (event) => {
    const retryButton = event.target.closest("[data-retry-boot]");
    if (retryButton) {
      retryButton.disabled = true;
      if (state.sidebar_owner_token_status === "provisioning") {
        window.clearTimeout(state.provisioning_retry_timer);
        state.provisioning_retry_timer = null;
        state.provisioning_retry_attempts = 0;
      }
      boot({ forceSidebarOAuth: true });
      return;
    }
    const retryTabButton = event.target.closest("[data-retry-tab]");
    if (retryTabButton) {
      retryTabButton.disabled = true;
      await switchTab(retryTabButton.dataset.retryTab);
      return;
    }
    const retryMaterialTypeButton = event.target.closest("[data-retry-material-type]");
    if (retryMaterialTypeButton) {
      retryMaterialTypeButton.disabled = true;
      await switchMaterialType(retryMaterialTypeButton.dataset.retryMaterialType);
      return;
    }
    const retryOrderTypeButton = event.target.closest("[data-retry-order-type]");
    if (retryOrderTypeButton) {
      retryOrderTypeButton.disabled = true;
      await switchOrderType(retryOrderTypeButton.dataset.retryOrderType);
      return;
    }
    const profileViewButton = event.target.closest("[data-profile-view]");
    if (profileViewButton) {
      await switchProfileView(profileViewButton.dataset.profileView);
      return;
    }
    const refreshTimelineButton = event.target.closest("[data-refresh-timeline]");
    if (refreshTimelineButton) {
      refreshTimelineButton.disabled = true;
      try {
        await refreshTimeline();
      } catch (error) {
        showToast(error.message || "时间线刷新失败", "error");
        if (state.activeTab === "profile" && state.profileView === "timeline") renderProfileTimeline();
      }
      return;
    }
    const loadMoreTimelineButton = event.target.closest("[data-load-more-timeline]");
    if (loadMoreTimelineButton) {
      loadMoreTimelineButton.disabled = true;
      try {
        await loadMoreTimeline();
      } catch (error) {
        showToast(error.message || "加载更多失败", "error");
        loadMoreTimelineButton.disabled = false;
      }
      return;
    }
    const timelineSourceButton = event.target.closest("[data-timeline-source]");
    if (timelineSourceButton) {
      const item = (state.data.timeline.items || [])[Number(timelineSourceButton.dataset.timelineSource)];
      if (!item) return;
      timelineSourceButton.disabled = true;
      try {
        await openTimelineSource(item);
      } finally {
        timelineSourceButton.disabled = false;
      }
      return;
    }
    const qButton = event.target.closest("[data-toggle-questionnaire]");
    if (qButton) {
      const card = content.querySelector('[data-questionnaire-card="' + qButton.dataset.toggleQuestionnaire + '"]');
      if (card) card.classList.toggle("open");
      return;
    }
    const materialTypeButton = event.target.closest("[data-material-type]");
    if (materialTypeButton) {
      await switchMaterialType(materialTypeButton.dataset.materialType);
      return;
    }
    const materialKeywordButton = event.target.closest("[data-material-keyword]");
    if (materialKeywordButton) {
      await executeMaterialSearch(materialKeywordButton.dataset.materialKeyword || "");
      return;
    }
    const materialSearchClearButton = event.target.closest("[data-material-search-clear]");
    if (materialSearchClearButton) {
      await executeMaterialSearch("");
      return;
    }
    const materialThumbRetryButton = event.target.closest("[data-material-thumb-retry]");
    if (materialThumbRetryButton) {
      const card = materialThumbRetryButton.closest("[data-material-card]");
      loadMaterialThumbnail(card);
      return;
    }
    const orderTypeButton = event.target.closest("[data-order-type]");
    if (orderTypeButton) {
      await switchOrderType(orderTypeButton.dataset.orderType);
      return;
    }
    const copyButton = event.target.closest("[data-copy-url]");
    if (copyButton) {
      copyButton.disabled = true;
      try {
        await copyLink(copyButton.dataset.copyUrl);
      } finally {
        copyButton.disabled = false;
      }
      return;
    }
    const materialSendButton = event.target.closest("[data-material-send]");
    if (materialSendButton) {
      materialSendButton.disabled = true;
      try {
        await sendMaterial(materialSendButton.dataset.materialSend);
      } finally {
        materialSendButton.disabled = false;
      }
      return;
    }
    const productTypeButton = event.target.closest("[data-product-type]");
    if (productTypeButton) {
      state.productType = productTypeButton.dataset.productType || "regular";
      renderProducts();
      return;
    }
    const productSendButton = event.target.closest("[data-product-send]");
    if (productSendButton) {
      productSendButton.disabled = true;
      try {
        await sendProduct(productSendButton.dataset.productSend, productSendButton.dataset.productKind || state.productType);
      } finally {
        productSendButton.disabled = false;
      }
      return;
    }
    const orderDetailButton = event.target.closest("[data-order-detail-url]");
    if (orderDetailButton) {
      openOrderDetail(orderDetailButton.dataset.orderDetailUrl);
      return;
    }
  });

  content.addEventListener("error", (event) => {
    const image = event.target.closest ? event.target.closest("[data-material-thumb-img]") : null;
    if (!image) return;
    const parent = image.parentElement;
    if (!parent) return;
    parent.textContent = "预览不可用";
    parent.classList.add("preview-unavailable");
  }, true);

  document.getElementById("change-mobile-button").addEventListener("click", openMobileModal);
  document.getElementById("close-mobile-modal").addEventListener("click", closeMobileModal);
  document.getElementById("cancel-mobile-button").addEventListener("click", closeMobileModal);
  confirmMobileButton.addEventListener("click", saveMobile);

  boot();
})();

(function () {
  "use strict";

  const root = document.querySelector("[data-wecom-tags-page]");
  if (!root) return;

  const PAGE_SIZE = 20;
  const state = {
    groups: [],
    totalTags: 0,
    tagLimit: 1000,
    syncedAt: "",
    query: "",
    selectedGroupKey: "",
    page: 1,
    busy: false,
  };

  const els = {
    query: root.querySelector('[data-role="query"]'),
    groupList: root.querySelector('[data-role="group-list"]'),
    groupSummary: root.querySelector('[data-role="group-summary"]'),
    currentGroupName: root.querySelector('[data-role="current-group-name"]'),
    currentGroupMeta: root.querySelector('[data-role="current-group-meta"]'),
    tagRows: root.querySelector('[data-role="tag-rows"]'),
    empty: root.querySelector('[data-role="empty"]'),
    pager: root.querySelector('[data-role="pager"]'),
    pagerText: root.querySelector('[data-role="pager-text"]'),
    feedback: root.querySelector('[data-role="feedback"]'),
    capacityText: root.querySelector('[data-role="capacity-text"]'),
    capacityBar: root.querySelector('[data-role="capacity-bar"]'),
    capacityWarning: root.querySelector('[data-role="capacity-warning"]'),
    modal: root.querySelector('[data-role="modal"]'),
    modalTitle: root.querySelector('[data-role="modal-title"]'),
    modalBody: root.querySelector('[data-role="modal-body"]'),
    createTagButton: root.querySelector('[data-action="create-tag"]'),
    editGroupButton: root.querySelector('[data-action="edit-group"]'),
    deleteGroupButton: root.querySelector('[data-action="delete-group"]'),
  };

  function apiTags() {
    return root.dataset.apiTags || "/api/admin/wecom/tags";
  }

  function apiGroups() {
    return root.dataset.apiGroups || "/api/admin/wecom/tag-groups";
  }

  function apiSync() {
    return root.dataset.apiSync || "/api/admin/wecom/tags/sync";
  }

  function escapeHtml(value) {
    return String(value || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function normalized(value) {
    return String(value || "").trim();
  }

  function validateCatalogGroups(payload) {
    const groups = payload && payload.groups;
    if (!Array.isArray(groups)) throw new Error("企微标签数据格式异常，请刷新后重试。");
    for (const group of groups) {
      if (!group || typeof group !== "object"
        || !normalized(group.group_id)
        || !normalized(group.group_name)
        || !Array.isArray(group.tags)) {
        throw new Error("企微标签数据格式异常，请刷新后重试。");
      }
      for (const tag of group.tags) {
        if (!tag || typeof tag !== "object"
          || !normalized(tag.tag_id)
          || !normalized(tag.tag_name)) {
          throw new Error("企微标签数据格式异常，请刷新后重试。");
        }
      }
    }
    return groups;
  }

  function formatSyncedAt(value) {
    const text = normalized(value);
    if (!text) return "-";
    return text.replace("T", " ").slice(0, 16);
  }

  function groupKey(group) {
    return normalized(group.group_key) || normalized(group.group_id) || `group-name:${normalized(group.group_name)}`;
  }

  function setFeedback(message, kind) {
    if (!els.feedback) return;
    const text = normalized(message);
    els.feedback.hidden = !text;
    els.feedback.textContent = text;
    els.feedback.className = "wecom-tags-feedback admin-alert";
    if (text) {
      els.feedback.classList.add(kind === "error" ? "admin-alert--error" : "admin-alert--success");
    }
  }

  function friendlyError(error, fallback) {
    const text = normalized(error && error.message);
    return text || fallback;
  }

  function idempotencyKey(action) {
    return `wecom-tags-${action}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  }

  function writeOptions(method, body, action) {
    const options = {
      method,
      headers: { "Idempotency-Key": idempotencyKey(action) },
    };
    if (body !== undefined) options.body = JSON.stringify(body);
    return options;
  }

  async function requestJson(url, options = {}, fallback) {
    const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
    let response;
    try {
      response = await fetch(url, {
        credentials: "same-origin",
        ...options,
        headers,
      });
    } catch (_error) {
      throw new Error(fallback);
    }
    const payload = await response.json().catch(() => ({}));
    if (!response.ok || payload.ok === false) {
      throw new Error(normalized(payload.error) || fallback);
    }
    return payload;
  }

  function groupMatches(group, query) {
    if (!query) return true;
    const groupName = normalized(group.group_name).toLowerCase();
    if (groupName.includes(query)) return true;
    return (group.tags || []).some((tag) => {
      const haystack = `${tag.tag_name || ""} ${tag.tag_id || ""}`.toLowerCase();
      return haystack.includes(query);
    });
  }

  function filteredGroups() {
    const query = normalized(state.query).toLowerCase();
    return state.groups.filter((group) => groupMatches(group, query));
  }

  function selectedGroup() {
    return state.groups.find((group) => groupKey(group) === state.selectedGroupKey) || null;
  }

  function filteredTags(group) {
    if (!group) return [];
    const query = normalized(state.query).toLowerCase();
    const tags = Array.isArray(group.tags) ? group.tags : [];
    if (!query) return tags;
    if (normalized(group.group_name).toLowerCase().includes(query)) return tags;
    return tags.filter((tag) => {
      const haystack = `${tag.tag_name || ""} ${tag.tag_id || ""}`.toLowerCase();
      return haystack.includes(query);
    });
  }

  function ensureSelectedGroup() {
    const groups = filteredGroups();
    if (!groups.length) {
      state.selectedGroupKey = "";
      state.page = 1;
      return;
    }
    if (!groups.some((group) => groupKey(group) === state.selectedGroupKey)) {
      state.selectedGroupKey = groupKey(groups[0]);
      state.page = 1;
    }
  }

  function renderCapacity() {
    const limit = Math.max(1, Number(state.tagLimit || 1000));
    const total = Math.max(0, Number(state.totalTags || 0));
    const percent = Math.min(100, Math.round((total / limit) * 100));
    els.capacityText.textContent = `${total} / ${limit}`;
    els.capacityBar.querySelector("span").style.width = `${percent}%`;
    els.capacityBar.classList.toggle("is-warn", total >= 900 && total < limit);
    els.capacityBar.classList.toggle("is-danger", total >= limit);
    els.capacityWarning.hidden = true;
    els.capacityWarning.textContent = "";
    els.createTagButton.disabled = state.busy || total >= limit;
    if (total >= limit) {
      els.capacityWarning.textContent = "标签数量已达到 1000 上限，不能继续新增标签。";
      els.capacityWarning.hidden = false;
    } else if (total >= 900) {
      els.capacityWarning.textContent = "标签数量已接近 1000 上限，建议清理无效标签后再新增。";
      els.capacityWarning.hidden = false;
    }
  }

  function renderGroups() {
    const groups = filteredGroups();
    els.groupSummary.textContent = `共 ${groups.length} 个标签组`;
    if (!groups.length) {
      els.groupList.innerHTML = '<div class="wecom-tags-empty">没有匹配到标签</div>';
      return;
    }
    els.groupList.innerHTML = groups.map((group) => {
      const key = groupKey(group);
      const active = key === state.selectedGroupKey ? " is-active" : "";
      return `
        <button class="wecom-tags-group${active}" type="button" data-group-key="${escapeHtml(key)}">
          <span class="wecom-tags-group-row">
            <span class="wecom-tags-group-name">${escapeHtml(group.group_name || "未命名标签组")}</span>
            <span class="wecom-tags-group-count">${Number(group.tag_count || 0)}</span>
          </span>
        </button>
      `;
    }).join("");
  }

  function renderTags() {
    const group = selectedGroup();
    const tags = filteredTags(group);
    const total = tags.length;
    const maxPage = Math.max(1, Math.ceil(total / PAGE_SIZE));
    if (state.page > maxPage) state.page = maxPage;
    const start = (state.page - 1) * PAGE_SIZE;
    const rows = tags.slice(start, start + PAGE_SIZE);

    els.currentGroupName.textContent = group ? normalized(group.group_name) || "未命名标签组" : "标签组";
    els.currentGroupMeta.textContent = group ? `${total} 个标签；每页 ${PAGE_SIZE} 个` : "当前没有标签组";
    els.editGroupButton.disabled = state.busy || !group;
    els.deleteGroupButton.disabled = state.busy || !group;
    els.tagRows.innerHTML = rows.map((tag) => `
      <tr>
        <td><strong>${escapeHtml(tag.tag_name || "未命名标签")}</strong></td>
        <td>${Number(tag.usage_count || 0)}</td>
        <td>${escapeHtml(formatSyncedAt(tag.synced_at || state.syncedAt))}</td>
        <td>
          <div class="wecom-tags-table-actions">
            <button class="admin-button admin-button--ghost" type="button" data-tag-detail="${escapeHtml(tag.tag_id)}">详情</button>
            <button class="admin-button admin-button--ghost" type="button" data-tag-copy="${escapeHtml(tag.tag_id)}">复制 tag_id</button>
            <button class="admin-button admin-button--ghost" type="button" data-tag-edit="${escapeHtml(tag.tag_id)}">编辑</button>
            <button class="admin-button admin-button--ghost" type="button" data-tag-delete="${escapeHtml(tag.tag_id)}">删除</button>
          </div>
        </td>
      </tr>
    `).join("");
    els.empty.hidden = rows.length > 0 || total > 0;
    els.pagerText.textContent = `第 ${state.page} / ${maxPage} 页，共 ${total} 个`;
    root.querySelector('[data-action="prev-page"]').disabled = state.page <= 1;
    root.querySelector('[data-action="next-page"]').disabled = state.page >= maxPage;
  }

  function render() {
    ensureSelectedGroup();
    renderCapacity();
    renderGroups();
    renderTags();
  }

  function findTag(tagId) {
    const id = normalized(tagId);
    for (const group of state.groups) {
      const tag = (group.tags || []).find((item) => normalized(item.tag_id) === id);
      if (tag) return { group, tag };
    }
    return { group: null, tag: null };
  }

  function openModal(title, bodyHtml) {
    els.modalTitle.textContent = title;
    els.modalBody.innerHTML = bodyHtml;
    els.modal.hidden = false;
  }

  function closeModal() {
    els.modal.hidden = true;
    els.modalBody.innerHTML = "";
  }

  function modalActions(actions) {
    return `<div class="wecom-tags-actions" style="justify-content:flex-end;margin-top:14px;">${actions}</div>`;
  }

  async function loadTags(successMessage) {
    setFeedback("", "");
    state.busy = true;
    renderCapacity();
    try {
      const payload = await requestJson(apiTags(), { method: "GET" }, "企微标签同步失败，请检查企微配置或稍后重试。");
      state.groups = validateCatalogGroups(payload);
      state.totalTags = Number(payload.total_tags || 0);
      state.tagLimit = Number(payload.tag_limit || 1000);
      state.syncedAt = normalized(payload.synced_at);
      if (!state.selectedGroupKey && state.groups[0]) state.selectedGroupKey = groupKey(state.groups[0]);
      if (payload.source_status === "cache_fallback") {
        setFeedback(normalized(payload.sync_error) || "企微远程同步失败，已展示上一次本地缓存。", "error");
      } else if (successMessage) setFeedback(successMessage, "success");
    } catch (error) {
      state.groups = [];
      state.totalTags = 0;
      setFeedback(friendlyError(error, "企微标签同步失败，请检查企微配置或稍后重试。"), "error");
    } finally {
      state.busy = false;
      render();
    }
  }

  async function syncTags() {
    setBusy(true);
    try {
      await requestJson(apiSync(), writeOptions("POST", { source: "admin_wecom_tags" }, "sync"), "企微标签同步失败，请稍后重试。");
      await loadTags("企微标签同步成功");
    } catch (error) {
      setFeedback(friendlyError(error, "企微标签同步失败，请稍后重试。"), "error");
    } finally {
      setBusy(false);
      render();
    }
  }

  function setBusy(isBusy) {
    state.busy = Boolean(isBusy);
    root.querySelectorAll("button").forEach((button) => {
      if (button.dataset.action === "close-modal") return;
      button.disabled = state.busy;
    });
    renderCapacity();
  }

  async function refreshAndSelect(selector) {
    const previousQuery = state.query;
    await loadTags("");
    state.query = previousQuery;
    const matched = selector ? state.groups.find(selector) : null;
    if (matched) {
      state.selectedGroupKey = groupKey(matched);
      state.page = 1;
      render();
    }
  }

  function openCreateGroupModal() {
    openModal("新增标签组", `
      <form class="wecom-tags-form" data-form="create-group">
        <label><span>标签组名称</span><input name="group_name" type="text" required></label>
        <label><span>第一个标签名称</span><input name="first_tag_name" type="text" required></label>
        ${modalActions('<button class="admin-button admin-button--ghost" type="button" data-action="close-modal">取消</button><button class="admin-button admin-button--primary" type="submit">保存</button>')}
      </form>
    `);
  }

  function openCreateTagModal() {
    if (state.totalTags >= state.tagLimit) {
      setFeedback("标签数量已达到 1000 上限，不能继续新增标签。", "error");
      return;
    }
    const group = selectedGroup();
    const options = state.groups.map((item) => {
      const selected = groupKey(item) === state.selectedGroupKey ? " selected" : "";
      return `<option value="${escapeHtml(groupKey(item))}"${selected}>${escapeHtml(item.group_name || "未命名标签组")}</option>`;
    }).join("");
    openModal("新增标签", `
      <form class="wecom-tags-form" data-form="create-tag">
        <label><span>所属标签组</span><select name="group_key" required>${options}</select></label>
        <label><span>标签名称</span><input name="tag_name" type="text" required></label>
        ${modalActions('<button class="admin-button admin-button--ghost" type="button" data-action="close-modal">取消</button><button class="admin-button admin-button--primary" type="submit">保存</button>')}
      </form>
    `);
  }

  function openEditGroupModal() {
    const group = selectedGroup();
    if (!group) return;
    openModal("编辑组名", `
      <form class="wecom-tags-form" data-form="edit-group">
        <label><span>标签组名称</span><input name="group_name" type="text" value="${escapeHtml(group.group_name)}" required></label>
        ${modalActions('<button class="admin-button admin-button--ghost" type="button" data-action="close-modal">取消</button><button class="admin-button admin-button--primary" type="submit">保存</button>')}
      </form>
    `);
  }

  function openTagDetail(tagId) {
    const found = findTag(tagId);
    if (!found.tag) return;
    openModal("标签详情", `
      <div class="wecom-tags-detail">
        <div class="wecom-tags-detail-row"><strong>标签名</strong><span>${escapeHtml(found.tag.tag_name || "未命名标签")}</span></div>
        <div class="wecom-tags-detail-row"><strong>tag_id</strong><span>${escapeHtml(found.tag.tag_id || "-")}</span></div>
        <div class="wecom-tags-detail-row"><strong>标签组</strong><span>${escapeHtml((found.group || {}).group_name || "-")}</span></div>
        <div class="wecom-tags-detail-row"><strong>使用人数</strong><span>${Number(found.tag.usage_count || 0)}</span></div>
      </div>
      ${modalActions(`<button class="admin-button admin-button--secondary" type="button" data-tag-copy="${escapeHtml(found.tag.tag_id)}">复制 tag_id</button><button class="admin-button admin-button--ghost" type="button" data-action="close-modal">关闭</button>`)}
    `);
  }

  function openEditTagModal(tagId) {
    const found = findTag(tagId);
    if (!found.tag) return;
    openModal("编辑标签", `
      <form class="wecom-tags-form" data-form="edit-tag" data-tag-id="${escapeHtml(found.tag.tag_id)}">
        <label><span>标签名称</span><input name="tag_name" type="text" value="${escapeHtml(found.tag.tag_name)}" required></label>
        ${modalActions('<button class="admin-button admin-button--ghost" type="button" data-action="close-modal">取消</button><button class="admin-button admin-button--primary" type="submit">保存</button>')}
      </form>
    `);
  }

  async function copyTagId(tagId) {
    const value = normalized(tagId);
    if (!value) {
      setFeedback("复制失败，请手动打开详情复制。", "error");
      return;
    }
    try {
      await navigator.clipboard.writeText(value);
      setFeedback("tag_id 已复制", "success");
    } catch (_error) {
      setFeedback("复制失败，请手动打开详情复制。", "error");
    }
  }

  async function submitForm(form) {
    const formData = new FormData(form);
    const kind = form.dataset.form;
    setBusy(true);
    try {
      if (kind === "create-group") {
        const groupName = normalized(formData.get("group_name"));
        await requestJson(apiGroups(), writeOptions("POST", {
            group_name: groupName,
            first_tag_name: normalized(formData.get("first_tag_name")),
          }, "create-group"), "标签组创建失败，请检查名称是否重复或企微接口权限。");
        closeModal();
        await refreshAndSelect((group) => normalized(group.group_name) === groupName);
        setFeedback("标签组创建成功", "success");
      }
      if (kind === "create-tag") {
        const group = state.groups.find((item) => groupKey(item) === normalized(formData.get("group_key")));
        if (!group) throw new Error("必须选择标签组");
        if (!normalized(group.group_id)) throw new Error("当前标签组缺少 group_id，无法执行该操作，请先同步企微标签。");
        await requestJson(apiTags(), writeOptions("POST", {
            group_id: group.group_id,
            group_name: group.group_name,
            tag_name: normalized(formData.get("tag_name")),
          }, "create-tag"), "标签创建失败，请检查名称是否重复或企微接口权限。");
        closeModal();
        state.selectedGroupKey = groupKey(group);
        await refreshAndSelect((item) => normalized(item.group_id) === normalized(group.group_id));
        setFeedback("标签创建成功", "success");
      }
      if (kind === "edit-group") {
        const group = selectedGroup();
        if (!group || !normalized(group.group_id)) throw new Error("当前标签组缺少 group_id，无法执行该操作，请先同步企微标签。");
        const groupName = normalized(formData.get("group_name"));
        await requestJson(`${apiGroups()}/${encodeURIComponent(group.group_id)}`, writeOptions("PUT", { group_name: groupName }, "edit-group"), "标签组更新失败，请检查企微接口权限或名称是否重复。");
        closeModal();
        await refreshAndSelect((item) => normalized(item.group_id) === normalized(group.group_id) || normalized(item.group_name) === groupName);
        setFeedback("标签组已更新", "success");
      }
      if (kind === "edit-tag") {
        const tagId = normalized(form.dataset.tagId);
        await requestJson(`${apiTags()}/${encodeURIComponent(tagId)}`, writeOptions("PUT", { tag_name: normalized(formData.get("tag_name")) }, "edit-tag"), "标签更新失败，请检查名称是否重复或企微接口权限。");
        closeModal();
        await loadTags("");
        setFeedback("标签已更新", "success");
      }
    } catch (error) {
      setFeedback(friendlyError(error, "操作失败，请稍后重试。"), "error");
    } finally {
      setBusy(false);
      render();
    }
  }

  async function deleteCurrentGroup() {
    const group = selectedGroup();
    if (!group) return;
    if (!normalized(group.group_id)) {
      setFeedback("当前标签组缺少 group_id，无法执行该操作，请先同步企微标签。", "error");
      return;
    }
    if (!window.confirm("确认删除这个标签组吗？删除后该组下标签也会被删除，且不可恢复。")) return;
    setBusy(true);
    try {
      await requestJson(`${apiGroups()}/${encodeURIComponent(group.group_id)}`, writeOptions("DELETE", undefined, "delete-group"), "标签组删除失败，请稍后重试。");
      await loadTags("");
      setFeedback("标签组已删除", "success");
    } catch (error) {
      setFeedback(friendlyError(error, "标签组删除失败，请稍后重试。"), "error");
    } finally {
      setBusy(false);
      render();
    }
  }

  async function deleteTag(tagId) {
    const id = normalized(tagId);
    if (!id) {
      setFeedback("当前标签缺少 tag_id，无法执行该操作，请先同步企微标签。", "error");
      return;
    }
    if (!window.confirm("确认删除这个标签吗？删除后不可恢复。")) return;
    setBusy(true);
    try {
      await requestJson(`${apiTags()}/${encodeURIComponent(id)}`, writeOptions("DELETE", undefined, "delete-tag"), "标签删除失败，请稍后重试。");
      await loadTags("");
      setFeedback("标签已删除", "success");
    } catch (error) {
      setFeedback(friendlyError(error, "标签删除失败，请稍后重试。"), "error");
    } finally {
      setBusy(false);
      render();
    }
  }

  root.addEventListener("click", function (event) {
    const target = event.target.closest("button");
    if (!target || !root.contains(target)) return;
    const action = target.dataset.action;
    if (target.dataset.groupKey) {
      state.selectedGroupKey = target.dataset.groupKey;
      state.page = 1;
      render();
      return;
    }
    if (target.dataset.tagDetail !== undefined) openTagDetail(target.dataset.tagDetail);
    if (target.dataset.tagCopy !== undefined) copyTagId(target.dataset.tagCopy);
    if (target.dataset.tagEdit !== undefined) openEditTagModal(target.dataset.tagEdit);
    if (target.dataset.tagDelete !== undefined) deleteTag(target.dataset.tagDelete);
    if (action === "sync") syncTags();
    if (action === "create-group") openCreateGroupModal();
    if (action === "create-tag") openCreateTagModal();
    if (action === "edit-group") openEditGroupModal();
    if (action === "delete-group") deleteCurrentGroup();
    if (action === "close-modal") closeModal();
    if (action === "prev-page" && state.page > 1) {
      state.page -= 1;
      renderTags();
    }
    if (action === "next-page") {
      state.page += 1;
      renderTags();
    }
  });

  root.addEventListener("submit", function (event) {
    const form = event.target.closest("form[data-form]");
    if (!form) return;
    event.preventDefault();
    submitForm(form);
  });

  els.query.addEventListener("input", function (event) {
    state.query = normalized(event.target.value);
    state.page = 1;
    render();
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && !els.modal.hidden) closeModal();
  });

  loadTags("");
})();

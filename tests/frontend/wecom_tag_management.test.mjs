import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";
import vm from "node:vm";
import { fileURLToPath } from "node:url";


const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const scriptPath = path.join(
  root,
  "aicrm_next/crm/customer_tags/static/admin_console/wecom_tag_management.js",
);
const templatePath = path.join(
  root,
  "aicrm_next/crm/customer_tags/templates/admin_console/config_wecom_tags.html",
);


function loadContractValidator() {
  const element = {
    addEventListener() {},
    classList: { add() {}, toggle() {} },
    contains() { return true; },
    dataset: {},
    hidden: false,
    querySelector() { return element; },
    querySelectorAll() { return []; },
    style: {},
  };
  const context = {
    console,
    document: {
      addEventListener() {},
      querySelector() { return element; },
    },
  };
  context.window = context;
  const source = readFileSync(scriptPath, "utf8").replace(
    /\n  loadTags\(""\);\n\}\)\(\);\s*$/,
    "\n  window.__wecomTagsTest = { validateCatalogGroups };\n})();\n",
  );
  vm.runInNewContext(source, context, { filename: scriptPath });
  return context.__wecomTagsTest.validateCatalogGroups;
}


test("WeCom tag catalog accepts the canonical Next response fields", () => {
  const validateCatalogGroups = loadContractValidator();
  const groups = [{
    group_id: "group-1",
    group_name: "客户阶段",
    tag_count: 1,
    tags: [{ tag_id: "tag-1", tag_name: "新客", group_id: "group-1" }],
  }];

  assert.equal(validateCatalogGroups({ groups }), groups);
});


test("WeCom tag catalog rejects id/name drift instead of rendering blank names and zero ids", () => {
  const validateCatalogGroups = loadContractValidator();
  assert.throws(
    () => validateCatalogGroups({
      groups: [{ id: 1, name: "客户阶段", tags: [{ id: 2, name: "新客", group_id: 1 }] }],
    }),
    /企微标签数据格式异常/,
  );
});


test("WeCom tag management keeps the production management shell", () => {
  const template = readFileSync(templatePath, "utf8");
  assert.match(template, /data-action="sync">同步企微标签<\/button>/);
  assert.match(template, /data-action="create-group">新增标签组<\/button>/);
  assert.match(template, /data-action="create-tag">新增标签<\/button>/);
  assert.match(template, /placeholder="搜索标签组 \/ 标签 \/ tag_id"/);
  assert.match(template, /data-role="capacity-text">0 \/ 1000/);
  assert.match(template, /data-role="group-list"/);
  assert.match(template, /data-role="tag-rows"/);
});

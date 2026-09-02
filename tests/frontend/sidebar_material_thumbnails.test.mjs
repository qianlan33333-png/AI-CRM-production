import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";


const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const source = readFileSync(
  path.join(root, "aicrm_next/app/admin_console/static/sidebar_workbench/sidebar_workbench.js"),
  "utf8",
);
const template = readFileSync(
  path.join(root, "aicrm_next/app/admin_console/templates/sidebar_customer_workbench.html"),
  "utf8",
);


test("all sidebar material render paths share one thumbnail binder", () => {
  assert.match(source, /function loadMaterialThumbnail\(card\)/);
  assert.match(source, /function bindMaterialThumbnails\(items\)/);
  assert.match(source, /renderMaterials\(\)[\s\S]*?bindMaterialThumbnails\(rows\);/);
  assert.match(source, /onPage:[\s\S]*?bindMaterialThumbnails\(items\);/);
  assert.match(source, /if \(state\.materialThumbObserver\) state\.materialThumbObserver\.observe\(card\);\s*else loadMaterialThumbnail\(card\);/);
});


test("search changes cancel old query and thumbnail work, including cached results", () => {
  assert.match(source, /function destroyMaterialResources\(\)[\s\S]*?state\.materialImageController\.abort\(\)/);
  assert.match(source, /if \(state\.materialSearchController\) state\.materialSearchController\.abort\(\);\s*const requestVersion = \+\+state\.materialRequestVersion;/);
  assert.match(source, /requestVersion !== state\.materialRequestVersion/);
  assert.match(source, /requestedQuery !== state\.materialQuery/);
});


test("viewport cancellation can resume and terminal failures expose a manual retry", () => {
  assert.match(source, /image\.style\.opacity = "0"/);
  assert.match(source, /error\.reason === "outside_viewport"[\s\S]*?state\.materialThumbObserver\.observe\(card\)/);
  assert.match(source, /data-material-thumb-retry/);
  assert.match(source, /loadMaterialThumbnail\(card\);/);
  assert.match(source, /"预览不可用", false/);
});


test("WeCom receives cache-busted shared loader and sidebar script", () => {
  assert.match(template, /image_resource_loader\.js\?v=resource-governance-v2-pending-retry/);
  assert.match(template, /sidebar_workbench\.js\?v=20260902-oneid-provisioning/);
});

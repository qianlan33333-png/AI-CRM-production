# 企微 Callback 永久修复生产发布窗口执行清单

日期：2026-06-27
适用范围：把生产从 nginx emergency quick ACK 切换到 `webhook_inbox + 5002 callback ingress + callback worker`

## 0. 使用原则

这份清单只用于已批准的生产发布窗口。发布前不要在生产执行 nginx reload、systemd restart、migration 或 worker execute。

硬性边界：

- 不把当前 callback `200 success` 当作业务处理成功。
- 不在 callback HTTP path 内做真实外发。
- 不打开真实 WeCom/Payment/OAuth/OpenClaw/MCP 外呼 gate，除非另有明确审批。
- 不用同一个公网 URL 同时证明 web 和 ingress runtime 隔离。
- 不在 final readiness 通过前宣布永久修复完成。

## 1. 发布前 Go/No-Go

Go 条件：

- 已确认发布窗口和回滚负责人。
- 已确认可登录 `124.220.53.183`。
- 当前 quick ACK 仍保护页面，页面层可用。
- 本地 dry-run command plan 返回 `ok=true`、`dry_run_only=true`、`missing_assets=[]`。
- 生产代码已包含 `webhook_inbox`、5002 ingress、callback worker、admin webhook inbox、checker 和 runbook。

No-Go 条件：

- 无法登录生产主机。
- 无法确认当前 nginx 配置备份路径。
- 5001 页面健康异常。
- 5002 ingress 启动失败。
- deploy smoke 不能证明不同 `--web-base-url` 和 `--ingress-base-url`。
- rollback 命令或 nginx backup 不可用。

## 2. 发布前生成命令计划

在生产主机上执行：

```bash
cd /home/ubuntu/极简 crm
source /home/ubuntu/venvs/openclaw/bin/activate
set -a && source /home/ubuntu/.openclaw-wecom-pg.env && set +a
python scripts/ops/prepare_wecom_callback_ingress_cutover.py
```

期望：

- `ok=true`
- `dry_run_only=true`
- `missing_assets=[]`
- 输出包含 `preflight`、`install_and_start`、`cutover`、`callback_sample`、`worker_isolation_canary`、`downstream_worker_isolation_canary`、`internal_event_worker_isolation_canary`、`pressure_probe`、`rollback`、`reapply_cutover_after_rollback`、`rollback_drill_evidence`、`final_readiness`

## 3. Preflight

按 command plan 的 `preflight` 组执行，重点确认：

```bash
cd /home/ubuntu/极简 crm
test -f /home/ubuntu/venvs/openclaw/bin/activate
source /home/ubuntu/venvs/openclaw/bin/activate
test -f /home/ubuntu/.openclaw-wecom-pg.env
set -a && source /home/ubuntu/.openclaw-wecom-pg.env && set +a
test -n "${DATABASE_URL:-}"
python -m alembic heads
python -m alembic current
python -m alembic upgrade head
python scripts/ops/check_callback_quick_ack_state.py --skip-probe
```

Go 条件：

- migration 能到 `0054_webhook_inbox`
- quick ACK state checker 能识别当前状态
- 没有数据库连接或 migration 错误

## 4. 安装并启动 5002 Ingress 与 Worker

按 `install_and_start` 组执行：

```bash
cd /home/ubuntu/极简 crm
source /home/ubuntu/venvs/openclaw/bin/activate
set -a && source /home/ubuntu/.openclaw-wecom-pg.env && set +a
sudo cp deploy/openclaw-wecom-callback-ingress.service /etc/systemd/system/
sudo cp deploy/openclaw-wecom-callback-inbox-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl disable --now openclaw-wecom-callback-inbox-worker.timer || true
sudo systemctl enable openclaw-wecom-callback-ingress.service
sudo systemctl restart openclaw-wecom-callback-ingress.service
sudo systemctl enable openclaw-wecom-callback-inbox-worker.service
sudo systemctl restart openclaw-wecom-callback-inbox-worker.service
curl -sSf http://127.0.0.1:5002/health
python scripts/run_wecom_callback_inbox_worker.py --limit 20
set -o pipefail; python scripts/ops/check_wecom_callback_deploy_smoke.py --web-base-url http://127.0.0.1:5001 --ingress-base-url http://127.0.0.1:5002 | tee /tmp/wecom-callback-deploy-smoke.json
```

Go 条件：

- `http://127.0.0.1:5002/health` 返回 2xx
- worker service 为 `active`，旧 minute timer 为 disabled/inactive
- ingress health 明确返回 `durable_inbox_only=true`
- deploy smoke 通过，并且 `base_urls_distinct=true`
- `/tmp/wecom-callback-deploy-smoke.json` 保存成功

## 5. Nginx Cutover

按 `cutover` 组执行。nginx 修改必须手工合并，不能覆盖整个生产 server block。

```bash
cd /home/ubuntu/极简 crm
source /home/ubuntu/venvs/openclaw/bin/activate
set -a && source /home/ubuntu/.openclaw-wecom-pg.env && set +a
export AICRM_CALLBACK_CUTOVER_BACKUP="/etc/nginx/sites-enabled/youcangogogo.conf.bak-codex-callback-cutover-$(date +%Y%m%dT%H%M%S)"
printf '%s\n' "$AICRM_CALLBACK_CUTOVER_BACKUP" > /tmp/wecom-callback-cutover-backup-path
sudo cp /etc/nginx/sites-enabled/youcangogogo.conf "$AICRM_CALLBACK_CUTOVER_BACKUP"
sudoedit /etc/nginx/sites-enabled/youcangogogo.conf
sudo nginx -t
sudo systemctl reload nginx
python scripts/ops/check_wecom_callback_ingress_cutover.py --nginx-config /etc/nginx/sites-enabled/youcangogogo.conf
python scripts/ops/check_wecom_callback_permanent_fix_readiness.py --nginx-config /etc/nginx/sites-enabled/youcangogogo.conf
```

Go 条件：

- nginx backup 路径写入 `/tmp/wecom-callback-cutover-backup-path`
- 两个 callback route 都 proxy 到 `127.0.0.1:5002`
- 两个 callback route 都不再包含 `return 200 "success"`
- callback route 保留 `limit_req`、`limit_conn` 和 429 overload
- invalid callback POST 返回 app-level 4xx，不是 plain `success`

## 6. 生成合法 Callback 样本

```bash
cd /home/ubuntu/极简 crm
source /home/ubuntu/venvs/openclaw/bin/activate
set -a && source /home/ubuntu/.openclaw-wecom-pg.env && set +a
python scripts/ops/generate_wecom_callback_sample.py \
  --env-file /home/ubuntu/.openclaw-wecom-pg.env \
  --callback-base-url http://127.0.0.1:5002/wecom/external-contact/callback \
  --body-file /tmp/wecom-callback-sample.xml \
  --url-file /tmp/wecom-callback-sample.url \
  --metadata-file /tmp/wecom-callback-sample.json
test -s /tmp/wecom-callback-sample.xml
test -s /tmp/wecom-callback-sample.url
```

Go 条件：

- sample 可被当前生产 callback config 解密
- `/tmp/wecom-callback-sample.url` 和 `/tmp/wecom-callback-sample.xml` 都存在

## 7. 压测与 Same-Sample 证据

```bash
cd /home/ubuntu/极简 crm
source /home/ubuntu/venvs/openclaw/bin/activate
set -a && source /home/ubuntu/.openclaw-wecom-pg.env && set +a
set -o pipefail; python scripts/ops/probe_wecom_callback_pressure.py \
  --callback-url "$(cat /tmp/wecom-callback-sample.url)" \
  --callback-body-file /tmp/wecom-callback-sample.xml \
  --require-valid-callback-sample \
  --rate-per-minute 1200 \
  --duration-seconds 60 \
  | tee /tmp/wecom-callback-pressure.json
python scripts/ops/check_wecom_callback_ingestion_evidence.py --pressure-evidence-file /tmp/wecom-callback-pressure.json | tee /tmp/wecom-callback-ingestion.json
python scripts/run_wecom_callback_inbox_worker.py --limit 20
AICRM_WECOM_CALLBACK_INBOX_WORKER_EXECUTE=1 python scripts/run_wecom_callback_inbox_worker.py --execute --limit 20
python scripts/ops/check_wecom_callback_processing_evidence.py --pressure-evidence-file /tmp/wecom-callback-pressure.json | tee /tmp/wecom-callback-processing.json
python scripts/ops/check_wecom_callback_public_state.py --base-url http://127.0.0.1:5001 | tee /tmp/wecom-callback-public-state.json
set -o pipefail; python scripts/ops/check_wecom_callback_deploy_smoke.py --web-base-url http://127.0.0.1:5001 --ingress-base-url http://127.0.0.1:5002 | tee /tmp/wecom-callback-deploy-smoke.json
```

Go 条件：

- `sample_validation.ok=true`
- observed callback rate >= 1200/min
- callback P95 <= 200 ms
- callback P99 <= 500 ms
- `/health` P95 <= 100 ms
- `/sidebar/bind-mobile` P95 <= 300 ms
- sampled admin/sidebar routes 无 5xx
- pressure、ingestion、processing 三份 JSON 指向同一个 idempotency key
- public-state 证明公网 webhook inbox 路由已部署，invalid callback 是 app-level 4xx
- deploy-smoke 证明 5001/5002/admin/detail/callback routes 都已部署

## 8. 三类 Isolation Canary

### 8.1 Callback Worker Isolation

停 callback worker 后，发送 1 个合法 callback。通过条件是 callback 仍 ACK，页面不受影响，只增加 backlog。

保存：

```text
/tmp/wecom-callback-worker-isolation.json
```

### 8.2 Downstream External Effect Worker Isolation

停 downstream external push worker 后，发送 1 个合法 callback 并采样页面。通过条件是 callback ACK 和页面可用不受影响。

保存：

```text
/tmp/wecom-callback-downstream-worker-isolation.json
```

### 8.3 Internal Event Worker Isolation

停 internal event worker 后，发送 1 个合法 callback 并采样页面。通过条件是只增加 backlog，不影响 callback ACK 或页面。

保存：

```text
/tmp/wecom-callback-internal-event-worker-isolation.json
```

## 9. Rollback Drill

先执行 `rollback` 组，确认能回到 emergency quick ACK：

```bash
cd /home/ubuntu/极简 crm
test -s /tmp/wecom-callback-cutover-backup-path
export AICRM_CALLBACK_CUTOVER_BACKUP="$(cat /tmp/wecom-callback-cutover-backup-path)"
test -n "${AICRM_CALLBACK_CUTOVER_BACKUP:-}"
test -f "$AICRM_CALLBACK_CUTOVER_BACKUP"
sudo cp "$AICRM_CALLBACK_CUTOVER_BACKUP" /etc/nginx/sites-enabled/youcangogogo.conf
sudo nginx -t
sudo systemctl reload nginx
sudo systemctl stop openclaw-wecom-callback-inbox-worker.service || true
sudo systemctl stop openclaw-wecom-callback-ingress.service || true
curl -sSf http://127.0.0.1:5001/health
```

再执行 `reapply_cutover_after_rollback` 组，重新切回 5002 permanent cutover。

最后生成并校验 rollback evidence：

```bash
cd /home/ubuntu/极简 crm
source /home/ubuntu/venvs/openclaw/bin/activate
set -a && source /home/ubuntu/.openclaw-wecom-pg.env && set +a
python scripts/ops/check_wecom_callback_rollback_evidence.py --print-template > /tmp/wecom-callback-rollback.template.json
# 按实际 rollback/reapply 证据填写 /tmp/wecom-callback-rollback.json
python scripts/ops/check_wecom_callback_rollback_evidence.py --evidence-file /tmp/wecom-callback-rollback.json
```

Go 条件：

- rollback 后页面可用
- quick ACK 可恢复
- reapply 后 5002 cutover 再次生效
- `/tmp/wecom-callback-rollback.json` 通过 checker

## 10. Final Readiness

所有证据生成后执行：

```bash
cd /home/ubuntu/极简 crm
source /home/ubuntu/venvs/openclaw/bin/activate
set -a && source /home/ubuntu/.openclaw-wecom-pg.env && set +a
python scripts/ops/check_wecom_callback_permanent_fix_readiness.py \
  --nginx-config /etc/nginx/sites-enabled/youcangogogo.conf \
  --pressure-evidence-file /tmp/wecom-callback-pressure.json \
  --ingestion-evidence-file /tmp/wecom-callback-ingestion.json \
  --processing-evidence-file /tmp/wecom-callback-processing.json \
  --worker-isolation-evidence-file /tmp/wecom-callback-worker-isolation.json \
  --downstream-worker-isolation-evidence-file /tmp/wecom-callback-downstream-worker-isolation.json \
  --internal-event-worker-isolation-evidence-file /tmp/wecom-callback-internal-event-worker-isolation.json \
  --rollback-evidence-file /tmp/wecom-callback-rollback.json \
  --public-state-evidence-file /tmp/wecom-callback-public-state.json \
  --deploy-smoke-evidence-file /tmp/wecom-callback-deploy-smoke.json
```

完成条件：

- `ready_for_production_cutover=true`
- `ready_for_production_completion=true`
- `ok=true`
- webhook inbox backlog 健康
- public-state 不再显示 quick ACK
- deploy-smoke 证明 5001/5002 隔离
- rollback drill 证据通过

## 11. 发布后口径

只有 final readiness 全部通过后，才能把状态从：

```text
页面已临时恢复，callback 仍处于 emergency quick ACK
```

改成：

```text
页面已恢复，企微 callback 已切换到 webhook_inbox + 5002 ingress + worker 的永久处理链路
```

如果任一证据缺失，仍保持“永久修复未完成”的口径。

## 12. 关联文档

- `docs/reports/production_page_restore_investigation_20260627_zh.md`
- `docs/reports/wecom_callback_permanent_fix_acceptance_audit_20260627.md`
- `docs/runbooks/wecom_callback_storm.md`
- `docs/deploy_runbook.md`

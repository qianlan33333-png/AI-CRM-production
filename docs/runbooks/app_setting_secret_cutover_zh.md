# app_settings Secret Reference 切换手册

## 目标与边界

本手册用于把 `app_settings` 和运行环境文件中的敏感配置迁移到版本化文件 secret store。迁移完成后：

- secret 原文只存在于 `AICRM_SECRET_STORE_DIR` 下的不可变版本文件；
- `app_settings` 与运行环境文件只保存 `secretref:file:<KEY>:<VERSION>` 引用；
- 目录权限必须为 `0700`，版本文件权限必须为 `0600`，所有者必须是运行服务的系统用户；
- `AICRM_SECRET_REFERENCE_CUTOVER=true` 后，运行时拒绝敏感配置原文并对缺失、坏引用、错误权限执行失败关闭；
- 命令输出、部署日志和审计日志不得输出 secret 原文或完整引用。

本操作不新增产品能力，不启用被门禁阻断的外部调用。

## 上线前检查

1. 确认当前代码包含 `FileSecretStore`、引用解析和 expand-mode 兼容读取。
2. 确认 Web 与所有 worker 使用同一 Linux 用户，并能读取 `/home/ubuntu/.openclaw-wecom-pg.env`。
3. 确认 PostgreSQL 连接可用，`app_settings`、`admin_operation_logs` 表存在，Alembic 已升级到当前 release。
4. 禁止 `set -x`、`env`、`printenv` 或带值的 SQL 输出；不得把环境文件内容复制到工单或日志。
5. 确认目标目录所在磁盘有足够空间，并且 `/home/ubuntu` 不是不受信任的共享挂载。
6. 确认 `qianlan333/AI-CRM` 是唯一生产发布源；旧仓库的 `Deploy to Production` workflow 必须已停用或撤销生产部署凭据。仓库内 concurrency 不能替代这项跨仓库退役动作。

AI-CRM 自有共享服务 Token 已由 RAUTH 删除，不再做“按用途拆分共享 Bearer”或 legacy fallback。本手册只迁移仍在用的供应商/服务端 secret；机器身份必须在 migration 后通过 `scripts/ops/bootstrap_auth_clients.py` 注册独立 client credentials。切换顺序和命令见 [`../auth_client_credentials.md`](../auth_client_credentials.md)。

## 预演

先加载生产环境，再执行只读预演：

```bash
set -a
source /home/ubuntu/.openclaw-wecom-pg.env
set +a
export AICRM_SECRET_STORE_DIR="${AICRM_SECRET_STORE_DIR:-/home/ubuntu/.aicrm-secrets}"
python3 scripts/ops/migrate_app_setting_secrets.py --dry-run \
  --secret-store-dir "$AICRM_SECRET_STORE_DIR"
```

预演只报告 `key/source/version/present/status` 和计数，不创建目录、不写数据库、不修改环境文件。存在原文或历史审计原文时，`ok=false`，并通过 `plaintext_pending` 和 `audit_rows_redaction_pending` 说明待处理量；输出中不应出现任何配置值或命中的审计内容。

## 正式切换

### 发布事务固定顺序

生产发布不是“更新代码后重启”，而是一个由 systemd 持久闸门保护的事务，顺序不得调整：

1. 获取 `/home/ubuntu/.aicrm-production-deploy.lock` 主机锁，并完成增量 release bundle、base SHA、checksum、workflow SHA 校验；bundle 只携带相对已验证 release 第一父提交的增量对象，生产仓库缺少任一 bundle prerequisite 时必须在运行时事务开始前失败关闭；临时目录必须包含 GitHub run ID 与 attempt，避免并发上传互相覆盖。
2. 从已验证 release 中解出 runtime manager、manifest 与 deploy guard；此时不修改工作树。
3. 给 primary、active、approval 与 retired 运行单元安装 `00-aicrm-deploy-transaction-guard.conf`，创建 `/home/ubuntu/.aicrm-production-deploy-in-progress`。该文件存在且没有对应 `/run` 授权时，systemd `ConditionPathExists` 会拒绝 timer、依赖或人工启动；闸门文件持久化，主机重启不会绕过失败关闭。
4. 停止并核对全部 active/approval timer、对应 service、active service 和主 Web；退役 unit 必须 disabled、inactive、not-failed，5001 必须无监听。任一停止或状态核对失败都中止发布。
5. 只有运行时完全静默后，才允许 stash、`git reset --hard`、写 `.release-sha`、安装依赖和执行 Alembic。
6. 执行本手册下方的 Secret migration 与 reconciliation。任一对账项失败时保留 deploy-in-progress 闸门，Web 和 worker 均不得恢复。
7. 从当前 release 安装并 enable 唯一主 Web unit `openclaw-wecom-postgres.service`，删除重复的 `/etc/systemd/system/aicrm-web.service`。此时 deploy-in-progress 仍保留；只创建运行时授权 `/run/aicrm-production-web-start-authorized`，让 primary Web 的 OR 条件单独放行，timer/worker 继续被持久闸门阻断。该授权在重启时自动消失，不能跨重启绕过失败关闭。
8. 在恢复 worker 前执行本地 canary：要求 health 的 exact SHA 与 Secret 状态全绿，QR/OAuth 均 302 到企微官方入口，`appid/agentid` 存在，内层 `redirect_uri` 精确等于 `https://id-dev.youcangogogo.com/auth/wecom/callback`，并且没有真实外呼。若此阶段主机重启，`/run` 授权消失，持久 deploy-in-progress 会继续阻断 Web、worker 与 timer。
9. canary 与后台只读 smoke 通过后，创建 `/run/aicrm-production-runtime-start-authorized` 并删除 Web 专用授权；生产后台只读 probe 的单请求阈值为 20 秒，用于覆盖 push-center 冷查询，超时仍失败关闭。持久 deploy-in-progress 仍保留。此运行时授权只允许在当前启动周期恢复 active timer/service，approval timer 只恢复事务前已 enabled 的项。随后验证 primary/active 均 enabled+active，disabled approval 不得 active，retired 均 not-found+inactive+not-failed，且 `retired_unit_files` 必须覆盖全部 `retired_forbidden`。
10. callback smoke 与公网 exact-SHA 均通过后，最终提交动作先删除 `/run` 运行时授权、再删除持久 deploy-in-progress，并立即写入事务 commit 标志。若最终提交前主机重启，两个 `/run` 授权都会消失，持久闸门继续阻断完整 runtime。

任一步失败，EXIT cleanup 会重新创建持久闸门、再次静默运行单元并保持 Web 停止。恢复方式是修复原因后重跑完整、已验证的 AI-CRM 发布事务；禁止直接 `systemctl start`、删除闸门文件或只重跑 Secret checker。

在 worker 已停止、Alembic 已升级且当前 release 代码已就位后执行：

```bash
export AICRM_SECRET_STORE_DIR="${AICRM_SECRET_STORE_DIR:-/home/ubuntu/.aicrm-secrets}"
python3 scripts/ops/migrate_app_setting_secrets.py --execute \
  --secret-store-dir "$AICRM_SECRET_STORE_DIR" \
  --environment-file /home/ubuntu/.openclaw-wecom-pg.env \
  | tee /tmp/aicrm-secret-migration.json

set -a
source /home/ubuntu/.openclaw-wecom-pg.env
set +a

python3 scripts/ops/check_secret_reference_cutover.py \
  --secret-store-dir "$AICRM_SECRET_STORE_DIR" \
  --environment-file /home/ubuntu/.openclaw-wecom-pg.env \
  | tee /tmp/aicrm-secret-reconciliation.json
```

迁移顺序固定为：

1. 读取 DB 与当前进程环境中的敏感 key 清单；
2. 写入并 `fsync` 不可变版本文件；
3. 在一个数据库事务中把原文替换为目标引用，将历史审计中命中已知 secret 的字符串固定替换为 `[redacted]`，校验全部引用并启用 DB cutover 哨兵；
4. 原子更新环境文件，把敏感项替换为引用，并持久化 store 目录与 cutover 哨兵；
5. 重新加载环境文件，执行独立 reconciliation checker；
6. 执行 RAUTH client bootstrap 和 `check_auth_readiness.py`；两项均通过后才允许重启 Web、恢复 worker。

重复执行是幂等的：已经解析成功的引用不会新建版本，已脱敏审计行不会再次改写，也不会刷新对应 `app_settings.updated_at`。执行报告中的 `audit_rows_redacted` 仅为计数，不包含审计行 ID 或原内容。

## 第二系统公网 exact-SHA 收口

本机 `127.0.0.1:5001/health` 命中新 release 不代表第二系统公网入口已经就绪。Web、callback 和运行单元验证后，部署必须轮询 `https://id-dev.youcangogogo.com/health`，并要求返回 HTTP 200 且 `x-aicrm-release-sha == verified workflow SHA`。

该检查只读取第二系统健康入口，不修改 DNS、Nginx、Caddy 或其他服务器配置。`www.youcangogogo.com` 与 `150.158.82.186` 不属于本仓库的部署目标。

## 必须为零的对账项

`/tmp/aicrm-secret-reconciliation.json` 必须满足：

```text
ok=true
cutover_enabled=true
plaintext_sensitive_rows=0
unresolved_refs=0
unsafe_audit_hits=0
permission_errors=0
store_scan_errors=0
audit_scan_errors=0
plaintext_environment_entries=0
unresolved_environment_refs=0
environment_scan_errors=0
environment_permission_errors=0
environment_reference_mismatches=0
```

任一项非零都必须阻断发布。不要通过删除 checker、跳过失败命令或手工修改 JSON 继续上线。

## 失败处理

### 文件写入失败

数据库事务尚未开始，`app_settings` 保持原状。修复目录所有者、权限或磁盘问题后重新执行。若留下未发布的临时文件，checker 会失败；确认没有运行中的迁移进程后再由具备权限的运维人员清理异常临时项。

### 数据库事务失败

数据库会回滚，已经写入的不可变版本文件保留为孤立安全版本，不含数据库原文泄漏。修复数据库问题后重复执行；不要删除可能被其他 release 引用的版本文件。

### 环境文件更新失败

数据库可能已经完成引用切换，但服务尚未重启。保持 Web/worker 停止，修复环境文件所有者、`0600` 权限或目录权限，然后重复执行迁移与 checker。checker 全绿前不得启动旧 release。

### checker 发现审计原文

停止发布，定位命中的审计行并按安全事件流程处理。只能把敏感字段改为固定 `[redacted]` 或 `[pii]`，不得把原文复制到终端、工单或 PR。修复后重新执行 checker。

## 版本回滚

版本文件不会在轮换时删除。已知目标 key 与先前 `VERSION` 时，可构造引用并原子回切：

```bash
python3 scripts/ops/migrate_app_setting_secrets.py --execute \
  --secret-store-dir "$AICRM_SECRET_STORE_DIR" \
  --environment-file /home/ubuntu/.openclaw-wecom-pg.env \
  --rollback-key WECOM_SECRET \
  --rollback-reference 'secretref:file:WECOM_SECRET:VERSION'
```

回滚命令会先验证 key、版本文件、权限与所有者，再更新 DB 和环境文件；输出只包含 key、version 和状态。随后必须重新加载环境文件、重启相关运行单元并再次运行 checker。

代码回滚只能回到仍支持 `secretref:file:` 的 release。禁止直接回到不认识引用的旧 release；如必须执行灾难恢复，先停止所有运行单元并由安全负责人审批单独的数据恢复方案。不得恢复历史审计原文，也不得删除仍可能被引用的 secret 版本。

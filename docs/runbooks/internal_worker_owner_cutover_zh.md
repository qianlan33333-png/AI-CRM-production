# internal_worker 唯一 Owner 切换

## 目标与边界

本批次只把内部事件、内部 Outbox 与企微回调 Inbox 收敛到
`aicrm-internal-worker.service`。真实企微外发仍只允许
`aicrm-external-queue-runtime.service` 执行；本批次不切 scheduler、不搬目录、
不删表，也不重放历史任务。

## 前置证据

- 合并前必须已有至少两次 claimless observer 生产诊断通过，证明三个 lane 的
  heartbeat、LISTEN 连接、generation 与 predecessor 一致，且 observer 没有 claim。
- `production-current` Deployment Profile 必须处于 `enforce`，公开健康端点必须暴露
  当前精确主干 SHA。
- `queue_runtime_control.active_generation > 0`、`claim_enabled=true`，共享 generation
  marker 必须声明 `AICRM_QUEUE_RUNTIME_EXECUTE=1` 与
  `AICRM_QUEUE_CUTOVER_COMMITTED=1`。
- 变更必须通过 PR 审核与 `pr / gate`；合并后的 `main`
  必须通过完整检查后才能自动发布。

## 发布事务

PR 合并后由 `promote-production.yml` 在 `main` push 上先运行完整检查，
成功后自动发布该精确 SHA。运行单元管理器会在同一部署事务中：

1. 给新旧 unit 安装事务守卫。
2. 停止原 `aicrm-internal-queue-runtime.service`、
   `aicrm-inbox-queue-runtime.service` 与 claimless observer。
3. 禁用并删除这三个 predecessor unit 文件。
4. 安装并启动 `aicrm-internal-worker.service`；该服务从共享 generation marker
   获取执行开关和 generation，marker 缺失或非法时 fail closed。
5. 保持 `aicrm-external-queue-runtime.service` 不变，完成健康、数据和运行单元门禁后
   才提交发布。

不要在发布事务外手工 stop/start unit。

## 发布后只读验收

在公开健康 SHA 已等于发布 SHA 后运行：

```bash
gh workflow run internal-worker-production-diagnostics.yml --ref main \
  -f expected_release_sha=<exact-main-release-sha> \
  -f 'confirmation=DIAGNOSE AI-CRM INTERNAL WORKER OWNER READ ONLY'
```

验收必须同时证明：

- 新 owner 已 enabled/active，命令为 `--role internal_worker`，进程环境中的执行开关
  和 generation 与共享 marker 一致。
- 两个 predecessor 与 observer 均 disabled/inactive，且 `/etc/systemd/system` 中没有
  残留 unit 文件。
- 三类 heartbeat 只存在 `:role:internal_worker:` namespace，各有两个 lane，generation
  等于当前 active generation，LISTEN 均已连接。
- 正在执行的内部任务不存在 competing owner；输出只含数量，不含 payload、target 或 PII。
- external worker 命令未变化，诊断为只读，`real_external_call_executed=false`。

## 回滚

若部署或诊断任一门禁失败，立即回滚到上一生产发布组合。上一提交的 runtime manifest
会重新安装两个 predecessor 和 observer，并移除新的 combined owner；不得通过双开新旧
owner 进行验证。回滚后重新核对公开 SHA、三个 predecessor 的 systemd 状态、heartbeat
namespace、到期 backlog 和 lease，再决定是否重新晋级。

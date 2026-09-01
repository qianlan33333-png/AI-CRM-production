# UnionID 兼容身份模型（已被 OneID ADR 取代）

> 当前决策见 `docs/adr/2026-09-01-wechat-ecosystem-oneid-correlation.md`。本文仅保留为
> `crm_user_identity` 兼容路径的历史说明，不再定义系统 canonical customer identity。

## 唯一 canonical identity

`crm_user_identity.unionid` 是系统唯一的业务主身份。`external_userid`、`openid` 和规范化手机号只是 alias、查询入口或企微执行 handle，不能单独成为业务事实源。

所有运行时身份查询必须通过 `aicrm_next.crm.identity_contact.resolver.IdentityResolver`：

- `resolved`：请求内每个非空标识都唯一指向同一条 active `unionid`。
- `not_found`：没有 canonical 候选，且没有对应待处理 resolution queue。
- `pending`：没有可安全使用的 canonical，但现有 resolution queue 正在处理。
- `conflict`：alias 重复、多个输入指向不同 unionid，或 canonical 为 `pending_merge/conflict/deleted`。冲突禁止按更新时间或输入顺序挑选一条。

手机号查询和写入只使用 `mobile_normalized`。中国大陆手机号先归一为 11 位数字；原始格式不参与 identity match。

## 写入边界

- canonical mobile/profile/binding 只能按已经解析并锁定的 `crm_user_identity.unionid` 更新。
- 新 alias 已属于另一 unionid、命中多条 canonical 或 canonical 非 active 时，写入 `crm_user_identity_conflicts` 并阻断；禁止自动合并或覆盖。
- 未解析身份只进入 `crm_user_identity_resolution_queue`，不得新建 `people` 或 `external_contact_bindings` canonical 记录。
- 问卷表单内容可以先保存为 `unionid=''` 的业务事实，同时原始 alias 进入 resolution queue；解析完成前不得执行标签、外部推送或自动化成功消费，也不得据此创建 canonical identity。
- `customer.phone_bound` 只在 canonical mobile bind 成功后发出。
- `missing_unionid` 的发送、权益和自动化消费者必须处于 pending、blocked 或 `failed_retryable`，不得标记 `succeeded`。

## 兼容表职责

- `people`：历史只读数据，不再承担主身份或手机号新写。
- `external_contact_bindings`：历史/owner metadata 投影；不再创建或改写 external_userid → person_id canonical 绑定。
- `wecom_external_contact_identity_map`：企微 ingress/profile 投影，可用于展示和 owner 边界读取；不参与 canonical identity 决策。
- `wecom_external_contact_follow_users`：企微客户与企业成员关系投影。
- `contacts`：历史 CRM 展示投影，不参与 identity resolve 或 canonical 写入。

这些表本批次不删除。回滚使用上一精确 release SHA，不恢复 people-first、first-match 或长期双写。

## Single-corp invariant

部署的 corp 由 `WECOM_CORP_ID` 决定。请求、事件 payload 或调用参数不能覆盖已配置 corp；不一致在任何数据库写入或企微调用前返回 `corp_id_mismatch`。

## 现有接口

- `GET /api/identity/resolve`
- `GET /api/admin/identity/resolve`
- `GET /api/admin/identity/links/{identity_key}`

接口不新增产品能力。`conflict`/`pending` 返回结构化失败；成功响应继续提供兼容 identity view，但 `person_id` 仅是历史投影字段。

## 发布与对账

部署在停止任何 runtime unit 前运行 `scripts/ops/check_unionid_identity_cutover.py --phase preflight`，健康检查后再运行 `--phase post-deploy`。输出只包含 count、status、release SHA 和不可逆 count digest，不包含 unionid、external_userid、openid 或手机号原值。

详细 inventory、失败条件与回滚见 `docs/operations/unionid-identity-cutover.md`。

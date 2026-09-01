# ADR: 微信生态真实标识驱动的 OneID 关联

## 状态

Accepted

## 背景与约束

`customers.id` 是 CRM 内唯一稳定的 OneID，但外部系统不会在每个场景都返回 UnionID。
企微客户和会话存档必须能在只有 ExternalUserID 时工作；但本部署的公众号问卷、微信支付入口和微信小店
已经具备 UnionID 能力，因此将 UnionID 作为这三条业务链的强制提交条件，但它仍然不是 CRM 的物理主键。

本部署当前是单企业、私有化、自建企微应用。设计仍保留明确的 provider scope，避免将来接入代开发或
服务商应用时把不同调用方得到的加密 ID 错误合并。

非功能约束：

- 相同可信事件重复处理必须幂等，并发处理只能创建一个 Customer。
- 身份解析和合并与业务写入处于同一数据库事务；外部网络调用不得持有数据库事务。
- 原始身份值不得出现在迁移报告、日志或发布证据中。
- 允许暂时存在两个 Customer；不允许因昵称、姓名、未验证手机号或跨 scope OpenID 自动合并。

## 实际可获得标识矩阵

| 场景 | 稳定可得 | 条件可得 | 不应假设可得 | OneID 处理 |
| --- | --- | --- | --- | --- |
| 企微新增客户回调 | `corp_id + ExternalUserID`、跟进成员 `UserID` | 无 | UnionID、手机号 | 立即 ensure Customer；保存员工关系 |
| 企微客户详情（自建应用） | `corp_id + external_userid` | 微信联系人且能力配置满足时返回 UnionID | 企业微信联系人 UnionID | ExternalUserID 继续可用；官方 UnionID 到达后确定性关联/合并 |
| 企微代开发/服务商模式 | 调用方作用域内 external_userid | `pending_id`、由微信侧 UnionID/OpenID 转换得到的 external_userid | 客户详情直接返回 UnionID | pending_id 只能作为限时关联凭据，不能作为永久主键 |
| 公众号网页 OAuth / 问卷 | `appid + openid + unionid` | 无 | 企微 external_userid | 非微信浏览器禁止填写；缺 UnionID 强制 OAuth，授权后仍缺失则拒绝提交 |
| 小程序登录 | `appid + openid` | 满足 UnionID 下发条件时返回 UnionID | 手机号、企微 external_userid | 与公众号相同；手机号必须通过专用凭证验证后单独绑定 |
| 微信支付 JSAPI/小程序下单与回调 | OAuth `appid + payer.openid + unionid`；商户订单号、微信交易号 | 无 | 支付回调自身直接返回 UnionID | 下单前强制微信 OAuth 及 UnionID；回调的 `appid + payer.openid` 必须与原付款身份一致 |
| 微信小店订单 | 小店订单 UnionID，可选带小店作用域 OpenID | 礼物订单另有赠送者身份 | 付款人与收礼人相同 | 缺 UnionID 视为 provider 契约错误并拒绝入库；订单归属客户与赠送/付款身份分开保存 |
| 企微会话内容存档 | 消息 ID、内部成员 UserID、外部联系人 ExternalUserID | 已有身份图可补 UnionID | 消息事件直接给 UnionID | 按 ExternalUserID 直接落 customer_id，不等待 UnionID |

OpenID 唯一键必须包含 AppID；ExternalUserID 唯一键必须包含 corp/calling-party scope；UnionID 唯一键必须
包含微信开放平台账号 scope。订单号、交易号、问卷提交 ID、消息 ID 是业务幂等/关联键，不是客户身份。

## 决策

1. `customers.id` 是所有业务事实的客户外键；`customer_identities` 保存带 scope 的 provider identity。
2. 任一官方校验成功的强身份都可以立即创建 active Customer：
   - `wecom / external_userid / corp_id`
   - `wechat / openid / appid`
   - `wechat / unionid / open_platform_id`
3. 同一官方响应同时给出两个身份时，记录为确定性关联。UnionID 已属于另一 Customer 时，只有不存在
   同 scope 强身份矛盾才自动合并，并保持 UnionID Customer 存活。
4. 业务记录区分角色：
   - 订单：`customer_id`（权益/服务接收者）与 `payer_identity_id`（付款微信账户）。
   - 问卷：`customer_id`（答题人）及 OAuth identity；只有受信上下文才能关联企微客户。
   - 微信小店礼物订单：订单归属/收礼客户与赠送者身份分开。
5. 兼容期保留 UnionID/OpenID/ExternalUserID 快照和旧读路径；数据库触发器只负责从已验证 Identity
   派生 `customer_id`，不根据未验证 query 参数、手机号、昵称或姓名创建/合并 Customer。
6. 企微自建应用当前以“客户详情可选 UnionID”运行；若未来切换代开发模式，启用官方
   UnionID/OpenID 转 external_userid 与 pending_id 流程，不复用自建应用 scope。
7. 公众号问卷、微信支付和微信小店是本部署明确的 UnionID-required 业务边界；
   通过签名 OAuth 会话或官方 provider 数据验证，不接受前端表单自报 UnionID。

## 失败模式与处理

| 失败 | 行为 |
| --- | --- |
| 问卷/支付从非微信浏览器访问 | 可显示受限提示，禁止填写、提交和下单 |
| 问卷/支付会话缺 UnionID | 强制重新微信 OAuth；授权回调仍缺失时显式失败，不创建 OpenID-only 业务会话 |
| 微信小店订单缺 UnionID | 标记 provider 合同错误并终止入库，不退化为 OpenID-only 客户 |
| 支付回调 payer.openid 与订单付款身份不同 | 拒绝更新订单并记录身份冲突，不把回调账户覆盖进订单 |
| 同一 scope 强身份指向两个 Customer | 保持分开，写冲突记录 |
| 不同 appid 的 OpenID 值碰巧相同 | 因 scope 不同不关联 |
| UnionID 能力未配置或不适用 | 保留当前 Customer；低频再触发，不无限轮询 |
| 旧业务行只有 UnionID | 按明确开放平台 scope 回填；scope 不明确或多候选时保持 customer_id 为空并计入对账 |

## 取舍

好处是企微的首次建客仍不被 UnionID 阻断，而与本部署实际能力绑定的公众号问卷、微信支付和微信小店
则获得更强的跨生态归一保证。代价是这三类入口在 UnionID 配置异常时会 fail closed，必须明确监控授权失败而不能静默降级。

## 被替代方案

- “UnionID 是唯一主身份”：无法覆盖 UnionID 不下发或延迟下发的合法场景。
- “ExternalUserID 是全局主键”：它受企业与调用方 scope 约束，无法跨微信应用通用。
- “付款人就是客户”：代付和礼物订单会错误发放权益或污染画像。
- “手机号兜底自动合并”：手机号重分配、家庭共用和手填错误风险过高。

## 参考

- [企业微信新增客户回调](https://developer.work.weixin.qq.com/document/path/92130)
- [企业微信获取客户详情](https://developer.work.weixin.qq.com/document/path/92114)
- [微信支付 JSAPI 开发指引](https://pay.wechatpay.cn/doc/v3/merchant/4012791870)
- [微信支付成功回调](https://pay.wechatpay.cn/doc/v3/merchant/4012791861)
- [微信支付 OpenID/UnionID 通用规则](https://pay.wechatpay.cn/doc/v3/partner/4012081935)
- [小程序登录 code2Session](https://developers.weixin.qq.com/miniprogram/dev/OpenApiDoc/user-login/code2Session.html)
- [小程序支付后获取 UnionID](https://developers.weixin.qq.com/miniprogram/dev/OpenApiDoc/user-info/BasicInfo/getPaidUnionid.html)

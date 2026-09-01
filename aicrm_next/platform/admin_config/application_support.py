# ruff: noqa: F401
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aicrm_next.platform.shared.admin_action_runtime import ensure_admin_action_token
from aicrm_next.platform.platform_foundation.external_effects.jobs import (
    SCHEDULER_BATCH_SIZE_KEY,
    SCHEDULER_ENABLED_KEY,
    SCHEDULER_INTERVAL_SECONDS_KEY,
)
from aicrm_next.platform.platform_foundation.external_effects.models import WECOM_MESSAGE_PRIVATE_SEND
from aicrm_next.platform.platform_foundation.external_effects.realtime import (
    CHANNEL_ENTRY_REALTIME_EFFECT_TYPES,
    REALTIME_ALLOWED_TYPES_KEY,
    REALTIME_ENABLED_KEY,
    REALTIME_MAX_CONCURRENCY_KEY,
)
from aicrm_next.platform.platform_foundation.push_center.capability_registry import (
    PushCapability,
    get_push_capability,
    visible_push_capabilities,
)
from aicrm_next.platform.platform_foundation.push_center.repository import PushCenterRepository

from .category_registry import CONFIG_CATEGORIES, ConfigCategory, ConfigCategoryField, get_config_category
from .definitions import APP_SETTING_DEFINITIONS
from .repository import AdminConfigRepository
from .runtime_definitions import RUNTIME_CONFIG_DEFINITIONS
from .schema import CONFIG_SCHEMA, build_config_checklist, validate_config
from .secret_settings import current_setting_values, public_changed_row, setting_details, stored_value_matches
from .settings import SENSITIVE_KEYS, mask_value

if TYPE_CHECKING:
    from .application import AdminConfigReadService


TARGET_APP_SETTING = "app_setting"
TARGET_CONFIG_CATEGORY_ENABLED = "config_category_enabled"
TARGET_ADMIN_USER = "admin_user"
TARGET_MCP_TOOL_SETTING = "mcp_tool_setting"
TARGET_MARKETING_AUTOMATION_CONFIG = "marketing_automation_config"
TARGET_PUSH_CAPABILITY = "push_capability"
DEFAULT_SIGNUP_CONVERSION_KEY = "signup_conversion_v1"
DEFAULT_MARKETING_AUTOMATION_NAME = "自动化转化问卷初判"
DEFAULT_MARKETING_TARGET_EVENT = "signup_success"
DEFAULT_MARKETING_CHANNEL_TYPE = "text_message"
DEFAULT_MARKETING_CORE_THRESHOLD = 3
DEFAULT_MARKETING_TOP_THRESHOLD = 4
DEFAULT_MARKETING_DAY_START_HOUR = 9
DEFAULT_MARKETING_QUIET_HOUR_START = 23
DEFAULT_MARKETING_TIMEZONE = "Asia/Shanghai"
DEFAULT_SILENT_THRESHOLD_DAYS_BY_POOL = {
    "new_user": 7,
    "inactive_normal": 7,
    "inactive_focus": 7,
    "active_normal": 7,
    "active_focus": 7,
}

ROLE_LABELS = {
    "super_admin": "超级管理员",
    "config_admin": "配置管理员",
    "automation_admin": "自动化管理员",
    "questionnaire_admin": "问卷管理员",
    "viewer": "只读成员",
    "service_period_grid_collaborator": "周期商品数据协作者",
}
ADMIN_ASSIGNABLE_ROLE_OPTIONS = [
    {"value": key, "label": value}
    for key, value in ROLE_LABELS.items()
    if key not in {"super_admin", "service_period_grid_collaborator"}
]
ADMIN_LEVEL_LABELS = {"super_admin": "超级管理员", "admin": "管理员"}
MCP_TOOL_GROUP_LABELS = {
    "crm": "客户查询",
    "tasks": "触达任务",
    "config": "配置规则",
    "ops": "同步任务",
    "misc": "其他",
}
HTTP_URL_SETTING_KEYS = {
    "ADMIN_LOGIN_REDIRECT_URI",
    "WECOM_API_BASE",
    "DEEPSEEK_BASE_URL",
    "OPENCLAW_WEBHOOK_URL",
    "QUESTIONNAIRE_SUBMIT_WEBHOOK_URL",
    "WECHAT_PAY_NOTIFY_URL",
    "WECHAT_PAY_API_BASE",
    "ALIPAY_SERVER_URL",
    "ALIPAY_NOTIFY_URL",
    "ALIPAY_RETURN_URL",
    "WECHAT_SHOP_API_BASE",
    "AICRM_AUTH_ISSUER",
}
JSON_SETTING_KEYS = {"WECHAT_PAY_PRODUCT_CATALOG_JSON"}
PUSH_CAPABILITY_ADVANCED_KEYS = (
    ("OPENCLAW_WEBHOOK_URL", "OPENCLAW_WEBHOOK_URL", "OpenClaw Webhook 地址"),
    ("QUESTIONNAIRE_SUBMIT_WEBHOOK_URL", "QUESTIONNAIRE_SUBMIT_WEBHOOK_URL", "问卷提交 Webhook 地址"),
)
EXTRA_SETTING_DEFINITIONS: dict[str, dict[str, Any]] = {
    **RUNTIME_CONFIG_DEFINITIONS,
    "AICRM_ONEID_READ_ENABLED": {
        "key": "AICRM_ONEID_READ_ENABLED",
        "label": "OneID 侧边栏读切换",
        "mode": "editable",
        "input_type": "text",
        "type": "boolean",
        "section": "sidebar_identity",
        "capability_id": "core.crm",
        "description": "默认开启；设为 false 可回退到既有企微关系授权读取，OneID 写入和合并血缘仍保留。",
    },
    "WECHAT_OPEN_PLATFORM_SCOPE_ID": {
        "key": "WECHAT_OPEN_PLATFORM_SCOPE_ID",
        "label": "微信开放平台身份作用域",
        "mode": "editable",
        "input_type": "text",
        "type": "string",
        "section": "sidebar_identity",
        "capability_id": "core.crm",
        "description": "UnionID 唯一性所属的微信开放平台作用域；为空时使用当前私有化默认作用域。",
    },
    "SIDEBAR_PRODUCT_CONTEXT_TOKEN_TTL_SECONDS": {
        "key": "SIDEBAR_PRODUCT_CONTEXT_TOKEN_TTL_SECONDS",
        "label": "侧边栏商品上下文有效期",
        "mode": "editable",
        "input_type": "number",
        "type": "integer",
        "description": "侧边栏商品签名上下文 token 有效期（秒）。",
        "min": 1,
    },
    "SIDEBAR_CONTEXT_TOKEN_TTL_SECONDS": {
        "key": "SIDEBAR_CONTEXT_TOKEN_TTL_SECONDS",
        "label": "侧边栏上下文兼容有效期",
        "mode": "editable",
        "input_type": "number",
        "type": "integer",
        "description": "旧侧边栏上下文 token 有效期兼容配置（秒）。",
        "min": 1,
    },
    "AICRM_SIDEBAR_JSSDK_ADAPTER_MODE": {
        "key": "AICRM_SIDEBAR_JSSDK_ADAPTER_MODE",
        "label": "侧边栏 JSSDK Adapter 模式",
        "mode": "editable",
        "input_type": "text",
        "type": "string",
        "description": "默认安全模式；仅显式 real_enabled 且真实开关打开时才允许真实 JSSDK 调用。",
    },
    "AICRM_SIDEBAR_JSSDK_REAL_ENABLED": {
        "key": "AICRM_SIDEBAR_JSSDK_REAL_ENABLED",
        "label": "侧边栏 JSSDK 真实调用开关",
        "mode": "editable",
        "input_type": "text",
        "type": "boolean",
        "description": "填写 true / false 或 1 / 0。",
    },
    "AICRM_SIDEBAR_JSSDK_SECRET": {
        "key": "AICRM_SIDEBAR_JSSDK_SECRET",
        "label": "侧边栏 JSSDK Secret",
        "mode": "masked",
        "input_type": "password",
        "type": "secret",
        "description": "侧边栏 JSSDK 专用密钥；留空表示保持原值。",
    },
    "AICRM_SIDEBAR_JSSDK_TIMEOUT_SECONDS": {
        "key": "AICRM_SIDEBAR_JSSDK_TIMEOUT_SECONDS",
        "label": "侧边栏 JSSDK 超时时间",
        "mode": "editable",
        "input_type": "number",
        "type": "integer",
        "description": "侧边栏 JSSDK 请求超时时间（秒）。",
        "min": 1,
    },
    "AICRM_SIDEBAR_IMAGE_QUICK_KEYWORDS": {
        "key": "AICRM_SIDEBAR_IMAGE_QUICK_KEYWORDS",
        "label": "侧边栏图片素材快捷关键词",
        "mode": "editable",
        "input_type": "textarea",
        "type": "string",
        "description": "逗号或换行分隔；留空时隐藏快捷词，配置时必须为 3 至 5 个不重复关键词。",
    },
    "AICRM_QUESTIONNAIRE_EXTERNAL_PUSH_MODE": {
        "key": "AICRM_QUESTIONNAIRE_EXTERNAL_PUSH_MODE",
        "label": "问卷外推模式",
        "mode": "readonly",
        "input_type": "text",
        "type": "string",
        "description": "已废弃：问卷外推固定只进入统一外部动作队列，legacy/shadow 不再恢复同步外呼。",
    },
    "AICRM_WECOM_EXECUTION_MODE": {
        "key": "AICRM_WECOM_EXECUTION_MODE",
        "label": "企微执行模式",
        "mode": "editable",
        "input_type": "select",
        "type": "string",
        "options": ["disabled", "dry_run", "execute"],
        "description": "统一企微执行主开关：disabled 不执行，dry_run 只验收配置，execute 才允许真实企微外呼。",
    },
    "AICRM_WECOM_ENABLED_EFFECT_TYPES": {
        "key": "AICRM_WECOM_ENABLED_EFFECT_TYPES",
        "label": "企微允许执行 effect types",
        "mode": "editable",
        "input_type": "textarea",
        "type": "string",
        "description": "逗号或换行分隔，仅允许企微 effect type；留空表示没有企微真实执行白名单。",
    },
    "AICRM_EXTERNAL_EFFECT_WEBHOOK_EXECUTE": {
        "key": "AICRM_EXTERNAL_EFFECT_WEBHOOK_EXECUTE",
        "label": "Webhook 队列真实执行",
        "mode": "editable",
        "input_type": "text",
        "type": "boolean",
        "description": "开启后仍只执行允许列表中的 webhook effect_type；run-due 默认仍为 dry-run。",
    },
    "AICRM_EXTERNAL_EFFECT_WECOM_EXECUTE": {
        "key": "AICRM_EXTERNAL_EFFECT_WECOM_EXECUTE",
        "label": "企微消息队列真实执行",
        "mode": "editable",
        "input_type": "text",
        "type": "boolean",
        "description": "开启后仍必须命中 effect_type、owner、target、群 webhook/chat 白名单；不包含标签、欢迎语或批量放大能力。",
    },
    "AICRM_EXTERNAL_EFFECT_TEST_RECEIVER_ENABLED": {
        "key": "AICRM_EXTERNAL_EFFECT_TEST_RECEIVER_ENABLED",
        "label": "测试接收端启用",
        "mode": "editable",
        "input_type": "text",
        "type": "boolean",
        "description": "仅用于本域名 loopback 验收；生产常态应关闭。",
    },
    "AICRM_EXTERNAL_EFFECT_TEST_EXECUTION_ONLY": {
        "key": "AICRM_EXTERNAL_EFFECT_TEST_EXECUTION_ONLY",
        "label": "仅允许测试任务真实执行",
        "mode": "editable",
        "input_type": "text",
        "type": "boolean",
        "description": "开启后非 test_loopback / is_test 任务即使命中 allowlist 也会被阻断。",
    },
    "AICRM_EXTERNAL_EFFECT_ALLOWED_TYPES": {
        "key": "AICRM_EXTERNAL_EFFECT_ALLOWED_TYPES",
        "label": "允许执行的外部动作类型",
        "mode": "editable",
        "input_type": "text",
        "type": "string",
        "description": "逗号分隔；禁止 *。问卷外推填写 webhook.questionnaire_submission.push。",
    },
    REALTIME_ENABLED_KEY: {
        "key": REALTIME_ENABLED_KEY,
        "label": "统一队列实时唤醒",
        "mode": "editable",
        "input_type": "text",
        "type": "boolean",
        "description": "Deprecated compatibility alias（owner=integration_gateway，delete_after=2026-10-01）；当前由 typed WeCom execution config 派生，callback durable worker 内同步 claim，不再异步线程池执行。",
    },
    REALTIME_ALLOWED_TYPES_KEY: {
        "key": REALTIME_ALLOWED_TYPES_KEY,
        "label": "实时唤醒外部动作类型",
        "mode": "editable",
        "input_type": "text",
        "type": "string",
        "description": "Deprecated compatibility alias（delete_after=2026-10-01）；新配置使用 AICRM_WECOM_ENABLED_EFFECT_TYPES。",
    },
    REALTIME_MAX_CONCURRENCY_KEY: {
        "key": REALTIME_MAX_CONCURRENCY_KEY,
        "label": "实时唤醒并发上限",
        "mode": "editable",
        "input_type": "number",
        "type": "integer",
        "description": "Retired compatibility setting（delete_after=2026-10-01）；进程内 realtime executor 已删除，此值不再控制执行并发。",
        "min": 1,
    },
    "AICRM_EXTERNAL_EFFECT_ALLOWED_BASE_HOSTS": {
        "key": "AICRM_EXTERNAL_EFFECT_ALLOWED_BASE_HOSTS",
        "label": "允许生成 loopback URL 的域名",
        "mode": "editable",
        "input_type": "text",
        "type": "string",
        "description": "逗号分隔；生产应为 www.youcangogogo.com,youcangogogo.com，禁止 localhost / 127.0.0.1 / *。",
    },
    "AICRM_EXTERNAL_EFFECT_ALLOWED_OWNER_USERIDS": {
        "key": "AICRM_EXTERNAL_EFFECT_ALLOWED_OWNER_USERIDS",
        "label": "企微默认发送账号",
        "mode": "editable",
        "input_type": "text",
        "type": "string",
        "description": "兼容旧 key；当前执行层取第一个值作为默认 sender，不是完整 allowlist。",
    },
    "AICRM_WECOM_DEFAULT_SENDER_USERID": {
        "key": "AICRM_WECOM_DEFAULT_SENDER_USERID",
        "label": "企微默认 sender_userid",
        "mode": "editable",
        "input_type": "text",
        "type": "string",
        "description": "真实企微私信/群发在 payload 未指定 sender 时使用的默认发送账号；优先级高于旧 key。",
    },
    "AICRM_EXTERNAL_EFFECT_ALLOWED_TARGET_EXTERNAL_USERIDS": {
        "key": "AICRM_EXTERNAL_EFFECT_ALLOWED_TARGET_EXTERNAL_USERIDS",
        "label": "允许执行的私信客户 external_userid",
        "mode": "editable",
        "input_type": "textarea",
        "type": "string",
        "description": "逗号或换行分隔；真实私信执行必须单目标且命中。",
    },
    "AICRM_EXTERNAL_EFFECT_ALLOWED_GROUP_OPS_WEBHOOK_KEYS": {
        "key": "AICRM_EXTERNAL_EFFECT_ALLOWED_GROUP_OPS_WEBHOOK_KEYS",
        "label": "允许执行的群运营 webhook key",
        "mode": "editable",
        "input_type": "text",
        "type": "string",
        "description": "逗号分隔；真实群运营消息执行必须命中。",
    },
    "AICRM_EXTERNAL_EFFECT_ALLOWED_GROUP_CHAT_IDS": {
        "key": "AICRM_EXTERNAL_EFFECT_ALLOWED_GROUP_CHAT_IDS",
        "label": "允许执行的群 chat_id",
        "mode": "editable",
        "input_type": "textarea",
        "type": "string",
        "description": "逗号或换行分隔；为空时由 adapter 的单群和 webhook key 约束兜底。",
    },
    "AICRM_WECOM_PRIVATE_ADAPTER_MODE": {
        "key": "AICRM_WECOM_PRIVATE_ADAPTER_MODE",
        "capability_id": "core.automation",
        "label": "企微私信群发 Adapter 模式",
        "mode": "editable",
        "input_type": "text",
        "type": "string",
        "description": "disabled / fake / staging / production；渠道码欢迎语兜底使用该 adapter。",
    },
    "AICRM_ENABLE_REAL_WECOM_PRIVATE_MESSAGE": {
        "key": "AICRM_ENABLE_REAL_WECOM_PRIVATE_MESSAGE",
        "capability_id": "core.automation",
        "label": "允许真实企微私信群发",
        "mode": "editable",
        "input_type": "text",
        "type": "boolean",
        "description": "开启后 wecom.message.private.send 可调用企微 add_msg_template 单客户目标。",
    },
    "AICRM_WECOM_GROUP_ADAPTER_MODE": {
        "key": "AICRM_WECOM_GROUP_ADAPTER_MODE",
        "capability_id": "core.automation",
        "label": "企微客户群 Adapter 模式",
        "mode": "editable",
        "input_type": "text",
        "type": "string",
        "description": "disabled / fake / staging / production；群运营群消息使用。",
    },
    "AICRM_ENABLE_REAL_WECOM_GROUP_MESSAGE": {
        "key": "AICRM_ENABLE_REAL_WECOM_GROUP_MESSAGE",
        "capability_id": "core.automation",
        "label": "允许真实企微客户群群发",
        "mode": "editable",
        "input_type": "text",
        "type": "boolean",
        "description": "开启后 wecom.message.group.send 可调用企微 add_msg_template 群目标。",
    },
    "AICRM_EXTERNAL_EFFECT_WEBHOOK_TIMEOUT_SECONDS": {
        "key": "AICRM_EXTERNAL_EFFECT_WEBHOOK_TIMEOUT_SECONDS",
        "label": "Webhook 队列请求超时",
        "mode": "editable",
        "input_type": "number",
        "type": "integer",
        "description": "External Effect Queue 调用 webhook 的超时时间（秒）。",
        "min": 1,
    },
    SCHEDULER_ENABLED_KEY: {
        "key": SCHEDULER_ENABLED_KEY,
        "label": "统一队列自动调度",
        "mode": "editable",
        "input_type": "text",
        "type": "boolean",
        "description": "开启后由统一调度器定时捞所有到期 external_effect_job；能力开关只表示允许执行。",
    },
    SCHEDULER_INTERVAL_SECONDS_KEY: {
        "key": SCHEDULER_INTERVAL_SECONDS_KEY,
        "label": "统一队列调度间隔",
        "mode": "editable",
        "input_type": "number",
        "type": "integer",
        "description": "建议 60 秒，即每 1 分钟统一扫描全部到期外部动作队列。",
        "min": 60,
    },
    SCHEDULER_BATCH_SIZE_KEY: {
        "key": SCHEDULER_BATCH_SIZE_KEY,
        "label": "统一队列每轮处理上限",
        "mode": "editable",
        "input_type": "number",
        "type": "integer",
        "description": "调度器每轮最多处理多少条到期任务；实际仍逐条经过安全门禁和 adapter。",
        "min": 1,
    },
    "AICRM_EXTERNAL_EFFECT_PAYMENT_EXECUTE": {
        "key": "AICRM_EXTERNAL_EFFECT_PAYMENT_EXECUTE",
        "label": "支付查询队列真实执行（预留）",
        "mode": "editable",
        "input_type": "text",
        "type": "boolean",
        "description": "预留配置位；当前 External Effect Queue 未注册真实支付查询 adapter，开启不会产生真实支付查询。",
    },
    "AICRM_EXTERNAL_EFFECT_FEISHU_EXECUTE": {
        "key": "AICRM_EXTERNAL_EFFECT_FEISHU_EXECUTE",
        "label": "Feishu 通知队列真实执行（预留）",
        "mode": "editable",
        "input_type": "text",
        "type": "boolean",
        "description": "预留配置位；当前 External Effect Queue 未注册真实 Feishu adapter。",
    },
    "AICRM_EXTERNAL_EFFECT_OPENCLAW_EXECUTE": {
        "key": "AICRM_EXTERNAL_EFFECT_OPENCLAW_EXECUTE",
        "label": "OpenClaw 推送队列真实执行（预留）",
        "mode": "editable",
        "input_type": "text",
        "type": "boolean",
        "description": "预留配置位；当前 OpenClaw 真实执行仍由 integration_gateway 安全边界控制。",
    },
    "AICRM_EXTERNAL_EFFECT_MEDIA_UPLOAD_EXECUTE": {
        "key": "AICRM_EXTERNAL_EFFECT_MEDIA_UPLOAD_EXECUTE",
        "label": "素材上传队列真实执行（预留）",
        "mode": "editable",
        "input_type": "text",
        "type": "boolean",
        "description": "预留配置位；当前 External Effect Queue 未注册真实素材上传 adapter。",
    },
    "WECHAT_SHOP_ENABLED": {
        "key": "WECHAT_SHOP_ENABLED",
        "label": "微信小店已启用",
        "mode": "editable",
        "input_type": "text",
        "type": "boolean",
        "description": "配置中心中的微信小店开关；不会在本次改造中新增业务拦截。",
    },
    "WECHAT_SHOP_APPID": {
        "key": "WECHAT_SHOP_APPID",
        "label": "微信小店 AppID",
        "mode": "editable",
        "input_type": "text",
        "type": "string",
        "description": "微信小店接口使用的 AppID。",
    },
    "WECHAT_SHOP_APPSECRET": {
        "key": "WECHAT_SHOP_APPSECRET",
        "label": "微信小店 AppSecret",
        "mode": "masked",
        "input_type": "password",
        "type": "secret",
        "description": "微信小店接口密钥；留空表示保持原值。",
    },
    "WECHAT_SHOP_API_BASE": {
        "key": "WECHAT_SHOP_API_BASE",
        "label": "微信小店 API Base",
        "mode": "editable",
        "input_type": "url",
        "type": "string",
        "description": "默认 https://api.weixin.qq.com。",
    },
    "WECHAT_SHOP_CALLBACK_TOKEN": {
        "key": "WECHAT_SHOP_CALLBACK_TOKEN",
        "label": "微信小店 Callback Token",
        "mode": "masked",
        "input_type": "password",
        "type": "secret",
        "description": "微信小店回调签名 token；留空表示保持原值。",
    },
    "WECHAT_SHOP_HTTP_TIMEOUT_SECONDS": {
        "key": "WECHAT_SHOP_HTTP_TIMEOUT_SECONDS",
        "label": "微信小店请求超时",
        "mode": "editable",
        "input_type": "number",
        "type": "integer",
        "description": "微信小店接口请求超时时间（秒）。",
        "min": 1,
    },
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return _text(value).lower() in {"1", "true", "yes", "y", "on"}


def _filter_text_match(row: dict[str, Any], fields: list[str], query: str) -> bool:
    normalized = _text(query).lower()
    if not normalized:
        return True
    haystack = " ".join(_text(row.get(field)).lower() for field in fields)
    return normalized in haystack


def _normalize_int(value: Any, *, field_name: str, minimum: int | None = None) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} 必须是整数") from exc
    if minimum is not None and number < minimum:
        raise ValueError(f"{field_name} 不能小于 {minimum}")
    return number


def _validate_known_setting(key: str, value: str) -> str:
    normalized = _text(value)
    if key == "AICRM_SIDEBAR_IMAGE_QUICK_KEYWORDS":
        values: list[str] = []
        raw_items = normalized.replace("\r", "\n").replace("，", ",").replace("\n", ",").split(",")
        for item in raw_items:
            keyword = item.strip()
            if keyword and keyword not in values:
                values.append(keyword)
        if values and not 3 <= len(values) <= 5:
            raise ValueError(f"{key} 留空或配置 3 至 5 个不重复关键词")
        return ",".join(values)
    if key in {
        "WECOM_CORP_TAG_LIMIT",
        "WECOM_ARCHIVE_TIMEOUT",
        "DEEPSEEK_TIMEOUT_SECONDS",
        "OPENCLAW_FOCUS_MESSAGE_WEBHOOK_TIMEOUT_SECONDS",
        "WECHAT_PAY_TIMEOUT_SECONDS",
        "OUTBOUND_WEBHOOK_RETRY_MAX_ATTEMPTS",
        "OUTBOUND_WEBHOOK_RETRY_INTERVAL_SECONDS",
        "QUESTIONNAIRE_SUBMIT_WEBHOOK_TIMEOUT_SECONDS",
        "QUESTIONNAIRE_EXTERNAL_PUSH_TIMEOUT_SECONDS",
        "AICRM_EXTERNAL_EFFECT_WEBHOOK_TIMEOUT_SECONDS",
    }:
        return str(_normalize_int(normalized or "0", field_name=key, minimum=1))
    if key in {
        "OUTBOUND_WEBHOOK_RETRY_ENABLED",
        "DEEPSEEK_ENABLED",
        "WECHAT_PAY_ENABLED",
        "QUESTIONNAIRE_EXTERNAL_PUSH_GLOBAL_ENABLED",
        "AICRM_EXTERNAL_EFFECT_WEBHOOK_EXECUTE",
        "AICRM_EXTERNAL_EFFECT_WECOM_EXECUTE",
        REALTIME_ENABLED_KEY,
        "AICRM_EXTERNAL_EFFECT_TEST_RECEIVER_ENABLED",
        "AICRM_EXTERNAL_EFFECT_TEST_EXECUTION_ONLY",
        "AICRM_EXTERNAL_EFFECT_PAYMENT_EXECUTE",
        "AICRM_EXTERNAL_EFFECT_FEISHU_EXECUTE",
        "AICRM_EXTERNAL_EFFECT_OPENCLAW_EXECUTE",
        "AICRM_EXTERNAL_EFFECT_MEDIA_UPLOAD_EXECUTE",
        "AICRM_ENABLE_REAL_WECOM_PRIVATE_MESSAGE",
        "AICRM_ENABLE_REAL_WECOM_GROUP_MESSAGE",
    }:
        lowered = normalized.lower()
        if lowered in {"1", "true", "yes", "y", "on"}:
            return "true"
        if not lowered or lowered in {"0", "false", "no", "n", "off"}:
            return "false"
        raise ValueError(f"{key} 必须是 true / false")
    if key == "AICRM_QUESTIONNAIRE_EXTERNAL_PUSH_MODE":
        return "queue"
    if key == "AICRM_WECOM_EXECUTION_MODE":
        if not normalized:
            return "disabled"
        if normalized not in {"disabled", "dry_run", "execute"}:
            raise ValueError(f"{key} 只允许 disabled / dry_run / execute")
        return normalized
    if key in {"AICRM_EXTERNAL_EFFECT_ALLOWED_TYPES", REALTIME_ALLOWED_TYPES_KEY}:
        if "*" in {item.strip() for item in normalized.replace("\n", " ").replace(",", " ").split() if item.strip()}:
            raise ValueError(f"{key} 不允许使用 *")
        return normalized
    if key == "AICRM_WECOM_ENABLED_EFFECT_TYPES":
        allowed = {
            "wecom.contact.tag.mark",
            "wecom.contact.tag.unmark",
            "wecom.welcome_message.send",
            "wecom.message.private.send",
            "wecom.message.group.send",
            "wecom.profile.update",
            "wecom.media.upload",
        }
        values = [item.strip() for item in normalized.replace("\n", ",").split(",") if item.strip()]
        invalid = sorted({item for item in values if item not in allowed})
        if invalid:
            raise ValueError(f"{key} 包含不支持的企微 effect type: {', '.join(invalid)}")
        return ",".join(values)
    if key in {"AICRM_WECOM_PRIVATE_ADAPTER_MODE", "AICRM_WECOM_GROUP_ADAPTER_MODE"}:
        allowed_modes = {"disabled", "fake", "staging", "production"}
        if normalized and normalized not in allowed_modes:
            raise ValueError(f"{key} 只允许 disabled / fake / staging / production")
        return normalized or "disabled"
    if key == "AICRM_EXTERNAL_EFFECT_ALLOWED_BASE_HOSTS":
        blocked = {"*", "localhost", "127.0.0.1", "::1", "testserver"}
        hosts = {item.strip().lower().split(":", 1)[0] for item in normalized.replace("\n", ",").split(",") if item.strip()}
        if hosts & blocked:
            raise ValueError("AICRM_EXTERNAL_EFFECT_ALLOWED_BASE_HOSTS 不允许使用本地、测试或通配域名")
        return normalized
    if (
        key
        in {
            "WECOM_API_BASE",
            "DEEPSEEK_BASE_URL",
            "OPENCLAW_WEBHOOK_URL",
            "QUESTIONNAIRE_SUBMIT_WEBHOOK_URL",
            "WECHAT_PAY_NOTIFY_URL",
            "WECHAT_PAY_API_BASE",
        }
        and normalized
        and not normalized.startswith(("http://", "https://"))
    ):
        raise ValueError(f"{key} 必须以 http:// 或 https:// 开头")
    if key == "WECHAT_PAY_PRODUCT_CATALOG_JSON" and normalized:
        try:
            json.loads(normalized)
        except ValueError as exc:
            raise ValueError("WECHAT_PAY_PRODUCT_CATALOG_JSON 必须是合法 JSON") from exc
    return normalized


def _input_type_for_schema_type(field_type: str) -> str:
    if field_type == "integer":
        return "number"
    if field_type == "secret":
        return "password"
    return "text"


def _schema_setting_metadata() -> dict[str, dict[str, Any]]:
    metadata: dict[str, dict[str, Any]] = {}
    for group in CONFIG_SCHEMA.values():
        for key, field in group["fields"].items():
            field_type = _text(field.get("type")) or "string"
            metadata[key] = {
                "key": key,
                "label": _text(field.get("label")) or key,
                "mode": "masked" if field_type == "secret" or key in SENSITIVE_KEYS else "editable",
                "input_type": _input_type_for_schema_type(field_type),
                "type": field_type,
                "description": _text(field.get("help")),
                "required": bool(field.get("required")),
                "default": _text(field.get("default")),
                "min": field.get("min"),
                "max": field.get("max"),
            }
    return metadata


def _setting_metadata_map() -> dict[str, dict[str, Any]]:
    metadata = _schema_setting_metadata()
    for item in APP_SETTING_DEFINITIONS:
        key = _text(item.get("key"))
        if not key:
            continue
        merged = {**metadata.get(key, {}), **dict(item)}
        merged.setdefault("type", "secret" if merged.get("mode") == "masked" else "string")
        merged.setdefault("required", False)
        merged.setdefault("description", "")
        metadata[key] = merged
    for key, item in EXTRA_SETTING_DEFINITIONS.items():
        metadata[key] = {**metadata.get(key, {}), **dict(item)}
    for category in CONFIG_CATEGORIES:
        if category.enabled_key.startswith("CONFIG_CATEGORY_"):
            metadata.setdefault(
                category.enabled_key,
                {
                    "key": category.enabled_key,
                    "label": f"{category.label}生效开关",
                    "mode": "editable",
                    "input_type": "text",
                    "type": "boolean",
                    "description": "仅控制配置中心类目展示状态，不接入业务拦截。",
                    "required": False,
                },
            )
    return metadata


def _metadata_for_setting(key: str) -> dict[str, Any]:
    normalized_key = _text(key)
    metadata = _setting_metadata_map().get(normalized_key, {})
    if metadata:
        return dict(metadata)
    return {
        "key": normalized_key,
        "label": normalized_key,
        "mode": "masked" if normalized_key in SENSITIVE_KEYS else "editable",
        "input_type": "password" if normalized_key in SENSITIVE_KEYS else "text",
        "type": "secret" if normalized_key in SENSITIVE_KEYS else "string",
        "description": "",
        "required": False,
    }


def _is_boolean_setting(key: str, metadata: dict[str, Any]) -> bool:
    return _text(metadata.get("type")) == "boolean" or _text(key).endswith("_ENABLED")


def _is_integer_setting(metadata: dict[str, Any]) -> bool:
    return _text(metadata.get("type")) == "integer" or _text(metadata.get("input_type")) == "number"


def _normalize_boolean_text(value: Any) -> str:
    return "true" if _bool(value) else "false"


def _validate_category_setting(key: str, value: Any, metadata: dict[str, Any]) -> str:
    normalized_key = _text(key)
    normalized = _text(value)
    if _is_boolean_setting(normalized_key, metadata):
        return _normalize_boolean_text(normalized)
    if _is_integer_setting(metadata):
        minimum = metadata.get("min")
        maximum = metadata.get("max")
        minimum_int = int(minimum) if minimum is not None else 1
        number = _normalize_int(normalized or "0", field_name=normalized_key, minimum=minimum_int)
        if maximum is not None and number > int(maximum):
            raise ValueError(f"{normalized_key} 不能大于 {maximum}")
        return str(number)
    if normalized_key in HTTP_URL_SETTING_KEYS and normalized and not normalized.startswith(("http://", "https://")):
        raise ValueError(f"{normalized_key} 必须以 http:// 或 https:// 开头")
    if normalized_key in JSON_SETTING_KEYS and normalized:
        try:
            json.loads(normalized)
        except ValueError as exc:
            raise ValueError(f"{normalized_key} 必须是合法 JSON") from exc
    return _validate_known_setting(normalized_key, normalized)


def _json_loads(value: Any, *, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    text = _text(value)
    if not text:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _positive_int(value: Any, *, field_name: str, allow_none: bool = False) -> int | None:
    text = _text(value)
    if not text and allow_none:
        return None
    return _normalize_int(text if text else value, field_name=field_name, minimum=1)


def _bounded_int(value: Any, *, field_name: str, default: int, minimum: int, maximum: int | None = None) -> int:
    text = _text(value)
    number = _normalize_int(text if text else default, field_name=field_name, minimum=minimum)
    if maximum is not None and number > maximum:
        raise ValueError(f"{field_name} 不能大于 {maximum}")
    return number


def _normalize_timezone(value: Any) -> str:
    timezone = _text(value) or DEFAULT_MARKETING_TIMEZONE
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("timezone is invalid") from exc
    return timezone


def _normalize_silent_thresholds(value: Any) -> dict[str, int]:
    raw = value if isinstance(value, dict) else {}
    result: dict[str, int] = {}
    for pool_key, default_value in DEFAULT_SILENT_THRESHOLD_DAYS_BY_POOL.items():
        result[pool_key] = _bounded_int(
            raw.get(pool_key),
            field_name=f"silent_threshold_days_by_pool.{pool_key}",
            default=default_value,
            minimum=1,
        )
    return result


def _default_mcp_tool_defs() -> list[dict[str, Any]]:
    from aicrm_next.mcp_tool_catalog import MCP_TOOLS

    return [dict(item) for item in MCP_TOOLS]


def _default_tool_group(tool_name: str) -> str:
    if tool_name.startswith(("create_", "record_", "send_")):
        return "tasks"
    if "message_batch" in tool_name:
        return "ops"
    if tool_name.startswith(("get_", "resolve_", "search_")):
        return "crm"
    return "misc"


def _default_display_name(tool_name: str) -> str:
    return tool_name.replace("_", " ").title()


def _tool_group_label(value: str) -> str:
    normalized = _text(value)
    return MCP_TOOL_GROUP_LABELS.get(normalized, normalized or "-")


def _default_tool_description(tool_name: str, fallback: str = "") -> str:
    mapping = {
        "resolve_customer": "根据手机号、客户编号或 external_userid 定位客户。",
        "get_customer_context": "查看客户资料、互动记录和最近聊天。",
        "get_recent_messages": "查看客户最近聊天。",
    }
    return mapping.get(tool_name, fallback)


def _audit_action_label(action_type: str) -> str:
    mapping = {"create": "新建", "update": "更新"}
    normalized = _text(action_type)
    return mapping.get(normalized, normalized or "-")


def _capability_enabled_from_value(value: str, *, default: bool = False) -> bool:
    if not _text(value):
        return bool(default)
    return _bool(value)


def _effect_type_union_for_enabled_capabilities(read_service: "AdminConfigReadService") -> list[str]:
    effect_types: list[str] = []
    seen: set[str] = set()
    for capability in visible_push_capabilities(main_only=False):
        if not capability.toggleable or not capability.supports_real_execution:
            continue
        if not read_service._capability_enabled(capability, default=False):
            continue
        for effect_type in capability.effect_types:
            if effect_type not in seen:
                seen.add(effect_type)
                effect_types.append(effect_type)
        if capability.key == "welcome_message" and WECOM_MESSAGE_PRIVATE_SEND not in seen:
            seen.add(WECOM_MESSAGE_PRIVATE_SEND)
            effect_types.append(WECOM_MESSAGE_PRIVATE_SEND)
    return effect_types


def _derived_gate_payload(effect_types: list[str], capabilities: list[PushCapability]) -> dict[str, Any]:
    enabled_keys = {capability.key for capability in capabilities}
    effect_type_set = set(effect_types)
    channel_entry_realtime_types = [effect_type for effect_type in CHANNEL_ENTRY_REALTIME_EFFECT_TYPES if effect_type in effect_type_set]
    webhook_execute = any(
        capability.key in enabled_keys and capability.supports_real_execution and capability.adapter_family in {"webhook", "legacy_webhook", "mixed"}
        for capability in visible_push_capabilities(main_only=False)
    )
    wecom_execute = any(effect_type.startswith("wecom.") for effect_type in effect_types)
    payment_execute = any(capability.key in enabled_keys and capability.adapter_family == "payment" for capability in capabilities)
    feishu_execute = "feishu.webhook.notify" in effect_type_set
    openclaw_execute = "openclaw.context.push" in effect_type_set
    media_upload_execute = bool({"media.storage.upload", "wecom.media.upload"} & effect_type_set)
    test_receiver_enabled = "test_receiver" in enabled_keys
    return {
        "allowed_effect_types": effect_types,
        "webhook_execute": webhook_execute,
        "wecom_execute": wecom_execute,
        "payment_execute": payment_execute,
        "feishu_execute": feishu_execute,
        "openclaw_execute": openclaw_execute,
        "media_upload_execute": media_upload_execute,
        "test_receiver_enabled": test_receiver_enabled,
        "realtime_enabled": bool(channel_entry_realtime_types),
        "realtime_allowed_types": channel_entry_realtime_types,
    }


def _capability_requires_webhook_gate(capability: PushCapability) -> bool:
    return capability.adapter_family in {"webhook", "legacy_webhook"} or any(
        effect_type.startswith("webhook.")
        or effect_type
        in {
            "ai_assist.campaign.message.loopback",
            "group_ops.message.loopback",
            "group_ops.webhook.action.loopback",
        }
        for effect_type in capability.effect_types
    )


def _scheduler_state_for_read_service(read_service: "AdminConfigReadService") -> dict[str, Any]:
    enabled = read_service._capability_enabled_from_setting(SCHEDULER_ENABLED_KEY)
    interval_value, interval_source = read_service._setting_value_source(SCHEDULER_INTERVAL_SECONDS_KEY)
    batch_value, batch_source = read_service._setting_value_source(SCHEDULER_BATCH_SIZE_KEY)
    try:
        interval_seconds = _bounded_int(interval_value, field_name=SCHEDULER_INTERVAL_SECONDS_KEY, default=60, minimum=60, maximum=86400)
    except ValueError:
        interval_seconds = 60
    try:
        batch_size = _bounded_int(batch_value, field_name=SCHEDULER_BATCH_SIZE_KEY, default=20, minimum=1, maximum=500)
    except ValueError:
        batch_size = 20
    return {
        "enabled": enabled,
        "status": "enabled" if enabled else "disabled",
        "status_label": "自动调度已开启" if enabled else "自动调度未开启",
        "interval_seconds": interval_seconds,
        "interval_minutes": max(1, round(interval_seconds / 60)),
        "batch_size": batch_size,
        "interval_source": interval_source,
        "batch_size_source": batch_source,
        "setting_key": SCHEDULER_ENABLED_KEY,
        "description": "统一调度器按固定间隔扫描全部到期任务；业务能力开关只表示允许执行。",
    }

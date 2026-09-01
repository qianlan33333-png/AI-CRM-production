from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ConfigCategoryField:
    key: str
    block_title: str = "基础信息"
    readonly: bool = False


@dataclass(frozen=True)
class ConfigCategory:
    key: str
    label: str
    group_label: str
    enabled_key: str
    fields: tuple[ConfigCategoryField, ...]
    detail_href: str
    check_supported: bool
    sort_order: int


def field(key: str, *, block_title: str = "基础信息", readonly: bool = False) -> ConfigCategoryField:
    return ConfigCategoryField(key=key, block_title=block_title, readonly=readonly)


CONFIG_CATEGORIES: tuple[ConfigCategory, ...] = (
    ConfigCategory(
        key="wecom_base",
        label="企业微信基础",
        group_label="核心连接能力",
        enabled_key="CONFIG_CATEGORY_WECOM_BASE_ENABLED",
        detail_href="/admin/config/detail/wecom_base",
        check_supported=True,
        sort_order=10,
        fields=(
            field("WECOM_CORP_ID"),
            field("WECOM_AGENT_ID"),
            field("WECOM_SECRET", block_title="密钥"),
            field("WECOM_CONTACT_SECRET", block_title="密钥"),
            field("WECOM_API_BASE", block_title="接口"),
            field("WECOM_DEFAULT_OWNER_USERID", block_title="接口"),
            field("WECOM_CALLBACK_TOKEN", block_title="回调"),
            field("WECOM_CALLBACK_AES_KEY", block_title="回调"),
            field("WECOM_ARCHIVE_SECRET", block_title="会话存档"),
            field("WECOM_PRIVATE_KEY_PATH", block_title="会话存档"),
            field("WECOM_SDK_LIB_PATH", block_title="会话存档"),
            field("WECOM_ARCHIVE_TIMEOUT", block_title="会话存档"),
            field("WECOM_CORP_TAG_LIMIT", block_title="限制"),
        ),
    ),
    ConfigCategory(
        key="admin_access",
        label="后台访问",
        group_label="后台安全",
        enabled_key="CONFIG_CATEGORY_ADMIN_ACCESS_ENABLED",
        detail_href="/admin/config/detail/admin_access",
        check_supported=True,
        sort_order=20,
        fields=(
            field("ADMIN_AUTH_MODE"),
            field("ADMIN_LOGIN_REDIRECT_URI"),
            field("ADMIN_WECHAT_TRUSTED_DOMAIN"),
        ),
    ),
    ConfigCategory(
        key="sidebar_identity",
        label="侧边栏与身份",
        group_label="后台安全",
        enabled_key="CONFIG_CATEGORY_SIDEBAR_IDENTITY_ENABLED",
        detail_href="/admin/config/detail/sidebar_identity",
        check_supported=True,
        sort_order=30,
        fields=(
            field("AICRM_ONEID_READ_ENABLED"),
            field("WECHAT_OPEN_PLATFORM_SCOPE_ID"),
            field("SIDEBAR_PRODUCT_CONTEXT_TOKEN_TTL_SECONDS"),
            field("SIDEBAR_CONTEXT_TOKEN_TTL_SECONDS"),
            field("AICRM_SIDEBAR_JSSDK_ADAPTER_MODE", block_title="企微 JSSDK"),
            field("AICRM_SIDEBAR_JSSDK_REAL_ENABLED", block_title="企微 JSSDK"),
            field("AICRM_SIDEBAR_JSSDK_SECRET", block_title="企微 JSSDK"),
            field("AICRM_SIDEBAR_JSSDK_TIMEOUT_SECONDS", block_title="企微 JSSDK"),
            field("AICRM_SIDEBAR_IMAGE_QUICK_KEYWORDS", block_title="图片素材"),
        ),
    ),
    ConfigCategory(
        key="ai_automation",
        label="AI 与自动化",
        group_label="自动化能力",
        enabled_key="DEEPSEEK_ENABLED",
        detail_href="/admin/config/detail/ai_automation",
        check_supported=True,
        sort_order=40,
        fields=(
            field("DEEPSEEK_ENABLED"),
            field("DEEPSEEK_API_KEY"),
            field("DEEPSEEK_BASE_URL"),
            field("DEEPSEEK_ROUTER_MODEL"),
            field("DEEPSEEK_EXECUTION_MODEL"),
            field("DEEPSEEK_REASONER_MODEL"),
            field("DEEPSEEK_TIMEOUT_SECONDS"),
            field("AICRM_AUTH_ISSUER", block_title="统一授权平台"),
            field("AICRM_AUTH_SESSION_HASH_PEPPER", block_title="统一授权平台"),
            field("AICRM_AUTH_JWT_SIGNING_KEY", block_title="统一授权平台"),
            field("AICRM_AUTH_TRUSTED_PROXY_ADDRESSES", block_title="统一授权平台"),
            field("AICRM_AUTH_CA_FILE", block_title="机器身份"),
            field("AICRM_AUTH_AUTOMATION_WORKER_CLIENT_ID", block_title="机器身份"),
            field("AICRM_AUTH_AUTOMATION_WORKER_CLIENT_SECRET_REF", block_title="机器身份"),
            field("AICRM_AUTH_ARCHIVE_WORKER_CLIENT_ID", block_title="机器身份"),
            field("AICRM_AUTH_ARCHIVE_WORKER_CLIENT_SECRET_REF", block_title="机器身份"),
            field("AICRM_AUTH_CALLBACK_WORKER_CLIENT_ID", block_title="机器身份"),
            field("AICRM_AUTH_CALLBACK_WORKER_CLIENT_SECRET_REF", block_title="机器身份"),
            field("AICRM_AUTH_GROUP_BROADCAST_CLIENT_ID", block_title="机器身份"),
            field("AICRM_AUTH_GROUP_BROADCAST_CLIENT_SECRET_REF", block_title="机器身份"),
            field("AICRM_AUTH_IDENTITY_CLIENT_ID", block_title="机器身份"),
            field("AICRM_AUTH_IDENTITY_CLIENT_SECRET_REF", block_title="机器身份"),
            field("AICRM_AUTH_MCP_CLIENT_ID", block_title="机器身份"),
            field("AICRM_AUTH_MCP_CLIENT_SECRET_REF", block_title="机器身份"),
            field("AICRM_AUTH_EXTERNAL_AGENT_CLIENT_ID", block_title="机器身份"),
            field("AICRM_AUTH_EXTERNAL_AGENT_CLIENT_SECRET_REF", block_title="机器身份"),
            field("AICRM_AUTH_CAMPAIGN_AGENT_CLIENT_ID", block_title="机器身份"),
            field("AICRM_AUTH_CAMPAIGN_AGENT_CLIENT_SECRET_REF", block_title="机器身份"),
            field("AICRM_AUTH_OPS_REPORTER_CLIENT_ID", block_title="机器身份"),
            field("AICRM_AUTH_OPS_REPORTER_CLIENT_SECRET_REF", block_title="机器身份"),
            field("AICRM_AUTH_OPERATION_RUNNER_CLIENT_ID", block_title="机器身份"),
            field("AICRM_AUTH_OPERATION_RUNNER_CLIENT_SECRET_REF", block_title="机器身份"),
            field("AICRM_AUTH_OUTBOUND_WEBHOOK_CLIENT_ID", block_title="Webhook HMAC"),
        ),
    ),
    ConfigCategory(
        key="webhooks_push",
        label="Webhook 与外推",
        group_label="外部联通能力",
        enabled_key="QUESTIONNAIRE_EXTERNAL_PUSH_GLOBAL_ENABLED",
        detail_href="/admin/config/detail/webhooks_push",
        check_supported=True,
        sort_order=50,
        fields=(
            field("OPENCLAW_WEBHOOK_URL"),
            field("OPENCLAW_FOCUS_MESSAGE_WEBHOOK_TIMEOUT_SECONDS"),
            field("QUESTIONNAIRE_SUBMIT_WEBHOOK_URL", block_title="问卷提交"),
            field("QUESTIONNAIRE_SUBMIT_WEBHOOK_TIMEOUT_SECONDS", block_title="问卷提交"),
            field("QUESTIONNAIRE_EXTERNAL_PUSH_GLOBAL_ENABLED", block_title="问卷外推"),
            field("QUESTIONNAIRE_EXTERNAL_PUSH_TIMEOUT_SECONDS", block_title="问卷外推"),
            field("AICRM_EXTERNAL_EFFECT_ALLOWED_BASE_HOSTS", block_title="统一队列"),
            field("AICRM_EXTERNAL_EFFECT_TEST_EXECUTION_ONLY", block_title="统一队列"),
            field("AICRM_EXTERNAL_EFFECT_ALLOWED_TYPES", block_title="统一队列"),
            field("AICRM_EXTERNAL_EFFECT_REALTIME_ENABLED", block_title="统一队列"),
            field("AICRM_EXTERNAL_EFFECT_REALTIME_ALLOWED_TYPES", block_title="统一队列"),
            field("AICRM_EXTERNAL_EFFECT_REALTIME_MAX_CONCURRENCY", block_title="统一队列"),
            field("AICRM_EXTERNAL_EFFECT_WEBHOOK_EXECUTE", block_title="Webhook 执行"),
            field("AICRM_EXTERNAL_EFFECT_WEBHOOK_TIMEOUT_SECONDS", block_title="统一队列"),
            field("AICRM_EXTERNAL_EFFECT_WECOM_EXECUTE", block_title="企微执行"),
            field("AICRM_WECOM_EXECUTION_MODE", block_title="企微执行"),
            field("AICRM_WECOM_ENABLED_EFFECT_TYPES", block_title="企微执行"),
            field("AICRM_WECOM_DEFAULT_SENDER_USERID", block_title="企微执行"),
            field("AICRM_EXTERNAL_EFFECT_ALLOWED_OWNER_USERIDS", block_title="企微执行"),
            field("AICRM_EXTERNAL_EFFECT_ALLOWED_TARGET_EXTERNAL_USERIDS", block_title="企微执行"),
            field("AICRM_EXTERNAL_EFFECT_ALLOWED_GROUP_OPS_WEBHOOK_KEYS", block_title="企微执行"),
            field("AICRM_EXTERNAL_EFFECT_ALLOWED_GROUP_CHAT_IDS", block_title="企微执行"),
            field("AICRM_WECOM_PRIVATE_ADAPTER_MODE", block_title="企微执行"),
            field("AICRM_ENABLE_REAL_WECOM_PRIVATE_MESSAGE", block_title="企微执行"),
            field("AICRM_WECOM_GROUP_ADAPTER_MODE", block_title="企微执行"),
            field("AICRM_ENABLE_REAL_WECOM_GROUP_MESSAGE", block_title="企微执行"),
            field("AICRM_EXTERNAL_EFFECT_TEST_RECEIVER_ENABLED", block_title="测试接收端"),
            field("AICRM_EXTERNAL_EFFECT_PAYMENT_EXECUTE", block_title="预留执行开关"),
            field("AICRM_EXTERNAL_EFFECT_FEISHU_EXECUTE", block_title="预留执行开关"),
            field("AICRM_EXTERNAL_EFFECT_OPENCLAW_EXECUTE", block_title="预留执行开关"),
            field("AICRM_EXTERNAL_EFFECT_MEDIA_UPLOAD_EXECUTE", block_title="预留执行开关"),
            field("OUTBOUND_WEBHOOK_RETRY_ENABLED", block_title="重试"),
            field("OUTBOUND_WEBHOOK_RETRY_MAX_ATTEMPTS", block_title="重试"),
            field("OUTBOUND_WEBHOOK_RETRY_INTERVAL_SECONDS", block_title="重试"),
        ),
    ),
    ConfigCategory(
        key="reliability",
        label="稳定性",
        group_label="平台治理",
        enabled_key="CONFIG_CATEGORY_RELIABILITY_ENABLED",
        detail_href="/admin/config/detail/reliability",
        check_supported=True,
        sort_order=60,
        fields=(
            field("HTTP_DEFAULT_TIMEOUT"),
            field("HTTP_RETRY_MAX"),
            field("HTTP_RETRY_BACKOFF_BASE"),
            field("CIRCUIT_FAILURE_THRESHOLD"),
            field("CIRCUIT_RECOVERY_SECONDS"),
            field("RQ_DEFAULT_TIMEOUT"),
            field("OUTBOX_MAX_ATTEMPTS"),
            field("OUTBOX_BACKOFF_BASE_SECONDS"),
            field("REDIS_URL", block_title="基础设施"),
        ),
    ),
    ConfigCategory(
        key="wechat_pay",
        label="微信支付",
        group_label="外部联通能力",
        enabled_key="WECHAT_PAY_ENABLED",
        detail_href="/admin/config/detail/wechat_pay",
        check_supported=True,
        sort_order=70,
        fields=(
            field("WECHAT_PAY_ENABLED"),
            field("WECHAT_PAY_APP_ID"),
            field("WECHAT_PAY_APP_SECRET", block_title="密钥"),
            field("WECHAT_PAY_MCH_ID"),
            field("WECHAT_PAY_API_V3_KEY", block_title="密钥"),
            field("WECHAT_PAY_PRIVATE_KEY_PATH", block_title="密钥"),
            field("WECHAT_PAY_CERT_SERIAL_NO", block_title="密钥"),
            field("WECHAT_PAY_PLATFORM_PUBLIC_KEY_PATH", block_title="密钥"),
            field("WECHAT_PAY_PLATFORM_CERT_SERIAL_NO", block_title="密钥"),
            field("WECHAT_PAY_NOTIFY_URL", block_title="接口"),
            field("WECHAT_PAY_API_BASE", block_title="接口"),
            field("WECHAT_PAY_TIMEOUT_SECONDS", block_title="接口"),
            field("WECHAT_PAY_PRODUCT_CATALOG_JSON", block_title="商品"),
        ),
    ),
    ConfigCategory(
        key="alipay",
        label="支付宝支付",
        group_label="外部联通能力",
        enabled_key="ALIPAY_ENABLED",
        detail_href="/admin/config/detail/alipay",
        check_supported=True,
        sort_order=80,
        fields=(
            field("ALIPAY_ENABLED"),
            field("ALIPAY_APP_ID"),
            field("ALIPAY_APP_PRIVATE_KEY_PATH", block_title="密钥"),
            field("ALIPAY_PUBLIC_KEY_PATH", block_title="密钥"),
            field("ALIPAY_SERVER_URL", block_title="接口"),
            field("ALIPAY_NOTIFY_URL", block_title="接口"),
            field("ALIPAY_RETURN_URL", block_title="接口"),
            field("ALIPAY_SIGN_TYPE", block_title="接口"),
            field("ALIPAY_TIMEOUT_EXPRESS", block_title="接口"),
        ),
    ),
    ConfigCategory(
        key="wechat_shop",
        label="微信小店",
        group_label="外部联通能力",
        enabled_key="WECHAT_SHOP_ENABLED",
        detail_href="/admin/config/detail/wechat_shop",
        check_supported=True,
        sort_order=90,
        fields=(
            field("WECHAT_SHOP_ENABLED"),
            field("WECHAT_SHOP_APPID"),
            field("WECHAT_SHOP_APPSECRET", block_title="密钥"),
            field("WECHAT_SHOP_API_BASE", block_title="接口"),
            field("WECHAT_SHOP_CALLBACK_TOKEN", block_title="回调"),
            field("WECHAT_SHOP_HTTP_TIMEOUT_SECONDS", block_title="接口"),
        ),
    ),
    ConfigCategory(
        key="wechat_oauth",
        label="公众号授权",
        group_label="外部联通能力",
        enabled_key="CONFIG_CATEGORY_WECHAT_OAUTH_ENABLED",
        detail_href="/admin/config/detail/wechat_oauth",
        check_supported=True,
        sort_order=100,
        fields=(
            field("WECHAT_MP_APP_ID"),
            field("WECHAT_MP_APP_SECRET", block_title="密钥"),
            field("WECHAT_MP_OAUTH_SCOPE", block_title="授权"),
        ),
    ),
)

CONFIG_CATEGORY_MAP: dict[str, ConfigCategory] = {category.key: category for category in CONFIG_CATEGORIES}


def get_config_category(category_key: str) -> ConfigCategory | None:
    return CONFIG_CATEGORY_MAP.get(str(category_key or "").strip())

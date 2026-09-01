from __future__ import annotations

from typing import Any

CONFIG_SCHEMA: dict[str, dict[str, Any]] = {
    "wecom_base": {
        "label": "企业微信基础配置",
        "required": True,
        "fields": {
            "WECOM_CORP_ID": {
                "type": "string",
                "required": True,
                "label": "企业ID",
                "help": "在企业微信管理后台 → 我的企业中获取",
            },
            "WECOM_SECRET": {
                "type": "secret",
                "required": True,
                "label": "自建应用Secret",
                "help": "在应用管理 → 自建应用中获取",
            },
            "WECOM_AGENT_ID": {
                "type": "string",
                "required": True,
                "label": "应用AgentId",
                "help": "在应用管理 → 自建应用中获取",
            },
            "WECOM_CONTACT_SECRET": {
                "type": "secret",
                "required": True,
                "label": "通讯录Secret",
                "help": "在管理工具 → 通讯录同步中获取",
            },
            "WECOM_API_BASE": {
                "type": "string",
                "required": False,
                "label": "API基础地址",
                "default": "https://qyapi.weixin.qq.com",
                "help": "默认为官方地址，一般无需修改",
            },
            "WECOM_DEFAULT_OWNER_USERID": {
                "type": "string",
                "required": True,
                "label": "默认员工UserID",
                "help": "系统默认关联的员工企微UserID",
            },
        },
    },
    "wecom_callback": {
        "label": "企业微信回调配置",
        "required": True,
        "fields": {
            "WECOM_CALLBACK_TOKEN": {
                "type": "secret",
                "required": True,
                "label": "回调Token",
                "help": "在应用管理 → 自建应用 → 接收消息中设置",
            },
            "WECOM_CALLBACK_AES_KEY": {
                "type": "secret",
                "required": True,
                "label": "回调EncodingAESKey",
                "help": "43位字符，用于消息加解密",
            },
        },
    },
    "wecom_archive": {
        "label": "会话存档配置",
        "required": False,
        "fields": {
            "WECOM_ARCHIVE_SECRET": {
                "type": "secret",
                "required": False,
                "label": "会话存档Secret",
            },
            "WECOM_PRIVATE_KEY_PATH": {
                "type": "string",
                "required": False,
                "label": "RSA私钥路径",
                "default": "/home/ubuntu/wecom_private_key.pem",
            },
            "WECOM_SDK_LIB_PATH": {
                "type": "string",
                "required": False,
                "label": "SDK库路径",
                "default": "/home/ubuntu/wecom-sdk/C_sdk/libWeWorkFinanceSdk_C.so",
            },
            "WECOM_ARCHIVE_TIMEOUT": {
                "type": "integer",
                "required": False,
                "label": "存档超时(秒)",
                "default": "15",
            },
        },
    },
    "wechat_mp": {
        "label": "微信公众号配置（问卷OAuth）",
        "required": False,
        "fields": {
            "WECHAT_MP_APP_ID": {
                "type": "string",
                "required": False,
                "label": "公众号AppID",
            },
            "WECHAT_MP_APP_SECRET": {
                "type": "secret",
                "required": False,
                "label": "公众号AppSecret",
            },
            "WECHAT_MP_OAUTH_SCOPE": {
                "type": "string",
                "required": False,
                "label": "OAuth范围",
                "default": "snsapi_base",
            },
        },
    },
    "wechat_pay_h5": {
        "label": "微信内H5支付（JSAPI）",
        "required": False,
        "fields": {
            "WECHAT_PAY_ENABLED": {
                "type": "string",
                "required": False,
                "label": "启用微信支付",
                "default": "false",
            },
            "WECHAT_PAY_APP_ID": {
                "type": "string",
                "required": False,
                "label": "支付公众号AppID",
            },
            "WECHAT_PAY_APP_SECRET": {
                "type": "secret",
                "required": False,
                "label": "支付公众号AppSecret",
            },
            "WECHAT_PAY_MCH_ID": {
                "type": "string",
                "required": False,
                "label": "微信支付商户号",
            },
            "WECHAT_PAY_API_V3_KEY": {
                "type": "secret",
                "required": False,
                "label": "APIv3密钥",
            },
            "WECHAT_PAY_PRIVATE_KEY_PATH": {
                "type": "string",
                "required": False,
                "label": "商户API证书私钥路径",
            },
            "WECHAT_PAY_CERT_SERIAL_NO": {
                "type": "secret",
                "required": False,
                "label": "商户API证书序列号",
            },
            "WECHAT_PAY_PLATFORM_PUBLIC_KEY_PATH": {
                "type": "string",
                "required": False,
                "label": "微信支付平台证书/公钥路径",
            },
            "WECHAT_PAY_NOTIFY_URL": {
                "type": "string",
                "required": False,
                "label": "支付通知地址",
            },
            "WECHAT_PAY_PRODUCT_CATALOG_JSON": {
                "type": "string",
                "required": False,
                "label": "商品目录JSON",
            },
        },
    },
    "alipay_pay_wap": {
        "label": "支付宝手机网站支付（WAP）",
        "required": False,
        "fields": {
            "ALIPAY_ENABLED": {
                "type": "string",
                "required": False,
                "label": "启用支付宝支付",
                "default": "false",
            },
            "ALIPAY_APP_ID": {
                "type": "string",
                "required": False,
                "label": "支付宝应用AppID",
            },
            "ALIPAY_APP_PRIVATE_KEY_PATH": {
                "type": "string",
                "required": False,
                "label": "应用私钥路径",
            },
            "ALIPAY_PUBLIC_KEY_PATH": {
                "type": "string",
                "required": False,
                "label": "支付宝公钥路径",
            },
            "ALIPAY_SERVER_URL": {
                "type": "string",
                "required": False,
                "label": "支付宝网关地址",
                "default": "https://openapi.alipay.com/gateway.do",
            },
            "ALIPAY_SIGN_TYPE": {
                "type": "string",
                "required": False,
                "label": "签名类型",
                "default": "RSA2",
            },
            "ALIPAY_NOTIFY_URL": {
                "type": "string",
                "required": False,
                "label": "异步通知地址",
            },
            "ALIPAY_RETURN_URL": {
                "type": "string",
                "required": False,
                "label": "同步返回地址",
            },
            "ALIPAY_TIMEOUT_EXPRESS": {
                "type": "string",
                "required": False,
                "label": "支付超时时间",
                "default": "30m",
            },
        },
    },
    "admin_auth": {
        "label": "管理后台认证",
        "required": False,
        "fields": {
            "ADMIN_AUTH_MODE": {
                "type": "string",
                "required": False,
                "label": "认证模式",
                "default": "wecom_sso",
                "help": "固定使用 wecom_sso；企业微信是唯一人类登录上游。",
            },
            "ADMIN_LOGIN_REDIRECT_URI": {
                "type": "string",
                "required": False,
                "label": "SSO回调地址",
            },
            "ADMIN_WECHAT_TRUSTED_DOMAIN": {
                "type": "string",
                "required": False,
                "label": "可信域名",
            },
        },
    },
    "ai_services": {
        "label": "AI服务配置（DeepSeek）",
        "required": False,
        "fields": {
            "DEEPSEEK_ENABLED": {
                "type": "string",
                "required": False,
                "label": "启用DeepSeek",
                "default": "",
                "help": "设为 true 启用",
            },
            "DEEPSEEK_API_KEY": {
                "type": "secret",
                "required": False,
                "label": "API Key",
            },
            "DEEPSEEK_BASE_URL": {
                "type": "string",
                "required": False,
                "label": "API地址",
                "default": "https://api.deepseek.com",
            },
            "DEEPSEEK_ROUTER_MODEL": {
                "type": "string",
                "required": False,
                "label": "路由模型",
                "default": "deepseek-chat",
            },
            "DEEPSEEK_EXECUTION_MODEL": {
                "type": "string",
                "required": False,
                "label": "执行模型",
                "default": "deepseek-chat",
            },
            "DEEPSEEK_REASONER_MODEL": {
                "type": "string",
                "required": False,
                "label": "推理模型",
                "default": "deepseek-reasoner",
            },
            "DEEPSEEK_TIMEOUT_SECONDS": {
                "type": "integer",
                "required": False,
                "label": "超时(秒)",
                "default": "30",
            },
        },
    },
    "webhooks": {
        "label": "Webhook集成",
        "required": False,
        "fields": {
            "OPENCLAW_WEBHOOK_URL": {
                "type": "string",
                "required": False,
                "label": "OpenClaw Webhook地址",
                "default": "http://claw.youcangogogo.com/webhook",
            },
            "QUESTIONNAIRE_SUBMIT_WEBHOOK_URL": {
                "type": "string",
                "required": False,
                "label": "问卷提交Webhook地址",
            },
        },
    },
    "infrastructure": {
        "label": "基础设施",
        "required": False,
        "fields": {
            "REDIS_URL": {
                "type": "string",
                "required": False,
                "label": "Redis连接地址",
                "help": "如 redis://localhost:6379/0，配置后启用持久化任务队列",
            },
            "AICRM_AUTH_ISSUER": {
                "type": "string",
                "required": True,
                "label": "统一授权平台 Issuer",
            },
            "AICRM_AUTH_SESSION_HASH_PEPPER": {
                "type": "secret",
                "required": True,
                "label": "会话凭据哈希 Pepper",
            },
            "AICRM_AUTH_JWT_SIGNING_KEY": {
                "type": "secret",
                "required": True,
                "label": "机器访问 JWT 签名密钥",
            },
            "AICRM_AUTH_TRUSTED_PROXY_ADDRESSES": {
                "type": "string",
                "required": False,
                "label": "授权平台可信代理地址",
            },
            "AICRM_AUTH_CA_FILE": {
                "type": "string",
                "required": False,
                "label": "授权平台 CA 路径",
            },
            **{
                key: {
                    "type": "string",
                    "required": True,
                    "label": label,
                }
                for key, label in (
                    ("AICRM_AUTH_AUTOMATION_WORKER_CLIENT_ID", "自动化 Worker Client ID"),
                    ("AICRM_AUTH_ARCHIVE_WORKER_CLIENT_ID", "消息归档 Worker Client ID"),
                    ("AICRM_AUTH_CALLBACK_WORKER_CLIENT_ID", "回调 Worker Client ID"),
                    ("AICRM_AUTH_GROUP_BROADCAST_CLIENT_ID", "群发 Worker Client ID"),
                    ("AICRM_AUTH_IDENTITY_CLIENT_ID", "身份解析 Client ID"),
                    ("AICRM_AUTH_MCP_CLIENT_ID", "MCP Client ID"),
                    ("AICRM_AUTH_EXTERNAL_AGENT_CLIENT_ID", "外部 Agent Client ID"),
                    ("AICRM_AUTH_CAMPAIGN_AGENT_CLIENT_ID", "Campaign Agent Client ID"),
                    ("AICRM_AUTH_OPS_REPORTER_CLIENT_ID", "运营闭环上报 Client ID"),
                    ("AICRM_AUTH_OPERATION_RUNNER_CLIENT_ID", "本地运营执行器 Client ID"),
                )
            },
            **{
                key.removesuffix("_CLIENT_ID") + "_CLIENT_SECRET_REF": {
                    "type": "string",
                    "required": True,
                    "label": label.replace("Client ID", "Client Secret 引用"),
                }
                for key, label in (
                    ("AICRM_AUTH_AUTOMATION_WORKER_CLIENT_ID", "自动化 Worker Client ID"),
                    ("AICRM_AUTH_ARCHIVE_WORKER_CLIENT_ID", "消息归档 Worker Client ID"),
                    ("AICRM_AUTH_CALLBACK_WORKER_CLIENT_ID", "回调 Worker Client ID"),
                    ("AICRM_AUTH_GROUP_BROADCAST_CLIENT_ID", "群发 Worker Client ID"),
                    ("AICRM_AUTH_IDENTITY_CLIENT_ID", "身份解析 Client ID"),
                    ("AICRM_AUTH_MCP_CLIENT_ID", "MCP Client ID"),
                    ("AICRM_AUTH_EXTERNAL_AGENT_CLIENT_ID", "外部 Agent Client ID"),
                    ("AICRM_AUTH_CAMPAIGN_AGENT_CLIENT_ID", "Campaign Agent Client ID"),
                    ("AICRM_AUTH_OPS_REPORTER_CLIENT_ID", "运营闭环上报 Client ID"),
                    ("AICRM_AUTH_OPERATION_RUNNER_CLIENT_ID", "本地运营执行器 Client ID"),
                )
            },
            "AICRM_AUTH_OUTBOUND_WEBHOOK_CLIENT_ID": {
                "type": "string",
                "required": True,
                "label": "出站 Webhook Client ID",
            },
        },
    },
    "reliability": {
        "label": "可靠性 / 出站调用",
        "required": False,
        "fields": {
            "HTTP_DEFAULT_TIMEOUT": {
                "type": "integer",
                "required": False,
                "label": "出站HTTP默认超时(秒)",
                "default": "10",
                "help": "OutboundHttpClient 默认超时，单 webhook 场景调用方可覆盖",
                "min": 1,
                "max": 120,
            },
            "HTTP_RETRY_MAX": {
                "type": "integer",
                "required": False,
                "label": "出站HTTP最大重试次数",
                "default": "2",
                "help": "网络/5xx 失败时的额外尝试次数，0 表示不重试",
                "min": 0,
                "max": 10,
            },
            "HTTP_RETRY_BACKOFF_BASE": {
                "type": "integer",
                "required": False,
                "label": "重试退避基准(秒)",
                "default": "1",
                "help": "退避按 base * 2^attempt 增长",
                "min": 0,
                "max": 60,
            },
            "CIRCUIT_FAILURE_THRESHOLD": {
                "type": "integer",
                "required": False,
                "label": "熔断器失败阈值",
                "default": "5",
                "help": "连续失败达到该次数后开路",
                "min": 1,
                "max": 100,
            },
            "CIRCUIT_RECOVERY_SECONDS": {
                "type": "integer",
                "required": False,
                "label": "熔断器半开恢复(秒)",
                "default": "60",
                "min": 1,
                "max": 3600,
            },
            "RQ_DEFAULT_TIMEOUT": {
                "type": "integer",
                "required": False,
                "label": "RQ任务默认超时(秒)",
                "default": "300",
                "min": 10,
                "max": 3600,
            },
            "OUTBOX_MAX_ATTEMPTS": {
                "type": "integer",
                "required": False,
                "label": "outbox 最大投递次数",
                "default": "5",
                "help": "超过即终态 failed，需要人工介入",
                "min": 1,
                "max": 50,
            },
            "OUTBOX_BACKOFF_BASE_SECONDS": {
                "type": "integer",
                "required": False,
                "label": "outbox 退避基准(秒)",
                "default": "30",
                "min": 1,
                "max": 600,
            },
        },
    },
}


def validate_config(settings: dict[str, str]) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    for group_key, group in CONFIG_SCHEMA.items():
        for field_key, field in group["fields"].items():
            value = settings.get(field_key, "").strip()
            if field.get("required") and not value:
                errors.append(
                    {
                        "group": group["label"],
                        "field": field.get("label", field_key),
                        "key": field_key,
                        "error": "必填项不能为空",
                    }
                )
            if value and field.get("type") == "integer":
                try:
                    int_value = int(value)
                except ValueError:
                    errors.append(
                        {
                            "group": group["label"],
                            "field": field.get("label", field_key),
                            "key": field_key,
                            "error": "必须为整数",
                        }
                    )
                    continue
                # Range validation when ``min``/``max`` declared on the field.
                # Catches "fat-fingered" reliability knobs like a timeout of
                # 0 or a retry count in the millions before they reach
                # production.
                min_value = field.get("min")
                max_value = field.get("max")
                if min_value is not None and int_value < int(min_value):
                    errors.append(
                        {
                            "group": group["label"],
                            "field": field.get("label", field_key),
                            "key": field_key,
                            "error": f"不能小于 {min_value}",
                        }
                    )
                if max_value is not None and int_value > int(max_value):
                    errors.append(
                        {
                            "group": group["label"],
                            "field": field.get("label", field_key),
                            "key": field_key,
                            "error": f"不能大于 {max_value}",
                        }
                    )
    return errors


def build_config_checklist(settings: dict[str, str]) -> list[dict[str, Any]]:
    checklist: list[dict[str, Any]] = []
    for group_key, group in CONFIG_SCHEMA.items():
        items: list[dict[str, Any]] = []
        all_ok = True
        for field_key, field in group["fields"].items():
            value = settings.get(field_key, "").strip()
            configured = bool(value)
            required = bool(field.get("required"))
            if required and not configured:
                all_ok = False
            items.append(
                {
                    "key": field_key,
                    "label": field.get("label", field_key),
                    "required": required,
                    "configured": configured,
                    "type": field.get("type", "string"),
                }
            )
        checklist.append(
            {
                "group_key": group_key,
                "group_label": group["label"],
                "required": bool(group.get("required")),
                "complete": all_ok,
                "fields": items,
            }
        )
    return checklist


def get_all_schema_keys() -> list[str]:
    keys: list[str] = []
    for group in CONFIG_SCHEMA.values():
        keys.extend(group["fields"].keys())
    return keys

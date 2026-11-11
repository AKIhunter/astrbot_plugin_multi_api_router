from __future__ import annotations

import asyncio
import io
import inspect
import json
import math
import os
import re
import time
import wave
from collections.abc import AsyncGenerator, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import Provider
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.api.web import error_response, json_response, request
from astrbot.core.agent.message import ContentPart, Message
from astrbot.core.agent.tool import ToolSet
from astrbot.core.provider.entities import LLMResponse, ToolCallsResult
from astrbot.core.provider.register import provider_cls_map, register_provider_adapter
from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
from astrbot.core.star.star import StarMetadata, star_map
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from openai import APITimeoutError, AsyncOpenAI

try:
    from .request_budget import measure_request
    from .image_history import compact_contexts
except ImportError:
    from request_budget import measure_request
    from image_history import compact_contexts

PLUGIN_NAME = "astrbot_plugin_multi_api_router"
PROVIDER_TYPE = "multi_api_router"
# 「指定某个上游」模式在 AstrBot Provider 列表中的 ID 前缀，
# 例如 multi_api_router:upstream_2。调用方按普通 Provider ID 选择即可。
PINNED_PROVIDER_PREFIX = f"{PROVIDER_TYPE}:upstream_"
MAX_UPSTREAMS = 20
MIN_UPSTREAMS = 5

# 统一错误分类：重试、健康跳过、页面文案与失败记账都从这里取结论，
# 禁止在各处再单独枚举状态码，避免 429 与 5xx 走出两套路径。
DISPOSITION_RETRY = "retry_then_rotate"
DISPOSITION_ROTATE = "rotate_now_report"
DISPOSITION_IGNORE = "not_an_error"
DISPOSITION_FAIL = "fail_report"

CATEGORY_QUOTA = "quota_exhausted"
CATEGORY_TIMEOUT = "timeout"
CATEGORY_HTTP_5XX = "http_5xx"
CATEGORY_CONNECTION = "connection_error"
CATEGORY_HTTP_429 = "http_429"
CATEGORY_AUTH = "authentication_error"
CATEGORY_NOT_FOUND = "not_found"
CATEGORY_INVALID_REQUEST = "invalid_request"
CATEGORY_CLIENT = "client_error"
CATEGORY_CONFIG = "configuration_error"
CATEGORY_INTENTIONAL = "intentional"
CATEGORY_UNKNOWN = "model_execution_error"

# 429 也可能来自余额/额度耗尽；命中这些关键字时不再重试，直接切换并上报。
QUOTA_KEYWORDS = (
    "insufficient_quota",
    "insufficient quota",
    "not enough quota",
    "quota exceeded",
    "insufficient balance",
    "余额不足",
    "额度不足",
    "欠费",
)
# 撤回、上下文过期、策略变更与主动沉默都属预期结果，不计错误、不告警。
INTENTIONAL_KEYWORDS = (
    "message_recalled",
    "message_expired",
    "context_changed",
    "stale_context",
    "policy_changed",
    "no_reply",
    "intentional_silence",
    "proactive_silence",
    "superseded_by_new_message",
)
HEALTH_STATUS_LABELS = {
    CATEGORY_QUOTA: "额度不足或欠费",
    CATEGORY_AUTH: "认证失败",
    CATEGORY_NOT_FOUND: "接口或模型不存在",
    CATEGORY_HTTP_429: "限流或额度不足",
    CATEGORY_HTTP_5XX: "服务端异常",
    CATEGORY_TIMEOUT: "请求超时",
    CATEGORY_CONNECTION: "连接或请求失败",
    CATEGORY_INVALID_REQUEST: "请求参数或模型不兼容",
    CATEGORY_CLIENT: "请求被上游拒绝",
    CATEGORY_CONFIG: "配置不完整",
}
MODEL_LIST_ERROR_MESSAGES = {
    CATEGORY_AUTH: "认证失败，请检查 API Key 或模型列表权限",
    CATEGORY_QUOTA: "账户余额或额度不足，请充值或更换 API Key",
    CATEGORY_NOT_FOUND: "该服务未提供 OpenAI 兼容的 /models 接口",
    CATEGORY_HTTP_429: "请求过于频繁或额度受限，请稍后重试",
    CATEGORY_HTTP_5XX: "上游服务暂时异常，请稍后重试",
    CATEGORY_TIMEOUT: "获取模型列表超时，请检查 API Base 或稍后重试",
    CATEGORY_CONNECTION: "无法连接上游，请检查 API Base 和网络连接",
}
DEFAULT_MODEL_LIST_ERROR = "无法获取模型列表，请检查 API Base 和网络连接"
MAX_MODEL_OPTIONS = 500
# 控制台接口默认只回显掩码：保留前缀与末 4 位，中间固定 ****。
# 兼容开关 expose_plain_api_keys（默认关闭）可恢复旧行为。
API_KEY_MASK = "****"
# 明文 Key 揭示审计文件名，存放在插件数据目录下；记录内容不包含 Key 本身。
API_KEY_REVEAL_AUDIT = "api_key_reveal_audit.jsonl"
DEFAULT_MAX_INPUT_CHARS = 256 * 1024
API_TYPE_LABELS = {
    "chat": "聊天",
    "image_generation": "文/图生图",
    "image_to_text": "图转文",
    "embedding": "Embedding",
    "rerank": "Rerank",
    "stt": "STT",
    "tts": "TTS",
    "asr": "ASR",
    "multimodal": "多模态",
}
# AstrBot 的聊天 Provider 只能调用 Chat Completions 协议。视觉理解仍属于
# Chat Completions；生图、向量、排序与语音 API 只做对应协议的健康检测。
CHAT_COMPATIBLE_API_TYPES = {"chat", "image_to_text", "multimodal"}
# 自动健康检测避免每小时触发生图或语音生成等高成本请求；这些类型仍可手动检测。
AUTO_HEALTH_CHECK_API_TYPES = {
    "chat",
    "image_to_text",
    "embedding",
    "rerank",
    "multimodal",
}
AUTO_BIND_TARGETS = {
    "astrbot_plugin_bilibili_ai_bot": {
        "config_flag": "auto_bind_bilibili",
        "label": "B站后台任务",
        "paths": (
            ("SUMMARY_PROVIDER_ID",),
            ("GENERATION_PROVIDER_ID",),
            ("SCHEDULE_PLAN_PROVIDER_ID",),
        ),
    },
    "astrbot_plugin_memewiki": {
        "config_flag": "auto_bind_memewiki",
        "label": "梗百科轻量门控",
        "paths": (("gate_provider_id",),),
    },
}


class RetryBudgetExhausted(RuntimeError):
    """剩余总预算不足以完成一次完整重试时的切换信号。

    携带原始异常，外层仍按那次真实失败记账，但不再发起注定被截断的重试。
    """

    def __init__(self, message: str, last_error: Exception):
        super().__init__(message)
        self.last_error = last_error


class RouterConfigError(RuntimeError):
    """路由配置缺陷（无可用上游、指定上游缺失等），归类为 fail_report。"""


def _error_status(error: object) -> int | None:
    """从异常或健康结果字典里取 HTTP 状态码，取不到时返回 None。"""
    if isinstance(error, Mapping):
        raw = error.get("status_code", error.get("http_status"))
    else:
        raw = getattr(error, "status_code", None)
        if raw is None:
            raw = getattr(getattr(error, "response", None), "status_code", None)
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _error_text(error: object) -> str:
    """收集用于关键字判定的文本；解析失败时退回异常自身描述。"""
    if isinstance(error, str):
        return error
    if isinstance(error, Mapping):
        parts = [
            str(error.get(key))
            for key in ("message", "detail", "error", "body", "text", "status", "reason")
            if error.get(key) not in (None, "")
        ]
        return " ".join(parts)
    parts: list[str] = []
    body = getattr(error, "body", None)
    if body not in (None, ""):
        try:
            parts.append(json.dumps(body, ensure_ascii=False, default=str))
        except Exception:
            parts.append(str(body))
    response = getattr(error, "response", None)
    if response is not None:
        try:
            if response.text:
                parts.append(response.text)
        except Exception:
            pass
    try:
        parts.append(str(error))
    except Exception:
        pass
    return " ".join(parts)


# 文案里的 HTTP 状态码只用于日志诊断：classify 仍只认 status_code 属性，
# 保证新增锚点不改变任何分类结果。
_TEXT_STATUS = re.compile(r"HTTP\s*([1-5]\d{2})", re.IGNORECASE)


def _reply_fault(code: str, stage: str, reason: str, *, category: str = "",
                 disposition: str = "", slot: int = 0) -> None:
    """统一故障锚点：一条事件一行，取不到的字段不占位。"""
    fields = [f"[ReplyFault] code={code}", f"stage={stage}"]
    if category:
        fields.append(f"category={category}")
    if disposition:
        fields.append(f"disposition={disposition}")
    if slot:
        fields.append(f"slot=#{slot}")
    fields.append("reason=" + " ".join(str(reason).split())[:160])
    logger.warning(" ".join(fields))


def _blank_completion(response: object) -> bool:
    """与调用方 failed() 同口径：无正文、无工具调用、无结果链才算空。"""
    if getattr(response, "role", "") == "err":
        return False
    try:
        text = str(getattr(response, "completion_text", "") or "").strip()
    except Exception:
        return False
    chain = getattr(getattr(response, "result_chain", None), "chain", None)
    return not (text or getattr(response, "tools_call_name", None) or chain)


def classify(error: object) -> tuple[str, str]:
    """把所有上游错误收敛为 ``(category, disposition)``。

    category 只用于展示与日志；disposition 决定统一处置：
    ``retry_then_rotate`` / ``rotate_now_report`` / ``not_an_error`` / ``fail_report``。
    429 与 5xx 走同一分类路径，只有响应体带配额关键字时才改为立即切换。
    """
    text = _error_text(error).casefold()
    if any(keyword in text for keyword in QUOTA_KEYWORDS):
        return CATEGORY_QUOTA, DISPOSITION_ROTATE
    if any(keyword in text for keyword in INTENTIONAL_KEYWORDS):
        return CATEGORY_INTENTIONAL, DISPOSITION_IGNORE
    if isinstance(error, RouterConfigError):
        return CATEGORY_CONFIG, DISPOSITION_FAIL
    if isinstance(error, Mapping):
        error_type = str(error.get("error_type") or "").strip().casefold()
        if error_type in {"configurationerror", CATEGORY_CONFIG}:
            return CATEGORY_CONFIG, DISPOSITION_FAIL
    # SDK 超时与内置 TimeoutError 都是临时故障，允许同上游重试一次。
    if isinstance(error, (TimeoutError, APITimeoutError)):
        return CATEGORY_TIMEOUT, DISPOSITION_RETRY
    status = _error_status(error)
    if status is not None:
        if status == 408:
            return CATEGORY_TIMEOUT, DISPOSITION_RETRY
        if status == 429:
            return CATEGORY_HTTP_429, DISPOSITION_RETRY
        if 500 <= status <= 599:
            return CATEGORY_HTTP_5XX, DISPOSITION_RETRY
        if status in {401, 403}:
            return CATEGORY_AUTH, DISPOSITION_ROTATE
        if status == 402:
            return CATEGORY_QUOTA, DISPOSITION_ROTATE
        if status == 404:
            return CATEGORY_NOT_FOUND, DISPOSITION_ROTATE
        if status in {400, 422}:
            return CATEGORY_INVALID_REQUEST, DISPOSITION_ROTATE
        if 400 <= status <= 499:
            return CATEGORY_CLIENT, DISPOSITION_ROTATE
    # 连接失败在不同 SDK 版本里可能没有状态码，按类名识别以保持兼容。
    if type(error).__name__ == "APIConnectionError":
        return CATEGORY_CONNECTION, DISPOSITION_RETRY
    return CATEGORY_UNKNOWN, DISPOSITION_ROTATE


def _retry_after_seconds(error: object) -> float | None:
    """读取 Retry-After 头（秒数或 HTTP 日期），无有效值时返回 None。"""
    if isinstance(error, Mapping):
        headers = error.get("headers")
    else:
        headers = getattr(getattr(error, "response", None), "headers", None)
        if headers is None:
            headers = getattr(error, "headers", None)
    if not headers:
        return None
    try:
        raw = headers.get("retry-after") or headers.get("Retry-After")
    except Exception:
        return None
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        moment = parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        return None
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return max(0.0, (moment - datetime.now(timezone.utc)).total_seconds())


@dataclass
class UpstreamState:
    name: str
    priority: int
    supports_tools: bool
    client: ProviderOpenAIOfficial
    slot: int = 0
    api_type: str = "chat"
    route_enabled: bool = False
    consecutive_failures: int = 0
    circuit_open_until: float = 0.0
    calls: int = 0
    successes: int = 0
    failures: int = 0
    last_error: str = ""
    last_latency_ms: int = 0
    last_category: str = ""
    last_disposition: str = ""

    def available(self, now: float) -> bool:
        # 兼容旧状态，但历史失败不能冻结后续请求的接口。
        return True


def _register_router_adapter(cls):
    existing = provider_cls_map.get(PROVIDER_TYPE)
    if existing is not None:
        existing.cls_type = cls
        # 控制台「新增服务提供商」列表需要类型模板，否则选不到本插件。
        existing.default_config_tmpl = _router_default_config_tmpl()
        existing.provider_display_name = "多 API 路由"
        return cls
    return register_provider_adapter(
        PROVIDER_TYPE,
        "按优先级自动降级的多 API 路由 Provider",
        default_config_tmpl=_router_default_config_tmpl(),
        provider_display_name="多 API 路由",
    )(cls)


def _router_default_config_tmpl() -> dict:
    """AstrBot 控制台新增 Provider 时展示的默认模板。"""
    return {
        "id": PROVIDER_TYPE,
        "type": PROVIDER_TYPE,
        "provider_type": "chat_completion",
        "model": "automatic",
        "enable": True,
        "key": [""],
    }


@_register_router_adapter
class MultiApiRouterProvider(Provider):
    def __init__(self, plugin_config: dict, provider_settings: dict | None = None):
        original_config = dict(plugin_config) if isinstance(plugin_config, dict) else {}
        plugin_config = self._resolve_plugin_config(plugin_config)
        # 「指定上游」模式：AstrBot 可以为每个上游建一个 Provider 条目，
        # 条目里带 upstream_slot；调用方选中它时只走这个上游，不做自动分配。
        self.pinned_slot = self._detect_pinned_slot(original_config)
        # 整体 Provider ID 是所有下游插件引用路由的固定标识：恒定使用
        # PROVIDER_TYPE，忽略配置覆盖；指定上游实例按槽位号生成，不受影响。
        provider_id = (
            f"{PINNED_PROVIDER_PREFIX}{self.pinned_slot}"
            if self.pinned_slot
            else PROVIDER_TYPE
        )
        pinned_model = "automatic"
        if self.pinned_slot:
            raw = plugin_config.get(f"upstream_{self.pinned_slot}") or {}
            if isinstance(raw, dict) and str(raw.get("model") or "").strip():
                pinned_model = str(raw.get("model")).strip()
        display_name = str(
            original_config.get("display_name")
            or original_config.get("label")
            or ""
        ).strip()
        parent_provider_id = str(
            original_config.get("parent_provider_id") or ""
        ).strip()
        super().__init__(
            {
                "id": provider_id,
                "type": PROVIDER_TYPE,
                "model": pinned_model,
                "key": [""],
                "upstream_slot": self.pinned_slot,
                # ID 保持稳定供调用方保存；选择器优先展示干净名称。
                "display_name": display_name,
                "label": display_name,
                "name": display_name,
                "parent_provider_id": parent_provider_id,
            },
            provider_settings or {},
        )
        self.set_model(pinned_model)
        # 旧配置/控制台保留字段兼容，固定熔断策略已停用。
        self.failure_threshold = 0
        self.cooldown_seconds = 0
        self.max_attempts = self._bounded_int(
            plugin_config.get("max_attempts"), 3, 1, 5
        )
        self.request_timeout_seconds = self._bounded_int(
            plugin_config.get("request_timeout_seconds"), 45, 5, 600
        )
        self.upstream_retry_count = self._bounded_int(
            plugin_config.get("upstream_retry_count"), 1, 0, 5
        )
        self.retry_interval_seconds = self._bounded_float(
            plugin_config.get("retry_interval_seconds"), 1.0, 0, 3600
        )
        self.route_timeout_seconds = self._bounded_int(
            plugin_config.get("route_timeout_seconds"), 90, 5, 600
        )
        self.max_input_chars = self._bounded_int(
            plugin_config.get("max_input_chars"),
            DEFAULT_MAX_INPUT_CHARS,
            1000,
            2_000_000,
        )
        self.max_image_bytes = self._bounded_int(
            plugin_config.get("max_image_bytes"), 8 * 1024 * 1024, 1024, 64 * 1024 * 1024
        )
        self.max_total_image_bytes = self._bounded_int(
            plugin_config.get("max_total_image_bytes"), 32 * 1024 * 1024, 1024, 128 * 1024 * 1024
        )
        self.max_image_count = self._bounded_int(plugin_config.get("max_image_count"), 32, 1, 128)
        self.history_image_compaction_enabled = bool(
            plugin_config.get("history_image_compaction_enabled", True)
        )
        self.max_output_tokens = self._bounded_int(
            plugin_config.get("max_output_tokens"), 2048, 1, 131072
        )
        # 健康结果只用于展示和确认配置错误，临时异常不能阻止新请求。
        self.health_lookup: Any = None
        # 真实调用成功后回写健康结果，用于自动解除此前的健康跳过。
        self.health_recorder: Any = None
        self.skip_unhealthy = bool(plugin_config.get("skip_unhealthy_upstreams", True))
        self.health_skip_ttl_seconds = self._bounded_int(
            plugin_config.get("health_skip_ttl_seconds"), 21600, 60, 604800
        )
        # 真实调用全部失败后才回退到主模型（由插件侧注入解析函数）。
        self.fallback_to_main = bool(plugin_config.get("fallback_to_main_model", True))
        self.fallback_resolver: Any = None
        max_concurrency = self._bounded_int(
            plugin_config.get("max_concurrency"), 4, 1, 128
        )
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._state_lock = asyncio.Lock()
        self._all_upstreams = self._build_upstreams(
            plugin_config,
            only_slot=self.pinned_slot,
        )
        # 指定上游模式只保留目标上游，绝不自动分配到其它上游。
        if self.pinned_slot:
            self.upstreams = self._all_upstreams
        else:
            # “启用路由”只控制是否进入自动候选链；关闭后仍保留指定 Provider。
            self.upstreams = [
                item for item in self._all_upstreams if item.route_enabled
            ]

    @staticmethod
    def _detect_pinned_slot(provider_config: dict) -> int:
        raw = provider_config.get("upstream_slot")
        try:
            slot = int(raw)
        except (TypeError, ValueError):
            slot = 0
        if 1 <= slot <= MAX_UPSTREAMS:
            return slot
        # 兼容按 ID 命名（multi_api_router:upstream_3）直接实例化的场景。
        provider_id = str(provider_config.get("id") or "")
        if provider_id.startswith(PINNED_PROVIDER_PREFIX):
            try:
                slot = int(provider_id[len(PINNED_PROVIDER_PREFIX) :])
            except ValueError:
                slot = 0
        return slot if 1 <= slot <= MAX_UPSTREAMS else 0

    @staticmethod
    def _resolve_plugin_config(provider_config: dict) -> dict:
        if any(f"upstream_{slot}" in provider_config for slot in range(1, MAX_UPSTREAMS + 1)):
            return provider_config
        config_path = (
            Path(get_astrbot_data_path())
            / "config"
            / "astrbot_plugin_multi_api_router_config.json"
        )
        try:
            loaded = json.loads(config_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, TypeError):
            return provider_config
        if not isinstance(loaded, dict):
            return provider_config
        resolved = dict(loaded)
        public_id = str(provider_config.get("id") or "").strip()
        if public_id:
            resolved["provider_id"] = public_id
        return resolved

    @staticmethod
    def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return max(minimum, min(maximum, parsed))

    @staticmethod
    def _bounded_float(value: Any, default: float, minimum: float, maximum: float) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError):
            parsed = default
        if not math.isfinite(parsed):
            parsed = default
        return max(minimum, min(maximum, parsed))

    @staticmethod
    def _enable_thinking_value(raw: dict, extra_body: dict | None = None) -> bool:
        """兼容旧配置：优先读显式开关，其次读取 extra_body.enable_thinking。"""
        if not isinstance(raw, dict):
            return False
        if "enable_thinking" in raw:
            return bool(raw.get("enable_thinking", False))
        body = extra_body
        if body is None:
            body = raw.get("extra_body")
            if isinstance(body, str) and body.strip():
                try:
                    body = json.loads(body)
                except (ValueError, TypeError):
                    body = {}
        if isinstance(body, dict) and "enable_thinking" in body:
            return bool(body.get("enable_thinking", False))
        return False

    @staticmethod
    def _api_type(raw: dict) -> str:
        """旧配置没有 api_type 时按聊天 API 兼容。"""
        if not isinstance(raw, dict):
            return "chat"
        api_type = str(raw.get("api_type") or "chat").strip().lower()
        return api_type if api_type in API_TYPE_LABELS else "chat"

    @staticmethod
    def _route_enabled(raw: dict) -> bool:
        """兼容旧 enabled 字段；配置完整且无显式开关时默认参与路由。"""
        if not isinstance(raw, dict):
            return False
        if "route_enabled" in raw:
            return bool(raw.get("route_enabled"))
        if "enabled" in raw:
            return bool(raw.get("enabled"))
        return bool(
            str(raw.get("api_base") or "").strip()
            and str(raw.get("api_key") or "").strip()
            and str(raw.get("model") or "").strip()
        )

    @staticmethod
    def _upstream_enabled(raw: dict) -> bool:
        """保留旧内部调用名，语义等同“启用路由”."""
        return MultiApiRouterProvider._route_enabled(raw)

    @staticmethod
    def _chat_route_enabled(raw: dict) -> bool:
        return (
            MultiApiRouterProvider._route_enabled(raw)
            and MultiApiRouterProvider._api_type(raw)
            in CHAT_COMPATIBLE_API_TYPES
        )

    def _build_upstreams(
        self,
        plugin_config: dict,
        *,
        only_slot: int = 0,
    ) -> list[UpstreamState]:
        timeout = self.request_timeout_seconds
        upstreams: list[UpstreamState] = []
        for index in range(1, MAX_UPSTREAMS + 1):
            if only_slot and index != only_slot:
                continue
            raw = plugin_config.get(f"upstream_{index}") or {}
            if not isinstance(raw, dict):
                continue
            api_type = self._api_type(raw)
            if api_type not in CHAT_COMPATIBLE_API_TYPES:
                continue
            api_base = str(raw.get("api_base") or "").strip().rstrip("/")
            api_key = str(raw.get("api_key") or "").strip()
            model = str(raw.get("model") or "").strip()
            if not api_base or not api_key or not model:
                if self._route_enabled(raw):
                    logger.warning(
                        "[MultiApiRouter] 上游 %s 已启用路由但配置不完整，已跳过。",
                        index,
                    )
                continue
            name = str(raw.get("name") or f"upstream-{index}").strip()
            priority = self._bounded_int(raw.get("priority"), index * 10, -10000, 10000)
            extra_body = raw.get("extra_body") or {}
            if isinstance(extra_body, str):
                extra_body = extra_body.strip()
                if extra_body:
                    try:
                        extra_body = json.loads(extra_body)
                    except (ValueError, TypeError):
                        logger.warning(
                            "[MultiApiRouter] 上游 %s 的 extra_body 不是合法 JSON，已忽略：%s",
                            index,
                            extra_body[:80],
                        )
                        extra_body = {}
                else:
                    extra_body = {}
            if not isinstance(extra_body, dict):
                extra_body = {}
            # 思考开关给常见推理模型使用；旧的 extra_body 会继续保留，其它参数不受影响。
            extra_body["enable_thinking"] = self._enable_thinking_value(raw, extra_body)
            client = ProviderOpenAIOfficial(
                {
                    "id": f"{self.meta().id}:{index}",
                    "type": "openai_chat_completion",
                    "provider": "custom",
                    "key": [api_key],
                    "api_base": api_base,
                    "model": model,
                    "timeout": timeout,
                    "proxy": "",
                    "custom_headers": {},
                    "custom_extra_body": extra_body,
                },
                self.provider_settings,
            )
            upstreams.append(
                UpstreamState(
                    name=name,
                    priority=priority,
                    # 所有上游默认按「支持工具」处理；个别中转不支持时再由用户在页面取消。
                    supports_tools=bool(raw.get("supports_tools", True)),
                    client=client,
                    slot=index,
                    api_type=api_type,
                    route_enabled=self._route_enabled(raw),
                )
            )
        return sorted(upstreams, key=lambda item: item.priority)

    def get_current_key(self) -> str:
        return ""

    def get_keys(self) -> list[str]:
        return []

    def set_key(self, key: str) -> None:
        return None

    async def get_models(self) -> list[str]:
        return [item.client.get_model() for item in self.upstreams]

    @staticmethod
    def _text_size(value: Any) -> int:
        return measure_request(contexts=value).text_chars

    def _request_size(
        self,
        prompt: str | None,
        contexts: list[Message] | list[dict] | None,
        system_prompt: str | None,
    ) -> int:
        return measure_request(prompt, contexts, system_prompt).text_chars

    async def _record_success(self, upstream: UpstreamState, latency_ms: int) -> None:
        async with self._state_lock:
            previous_category = upstream.last_category
            previous_disposition = upstream.last_disposition
            upstream.calls += 1
            upstream.successes += 1
            upstream.consecutive_failures = 0
            upstream.circuit_open_until = 0.0
            upstream.last_error = ""
            upstream.last_latency_ms = latency_ms
            upstream.last_category = ""
            upstream.last_disposition = ""
        if previous_disposition == DISPOSITION_ROTATE:
            # 只有 rotate_now_report 会触发健康跳过；真实成功是解除它的最强证据。
            _reply_fault(
                "HEALTH_SKIP_CLEAR", "router",
                "真实调用成功，解除该槽位的健康跳过",
                category=previous_category, disposition=DISPOSITION_IGNORE,
                slot=upstream.slot,
            )
        if callable(self.health_recorder) and upstream.slot:
            # 真实调用成功是比探针更强的证据：直接把该槽位写成健康，
            # 让下一次自动路由立即解除健康跳过，不必等 TTL 或手动检测。
            protocol = "Chat Completions"
            if upstream.api_type in {"image_to_text", "multimodal"}:
                protocol = f"Chat Completions（{API_TYPE_LABELS[upstream.api_type]}）"
            try:
                self.health_recorder(
                    upstream.slot,
                    {
                        "ok": True,
                        "checked_at": int(time.time()),
                        "latency_ms": latency_ms,
                        "status": "正常",
                        "http_status": 200,
                        "error_type": None,
                        "disposition": DISPOSITION_IGNORE,
                        "api_type": upstream.api_type,
                        "protocol": protocol,
                    },
                )
            except Exception as exc:
                # 记账失败不能推翻已经成功的模型调用。
                logger.warning(
                    "[MultiApiRouter] 回写成功健康状态失败：%s。",
                    self._safe_error(exc),
                )

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        status = _error_status(exc)
        label = type(exc).__name__
        return f"{label} (HTTP {status})" if status else label

    async def _record_failure(
        self, upstream: UpstreamState, exc: Exception, latency_ms: int
    ) -> None:
        safe_error = self._safe_error(exc)
        category, disposition = classify(exc)
        # 分类结果已经落在下面的失败日志里；这里只补「为什么没认出来」的锚点。
        if _error_status(exc) is None:
            match = _TEXT_STATUS.search(_error_text(exc))
            if match:
                _reply_fault(
                    "PLAINTEXT_STATUS_UNPARSED", "router",
                    f"文案含 HTTP {match.group(1)}，但异常没有 status_code，按未知错误处理",
                    category=category, disposition=disposition, slot=upstream.slot,
                )
            elif (isinstance(exc, (ConnectionError, OSError))
                  and category == CATEGORY_UNKNOWN):
                # TimeoutError 也是 OSError 子类，但已有明确分类，不算「未知」。
                _reply_fault(
                    "RAW_CONN_UNKNOWN", "router",
                    f"连接类异常 {type(exc).__name__} 未命中连接分类，按未知错误处理",
                    category=category, disposition=disposition, slot=upstream.slot,
                )
        async with self._state_lock:
            upstream.calls += 1
            upstream.failures += 1
            upstream.consecutive_failures += 1
            upstream.last_error = safe_error
            upstream.last_latency_ms = latency_ms
            upstream.last_category = category
            upstream.last_disposition = disposition
            upstream.circuit_open_until = 0.0
        logger.warning(
            "[MultiApiRouter] 上游 %s 调用失败：%s；分类 %s/%s，将尝试下一个上游。",
            upstream.name,
            safe_error,
            category,
            disposition,
        )

    def _health_skip_reason(self, upstream: UpstreamState) -> str:
        """自动路由只跳过 rotate_now_report，不移除上游。

        临时超时、连接错误、429 与 5xx 只用于展示，不能冻结真实请求；
        只有近期检测确认的认证/接口/额度等配置错误才参与自动跳过。
        """
        # 固定选择某个 API 时必须真实调用目标，不能被自动路由的健康跳过策略拦截。
        if (
            self.pinned_slot
            or not self.skip_unhealthy
            or not callable(self.health_lookup)
        ):
            return ""
        try:
            result = self.health_lookup(upstream.slot)
        except Exception:
            return ""
        if not isinstance(result, dict) or result.get("ok"):
            return ""
        checked_at = int(result.get("checked_at") or 0)
        if checked_at and time.time() - checked_at > self.health_skip_ttl_seconds:
            # 检测结果过期（例如插件刚启动、长时间未复检），不据此拦截真实请求。
            return ""
        status_code = result.get("http_status")
        disposition = str(result.get("disposition") or "").strip()
        if not disposition:
            # 兼容旧检测结果：只让明确的错误状态码参与判定，2xx/空响应不拦截。
            if not isinstance(status_code, int) or status_code < 400:
                return ""
            _, disposition = classify(result)
        if disposition != DISPOSITION_ROTATE:
            return ""
        status = str(result.get("status") or "健康检查未通过")
        # 带上具体状态码，日志和页面可区分不同错误。
        return f"{status} / HTTP {status_code}" if status_code is not None else status

    @staticmethod
    def _retryable_error(exc: Exception) -> bool:
        """同上游重试与否完全由 classify() 的 disposition 决定。"""
        return classify(exc)[1] == DISPOSITION_RETRY

    async def _call_fallback(
        self,
        *,
        prompt: str | None,
        image_urls: list[str] | None,
        audio_urls: list[str] | None,
        func_tool: ToolSet | None,
        contexts: list[Message] | list[dict] | None,
        system_prompt: str | None,
        tool_calls_result: ToolCallsResult | list[ToolCallsResult] | None,
        extra_user_content_parts: list[ContentPart] | None,
        tool_choice: Literal["auto", "required"],
        kwargs: dict,
    ) -> LLMResponse | None:
        """所有上游真实调用都失败后，回退到主模型一次。

        健康检查跳过不算真实失败，不会触发回退；指定上游模式也不回退，
        以保证调用方选中某个 API 时行为可预期。
        """
        if (
            self.pinned_slot
            or not self.fallback_to_main
            or not callable(self.fallback_resolver)
        ):
            return None
        try:
            fallback = self.fallback_resolver()
            if inspect.isawaitable(fallback):
                fallback = await fallback
        except Exception as exc:
            logger.warning(
                "[MultiApiRouter] 解析主模型失败，无法回退：%s",
                self._safe_error(exc),
            )
            return None
        if fallback is None or fallback is self:
            return None
        logger.warning(
            "[MultiApiRouter] 所有上游真实调用失败，回退到主模型 %s。",
            getattr(fallback.meta(), "id", "<unknown>"),
        )
        return await fallback.text_chat(
            prompt=prompt,
            image_urls=image_urls,
            audio_urls=audio_urls,
            func_tool=func_tool,
            contexts=contexts,
            system_prompt=system_prompt,
            tool_calls_result=tool_calls_result,
            model=None,
            extra_user_content_parts=extra_user_content_parts,
            tool_choice=tool_choice,
            **kwargs,
        )

    async def _call_once(
        self,
        upstream: UpstreamState,
        *,
        prompt: str | None,
        image_urls: list[str] | None,
        audio_urls: list[str] | None,
        func_tool: ToolSet | None,
        contexts: list[Message] | list[dict] | None,
        system_prompt: str | None,
        tool_calls_result: ToolCallsResult | list[ToolCallsResult] | None,
        extra_user_content_parts: list[ContentPart] | None,
        tool_choice: Literal["auto", "required"],
        kwargs: dict,
        deadline: float | None = None,
    ) -> LLMResponse:
        if func_tool and not func_tool.empty() and not upstream.supports_tools:
            raise RuntimeError("upstream does not support tool calls")

        payloads, _ = await upstream.client._prepare_chat_payload(
            prompt,
            image_urls,
            audio_urls,
            contexts,
            system_prompt,
            tool_calls_result,
            model=upstream.client.get_model(),
            extra_user_content_parts=extra_user_content_parts,
        )
        requested_max = kwargs.get("max_tokens", self.max_output_tokens)
        payloads["max_tokens"] = self._bounded_int(
            requested_max, self.max_output_tokens, 1, self.max_output_tokens
        )
        for key in (
            "temperature",
            "top_p",
            "stop",
            "seed",
            "frequency_penalty",
            "presence_penalty",
            "response_format",
        ):
            if key in kwargs and kwargs[key] is not None:
                payloads[key] = kwargs[key]
        if func_tool and not func_tool.empty():
            payloads["tool_choice"] = tool_choice
        # 首次调用不计入重试次数；设为 0 时每个上游只调用一次。
        max_calls = self.upstream_retry_count + 1
        for attempt in range(max_calls):
            try:
                # _query 会改写 payload，重试必须复用相同输入；SDK 本层只尝试一次。
                return await upstream.client._query(
                    deepcopy(payloads),
                    func_tool,
                    request_max_retries=1,
                    conversation_id=kwargs.get("conversation_id"),
                )
            except Exception as exc:
                if attempt + 1 >= max_calls or not self._retryable_error(exc):
                    raise
                # 上游给了 Retry-After 就优先遵守，否则用固定配置间隔；
                # 两种等待都要通过下面的总预算校验，绝不无限期占用路由时间片。
                retry_after = _retry_after_seconds(exc)
                delay = (
                    retry_after
                    if retry_after is not None
                    else self.retry_interval_seconds
                )
                # 只有剩余总预算够「一次完整调用 + 重试间隔」时才重试；
                # 否则外层 wait_for 会在中途截断，等于白占一个上游的时间片。
                if deadline is not None:
                    left = deadline - time.monotonic()
                    if left < self.request_timeout_seconds + delay:
                        logger.warning(
                            "[MultiApiRouter] 上游 %s 剩余预算 %.1fs，不足一次完整重试"
                            "（单次超时 %ds + 重试间隔 %.2fs），跳过重试并切换下一个上游。",
                            upstream.name,
                            max(left, 0.0),
                            self.request_timeout_seconds,
                            delay,
                        )
                        raise RetryBudgetExhausted(
                            f"{upstream.name} 剩余预算不足，未重试", exc
                        ) from exc
                logger.warning(
                    "[MultiApiRouter] 上游 %s 临时异常：%s；%.2fs 后重试（%s/%s）。",
                    upstream.name, self._safe_error(exc), delay,
                    attempt + 1, self.upstream_retry_count,
                )
                # 外层总时限同时覆盖本次等待；取消直接传播，不执行或重放工具。
                await asyncio.sleep(delay)

    async def text_chat(
        self,
        prompt: str | None = None,
        session_id: str | None = None,
        image_urls: list[str] | None = None,
        audio_urls: list[str] | None = None,
        func_tool: ToolSet | None = None,
        contexts: list[Message] | list[dict] | None = None,
        system_prompt: str | None = None,
        tool_calls_result: ToolCallsResult | list[ToolCallsResult] | None = None,
        model: str | None = None,
        extra_user_content_parts: list[ContentPart] | None = None,
        tool_choice: Literal["auto", "required"] = "auto",
        request_max_retries: int | None = None,
        **kwargs,
    ) -> LLMResponse:
        del session_id, request_max_retries
        # 发送副本在路由边界压缩；不替换 runner 的消息，原生历史仍保存原图。
        current_in_contexts = bool(contexts) and prompt is None and not image_urls and not audio_urls
        if self.history_image_compaction_enabled:
            contexts, _ = compact_contexts(contexts, current_in_contexts=current_in_contexts)
        try:
            budget = measure_request(
                prompt, contexts, system_prompt, image_urls=image_urls,
                extra_user_content_parts=extra_user_content_parts, func_tool=func_tool,
                tool_calls_result=tool_calls_result, current_in_contexts=current_in_contexts,
                max_image_bytes=self.max_image_bytes,
                max_total_image_bytes=self.max_total_image_bytes,
                max_image_count=self.max_image_count,
            )
        except ValueError as exc:
            # 预算/格式校验是确定性失败，但调用方只看到无状态码的 ValueError。
            _reply_fault(
                "BUDGET_VALUEERROR", "router", f"{exc}（确定性失败，调用方可能按可重试处理）",
                category=CATEGORY_UNKNOWN, disposition=DISPOSITION_ROTATE,
            )
            raise
        size = budget.text_chars
        if size > self.max_input_chars:
            _reply_fault(
                "BUDGET_VALUEERROR", "router",
                f"输入字符数 {size} 超过路由预算 {self.max_input_chars}，未调用上游"
                "（确定性失败，调用方可能按可重试处理）",
                category=CATEGORY_UNKNOWN, disposition=DISPOSITION_ROTATE,
            )
            raise ValueError(
                f"输入字符数 {size} 超过路由预算 {self.max_input_chars}，未调用上游。"
            )
        if not self.upstreams:
            if self.pinned_slot:
                _reply_fault(
                    "CONFIG_ERROR_DISPOSITION", "router",
                    f"指定上游 #{self.pinned_slot} 未启用或配置不完整，路由判 fail_report",
                    category=CATEGORY_CONFIG, disposition=DISPOSITION_FAIL,
                    slot=self.pinned_slot,
                )
                raise RouterConfigError(
                    f"指定上游 #{self.pinned_slot} 未启用或配置不完整，"
                    "请在多 API 路由插件中检查。"
                )
            _reply_fault(
                "CONFIG_ERROR_DISPOSITION", "router",
                "没有可用上游，路由判 fail_report",
                category=CATEGORY_CONFIG, disposition=DISPOSITION_FAIL,
            )
            raise RouterConfigError(
                "多 API 路由器没有可用上游，请先在插件配置中填写 API。"
            )

        # 指定了 model 时，只走 model 匹配的上游（供「智能过滤」等插件锁定
        # 路由下某个具体模型，跳过优先级自动降级）；未指定则保持原降级链路。
        # 指定上游模式下 self.upstreams 已只有一个，不受 model 影响。
        candidates = self.upstreams
        if model and not self.pinned_slot:
            pinned = [u for u in self.upstreams if u.client.get_model() == model]
            if pinned:
                candidates = pinned

        errors: list[str] = []
        health_skipped: list[str] = []
        quota_seen = False  # 仅用于聚合错误锚点，不参与任何路由判断。
        attempts = 0
        route_timed_out = False
        # 排队、单上游重试与备用切换共用总时限，指定上游也不能无限等待。
        deadline = time.monotonic() + self.route_timeout_seconds
        # 带工具请求：只考虑声明支持工具的上游；一个都没有时明确报错，
        # 绝不静默丢掉工具、假装执行成功。
        needs_tools = bool(func_tool and not func_tool.empty())
        if needs_tools:
            candidates = [u for u in candidates if u.supports_tools]
            if not candidates:
                _reply_fault(
                    "CONFIG_ERROR_DISPOSITION", "router",
                    "请求需要工具但没有声明支持工具的上游，路由判 fail_report",
                    category=CATEGORY_CONFIG, disposition=DISPOSITION_FAIL,
                )
                raise RouterConfigError(
                    "本次请求需要调用工具，但当前路由没有任何声明支持工具调用的上游；"
                    "已按原样失败，未执行任何工具调用。请在上游列表中勾选"
                    "「支持工具调用」，或改用支持工具的主模型。"
                )
        acquired = False
        try:
            if deadline is None:
                await self._semaphore.acquire()
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    route_timed_out = True
                else:
                    try:
                        await asyncio.wait_for(
                            self._semaphore.acquire(),
                            timeout=remaining,
                        )
                    except TimeoutError:
                        route_timed_out = True
            acquired = not route_timed_out

            for upstream in candidates:
                if route_timed_out or attempts >= self.max_attempts:
                    break
                remaining = (
                    deadline - time.monotonic()
                    if deadline is not None
                    else None
                )
                if remaining is not None and remaining <= 0:
                    route_timed_out = True
                    break
                # 历史调用失败不阻止新请求；只跳过已确认的配置错误。
                skip_reason = self._health_skip_reason(upstream)
                if skip_reason:
                    health_skipped.append(f"{upstream.name}: {skip_reason}")
                    logger.warning(
                        "[MultiApiRouter] 跳过不健康上游 %s（#%d）：%s。",
                        upstream.name,
                        upstream.slot,
                        skip_reason,
                    )
                    continue
                attempts += 1
                started = time.monotonic()
                try:
                    call = self._call_once(
                        upstream,
                        prompt=prompt,
                        image_urls=image_urls,
                        audio_urls=audio_urls,
                        func_tool=func_tool,
                        contexts=contexts,
                        system_prompt=system_prompt,
                        tool_calls_result=tool_calls_result,
                        extra_user_content_parts=extra_user_content_parts,
                        tool_choice=tool_choice,
                        kwargs=kwargs,
                        deadline=deadline,
                    )
                    response = (
                        await asyncio.wait_for(call, timeout=remaining)
                        if remaining is not None
                        else await call
                    )
                    latency_ms = int((time.monotonic() - started) * 1000)
                    await self._record_success(upstream, latency_ms)
                    # 200 空正文在路由侧仍算成功；调用方会按空响应重试，
                    # 这里给出根因锚点，避免只看到调用方反复重试。
                    if _blank_completion(response):
                        _reply_fault(
                            "EMPTY_BODY_200", "router",
                            "HTTP 200 但正文为空且无工具调用，已记为上游成功；"
                            "调用方会按空响应重试",
                            category="empty_response", disposition=DISPOSITION_IGNORE,
                            slot=upstream.slot,
                        )
                    return response
                except RetryBudgetExhausted as exc:
                    latency_ms = int((time.monotonic() - started) * 1000)
                    # 真实失败已记在上游状态里；跳过重试的原因已在 _call_once 单独打日志。
                    await self._record_failure(upstream, exc.last_error, latency_ms)
                    quota_seen = quota_seen or upstream.last_category == CATEGORY_QUOTA
                    errors.append(
                        f"{upstream.name}: {self._safe_error(exc.last_error)}"
                        "（剩余预算不足，未重试）"
                    )
                except Exception as exc:
                    latency_ms = int((time.monotonic() - started) * 1000)
                    await self._record_failure(upstream, exc, latency_ms)
                    quota_seen = quota_seen or upstream.last_category == CATEGORY_QUOTA
                    errors.append(f"{upstream.name}: {self._safe_error(exc)}")
                    if (
                        deadline is not None
                        and time.monotonic() >= deadline - 0.01
                    ):
                        route_timed_out = True
                        break
        finally:
            if acquired:
                self._semaphore.release()

        # 只有真实调用失败（attempts > 0）才回退主模型；健康检查跳过不算。
        if attempts > 0 and not route_timed_out:
            try:
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        route_timed_out = True
                        fallback_response = None
                    else:
                        fallback_response = await asyncio.wait_for(
                            self._call_fallback(
                                prompt=prompt,
                                image_urls=image_urls,
                                audio_urls=audio_urls,
                                func_tool=func_tool,
                                contexts=contexts,
                                system_prompt=system_prompt,
                                tool_calls_result=tool_calls_result,
                                extra_user_content_parts=extra_user_content_parts,
                                tool_choice=tool_choice,
                                kwargs=kwargs,
                            ),
                            timeout=remaining,
                        )
                else:
                    fallback_response = await self._call_fallback(
                        prompt=prompt,
                        image_urls=image_urls,
                        audio_urls=audio_urls,
                        func_tool=func_tool,
                        contexts=contexts,
                        system_prompt=system_prompt,
                        tool_calls_result=tool_calls_result,
                        extra_user_content_parts=extra_user_content_parts,
                        tool_choice=tool_choice,
                        kwargs=kwargs,
                    )
            except TimeoutError:
                route_timed_out = True
                fallback_response = None
            except Exception as exc:
                logger.warning(
                    "[MultiApiRouter] 主模型回退也失败：%s。",
                    self._safe_error(exc),
                )
                fallback_response = None
            if fallback_response is not None:
                return fallback_response

        if route_timed_out:
            raise RuntimeError(
                f"多 API 路由超过总时限 {self.route_timeout_seconds} 秒，"
                f"已停止后续调用；已尝试 {attempts} 个上游。"
            )
        if attempts == 0:
            detail = "；".join(health_skipped) or "无可用上游"
            # 文案里嵌了上游状态（如 HTTP 401），调用方可能据此误判为认证错误。
            _reply_fault(
                "HEALTH_SKIP_ALL_401" if "HTTP 401" in detail else "HEALTH_SKIP_ALL",
                "router", f"全部上游被健康跳过：{detail}；路由判 fail_report",
                category=CATEGORY_CONFIG, disposition=DISPOSITION_FAIL,
            )
            raise RouterConfigError(f"没有可调用的上游：{detail}。")
        if quota_seen:
            # 聚合文案只保留 HTTP 429，配额关键字已丢失；调用方会按普通限流重试。
            _reply_fault(
                "AGGREGATE_DROP_QUOTA", "router",
                "配额类失败被聚合为普通 HTTP 429，调用方会重试而不是立即切换",
                category=CATEGORY_QUOTA, disposition=DISPOSITION_ROTATE,
            )
        raise RuntimeError(
            f"多 API 路由失败，已尝试 {attempts} 个上游：{'; '.join(errors)}"
        )

    async def text_chat_stream(self, **kwargs) -> AsyncGenerator[LLMResponse, None]:
        response = await self.text_chat(**kwargs)
        yield response

    def status_lines(self) -> list[str]:
        mode = (
            f"指定上游 #{self.pinned_slot}（不自动分配）"
            if self.pinned_slot
            else "整体自动分配（按优先级，失败切换）"
        )
        lines = [
            f"Provider: {self.meta().id}",
            f"模式: {mode}",
            (
                f"上游: {len(self.upstreams)}，每次最多尝试 {self.max_attempts} 个，"
                f"临时异常最多重试 {self.upstream_retry_count} 次，"
                f"重试间隔 {self.retry_interval_seconds:g}s，不冻结接口，"
                f"单次接口超时 {self.request_timeout_seconds}s，"
                f"总路由超时 {self.route_timeout_seconds}s，"
                f"输出上限 {self.max_output_tokens} tokens"
            ),
        ]
        if not self.upstreams:
            lines.append("尚未配置可用上游。")
            return lines
        for upstream in self.upstreams:
            state = "可用"
            skip_reason = self._health_skip_reason(upstream)
            if skip_reason:
                state = f"跳过（{skip_reason}）"
            lines.append(
                f"- {upstream.name}: {state}，成功/调用 "
                f"{upstream.successes}/{upstream.calls}，最近 {upstream.last_latency_ms}ms"
            )
        return lines


@register(
    PLUGIN_NAME,
    "AKIhunter",
    "多个 OpenAI 兼容 API 的优先级路由、异常重试与自动降级 Provider",
    "1.3.3",
)
class MultiApiRouterPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.router: MultiApiRouterProvider | None = None
        # 所有向 AstrBot 注册的实例：整体路由 + 每个上游一个「指定上游」Provider。
        self._router_instances: list[MultiApiRouterProvider] = []
        self._public_provider_configs: list[dict] = []
        self._binding_lock = asyncio.Lock()
        self._binding_state: dict = {"version": 1, "bindings": {}}
        self._binding_messages: list[str] = []
        self._health_results: dict[int, dict] = {}
        self._health_inflight: set[int] = set()
        self._health_semaphore = asyncio.Semaphore(2)
        self._model_list_semaphore = asyncio.Semaphore(2)
        self._auto_check_task: asyncio.Task | None = None
        self._auto_check_stop: asyncio.Event | None = None
        self._state_path = Path(
            StarTools.get_data_dir(PLUGIN_NAME)
        ) / "auto_bindings.json"
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._load_binding_state()
        self._register_web_apis()

    async def initialize(self):
        if not self.config.get("enabled", True):
            logger.info("[MultiApiRouter] 插件已禁用。")
            return
        provider_id = await self._install_router_providers()
        await self._reconcile_auto_bindings()
        logger.info(
            "[MultiApiRouter] Provider %s 已注册，已加载 %d 个上游；"
            "指定上游 Provider %d 个。",
            provider_id,
            len(self.router.upstreams) if self.router else 0,
            max(0, len(self._router_instances) - 1),
        )
        await self._verify_pinned_providers()
        self._start_auto_health_check()

    async def _verify_pinned_providers(self) -> None:
        """保留线上启动自检，补齐框架初始化阶段遗漏的指定上游。"""
        try:
            manager = self.context.provider_manager
            entries = [
                item for item in manager.providers_config
                if isinstance(item, dict) and str(item.get("type")) == PROVIDER_TYPE
            ]
            missing = [
                item.get("id") for item in entries
                if item.get("id") and item.get("id") not in manager.inst_map
            ]
            logger.warning(
                "[MultiApiRouter] 指定上游自检：配置 %d 个，已注册 %d 个，缺失 %s",
                len(entries), len(entries) - len(missing), missing or "无",
            )
            for provider_id in missing:
                entry = next((i for i in entries if i.get("id") == provider_id), None)
                if entry is None:
                    continue
                try:
                    await manager.load_provider(entry)
                    logger.warning("[MultiApiRouter] 已补齐缺失 Provider：%s", provider_id)
                except Exception as exc:
                    logger.warning("[MultiApiRouter] 补齐 %s 失败：%s", provider_id, exc)
        except Exception as exc:
            logger.warning("[MultiApiRouter] 自检异常：%s", exc)

    @staticmethod
    def _pinned_provider_id(slot: int) -> str:
        return f"{PINNED_PROVIDER_PREFIX}{slot}"

    @staticmethod
    def _upstream_provider_label(upstream: UpstreamState) -> str:
        name = str(upstream.name or "").strip()
        model = str(upstream.client.get_model() or "").strip()
        return name or model or f"上游 {upstream.slot}"

    @staticmethod
    def _provider_source_id(label: str, slot: int, used: set[str]) -> str:
        """保留线上 4.29 兼容逻辑：source 使用上游名称，重名按槽位区分。"""
        base = str(label or "").strip() or f"上游 {slot}"
        candidate = base
        if candidate in used:
            candidate = f"{base}（{slot}）"
        index = 2
        while candidate in used:
            candidate = f"{base}（{slot}-{index}）"
            index += 1
        used.add(candidate)
        return candidate

    def _provider_entry(
        self,
        provider_id: str,
        model: str,
        slot: int = 0,
        display_name: str = "",
        parent_provider_id: str = "",
        provider_source_id: str = "",
    ) -> dict:
        entry = {
            "id": provider_id,
            "type": PROVIDER_TYPE,
            "provider_type": "chat_completion",
            "model": model,
            "enable": True,
            "display_name": display_name,
            "label": display_name,
            "name": display_name,
        }
        if provider_source_id:
            entry["provider_source_id"] = provider_source_id
        if slot:
            # AstrBot Provider 条目本身不认识这个字段，插件实例化时用它锁定上游。
            entry["upstream_slot"] = slot
            entry["parent_provider_id"] = parent_provider_id
        return entry

    async def _close_router_instance(self, inst: "MultiApiRouterProvider") -> None:
        for upstream in getattr(inst, "_all_upstreams", []):
            try:
                await upstream.client.client.close()
            except Exception:
                pass

    def _resolve_fallback_provider(self):
        """真实调用全部失败时使用的「主模型」。

        优先用配置里的 fallback_provider_id；未填写则用 AstrBot 默认主模型。
        返回 None 表示不回退，避免路由调用自己造成递归。
        """
        provider_manager = self.context.provider_manager
        fallback_id = str(self.config.get("fallback_provider_id") or "").strip()
        if not fallback_id:
            fallback_id = str(
                getattr(provider_manager, "default_chat_provider_id", "") or ""
            ).strip()
        if not fallback_id:
            current = getattr(provider_manager, "curr_provider_inst", None)
            fallback_id = str(getattr(current, "provider_config", {}).get("id", ""))
        if not fallback_id:
            return None
        if fallback_id in self._router_ids():
            return None
        candidate = provider_manager.inst_map.get(fallback_id)
        if candidate is None or not isinstance(candidate, Provider):
            return None
        if getattr(candidate, "provider_config", {}).get("type") == PROVIDER_TYPE:
            return None
        return candidate

    def _router_ids(self) -> set[str]:
        return {
            str(inst.meta().id)
            for inst in self._router_instances
        } | {PROVIDER_TYPE}

    async def _install_router_providers(self) -> str:
        """注册「整体」和「每个上游」两类 AstrBot Provider。

        两类实例使用同一个 Provider 类与同一套 text_chat 接口，区别只在
        upstream_slot：为空时按优先级自动分配，有值时只走指定上游。
        """
        provider_manager = self.context.provider_manager
        # 先清理上一轮（热重载/插件重载）遗留的本插件实例。
        stale_instances = list(self._router_instances)
        for pid, inst in list(provider_manager.inst_map.items()):
            if isinstance(inst, MultiApiRouterProvider) and inst not in stale_instances:
                stale_instances.append(inst)
        for existing in stale_instances:
            if existing in provider_manager.provider_insts:
                provider_manager.provider_insts.remove(existing)
            existing_id = existing.meta().id
            if provider_manager.inst_map.get(existing_id) is existing:
                del provider_manager.inst_map[existing_id]
            await self._close_router_instance(existing)
        self._router_instances = []

        overall_config = dict(self.config)
        overall_config["display_name"] = "多 API 路由（自动）"
        router = MultiApiRouterProvider(overall_config)
        provider_id = router.meta().id
        existing = provider_manager.inst_map.get(provider_id)
        if existing is not None:
            existing_meta = existing.meta()
            if existing_meta.type != PROVIDER_TYPE:
                raise RuntimeError(
                    f"Provider ID {provider_id} 已被其他类型占用，"
                    "请修改多 API 路由插件的 provider_id。"
                )
            if existing in provider_manager.provider_insts:
                provider_manager.provider_insts.remove(existing)
            if provider_manager.inst_map.get(provider_id) is existing:
                del provider_manager.inst_map[provider_id]
            await self._close_router_instance(existing)

        router.health_lookup = self._health_results.get
        router.health_recorder = self._record_health_success
        router.fallback_resolver = self._resolve_fallback_provider
        instances = [router]
        # 保留线上 source 名称映射，重载时排除本插件旧 source，避免追加后缀。
        used_source_ids = {
            str(item.get("id") or "") for item in provider_manager.providers_config
            if isinstance(item, dict)
        }
        used_source_ids |= {
            str(item.get("id") or "") for item in provider_manager.provider_sources_config
            if isinstance(item, dict) and item.get("type") != PROVIDER_TYPE
        }
        entries = [
            self._provider_entry(
                provider_id,
                "automatic",
                display_name="多 API 路由（自动）",
                provider_source_id=self._provider_source_id(
                    "多 API 路由（自动）", 0, used_source_ids
                ),
            )
        ]

        # 所有配置完整的聊天兼容 API 都注册直连 Provider；“启用路由”只影响
        # 整体自动候选链，不影响调用方固定选择某个 API。
        for upstream in router._all_upstreams:
            pinned_id = self._pinned_provider_id(upstream.slot)
            pinned_config = dict(self.config)
            pinned_config["provider_id"] = pinned_id
            pinned_config["upstream_slot"] = upstream.slot
            pinned_config["display_name"] = self._upstream_provider_label(upstream)
            pinned_config["parent_provider_id"] = provider_id
            pinned = MultiApiRouterProvider(pinned_config)
            pinned.health_lookup = self._health_results.get
            pinned.health_recorder = self._record_health_success
            # 指定上游模式不自动降级，也不回退主模型。
            pinned.fallback_resolver = None
            instances.append(pinned)
            entries.append(
                self._provider_entry(
                    pinned_id,
                    pinned.meta().model or upstream.client.get_model(),
                    upstream.slot,
                    self._upstream_provider_label(upstream),
                    provider_id,
                    self._provider_source_id(
                        self._upstream_provider_label(upstream), upstream.slot, used_source_ids
                    ),
                )
            )

        for inst in instances:
            self.context.register_provider(inst)
            provider_manager.inst_map[inst.meta().id] = inst
        self._router_instances = instances
        self.router = router

        # 把条目注入 AstrBot 的 provider 列表，控制台与模型选择器立刻可见。
        # 只改内存列表，不写 cmd_config.json，避免核心启动顺序造成的重复加载。
        desired_ids = {item["id"] for item in entries}
        # 清理已停用上游遗留的条目（仅限本插件类型的条目）。
        for item in list(provider_manager.providers_config):
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id") or "")
            if item.get("type") == PROVIDER_TYPE and item_id not in desired_ids:
                provider_manager.providers_config.remove(item)
        # 已存在的条目就地更新，保证停用/改名/换模型后控制台显示同步。
        existing_items = {
            str(item.get("id")): item
            for item in provider_manager.providers_config
            if isinstance(item, dict)
        }
        for entry in entries:
            current = existing_items.get(entry["id"])
            if current is None:
                provider_manager.providers_config.append(entry)
            else:
                current.update(entry)
        # AstrBot 4.29 的 Provider/source 双层引用只在内存中同步。
        desired_source_ids = {
            str(entry.get("provider_source_id") or "") for entry in entries
            if entry.get("provider_source_id")
        }
        source_items = provider_manager.provider_sources_config
        previous_source_ids = {
            str(item.get("id") or "") for item in source_items
            if isinstance(item, dict) and item.get("type") == PROVIDER_TYPE
        }
        for item in list(source_items):
            if (
                isinstance(item, dict) and item.get("type") == PROVIDER_TYPE
                and str(item.get("id") or "") not in desired_source_ids
            ):
                source_items.remove(item)
        existing_sources = {
            str(item.get("id") or ""): item for item in source_items if isinstance(item, dict)
        }
        for entry in entries:
            source_id = str(entry.get("provider_source_id") or "")
            if not source_id:
                continue
            payload = {
                "id": source_id, "type": PROVIDER_TYPE, "provider_type": "chat_completion",
                "display_name": entry.get("display_name", ""),
                "label": entry.get("label", ""), "name": entry.get("name", ""),
            }
            if entry.get("upstream_slot"):
                payload["upstream_slot"] = entry["upstream_slot"]
                payload["parent_provider_id"] = entry.get("parent_provider_id", "")
            current_source = existing_sources.get(source_id)
            if current_source is None:
                source_items.append(payload)
            else:
                current_source.update(payload)
        default_config = getattr(
            getattr(provider_manager, "acm", None), "confs", {}
        ).get("default")
        if default_config is not None and "provider_sources" not in default_config:
            default_config["provider_sources"] = source_items
        if previous_source_ids != desired_source_ids:
            logger.info(
                "[MultiApiRouter] provider_source ID 已改为上游名称（%d 条）：%s",
                len(desired_source_ids), "、".join(sorted(desired_source_ids)) or "（无）",
            )
        else:
            logger.debug(
                "[MultiApiRouter] provider_source 未变化：%s",
                "、".join(sorted(desired_source_ids)),
            )
        self._public_provider_configs = [
            item
            for item in provider_manager.providers_config
            if isinstance(item, dict)
            and item.get("type") == PROVIDER_TYPE
            and str(item.get("id")) in desired_ids
        ]
        return provider_id

    def _start_auto_health_check(self) -> None:
        """启动每小时一次的自动上游健康检测，异常写 warning 日志。

        启动后会先做一轮初检（短暂延迟，等 router 就绪），之后每 1 小时
        复检一次；两次检查之间健康状态保持在 _health_results 中持续展示。
        """
        if self._auto_check_task is not None and not self._auto_check_task.done():
            return
        self._auto_check_stop = asyncio.Event()
        self._auto_check_task = asyncio.create_task(self._auto_check_all())
        logger.info("[MultiApiRouter] 已启动自动上游健康检测（每 1 小时一次）。")

    async def _auto_check_all(self) -> None:
        assert self._auto_check_stop is not None
        # 初检延迟：给 router 初始化留一点缓冲，避免启动即并发打上游。
        initial_delay = 10
        try:
            await asyncio.wait_for(self._auto_check_stop.wait(), timeout=initial_delay)
            return
        except asyncio.TimeoutError:
            pass
        while not self._auto_check_stop.is_set():
            await self._run_auto_check_once()
            # 每小时复检一次；期间状态持续显示，直到下一次检查更新。
            try:
                await asyncio.wait_for(self._auto_check_stop.wait(), timeout=3600)
                break
            except asyncio.TimeoutError:
                pass

    async def _run_auto_check_once(self) -> None:
        """对已配置（api_base/api_key/model 齐全）的槽位做一次检测并更新状态。"""
        for slot in self._render_slots():
            raw = self.config.get(f"upstream_{slot}") or {}
            if not isinstance(raw, dict):
                continue
            if MultiApiRouterProvider._api_type(raw) not in AUTO_HEALTH_CHECK_API_TYPES:
                continue
            if not (
                str(raw.get("api_base") or "").strip()
                and str(raw.get("api_key") or "").strip()
                and str(raw.get("model") or "").strip()
            ):
                continue
            if slot in self._health_inflight:
                continue
            try:
                result = await self._check_upstream_slot(slot)
            except Exception:
                continue
            if not result.get("ok"):
                logger.warning(
                    "[MultiApiRouter] 自动检测异常：上游 #%d（%s）状态「%s」。",
                    slot,
                    str(raw.get("name") or f"Upstream {slot}"),
                    result.get("status"),
                )

    @staticmethod
    def _get_path(config: dict, path: tuple[str, ...]) -> Any:
        value: Any = config
        for key in path:
            if not isinstance(value, dict) or key not in value:
                return None
            value = value[key]
        return value

    @staticmethod
    def _set_path(config: dict, path: tuple[str, ...], value: Any) -> bool:
        target: Any = config
        for key in path[:-1]:
            if not isinstance(target, dict) or key not in target:
                return False
            target = target[key]
        if not isinstance(target, dict):
            return False
        target[path[-1]] = value
        return True

    @staticmethod
    def _is_unset_provider(value: Any) -> bool:
        return value is None or (
            isinstance(value, str)
            and value.strip().lower() in {"", "default"}
        )

    @staticmethod
    def _binding_key(plugin_name: str, path: tuple[str, ...]) -> str:
        return f"{plugin_name}:{'.'.join(path)}"

    def _load_binding_state(self) -> None:
        if not self._state_path.exists():
            return
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("bindings"), dict):
                self._binding_state = data
        except Exception as exc:
            logger.warning(
                "[MultiApiRouter] 自动绑定状态文件无效，已忽略：%s",
                type(exc).__name__,
            )

    def _save_binding_state(self) -> None:
        temp_path = self._state_path.with_suffix(".tmp")
        payload = json.dumps(
            self._binding_state,
            ensure_ascii=False,
            indent=2,
        )
        temp_path.write_text(payload, encoding="utf-8")
        os.chmod(temp_path, 0o600)
        temp_path.replace(self._state_path)

    def _auto_bind_ready(self) -> bool:
        if not self.config.get("auto_bind_enabled", True):
            return False
        require_ready = self.config.get("auto_bind_require_ready_upstream", True)
        return not require_ready or bool(self.router and self.router.upstreams)

    @staticmethod
    def _find_plugin_metadata(plugin_name: str) -> StarMetadata | None:
        for metadata in star_map.values():
            if metadata.name == plugin_name and metadata.activated:
                return metadata
        return None

    def _target_enabled(self, plugin_name: str) -> bool:
        target = AUTO_BIND_TARGETS.get(plugin_name)
        return bool(
            target
            and self.config.get(str(target["config_flag"]), True)
        )

    def _preview_metadata(
        self,
        metadata: StarMetadata,
        provider_id: str,
    ) -> list[str]:
        plugin_name = str(metadata.name or "")
        target = AUTO_BIND_TARGETS.get(plugin_name)
        config = metadata.config
        if not target or config is None or not self._target_enabled(plugin_name):
            return []
        only_empty = self.config.get("auto_bind_only_empty", True)
        preview: list[str] = []
        bindings = self._binding_state["bindings"]
        for path in target["paths"]:
            current = self._get_path(config, path)
            key = self._binding_key(plugin_name, path)
            record = bindings.get(key)
            previously_applied = record.get("applied") if isinstance(record, dict) else None
            if current == provider_id:
                preview.append(f"{plugin_name}.{'.'.join(path)}：已绑定")
            elif record and current == previously_applied:
                preview.append(
                    f"{plugin_name}.{'.'.join(path)}：更新绑定 -> {provider_id}"
                )
            elif only_empty and not self._is_unset_provider(current):
                preview.append(
                    f"{plugin_name}.{'.'.join(path)}：保留手动值 {current}"
                )
            else:
                preview.append(
                    f"{plugin_name}.{'.'.join(path)}：{current or '<空>'} -> {provider_id}"
                )
        return preview

    async def _apply_metadata_bindings(
        self,
        metadata: StarMetadata,
        provider_id: str,
    ) -> list[str]:
        plugin_name = str(metadata.name or "")
        target = AUTO_BIND_TARGETS.get(plugin_name)
        config = metadata.config
        if not target or config is None or not self._target_enabled(plugin_name):
            return []
        only_empty = self.config.get("auto_bind_only_empty", True)
        bindings = self._binding_state["bindings"]
        changed = False
        messages: list[str] = []
        for path in target["paths"]:
            current = self._get_path(config, path)
            key = self._binding_key(plugin_name, path)
            record = bindings.get(key)
            previously_applied = record.get("applied") if isinstance(record, dict) else None

            if current == provider_id:
                messages.append(
                    f"{plugin_name}.{'.'.join(path)}："
                    + ("已自动绑定" if record else "已手动绑定")
                )
                continue

            if record and current not in {previously_applied, provider_id}:
                del bindings[key]
                changed = True
                messages.append(
                    f"{plugin_name}.{'.'.join(path)}：检测到手动修改，停止管理"
                )
                continue

            if only_empty and not record and not self._is_unset_provider(current):
                messages.append(
                    f"{plugin_name}.{'.'.join(path)}：保留手动值"
                )
                continue

            original = record.get("original") if isinstance(record, dict) else current
            if not self._set_path(config, path, provider_id):
                messages.append(f"{plugin_name}.{'.'.join(path)}：字段不存在")
                continue
            bindings[key] = {
                "plugin": plugin_name,
                "path": list(path),
                "original": original,
                "applied": provider_id,
            }
            changed = True
            messages.append(f"{plugin_name}.{'.'.join(path)}：已自动绑定")

        if changed:
            save_async = getattr(config, "save_config_async", None)
            if callable(save_async):
                await save_async()
            else:
                config.save_config()
            self._save_binding_state()
        return messages

    async def _restore_auto_bindings(self) -> list[str]:
        bindings = self._binding_state.get("bindings", {})
        messages: list[str] = []
        changed_configs: dict[int, Any] = {}
        for key, record in list(bindings.items()):
            if not isinstance(record, dict):
                del bindings[key]
                continue
            plugin_name = str(record.get("plugin") or "")
            metadata = self._find_plugin_metadata(plugin_name)
            if metadata is None or metadata.config is None:
                continue
            path = tuple(str(item) for item in record.get("path", []))
            if not path:
                del bindings[key]
                continue
            current = self._get_path(metadata.config, path)
            if current == record.get("applied"):
                self._set_path(metadata.config, path, record.get("original", ""))
                changed_configs[id(metadata.config)] = metadata.config
                messages.append(f"{plugin_name}.{'.'.join(path)}：已恢复")
            else:
                messages.append(
                    f"{plugin_name}.{'.'.join(path)}：已有手动修改，未覆盖"
                )
            del bindings[key]
        for config in changed_configs.values():
            save_async = getattr(config, "save_config_async", None)
            if callable(save_async):
                await save_async()
            else:
                config.save_config()
        self._save_binding_state()
        return messages

    async def _reconcile_auto_bindings(self) -> None:
        async with self._binding_lock:
            if not self.config.get("auto_bind_enabled", True):
                if self.config.get("auto_bind_restore_on_disable", True):
                    self._binding_messages = await self._restore_auto_bindings()
                return
            if not self._auto_bind_ready():
                self._binding_messages = [
                    "自动绑定等待中：至少配置并启用一个完整上游后才会应用。"
                ]
                return
            provider_id = self.router.meta().id if self.router else "multi_api_router"
            messages: list[str] = []
            for plugin_name in AUTO_BIND_TARGETS:
                metadata = self._find_plugin_metadata(plugin_name)
                if metadata is not None:
                    messages.extend(
                        await self._apply_metadata_bindings(metadata, provider_id)
                    )
            self._binding_messages = messages

    @filter.on_plugin_loaded()
    async def auto_bind_loaded_plugin(self, metadata: StarMetadata):
        if not metadata.name:
            return
        if not self.config.get("auto_bind_enabled", True):
            if self.config.get("auto_bind_restore_on_disable", True):
                async with self._binding_lock:
                    self._binding_messages = await self._restore_auto_bindings()
            return
        if not self._auto_bind_ready():
            return
        if metadata.name not in AUTO_BIND_TARGETS:
            return
        async with self._binding_lock:
            provider_id = self.router.meta().id if self.router else "multi_api_router"
            self._binding_messages.extend(
                await self._apply_metadata_bindings(metadata, provider_id)
            )

    def _register_web_apis(self) -> None:
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/upstreams",
            self.api_upstreams,
            ["GET"],
            "多 API 路由上游列表",
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/upstreams/check",
            self.api_check_upstream,
            ["POST"],
            "检测单个模型 API 上游",
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/upstreams/models",
            self.api_list_models,
            ["POST"],
            "使用当前 API Base 和 Key 获取可见模型列表",
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/upstreams/config",
            self.api_upstreams_config,
            ["GET"],
            "读取模型 API 上游配置（API Key 默认掩码，仅控制台调用）",
        )
        # 明文 Key 只在用户点击「显示」时按需返回，独立路径便于鉴权与审计。
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/upstreams/config/reveal",
            self.api_reveal_upstream_key,
            ["POST"],
            "按需揭示单个上游的明文 API Key（需登录，写审计日志）",
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/upstreams/save",
            self.api_save_upstreams,
            ["POST"],
            "保存模型 API 上游配置",
        )

    def _retired_slots(self) -> list[int]:
        """已删除（退役）的槽位号：新增上游时永不复用，避免旧引用静默指向新上游。

        旧配置没有该字段时视为空列表；非法值直接忽略，保证向后兼容。
        """
        raw = self.config.get("retired_slots")
        if not isinstance(raw, list):
            return []
        slots: set[int] = set()
        for item in raw:
            try:
                slot = int(item)
            except (TypeError, ValueError):
                continue
            if 1 <= slot <= MAX_UPSTREAMS:
                slots.add(slot)
        return sorted(slots)

    def _render_slots(self) -> list[int]:
        """要展示给控制台的槽位号：至少 MIN_UPSTREAMS 个基础槽位，
        并纳入配置里已存在的高编号槽位（支持「新增 API」扩展到 MAX_UPSTREAMS）。"""
        existing = {
            slot
            for slot in range(1, MAX_UPSTREAMS + 1)
            if f"upstream_{slot}" in self.config
        }
        highest = max(existing) if existing else 0
        count = max(MIN_UPSTREAMS, highest)
        return list(range(1, count + 1))

    def _upstream_public_data(self, slot: int) -> dict:
        raw = self.config.get(f"upstream_{slot}") or {}
        if not isinstance(raw, dict):
            raw = {}
        api_base = str(raw.get("api_base") or "").strip()
        try:
            host = urlsplit(api_base).netloc
        except ValueError:
            host = ""
        configured = bool(
            api_base
            and str(raw.get("api_key") or "").strip()
            and str(raw.get("model") or "").strip()
        )
        return {
            "slot": slot,
            "route_enabled": MultiApiRouterProvider._chat_route_enabled(raw),
            # 兼容旧页面/API 客户端，enabled 与 route_enabled 保持同值。
            "enabled": MultiApiRouterProvider._chat_route_enabled(raw),
            "configured": configured,
            "name": str(raw.get("name") or f"Upstream {slot}"),
            "api_base": api_base,
            "host": host,
            "model": str(raw.get("model") or ""),
            "api_type": MultiApiRouterProvider._api_type(raw),
            "api_type_label": API_TYPE_LABELS[
                MultiApiRouterProvider._api_type(raw)
            ],
            "chat_compatible": (
                MultiApiRouterProvider._api_type(raw)
                in CHAT_COMPATIBLE_API_TYPES
            ),
            "priority": self.router._bounded_int(
                raw.get("priority"), slot * 10, -10000, 10000
            ) if self.router else slot * 10,
            "supports_tools": bool(raw.get("supports_tools", True)),
            "enable_thinking": MultiApiRouterProvider._enable_thinking_value(raw),
            "checking": slot in self._health_inflight,
            "provider_id": self._pinned_provider_id(slot),
            "provider_label": str(raw.get("name") or raw.get("model") or f"上游 {slot}"),
            "health": self._health_results.get(slot),
        }

    @staticmethod
    def _health_check_audio() -> bytes:
        """生成短静音 WAV，避免健康检查依赖外部样本文件。"""
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(b"\x00\x00" * 1600)
        return buffer.getvalue()

    async def _execute_typed_probe(
        self,
        client: AsyncOpenAI,
        *,
        api_type: str,
        model: str,
    ) -> tuple[bool, str]:
        """按 API 分类调用对应协议，防止把非聊天接口伪装成 Chat Completions。"""
        if api_type in {"chat", "image_to_text", "multimodal"}:
            protocol = "Chat Completions"
            if api_type in {"image_to_text", "multimodal"}:
                # 健康检查只验证端点与模型可用性。部分兼容服务拒绝 data URL，
                # 用内嵌图片会把可用的视觉模型误判为故障。
                protocol = f"Chat Completions（{API_TYPE_LABELS[api_type]}）"
            response = await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "Reply with exactly OK."}],
                # 推理 token 也占输出预算；8 token 会让可用模型在正文前被截断。
                max_tokens=256,
                temperature=0,
            )
            text = ""
            if response.choices:
                text = str(response.choices[0].message.content or "").strip()
            return bool(text), protocol

        if api_type == "image_generation":
            response = await client.images.generate(
                model=model,
                prompt="A plain white square on a white background.",
                n=1,
                # GPT Image 使用兼容的方形尺寸，避免旧检测尺寸被上游拒绝。
                size="1024x1024" if model.lower().startswith("gpt-image") else "256x256",
            )
            return bool(response.data), "Images Generations（手动检测可能计费）"

        if api_type == "embedding":
            response = await client.embeddings.create(
                model=model,
                input="health check",
            )
            valid = bool(response.data and response.data[0].embedding)
            return valid, "Embeddings"

        if api_type == "rerank":
            response = await client.post(
                "/rerank",
                cast_to=dict,
                body={
                    "model": model,
                    "query": "health",
                    "documents": ["health", "other"],
                    "top_n": 1,
                },
            )
            return bool(response), "Rerank"

        if api_type in {"stt", "asr"}:
            await client.audio.transcriptions.create(
                model=model,
                file=("health.wav", self._health_check_audio(), "audio/wav"),
            )
            return True, "Audio Transcriptions"

        if api_type == "tts":
            response = await client.audio.speech.create(
                model=model,
                voice="alloy",
                input="OK",
            )
            return bool(response), "Audio Speech（手动检测可能计费）"

        raise ValueError(f"不支持的 API 类型: {api_type}")

    async def _probe_upstream(self, slot: int) -> dict:
        raw = self.config.get(f"upstream_{slot}") or {}
        if not isinstance(raw, dict):
            raw = {}
        name = str(raw.get("name") or f"Upstream {slot}")
        api_base = str(raw.get("api_base") or "").strip().rstrip("/")
        api_key = str(raw.get("api_key") or "").strip()
        model = str(raw.get("model") or "").strip()
        api_type = MultiApiRouterProvider._api_type(raw)
        if not api_base or not api_key or not model:
            return {
                "ok": False,
                "checked_at": int(time.time()),
                "latency_ms": 0,
                "status": "配置不完整",
                "http_status": None,
                "error_type": "ConfigurationError",
                "category": CATEGORY_CONFIG,
                "disposition": DISPOSITION_FAIL,
                "api_type": api_type,
                "protocol": "",
            }

        timeout = MultiApiRouterProvider._bounded_int(
            self.config.get("health_check_timeout_seconds"), 15, 5, 60
        )
        # 生图响应常超过聊天探针的 15 秒；只延长手动生图检测的等待时间。
        if api_type == "image_generation":
            timeout = max(timeout, 60)
        started = time.monotonic()
        client = AsyncOpenAI(
            api_key=api_key,
            base_url=api_base,
            timeout=timeout,
            max_retries=0,
        )
        try:
            has_response, protocol = await asyncio.wait_for(
                self._execute_typed_probe(
                    client,
                    api_type=api_type,
                    model=model,
                ),
                timeout=timeout + 1,
            )
            latency_ms = int((time.monotonic() - started) * 1000)
            if not has_response:
                # 空响应多见于思考型模型耗尽探针输出预算，不等于配置错误，
                # 因此只展示、不参与自动跳过，避免冻结真实请求。
                return {
                    "ok": False,
                    "checked_at": int(time.time()),
                    "latency_ms": latency_ms,
                    "status": "响应为空",
                    "http_status": 200,
                    "error_type": "EmptyResponse",
                    "category": "empty_response",
                    "disposition": DISPOSITION_IGNORE,
                    "api_type": api_type,
                    "protocol": protocol,
                }
            logger.info(
                "[MultiApiRouter] 上游检测成功：%s，%dms。",
                name,
                latency_ms,
            )
            return {
                "ok": True,
                "checked_at": int(time.time()),
                "latency_ms": latency_ms,
                "status": "正常",
                "http_status": 200,
                "error_type": None,
                "api_type": api_type,
                "protocol": protocol,
            }
        except Exception as exc:
            latency_ms = int((time.monotonic() - started) * 1000)
            category, disposition = classify(exc)
            status_code = _error_status(exc)
            error_type = type(exc).__name__
            # 判定全部来自 classify()，这里只负责把分类翻成页面文案。
            status = HEALTH_STATUS_LABELS.get(category, "连接或请求失败")
            logger.warning(
                "[MultiApiRouter] 上游检测失败：%s，%s%s；分类 %s/%s。",
                name,
                error_type,
                f" HTTP {status_code}" if status_code else "",
                category,
                disposition,
            )
            return {
                "ok": False,
                "checked_at": int(time.time()),
                "latency_ms": latency_ms,
                "status": status,
                "http_status": status_code,
                "error_type": error_type,
                "category": category,
                "disposition": disposition,
                "api_type": api_type,
                "protocol": API_TYPE_LABELS[api_type],
            }
        finally:
            await client.close()

    def _record_health_success(self, slot: int, result: dict) -> None:
        """真实调用成功时覆盖该槽位健康结果，立即解除自动路由的健康跳过。"""
        if 1 <= slot <= MAX_UPSTREAMS:
            # 与 _check_upstream_slot 保持一致：写入都在事件循环内完成，不另加锁。
            self._health_results[slot] = result

    async def _check_upstream_slot(self, slot: int) -> dict:
        if slot < 1 or slot > MAX_UPSTREAMS:
            raise ValueError(f"上游编号必须在 1 到 {MAX_UPSTREAMS} 之间。")
        if slot in self._health_inflight:
            raise RuntimeError("该上游正在检测中。")
        self._health_inflight.add(slot)
        try:
            async with self._health_semaphore:
                result = await self._probe_upstream(slot)
                self._health_results[slot] = result
                return result
        finally:
            self._health_inflight.discard(slot)

    def _strategy_data(self) -> dict:
        """页面展示用的「当前生效策略」摘要，避免把同一组参数重复摆两遍。"""
        router = self.router
        if router is None:
            return {"mode": "disabled", "summary": "插件未启用"}
        return {
            "mode": "overall",
            "summary": (
                f"整体自动分配：按顺序依次尝试，首个响应即停止，"
                f"单次最多 {router.max_attempts} 个、总时限 {router.route_timeout_seconds}s；"
                f"单次接口超时 {router.request_timeout_seconds}s，"
                f"每个上游临时异常最多重试 {router.upstream_retry_count} 次，"
                f"间隔 {router.retry_interval_seconds:g}s，不按连续失败冻结接口"
            ),
            "overall_provider_id": router.meta().id,
            "pinned_provider_prefix": PINNED_PROVIDER_PREFIX,
            "max_attempts": router.max_attempts,
            "route_timeout_seconds": router.route_timeout_seconds,
            "failure_threshold": router.failure_threshold,
            "cooldown_seconds": router.cooldown_seconds,
            "circuit_breaker_enabled": False,
            "request_timeout_seconds": router.request_timeout_seconds,
            "upstream_retry_count": router.upstream_retry_count,
            "retry_interval_seconds": router.retry_interval_seconds,
            "upstream_max_attempts": router.upstream_retry_count + 1,
            "skip_unhealthy_upstreams": router.skip_unhealthy,
            "fallback_to_main_model": router.fallback_to_main,
            "upstream_count": len(router.upstreams),
        }

    async def api_upstreams(self):
        return json_response(
            {
                "provider_id": (
                    self.router.meta().id if self.router else "multi_api_router"
                ),
                "runtime_upstream_count": (
                    len(self.router.upstreams) if self.router else 0
                ),
                "strategy": self._strategy_data(),
                "retired_slots": self._retired_slots(),
                "upstreams": [
                    self._upstream_public_data(slot) for slot in self._render_slots()
                ],
            }
        )

    async def api_check_upstream(self):
        payload = await request.json(default={})
        try:
            slot = int(payload.get("slot", 0))
            result = await self._check_upstream_slot(slot)
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
        except RuntimeError as exc:
            return error_response(str(exc), status_code=409)
        return json_response(
            {
                "slot": slot,
                "result": result,
                "upstream": self._upstream_public_data(slot),
            }
        )

    @staticmethod
    def _normalize_model_api_base(value: Any) -> str:
        raw = str(value or "").strip()[:2048]
        try:
            parsed = urlsplit(raw)
        except ValueError as exc:
            raise ValueError("API Base 不是合法 URL") from exc
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("API Base 必须是完整的 http:// 或 https:// 地址")
        if parsed.username or parsed.password:
            raise ValueError("API Base 不允许包含用户名或密码")
        if parsed.query or parsed.fragment:
            raise ValueError("API Base 不允许包含查询参数或片段")
        path = parsed.path.rstrip("/")
        if path.endswith("/models"):
            path = path[: -len("/models")]
        return urlunsplit((parsed.scheme, parsed.netloc, path, "", "")).rstrip("/")

    async def _fetch_visible_models(
        self,
        api_base: str,
        api_key: str,
    ) -> tuple[list[str], bool]:
        timeout = MultiApiRouterProvider._bounded_int(
            self.config.get("health_check_timeout_seconds"), 15, 5, 60
        )
        client = AsyncOpenAI(
            api_key=api_key,
            base_url=api_base,
            timeout=timeout,
            max_retries=0,
        )
        try:
            response = await asyncio.wait_for(
                client.models.list(),
                timeout=timeout + 1,
            )
            data = getattr(response, "data", response)
            model_ids = sorted(
                {
                    str(getattr(item, "id", "") or "").strip()
                    for item in data
                    if str(getattr(item, "id", "") or "").strip()
                },
                key=str.casefold,
            )
            return model_ids[:MAX_MODEL_OPTIONS], len(model_ids) > MAX_MODEL_OPTIONS
        finally:
            await client.close()

    async def api_list_models(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象", status_code=400)
        unknown = set(payload) - {"api_base", "api_key", "slot"}
        if unknown:
            return error_response(
                f"包含不支持的字段: {sorted(unknown)}",
                status_code=400,
            )
        try:
            api_base = self._normalize_model_api_base(payload.get("api_base"))
            api_key = str(payload.get("api_key") or "").strip()[:2048]
            # 页面默认展示掩码；未编辑时用 slot 读取已保存密钥，避免要求用户重新输入。
            if (not api_key or self._is_masked_api_key(api_key)) and payload.get("slot") is not None:
                try:
                    slot = int(payload.get("slot"))
                except (TypeError, ValueError):
                    raise ValueError(
                        f"slot 必须是 1 到 {MAX_UPSTREAMS} 之间的整数"
                    )
                if slot < 1 or slot > MAX_UPSTREAMS:
                    raise ValueError(f"slot 必须在 1 到 {MAX_UPSTREAMS} 之间")
                saved = self.config.get(f"upstream_{slot}")
                saved_key = (
                    str(saved.get("api_key") or "").strip()
                    if isinstance(saved, dict) else ""
                )
                if saved_key:
                    api_key = saved_key
            if not api_key or self._is_masked_api_key(api_key):
                raise ValueError("请先填写 API Key")
            async with self._model_list_semaphore:
                models, truncated = await self._fetch_visible_models(
                    api_base,
                    api_key,
                )
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
        except Exception as exc:
            # 判定全部来自 classify()，这里只负责把分类翻成页面文案与 HTTP 码。
            category, disposition = classify(exc)
            status_code = _error_status(exc)
            message = MODEL_LIST_ERROR_MESSAGES.get(category, DEFAULT_MODEL_LIST_ERROR)
            logger.warning(
                "[MultiApiRouter] 获取模型列表失败：%s%s；分类 %s/%s。",
                type(exc).__name__,
                f" HTTP {status_code}" if status_code else "",
                category,
                disposition,
            )
            return error_response(
                message,
                status_code=504 if category == CATEGORY_TIMEOUT else 502,
            )
        return json_response(
            {
                "models": models,
                "count": len(models),
                "truncated": truncated,
            }
        )

    @staticmethod
    def _mask_api_key(api_key: str) -> str:
        """把 API Key 转成可安全展示的掩码（保留前缀与末 4 位）。

        Args:
            api_key: 原始密钥文本。

        Returns:
            形如 ``sk-a1b2****cdef`` 的掩码；空值返回空串，过短的密钥整体遮蔽。
        """
        key = str(api_key or "").strip()
        if not key:
            return ""
        # 长度不足时无法安全保留前后缀，整体遮蔽避免泄露过多字符。
        if len(key) <= 8:
            return API_KEY_MASK
        return f"{key[: min(7, len(key) - 5)]}{API_KEY_MASK}{key[-4:]}"

    @staticmethod
    def _is_masked_api_key(value: Any) -> bool:
        """判断表单提交的 API Key 是否仍是掩码形态（即用户未真正编辑）。"""
        return API_KEY_MASK in str(value or "")

    # 上游完整配置，供控制台编辑表单回填。
    # 默认只返回掩码（保留前缀与末 4 位）；兼容开关打开时才回退为明文。
    # 明文需走 /upstreams/config/reveal（带登录态校验与审计）。
    def _upstream_full_data(self, slot: int, *, expose_plain_key: bool = False) -> dict:
        raw = self.config.get(f"upstream_{slot}") or {}
        if not isinstance(raw, dict):
            raw = {}
        api_base = str(raw.get("api_base") or "").strip()
        api_key = str(raw.get("api_key") or "").strip()
        model = str(raw.get("model") or "").strip()
        api_key_masked = self._mask_api_key(api_key)
        return {
            "slot": slot,
            "route_enabled": MultiApiRouterProvider._chat_route_enabled(raw),
            "enabled": MultiApiRouterProvider._chat_route_enabled(raw),
            # 页面需要这两个字段来判断状态与展示健康结果；
            # 之前只返回配置值，导致加载后全部显示「配置不完整 / 未检测」。
            "configured": bool(api_base and api_key and model),
            "name": str(raw.get("name") or "").strip(),
            "api_base": api_base,
            # api_key 是表单回填值（默认掩码）；api_key_present 供页面判断是否显示「显示」按钮。
            "api_key": api_key if expose_plain_key else api_key_masked,
            "api_key_masked": api_key_masked,
            "api_key_present": bool(api_key),
            "model": model,
            "api_type": MultiApiRouterProvider._api_type(raw),
            "api_type_label": API_TYPE_LABELS[
                MultiApiRouterProvider._api_type(raw)
            ],
            "chat_compatible": (
                MultiApiRouterProvider._api_type(raw)
                in CHAT_COMPATIBLE_API_TYPES
            ),
            "priority": int(raw.get("priority", slot * 10)),
            "supports_tools": bool(raw.get("supports_tools", True)),
            "enable_thinking": MultiApiRouterProvider._enable_thinking_value(raw),
            "provider_id": self._pinned_provider_id(slot),
            "provider_label": str(raw.get("name") or model or f"上游 {slot}"),
            "health": self._health_results.get(slot),
        }

    async def api_upstreams_config(self):
        try:
            global_fields = {
                "enabled": bool(self.config.get("enabled", True)),
                "provider_id": str(self.config.get("provider_id") or "multi_api_router").strip(),
                "request_timeout_seconds": self.router._bounded_int(
                    self.config.get("request_timeout_seconds"), 45, 5, 600
                ) if self.router else int(self.config.get("request_timeout_seconds", 45)),
                "route_timeout_seconds": self.router._bounded_int(
                    self.config.get("route_timeout_seconds"), 90, 5, 600
                ) if self.router else int(self.config.get("route_timeout_seconds", 90)),
                "max_attempts": self.router._bounded_int(
                    self.config.get("max_attempts"), 3, 1, 5
                ) if self.router else int(self.config.get("max_attempts", 3)),
                "upstream_retry_count": MultiApiRouterProvider._bounded_int(
                    self.config.get("upstream_retry_count"), 1, 0, 5
                ),
                "retry_interval_seconds": MultiApiRouterProvider._bounded_float(
                    self.config.get("retry_interval_seconds"), 1.0, 0, 3600
                ),
                "failure_threshold": 0,
                "cooldown_seconds": 0,
                "max_concurrency": self.router._bounded_int(
                    self.config.get("max_concurrency"), 4, 1, 128
                ) if self.router else int(self.config.get("max_concurrency", 4)),
                "max_input_chars": self.router._bounded_int(
                    self.config.get("max_input_chars"),
                    DEFAULT_MAX_INPUT_CHARS,
                    1000,
                    2000000,
                ) if self.router else int(
                    self.config.get("max_input_chars", DEFAULT_MAX_INPUT_CHARS)
                ),
                "max_output_tokens": self.router._bounded_int(
                    self.config.get("max_output_tokens"), 2048, 1, 131072
                ) if self.router else int(self.config.get("max_output_tokens", 2048)),
                **{key: self._coerce_global_int(key, self.config.get(key, default))
                   for key, (default, _, _) in self.GLOBAL_INT_FIELDS.items()
                   if key in ("max_image_bytes", "max_total_image_bytes", "max_image_count")},
                "history_image_compaction_enabled": bool(
                    self.config.get("history_image_compaction_enabled", True)
                ),
                "health_check_timeout_seconds": self.router._bounded_int(
                    self.config.get("health_check_timeout_seconds"), 15, 5, 60
                ) if self.router else int(self.config.get("health_check_timeout_seconds", 15)),
                # 新增策略项：健康检查跳过与主模型回退。
                "skip_unhealthy_upstreams": bool(
                    self.config.get("skip_unhealthy_upstreams", True)
                ),
                "health_skip_ttl_seconds": self.router._bounded_int(
                    self.config.get("health_skip_ttl_seconds"), 21600, 60, 604800
                ) if self.router else int(self.config.get("health_skip_ttl_seconds", 21600)),
                "fallback_to_main_model": bool(
                    self.config.get("fallback_to_main_model", True)
                ),
                "fallback_provider_id": str(
                    self.config.get("fallback_provider_id") or ""
                ).strip(),
            }
        except Exception:
            global_fields = {key: self.config.get(key) for key in (
                "enabled", "provider_id", "request_timeout_seconds", "max_attempts",
                "route_timeout_seconds",
                "upstream_retry_count", "retry_interval_seconds",
                "failure_threshold", "cooldown_seconds", "max_concurrency",
                "max_input_chars", "max_output_tokens", "health_check_timeout_seconds",
                "max_image_bytes", "max_total_image_bytes", "max_image_count",
                "history_image_compaction_enabled",
                "skip_unhealthy_upstreams", "health_skip_ttl_seconds",
                "fallback_to_main_model", "fallback_provider_id",
            )}
            global_fields.update(failure_threshold=0, cooldown_seconds=0)
            global_fields.update(
                upstream_retry_count=MultiApiRouterProvider._bounded_int(
                    self.config.get("upstream_retry_count"), 1, 0, 5
                ),
                retry_interval_seconds=MultiApiRouterProvider._bounded_float(
                    self.config.get("retry_interval_seconds"), 1.0, 0, 3600
                ),
            )
        # 整体 ID 恒定，配置里即使残留旧值也不影响展示与运行时。
        global_fields["provider_id"] = PROVIDER_TYPE
        return json_response(
            {
                "provider_id": (
                    self.router.meta().id if self.router else "multi_api_router"
                ),
                "runtime_upstream_count": (
                    len(self.router.upstreams) if self.router else 0
                ),
                "strategy": self._strategy_data(),
                "global": global_fields,
                "retired_slots": self._retired_slots(),
                "upstreams": [
                    self._upstream_full_data(
                        slot,
                        expose_plain_key=bool(
                            self.config.get("expose_plain_api_keys", False)
                        ),
                    )
                    for slot in self._render_slots()
                ],
            }
        )

    def _reveal_audit_path(self) -> Path:
        """审计日志与 auto_bindings.json 同目录，便于随插件数据一起备份。"""
        return self._state_path.parent / API_KEY_REVEAL_AUDIT

    def _write_reveal_audit(self, record: dict) -> bool:
        """追加一条明文 Key 揭示审计记录；写入失败返回 False（调用方应拒绝返回密钥）。"""
        try:
            path = self._reveal_audit_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            logger.exception("[MultiApiRouter] 写入 API Key 揭示审计日志失败")
            return False
        return True

    async def api_reveal_upstream_key(self):
        """按需揭示单个上游的明文 API Key。

        安全约束：
        - 必须带控制台登录态（request.username 由 Dashboard JWT 注入）；
        - 每次调用都先写审计日志（谁、何时、哪个上游、来源 IP/UA），日志不记 Key；
        - 审计写入失败时拒绝返回密钥，避免出现无记录的明文外泄。
        """
        username = str(getattr(request, "username", "") or "").strip()
        if not username:
            return error_response(
                "需要登录 AstrBot 控制台后才能查看 API Key",
                status_code=401,
            )
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象", status_code=400)
        unknown = set(payload) - {"slot"}
        if unknown:
            return error_response(
                f"包含不支持的字段: {sorted(unknown)}",
                status_code=400,
            )
        try:
            slot = int(payload.get("slot"))
        except (TypeError, ValueError):
            return error_response(
                f"slot 必须是 1 到 {MAX_UPSTREAMS} 之间的整数",
                status_code=400,
            )
        if slot < 1 or slot > MAX_UPSTREAMS:
            return error_response(
                f"slot 必须在 1 到 {MAX_UPSTREAMS} 之间",
                status_code=400,
            )
        raw = self.config.get(f"upstream_{slot}")
        raw = raw if isinstance(raw, dict) else {}
        api_key = str(raw.get("api_key") or "").strip()
        if not api_key:
            return error_response("该上游尚未配置 API Key", status_code=404)

        headers = getattr(request, "headers", {}) or {}
        record = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "timestamp": int(time.time()),
            "username": username,
            "slot": slot,
            "upstream_name": str(raw.get("name") or "").strip()[:80],
            "client_host": str(getattr(request, "client_host", "") or ""),
            "user_agent": str(headers.get("user-agent", "") or "")[:512],
        }
        if not self._write_reveal_audit(record):
            return error_response(
                "审计日志写入失败，已拒绝返回 API Key",
                status_code=500,
            )
        logger.info(
            "[MultiApiRouter] 用户 %s 查看了上游 %s 的明文 API Key（来源 %s）。",
            username,
            slot,
            record["client_host"] or "unknown",
        )
        return json_response(
            {
                "slot": slot,
                "api_key": api_key,
                "revealed_by": username,
            }
        )

    @staticmethod
    def _bounded_priority(value, default: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return max(-10000, min(10000, parsed))

    # 全局数字字段范围：key -> (default, min, max)
    GLOBAL_INT_FIELDS = {
        "request_timeout_seconds": (45, 5, 600),
        "route_timeout_seconds": (90, 5, 600),
        "max_attempts": (3, 1, 5),
        "upstream_retry_count": (1, 0, 5),
        # 旧客户端仍可提交这些字段，但保存时归零，不再激活固定冻结。
        "failure_threshold": (0, 0, 0),
        "cooldown_seconds": (0, 0, 0),
        "max_concurrency": (4, 1, 128),
        "max_input_chars": (DEFAULT_MAX_INPUT_CHARS, 1000, 2000000),
        "max_image_bytes": (8 * 1024 * 1024, 1024, 64 * 1024 * 1024),
        "max_total_image_bytes": (32 * 1024 * 1024, 1024, 128 * 1024 * 1024),
        "max_image_count": (32, 1, 128),
        "max_output_tokens": (2048, 1, 131072),
        "health_check_timeout_seconds": (15, 5, 60),
        "health_skip_ttl_seconds": (21600, 60, 604800),
    }

    GLOBAL_FLOAT_FIELDS = {"retry_interval_seconds": (1.0, 0, 3600)}

    def _validate_timing_value(self, key: str, value) -> int | float:
        """页面数字必须合法；先校验再写入，避免非法重试配置污染内存。"""
        bounds = self.GLOBAL_FLOAT_FIELDS if key in self.GLOBAL_FLOAT_FIELDS else self.GLOBAL_INT_FIELDS
        _, low, high = bounds[key]
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError):
            raise ValueError(f"{key} 必须是 {low} 到 {high} 之间的数字")
        if isinstance(value, bool) or not math.isfinite(parsed) or not low <= parsed <= high:
            raise ValueError(f"{key} 必须是 {low} 到 {high} 之间的数字")
        if key in self.GLOBAL_INT_FIELDS:
            if not parsed.is_integer():
                raise ValueError(f"{key} 必须是整数")
            return int(parsed)
        return parsed

    def _coerce_global_int(self, key: str, value) -> int:
        default, low, high = self.GLOBAL_INT_FIELDS[key]
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = int(self.config.get(key, default) or default)
        return max(low, min(high, parsed))

    def _coerce_upstream_payload(self, slot: int, raw: Any) -> dict:
        if raw is None:
            return {"enabled": False}
        if not isinstance(raw, dict):
            raise ValueError(f"上游 {slot} 必须是对象")
        allowed = {
            "route_enabled", "enabled", "api_type",
            "name", "api_base", "api_key", "model",
            "priority", "supports_tools", "enable_thinking", "extra_body",
        }
        unknown = set(raw) - allowed
        if unknown:
            raise ValueError(f"上游 {slot} 包含不支持的字段: {sorted(unknown)}")
        name = str(raw.get("name") or "").strip()[:80]
        api_base = str(raw.get("api_base") or "").strip()[:2048]
        # 页面默认回显掩码。用户未编辑就直接保存时沿用已存密钥，
        # 否则会把真 Key 覆盖成 "****"；掩码形态或与当前掩码一致都算未编辑。
        existing_raw = self.config.get(f"upstream_{slot}")
        existing_raw = existing_raw if isinstance(existing_raw, dict) else {}
        existing_key = str(existing_raw.get("api_key") or "").strip()
        submitted_key = raw.get("api_key")
        if submitted_key is None:
            api_key = existing_key
        else:
            candidate = str(submitted_key or "").strip()[:2048]
            if self._is_masked_api_key(candidate) or (
                existing_key and candidate == self._mask_api_key(existing_key)
            ):
                api_key = existing_key
            else:
                api_key = candidate
        model = str(raw.get("model") or "").strip()[:256]
        api_type = str(raw.get("api_type") or "chat").strip().lower()
        if api_type not in API_TYPE_LABELS:
            raise ValueError(
                f"上游 {slot} 的 API 类型无效，可选值: {sorted(API_TYPE_LABELS)}"
            )
        # 新字段优先，旧 enabled 继续兼容；无显式开关时完整配置默认参与路由。
        route_enabled = (
            bool(raw.get("route_enabled"))
            if "route_enabled" in raw
            else bool(raw.get("enabled"))
            if "enabled" in raw
            else bool(api_base and api_key and model)
        )
        # 当前宿主没有生图 Provider，且非聊天协议不能伪装成 Chat Provider。
        # 分类仍会保存并按真实协议检测，但自动路由只开放聊天兼容类型。
        if api_type not in CHAT_COMPATIBLE_API_TYPES:
            route_enabled = False
        priority = self._bounded_priority(raw.get("priority"), slot * 10)
        # 默认开启工具调用；只有用户明确取消时才保存为 False。
        supports_tools = bool(raw.get("supports_tools", True))
        if "enable_thinking" in raw:
            enable_thinking = bool(raw.get("enable_thinking", False))
        else:
            existing = self.config.get(f"upstream_{slot}")
            enable_thinking = MultiApiRouterProvider._enable_thinking_value(
                existing if isinstance(existing, dict) else {}
            )
        if route_enabled and (not api_base or not api_key or not model):
            raise ValueError(
                f"上游 {slot} 已启用路由但缺少 API Base / API Key / 模型 ID"
            )
        # 保留 extra_body：前端表单不展示该字段，这里若前端未提交则沿用已有配置，
        # 避免热重载/保存时把上游自定义请求体（如 {"enable_thinking": false}）静默清空。
        extra_body = raw.get("extra_body")
        if extra_body is None:
            existing = self.config.get(f"upstream_{slot}")
            if isinstance(existing, dict):
                extra_body = existing.get("extra_body")
        result = {
            # 配置落盘继续使用 enabled，避免旧配置迁移和旧版本回滚歧义；
            # Web API 与运行态统一暴露 route_enabled 语义。
            "enabled": route_enabled,
            "api_type": api_type,
            "name": name,
            "api_base": api_base,
            "api_key": api_key,
            "model": model,
            "priority": priority,
            "supports_tools": supports_tools,
            "enable_thinking": enable_thinking,
        }
        if extra_body is not None:
            result["extra_body"] = extra_body
        return result

    async def _reload_router(self) -> None:
        """配置变更后热重载：整体与指定上游 Provider 一起重建，避免重启 AstrBot。"""
        await self._install_router_providers()
        logger.info(
            "[MultiApiRouter] 配置已保存并热重载，当前 %d 个上游，%d 个 Provider。",
            len(self.router.upstreams) if self.router else 0,
            len(self._router_instances),
        )

    async def api_save_upstreams(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象", status_code=400)
        unknown = set(payload) - (
            {"global", "delete_slots"}
            | {f"upstream_{slot}" for slot in range(1, MAX_UPSTREAMS + 1)}
        )
        if unknown:
            return error_response(f"包含不支持的配置项: {sorted(unknown)}", status_code=400)

        try:
            global_update = payload.get("global", {})
            if not isinstance(global_update, dict):
                raise ValueError("global 必须是对象")
            # 四个页面策略项先完整校验，再执行任何配置变更。
            global_update = dict(global_update)
            for key in (
                "request_timeout_seconds", "max_attempts",
                "upstream_retry_count", "retry_interval_seconds",
            ):
                if key in global_update:
                    global_update[key] = self._validate_timing_value(key, global_update[key])
            # 0. 处理待删除的槽位
            if "delete_slots" in payload:
                delete_slots = payload["delete_slots"]
                if not isinstance(delete_slots, list):
                    raise ValueError("delete_slots 必须是数组")
                for raw_slot in delete_slots:
                    try:
                        slot = int(raw_slot)
                    except (TypeError, ValueError):
                        raise ValueError(f"delete_slots 含非法编号: {raw_slot}")
                    if slot < 1 or slot > MAX_UPSTREAMS:
                        raise ValueError(f"上游编号必须在 1 到 {MAX_UPSTREAMS} 之间。")
                    key = f"upstream_{slot}"
                    if key in self.config:
                        del self.config[key]
                        # 删除即退役：编号不再复用，防止其他插件保存的旧
                        # multi_api_router:upstream_N 静默指向一个新上游。
                        retired = self._retired_slots()
                        if slot not in retired:
                            retired.append(slot)
                        self.config["retired_slots"] = sorted(retired)
                    self._health_results.pop(slot, None)

            # 1. 校验并写入内存配置
            if "global" in payload:
                allowed_global = {
                    "enabled", "provider_id", "request_timeout_seconds",
                    "route_timeout_seconds",
                    "max_attempts", "failure_threshold", "cooldown_seconds",
                    "upstream_retry_count", "retry_interval_seconds",
                    "max_concurrency", "max_input_chars", "max_output_tokens",
                    "max_image_bytes", "max_total_image_bytes", "max_image_count",
                    "history_image_compaction_enabled",
                    "health_check_timeout_seconds", "skip_unhealthy_upstreams",
                    "health_skip_ttl_seconds", "fallback_to_main_model",
                    "fallback_provider_id",
                }
                unknown_global = set(global_update) - allowed_global
                if unknown_global:
                    raise ValueError(f"global 包含不支持的字段: {sorted(unknown_global)}")
                for key, value in global_update.items():
                    if key == "provider_id":
                        # 整体 ID 已固定：为兼容旧页面缓存保留该字段，但忽略提交值。
                        submitted = str(value or "").strip()
                        if submitted and submitted != PROVIDER_TYPE:
                            logger.warning(
                                "[MultiApiRouter] provider_id 已固定为 %s，忽略提交值 %s",
                                PROVIDER_TYPE,
                                submitted,
                            )
                        self.config["provider_id"] = PROVIDER_TYPE
                    elif key == "enabled":
                        self.config["enabled"] = bool(value)
                    elif key == "history_image_compaction_enabled":
                        self.config[key] = bool(value)
                    elif key in self.GLOBAL_INT_FIELDS:
                        self.config[key] = self._coerce_global_int(key, value)
                    elif key in self.GLOBAL_FLOAT_FIELDS:
                        self.config[key] = value
                    # 新增策略项：布尔开关与回退 Provider ID（空字符串表示用默认主模型）。
                    elif key == "skip_unhealthy_upstreams":
                        self.config[key] = bool(value)
                    elif key == "fallback_to_main_model":
                        self.config[key] = bool(value)
                    elif key == "fallback_provider_id":
                        self.config[key] = str(value or "").strip()[:128]
                    else:
                        raise ValueError(f"不支持的全局字段: {key}")
            for slot in range(1, MAX_UPSTREAMS + 1):
                key = f"upstream_{slot}"
                if key in payload:
                    self.config[key] = self._coerce_upstream_payload(slot, payload[key])

            # 2. 落盘（AstrBotConfig.save_config 原子写 + fsync）
            self.config.save_config()

            # 3. 热重载 router 使新配置立即生效
            await self._reload_router()
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
        except Exception as exc:
            logger.exception("[MultiApiRouter] 保存上游配置失败")
            return error_response(f"保存失败: {exc}", status_code=500)

        return json_response(
            {
                "ok": True,
                "provider_id": self.router.meta().id if self.router else "multi_api_router",
                "runtime_upstream_count": len(self.router.upstreams) if self.router else 0,
            }
        )

    @filter.command("模型路由状态")
    async def router_status(self, event: AstrMessageEvent):
        if self.router is None:
            yield event.plain_result("多 API 路由 Provider 当前未启用。")
            return
        lines = self.router.status_lines()
        lines.append("自动绑定：")
        lines.extend(self._binding_messages or ["尚无绑定记录。"])
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("模型路由检测")
    async def check_upstreams_command(self, event: AstrMessageEvent):
        configured_slots = [
            slot
            for slot in self._render_slots()
            if self._upstream_public_data(slot)["configured"]
        ]
        if not configured_slots:
            yield event.plain_result("没有配置完整的模型 API 上游。")
            return
        lines = ["模型 API 上游检测："]
        for slot in configured_slots:
            result = await self._check_upstream_slot(slot)
            upstream = self._upstream_public_data(slot)
            lines.append(
                f"- {upstream['name']} / {upstream['model']}："
                f"{result['status']}，{result['latency_ms']}ms"
                + (
                    f"，HTTP {result['http_status']}"
                    if result.get("http_status")
                    else ""
                )
            )
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("模型路由绑定预览")
    async def binding_preview(self, event: AstrMessageEvent):
        if self.router is None:
            yield event.plain_result("多 API 路由 Provider 当前未启用。")
            return
        if not self.router.upstreams and self.config.get(
            "auto_bind_require_ready_upstream", True
        ):
            yield event.plain_result(
                "尚未配置完整上游；自动绑定会等待，不会影响现有任务。"
            )
            return
        lines = ["自动绑定预览（不会修改配置）："]
        for plugin_name in AUTO_BIND_TARGETS:
            metadata = self._find_plugin_metadata(plugin_name)
            if metadata is not None:
                lines.extend(
                    self._preview_metadata(metadata, self.router.meta().id)
                )
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("模型路由恢复绑定")
    async def restore_bindings(self, event: AstrMessageEvent):
        async with self._binding_lock:
            messages = await self._restore_auto_bindings()
            self._binding_messages = messages
        yield event.plain_result(
            "\n".join(messages or ["没有需要恢复的自动绑定。"])
        )

    async def terminate(self):
        if self._auto_check_stop is not None:
            self._auto_check_stop.set()
        if self._auto_check_task is not None and not self._auto_check_task.done():
            self._auto_check_task.cancel()
        self._auto_check_task = None
        self._auto_check_stop = None
        if self.router is None:
            return
        provider_manager = self.context.provider_manager
        provider_insts = provider_manager.provider_insts
        # 移除整体与所有指定上游 Provider，避免热重载后残留。
        for inst in self._router_instances:
            if inst in provider_insts:
                provider_insts.remove(inst)
            inst_id = inst.meta().id
            if provider_manager.inst_map.get(inst_id) is inst:
                del provider_manager.inst_map[inst_id]
            await self._close_router_instance(inst)
        self._router_instances = []
        # 按 id 清理注入的 Provider 条目，避免重载后控制台残留幽灵项。
        public_ids = {str(entry.get("id") or "") for entry in self._public_provider_configs}
        for item in list(provider_manager.providers_config):
            if not isinstance(item, dict):
                continue
            if (
                item.get("type") == PROVIDER_TYPE
                and str(item.get("id") or "") in public_ids
            ):
                provider_manager.providers_config.remove(item)
        self._public_provider_configs = []
        self.router = None

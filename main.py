from __future__ import annotations

import asyncio
import inspect
import json
import os
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

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
from openai import AsyncOpenAI


PLUGIN_NAME = "astrbot_plugin_multi_api_router"
PROVIDER_TYPE = "multi_api_router"
# 「指定某个上游」模式在 AstrBot Provider 列表中的 ID 前缀，
# 例如 multi_api_router:upstream_2。调用方按普通 Provider ID 选择即可。
PINNED_PROVIDER_PREFIX = f"{PROVIDER_TYPE}:upstream_"
MAX_UPSTREAMS = 20
MIN_UPSTREAMS = 5
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


@dataclass
class UpstreamState:
    name: str
    priority: int
    supports_tools: bool
    client: ProviderOpenAIOfficial
    slot: int = 0
    consecutive_failures: int = 0
    circuit_open_until: float = 0.0
    calls: int = 0
    successes: int = 0
    failures: int = 0
    last_error: str = ""
    last_latency_ms: int = 0

    def available(self, now: float) -> bool:
        return self.circuit_open_until <= now


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
        provider_id = str(plugin_config.get("provider_id") or PROVIDER_TYPE).strip()
        # 「指定上游」模式：AstrBot 可以为每个上游建一个 Provider 条目，
        # 条目里带 upstream_slot；调用方选中它时只走这个上游，不做自动分配。
        self.pinned_slot = self._detect_pinned_slot(original_config)
        pinned_model = "automatic"
        if self.pinned_slot:
            raw = plugin_config.get(f"upstream_{self.pinned_slot}") or {}
            if isinstance(raw, dict) and str(raw.get("model") or "").strip():
                pinned_model = str(raw.get("model")).strip()
        super().__init__(
            {
                "id": provider_id,
                "type": PROVIDER_TYPE,
                "model": pinned_model,
                "key": [""],
                "upstream_slot": self.pinned_slot,
            },
            provider_settings or {},
        )
        self.set_model(pinned_model)
        self.failure_threshold = self._bounded_int(
            plugin_config.get("failure_threshold"), 2, 1, 100
        )
        self.cooldown_seconds = self._bounded_int(
            plugin_config.get("cooldown_seconds"), 300, 1, 86400
        )
        self.max_attempts = self._bounded_int(
            plugin_config.get("max_attempts"), 3, 1, 5
        )
        self.max_input_chars = self._bounded_int(
            plugin_config.get("max_input_chars"), 48000, 1000, 2_000_000
        )
        self.max_output_tokens = self._bounded_int(
            plugin_config.get("max_output_tokens"), 2048, 1, 131072
        )
        # 上游健康检查结果由插件侧注入（slot -> 检测结果），路由时跳过不通的上游。
        self.health_lookup: Any = None
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
        self._all_upstreams = self._build_upstreams(plugin_config)
        # 指定上游模式只保留目标上游，绝不自动分配到其它上游。
        if self.pinned_slot:
            self.upstreams = [
                item for item in self._all_upstreams if item.slot == self.pinned_slot
            ]
        else:
            self.upstreams = self._all_upstreams

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

    def _build_upstreams(self, plugin_config: dict) -> list[UpstreamState]:
        timeout = self._bounded_int(
            plugin_config.get("request_timeout_seconds"), 45, 5, 600
        )
        upstreams: list[UpstreamState] = []
        for index in range(1, MAX_UPSTREAMS + 1):
            raw = plugin_config.get(f"upstream_{index}") or {}
            if not isinstance(raw, dict) or not raw.get("enabled", False):
                continue
            api_base = str(raw.get("api_base") or "").strip().rstrip("/")
            api_key = str(raw.get("api_key") or "").strip()
            model = str(raw.get("model") or "").strip()
            if not api_base or not api_key or not model:
                logger.warning(
                    "[MultiApiRouter] 上游 %s 已启用但配置不完整，已跳过。",
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
        if value is None:
            return 0
        if isinstance(value, str):
            return len(value)
        if isinstance(value, dict):
            return sum(
                MultiApiRouterProvider._text_size(k)
                + MultiApiRouterProvider._text_size(v)
                for k, v in value.items()
            )
        if isinstance(value, (list, tuple)):
            return sum(MultiApiRouterProvider._text_size(v) for v in value)
        return len(str(value))

    def _request_size(
        self,
        prompt: str | None,
        contexts: list[Message] | list[dict] | None,
        system_prompt: str | None,
    ) -> int:
        return (
            self._text_size(prompt)
            + self._text_size(contexts)
            + self._text_size(system_prompt)
        )

    async def _record_success(self, upstream: UpstreamState, latency_ms: int) -> None:
        async with self._state_lock:
            upstream.calls += 1
            upstream.successes += 1
            upstream.consecutive_failures = 0
            upstream.circuit_open_until = 0.0
            upstream.last_error = ""
            upstream.last_latency_ms = latency_ms

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        status = getattr(exc, "status_code", None)
        if status is None:
            response = getattr(exc, "response", None)
            status = getattr(response, "status_code", None)
        label = type(exc).__name__
        return f"{label} (HTTP {status})" if status else label

    async def _record_failure(
        self, upstream: UpstreamState, exc: Exception, latency_ms: int
    ) -> None:
        safe_error = self._safe_error(exc)
        async with self._state_lock:
            upstream.calls += 1
            upstream.failures += 1
            upstream.consecutive_failures += 1
            upstream.last_error = safe_error
            upstream.last_latency_ms = latency_ms
            if upstream.consecutive_failures >= self.failure_threshold:
                upstream.circuit_open_until = time.monotonic() + self.cooldown_seconds
        logger.warning(
            "[MultiApiRouter] 上游 %s 调用失败：%s；将尝试下一个上游。",
            upstream.name,
            safe_error,
        )

    def _health_skip_reason(self, upstream: UpstreamState) -> str:
        """健康检查发现上游不通时跳过本次调用，但不移除上游。

        只有最近一次检测明确失败、且在有效期内才跳过；调用失败只会累计
        连续失败并暂停（熔断），等冷却结束或下次检测恢复。
        """
        if not self.skip_unhealthy or not callable(self.health_lookup):
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
        return str(result.get("status") or "健康检查未通过")

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
        return await upstream.client._query(
            payloads,
            func_tool,
            request_max_retries=1,
            conversation_id=kwargs.get("conversation_id"),
        )

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
        size = self._request_size(prompt, contexts, system_prompt)
        if size > self.max_input_chars:
            raise ValueError(
                f"输入字符数 {size} 超过路由预算 {self.max_input_chars}，未调用上游。"
            )
        if not self.upstreams:
            if self.pinned_slot:
                raise RuntimeError(
                    f"指定上游 #{self.pinned_slot} 未启用或配置不完整，"
                    "请在多 API 路由插件中检查。"
                )
            raise RuntimeError("多 API 路由器没有可用上游，请先在插件配置中填写 API。")

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
        attempts = 0
        # 带工具请求：只考虑声明支持工具的上游；一个都没有时明确报错，
        # 绝不静默丢掉工具、假装执行成功。
        needs_tools = bool(func_tool and not func_tool.empty())
        if needs_tools:
            candidates = [u for u in candidates if u.supports_tools]
            if not candidates:
                raise RuntimeError(
                    "本次请求需要调用工具，但当前路由没有任何声明支持工具调用的上游；"
                    "已按原样失败，未执行任何工具调用。请在上游列表中勾选"
                    "「支持工具调用」，或改用支持工具的主模型。"
                )
        async with self._semaphore:
            for upstream in candidates:
                if attempts >= self.max_attempts:
                    break
                if not upstream.available(time.monotonic()):
                    continue
                # 健康检查明确不通的上游直接跳过并写日志；不删除、不计真实失败。
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
                    response = await self._call_once(
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
                    )
                    latency_ms = int((time.monotonic() - started) * 1000)
                    await self._record_success(upstream, latency_ms)
                    return response
                except Exception as exc:
                    latency_ms = int((time.monotonic() - started) * 1000)
                    await self._record_failure(upstream, exc, latency_ms)
                    errors.append(f"{upstream.name}: {self._safe_error(exc)}")

        # 只有真实调用失败（attempts > 0）才回退主模型；健康检查跳过不算。
        if attempts > 0:
            try:
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
            except Exception as exc:
                logger.warning(
                    "[MultiApiRouter] 主模型回退也失败：%s。",
                    self._safe_error(exc),
                )
                fallback_response = None
            if fallback_response is not None:
                return fallback_response

        if attempts == 0:
            detail = "；".join(health_skipped) or "无可用上游"
            raise RuntimeError(f"没有可调用的上游：{detail}。")
        raise RuntimeError(
            f"多 API 路由失败，已尝试 {attempts} 个上游：{'; '.join(errors)}"
        )

    async def text_chat_stream(self, **kwargs) -> AsyncGenerator[LLMResponse, None]:
        response = await self.text_chat(**kwargs)
        yield response

    def status_lines(self) -> list[str]:
        now = time.monotonic()
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
                f"输出上限 {self.max_output_tokens} tokens"
            ),
        ]
        if not self.upstreams:
            lines.append("尚未配置可用上游。")
            return lines
        for upstream in self.upstreams:
            remaining = max(0, int(upstream.circuit_open_until - now))
            state = f"熔断 {remaining}s" if remaining else "可用"
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
    "多个 OpenAI 兼容 API 的优先级路由、熔断与自动降级 Provider",
    "1.3.0",
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
        self._start_auto_health_check()

    @staticmethod
    def _pinned_provider_id(slot: int) -> str:
        return f"{PINNED_PROVIDER_PREFIX}{slot}"

    def _provider_entry(
        self,
        provider_id: str,
        model: str,
        slot: int = 0,
    ) -> dict:
        entry = {
            "id": provider_id,
            "type": PROVIDER_TYPE,
            "provider_type": "chat_completion",
            "model": model,
            "enable": True,
        }
        if slot:
            # AstrBot Provider 条目本身不认识这个字段，插件实例化时用它锁定上游。
            entry["upstream_slot"] = slot
        return entry

    async def _close_router_instance(self, inst: "MultiApiRouterProvider") -> None:
        for upstream in getattr(inst, "upstreams", []):
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
        } | {str(self.config.get("provider_id") or PROVIDER_TYPE)}

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

        router = MultiApiRouterProvider(self.config)
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
        router.fallback_resolver = self._resolve_fallback_provider
        instances = [router]
        entries = [self._provider_entry(provider_id, "automatic")]

        # 为每个已配置上游注册一个可直接在 AstrBot 中选择的 Provider。
        for upstream in router.upstreams:
            pinned_id = self._pinned_provider_id(upstream.slot)
            pinned_config = dict(self.config)
            pinned_config["provider_id"] = pinned_id
            pinned_config["upstream_slot"] = upstream.slot
            pinned = MultiApiRouterProvider(pinned_config)
            pinned.health_lookup = self._health_results.get
            # 指定上游模式不自动降级，也不回退主模型。
            pinned.fallback_resolver = None
            instances.append(pinned)
            entries.append(
                self._provider_entry(
                    pinned_id,
                    pinned.meta().model or upstream.client.get_model(),
                    upstream.slot,
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
            f"/{PLUGIN_NAME}/upstreams/config",
            self.api_upstreams_config,
            ["GET"],
            "读取模型 API 上游完整配置（含密钥，仅控制台调用）",
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/upstreams/save",
            self.api_save_upstreams,
            ["POST"],
            "保存模型 API 上游配置",
        )

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
            "enabled": bool(raw.get("enabled", False)),
            "configured": configured,
            "name": str(raw.get("name") or f"Upstream {slot}"),
            "api_base": api_base,
            "host": host,
            "model": str(raw.get("model") or ""),
            "priority": self.router._bounded_int(
                raw.get("priority"), slot * 10, -10000, 10000
            ) if self.router else slot * 10,
            "supports_tools": bool(raw.get("supports_tools", True)),
            "checking": slot in self._health_inflight,
            "provider_id": self._pinned_provider_id(slot),
            "health": self._health_results.get(slot),
        }

    async def _probe_upstream(self, slot: int) -> dict:
        raw = self.config.get(f"upstream_{slot}") or {}
        if not isinstance(raw, dict):
            raw = {}
        name = str(raw.get("name") or f"Upstream {slot}")
        api_base = str(raw.get("api_base") or "").strip().rstrip("/")
        api_key = str(raw.get("api_key") or "").strip()
        model = str(raw.get("model") or "").strip()
        if not api_base or not api_key or not model:
            return {
                "ok": False,
                "checked_at": int(time.time()),
                "latency_ms": 0,
                "status": "配置不完整",
                "http_status": None,
                "error_type": "ConfigurationError",
            }

        timeout = MultiApiRouterProvider._bounded_int(
            self.config.get("health_check_timeout_seconds"), 15, 5, 60
        )
        started = time.monotonic()
        client = AsyncOpenAI(
            api_key=api_key,
            base_url=api_base,
            timeout=timeout,
            max_retries=0,
        )
        try:
            response = await asyncio.wait_for(
                client.chat.completions.create(
                    model=model,
                    messages=[
                        {
                            "role": "user",
                            "content": "Reply with exactly OK.",
                        }
                    ],
                    max_tokens=8,
                    temperature=0,
                ),
                timeout=timeout + 1,
            )
            text = ""
            if response.choices:
                text = str(response.choices[0].message.content or "").strip()
            latency_ms = int((time.monotonic() - started) * 1000)
            if not text:
                return {
                    "ok": False,
                    "checked_at": int(time.time()),
                    "latency_ms": latency_ms,
                    "status": "响应为空",
                    "http_status": 200,
                    "error_type": "EmptyResponse",
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
            }
        except Exception as exc:
            latency_ms = int((time.monotonic() - started) * 1000)
            status_code = getattr(exc, "status_code", None)
            if status_code is None:
                response = getattr(exc, "response", None)
                status_code = getattr(response, "status_code", None)
            error_type = type(exc).__name__
            if status_code in {401, 403}:
                status = "认证失败"
            elif status_code == 404:
                status = "接口或模型不存在"
            elif status_code == 429:
                status = "限流或额度不足"
            elif status_code and int(status_code) >= 500:
                status = "服务端异常"
            elif isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
                status = "请求超时"
            else:
                status = "连接或请求失败"
            logger.warning(
                "[MultiApiRouter] 上游检测失败：%s，%s%s。",
                name,
                error_type,
                f" HTTP {status_code}" if status_code else "",
            )
            return {
                "ok": False,
                "checked_at": int(time.time()),
                "latency_ms": latency_ms,
                "status": status,
                "http_status": status_code,
                "error_type": error_type,
            }
        finally:
            await client.close()

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
                f"整体自动分配：按优先级依次尝试，单次最多 {router.max_attempts} 个；"
                f"连续失败 {router.failure_threshold} 次暂停 {router.cooldown_seconds}s"
            ),
            "overall_provider_id": router.meta().id,
            "pinned_provider_prefix": PINNED_PROVIDER_PREFIX,
            "max_attempts": router.max_attempts,
            "failure_threshold": router.failure_threshold,
            "cooldown_seconds": router.cooldown_seconds,
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

    # 上游完整配置（含 api_key），供控制台编辑表单回填。
    # 该接口会暴露密钥，AstrBot 控制台页面本身需要登录，插件无法自行鉴权；
    # 这里约定只在 views 页面（管理员）调用，不暴露给聊天命令。
    def _upstream_full_data(self, slot: int) -> dict:
        raw = self.config.get(f"upstream_{slot}") or {}
        if not isinstance(raw, dict):
            raw = {}
        return {
            "slot": slot,
            "enabled": bool(raw.get("enabled", False)),
            "name": str(raw.get("name") or "").strip(),
            "api_base": str(raw.get("api_base") or "").strip(),
            "api_key": str(raw.get("api_key") or "").strip(),
            "model": str(raw.get("model") or "").strip(),
            "priority": int(raw.get("priority", slot * 10)),
            "supports_tools": bool(raw.get("supports_tools", True)),
            "provider_id": self._pinned_provider_id(slot),
        }

    async def api_upstreams_config(self):
        try:
            global_fields = {
                "enabled": bool(self.config.get("enabled", True)),
                "provider_id": str(self.config.get("provider_id") or "multi_api_router").strip(),
                "request_timeout_seconds": self.router._bounded_int(
                    self.config.get("request_timeout_seconds"), 45, 5, 600
                ) if self.router else int(self.config.get("request_timeout_seconds", 45)),
                "max_attempts": self.router._bounded_int(
                    self.config.get("max_attempts"), 3, 1, 5
                ) if self.router else int(self.config.get("max_attempts", 3)),
                "failure_threshold": self.router._bounded_int(
                    self.config.get("failure_threshold"), 2, 1, 100
                ) if self.router else int(self.config.get("failure_threshold", 2)),
                "cooldown_seconds": self.router._bounded_int(
                    self.config.get("cooldown_seconds"), 300, 1, 86400
                ) if self.router else int(self.config.get("cooldown_seconds", 300)),
                "max_concurrency": self.router._bounded_int(
                    self.config.get("max_concurrency"), 4, 1, 128
                ) if self.router else int(self.config.get("max_concurrency", 4)),
                "max_input_chars": self.router._bounded_int(
                    self.config.get("max_input_chars"), 48000, 1000, 2000000
                ) if self.router else int(self.config.get("max_input_chars", 48000)),
                "max_output_tokens": self.router._bounded_int(
                    self.config.get("max_output_tokens"), 2048, 1, 131072
                ) if self.router else int(self.config.get("max_output_tokens", 2048)),
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
                "failure_threshold", "cooldown_seconds", "max_concurrency",
                "max_input_chars", "max_output_tokens", "health_check_timeout_seconds",
                "skip_unhealthy_upstreams", "health_skip_ttl_seconds",
                "fallback_to_main_model", "fallback_provider_id",
            )}
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
                "upstreams": [
                    self._upstream_full_data(slot) for slot in self._render_slots()
                ],
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
        "max_attempts": (3, 1, 5),
        "failure_threshold": (2, 1, 100),
        "cooldown_seconds": (300, 1, 86400),
        "max_concurrency": (4, 1, 128),
        "max_input_chars": (48000, 1000, 2000000),
        "max_output_tokens": (2048, 1, 131072),
        "health_check_timeout_seconds": (15, 5, 60),
        "health_skip_ttl_seconds": (21600, 60, 604800),
    }

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
            "enabled", "name", "api_base", "api_key", "model",
            "priority", "supports_tools", "extra_body",
        }
        unknown = set(raw) - allowed
        if unknown:
            raise ValueError(f"上游 {slot} 包含不支持的字段: {sorted(unknown)}")
        enabled = bool(raw.get("enabled", False))
        name = str(raw.get("name") or "").strip()[:80]
        api_base = str(raw.get("api_base") or "").strip()[:2048]
        api_key = str(raw.get("api_key") or "").strip()[:2048]
        model = str(raw.get("model") or "").strip()[:256]
        priority = self._bounded_priority(raw.get("priority"), slot * 10)
        # 默认开启工具调用；只有用户明确取消时才保存为 False。
        supports_tools = bool(raw.get("supports_tools", True))
        if enabled and (not api_base or not api_key or not model):
            raise ValueError(f"上游 {slot} 已启用但缺少 API Base / API Key / 模型 ID")
        # 保留 extra_body：前端表单不展示该字段，这里若前端未提交则沿用已有配置，
        # 避免热重载/保存时把上游自定义请求体（如 {"enable_thinking": false}）静默清空。
        extra_body = raw.get("extra_body")
        if extra_body is None:
            existing = self.config.get(f"upstream_{slot}")
            if isinstance(existing, dict):
                extra_body = existing.get("extra_body")
        result = {
            "enabled": enabled,
            "name": name,
            "api_base": api_base,
            "api_key": api_key,
            "model": model,
            "priority": priority,
            "supports_tools": supports_tools,
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
                    self._health_results.pop(slot, None)

            # 1. 校验并写入内存配置
            if "global" in payload:
                global_update = payload["global"]
                if not isinstance(global_update, dict):
                    raise ValueError("global 必须是对象")
                allowed_global = {
                    "enabled", "provider_id", "request_timeout_seconds",
                    "max_attempts", "failure_threshold", "cooldown_seconds",
                    "max_concurrency", "max_input_chars", "max_output_tokens",
                    "health_check_timeout_seconds", "skip_unhealthy_upstreams",
                    "health_skip_ttl_seconds", "fallback_to_main_model",
                    "fallback_provider_id",
                }
                unknown_global = set(global_update) - allowed_global
                if unknown_global:
                    raise ValueError(f"global 包含不支持的字段: {sorted(unknown_global)}")
                for key, value in global_update.items():
                    if key == "provider_id":
                        provider_id = str(value or "").strip()
                        if not provider_id:
                            raise ValueError("provider_id 不能为空")
                        self.config["provider_id"] = provider_id
                    elif key == "enabled":
                        self.config["enabled"] = bool(value)
                    elif key in self.GLOBAL_INT_FIELDS:
                        self.config[key] = self._coerce_global_int(key, value)
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

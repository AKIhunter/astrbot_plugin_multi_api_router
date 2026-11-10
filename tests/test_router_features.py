import asyncio
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import main as router_module


def upstream(
    slot,
    *,
    enabled=True,
    api_type="chat",
    priority=None,
):
    return {
        "enabled": enabled,
        "api_type": api_type,
        "name": f"api-{slot}",
        "api_base": "https://example.invalid/v1",
        "api_key": "test-key",
        "model": f"model-{slot}",
        "priority": priority if priority is not None else slot * 10,
        "supports_tools": True,
    }


class RouterFeatureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.providers = []

    def build_provider(self, config):
        base = {
            "provider_id": router_module.PROVIDER_TYPE,
            "fallback_to_main_model": False,
            "failure_threshold": 99,
            "max_attempts": 5,
            **config,
        }
        provider = router_module.MultiApiRouterProvider(base)
        self.providers.append(provider)
        return provider

    async def test_large_base64_passes_text_budget_and_history_is_not_changed(self):
        from test_image_budget import image, user
        provider = self.build_provider({"max_input_chars": 1000, "upstream_1": upstream(1)})
        original = [user(image(b"1" * 300000))]
        seen = []

        async def fake_call(_self, item, **kwargs):
            seen.append(kwargs["contexts"])
            return "ok"

        provider._call_once = types.MethodType(fake_call, provider)
        self.assertEqual(await provider.text_chat(contexts=original), "ok")
        self.assertEqual(seen[0], original)
        self.assertEqual(original[0]["content"][0]["type"], "image_url")

    async def test_text_and_tool_budget_reject_before_upstream(self):
        from astrbot.core.agent.tool import ToolSet, FunctionTool
        provider = self.build_provider({"max_input_chars": 1000, "upstream_1": upstream(1)})
        with mock.patch.object(provider, "_call_once", new_callable=mock.AsyncMock) as call:
            for kwargs in ({"prompt": "x" * 1001},
                           {"prompt": "你好", "func_tool": ToolSet([
                               FunctionTool(name="fixture", description="x" * 1001, parameters={})
                           ])}):
                with self.subTest(kwargs=list(kwargs)):
                    with self.assertRaisesRegex(ValueError, "字符数"):
                        await provider.text_chat(**kwargs)
            call.assert_not_called()

    async def test_compaction_reaches_upstream_without_changing_input(self):
        from test_image_budget import image, user
        provider = self.build_provider({"upstream_1": upstream(1)})
        original = [user(image()) for _ in range(5)]
        seen = []

        async def fake_call(_self, item, **kwargs):
            seen.append(kwargs["contexts"])
            return "ok"

        provider._call_once = types.MethodType(fake_call, provider)
        await provider.text_chat(contexts=original)
        self.assertEqual(seen[0][0]["content"][0]["type"], "text")
        self.assertEqual(original[0]["content"][0]["type"], "image_url")

    async def test_oversized_current_image_rejected_before_upstream(self):
        from test_image_budget import image
        provider = self.build_provider({"max_image_bytes": 1024, "upstream_1": upstream(1)})
        with mock.patch.object(provider, "_call_once", new_callable=mock.AsyncMock) as call:
            with self.assertRaisesRegex(ValueError, "单张"):
                await provider.text_chat(prompt="看图", image_urls=[image(b"x" * 1025)["image_url"]["url"]])
            call.assert_not_called()

    async def asyncTearDown(self):
        for provider in self.providers:
            for item in provider._all_upstreams:
                await item.client.client.close()

    async def test_route_uses_order_and_stops_after_first_response(self):
        provider = self.build_provider(
            {
                "upstream_1": upstream(1),
                "upstream_2": upstream(2),
                "upstream_3": upstream(3),
            }
        )
        calls = []

        async def fake_call(_self, item, **_kwargs):
            calls.append(item.slot)
            if item.slot == 1:
                raise RuntimeError("first failed")
            return "second response"

        provider._call_once = types.MethodType(fake_call, provider)
        result = await provider.text_chat(prompt="hello")

        self.assertEqual(result, "second response")
        self.assertEqual(calls, [1, 2])

    async def test_failed_requests_do_not_freeze_later_requests(self):
        provider = self.build_provider({
            "failure_threshold": 2,
            "cooldown_seconds": 300,
            "upstream_1": upstream(1),
        })
        item = provider.upstreams[0]
        with mock.patch.object(
            provider, "_call_once",
            side_effect=[TimeoutError(), TimeoutError(), "recovered"],
        ) as call:
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, "路由失败"):
                    await provider.text_chat(prompt="fixture")
            self.assertEqual(item.consecutive_failures, 2)
            self.assertEqual(item.circuit_open_until, 0)
            # 即使旧实例残留未来的熔断截止时间，也不能挡住新请求。
            item.circuit_open_until = router_module.time.monotonic() + 300
            self.assertTrue(item.available(router_module.time.monotonic()))
            self.assertEqual(await provider.text_chat(prompt="fixture"), "recovered")
            self.assertEqual(call.await_count, 3)
        self.assertEqual(item.consecutive_failures, 0)
        self.assertEqual(provider.cooldown_seconds, 0)

    async def test_transient_error_retries_same_input_once(self):
        import httpx
        from openai import InternalServerError
        provider = self.build_provider({"upstream_1": upstream(1)})
        client = provider.upstreams[0].client
        payload = {"model": "model-1", "messages": [{"role": "user", "content": "fixture"}],
                   "custom_field": {"same_input": True}}
        seen = []
        failure = InternalServerError(
            "fixture failure", response=httpx.Response(502, request=httpx.Request(
                "POST", "https://example.invalid/v1/chat/completions")), body=None,
        )

        async def query(request_payload, tools, **kwargs):
            self.assertEqual(kwargs["request_max_retries"], 1)
            self.assertEqual(kwargs["conversation_id"], "fixture-conversation")
            seen.append(request_payload.pop("custom_field"))
            if len(seen) == 1:
                request_payload["messages"].clear()
                raise failure
            self.assertEqual(request_payload["messages"], payload["messages"])
            return "recovered"

        with mock.patch.object(client, "_prepare_chat_payload",
                               return_value=(payload, [])), \
             mock.patch.object(client, "_query", side_effect=query) as call, \
             mock.patch.object(router_module.asyncio, "sleep",
                               new_callable=mock.AsyncMock) as sleep:
            self.assertEqual(await provider.text_chat(
                prompt="fixture", conversation_id="fixture-conversation",
            ), "recovered")
            self.assertEqual(call.await_count, 2)
            sleep.assert_awaited_once_with(1.0)
        self.assertEqual(seen, [{"same_input": True}, {"same_input": True}])
        self.assertEqual(len(payload["messages"]), 1)

    async def test_temporary_errors_exhaust_two_attempts_then_next_request_can_recover(self):
        provider = self.build_provider({"upstream_1": upstream(1)})
        client = provider.upstreams[0].client
        with mock.patch.object(client, "_prepare_chat_payload",
                               return_value=({"model": "model-1"}, [])), \
             mock.patch.object(client, "_query",
                               side_effect=[TimeoutError(), TimeoutError(), "next response"]) as call, \
             mock.patch.object(router_module.asyncio, "sleep",
                               new_callable=mock.AsyncMock) as sleep:
            with self.assertRaisesRegex(RuntimeError, "路由失败"):
                await provider.text_chat(prompt="fixture")
            self.assertEqual(call.await_count, 2)
            self.assertEqual(await provider.text_chat(prompt="fixture"), "next response")
            self.assertEqual(call.await_count, 3)
            sleep.assert_awaited_once()

    async def test_rejected_requests_are_not_retried_in_place(self):
        import httpx
        from openai import APIStatusError
        provider = self.build_provider({"upstream_1": upstream(1)})
        client = provider.upstreams[0].client
        for status in (400, 401, 403, 404, 422, 429):
            with self.subTest(status=status):
                error = APIStatusError(
                    "fixture rejection", response=httpx.Response(
                        status, request=httpx.Request("POST", "https://example.invalid/v1")),
                    body=None,
                )
                with mock.patch.object(client, "_prepare_chat_payload",
                                       return_value=({"model": "model-1"}, [])), \
                     mock.patch.object(client, "_query", side_effect=error) as call, \
                     mock.patch.object(router_module.asyncio, "sleep",
                                       new_callable=mock.AsyncMock) as sleep:
                    with self.assertRaisesRegex(RuntimeError, "路由失败"):
                        await provider.text_chat(prompt="fixture")
                    call.assert_awaited_once()
                    sleep.assert_not_awaited()

    async def test_route_deadline_skips_retry_instead_of_waiting(self):
        provider = self.build_provider({"upstream_1": upstream(1)})
        provider.route_timeout_seconds = 0.02
        capacity = provider._semaphore._value
        client = provider.upstreams[0].client
        with mock.patch.object(client, "_prepare_chat_payload",
                               return_value=({"model": "model-1"}, [])), \
             mock.patch.object(client, "_query", side_effect=TimeoutError()) as call:
            # 新行为：不再启动注定被总时限截断的等待，直接报告剩余预算不足。
            with self.assertRaisesRegex(RuntimeError, "剩余预算不足"):
                await provider.text_chat(prompt="fixture")
            call.assert_awaited_once()
        self.assertEqual(provider._semaphore._value, capacity)

    async def test_retry_is_skipped_when_remaining_budget_is_insufficient(self):
        provider = self.build_provider({
            "upstream_1": upstream(1),
            "upstream_2": upstream(2),
        })
        # 10 秒总预算 < 单次超时 45 秒 + 重试间隔 1 秒：不发注定被截断的重试。
        provider.route_timeout_seconds = 10
        first = provider.upstreams[0].client
        second = provider.upstreams[1].client
        with mock.patch.object(first, "_prepare_chat_payload",
                               return_value=({"model": "model-1"}, [])), \
             mock.patch.object(second, "_prepare_chat_payload",
                               return_value=({"model": "model-2"}, [])), \
             mock.patch.object(first, "_query", side_effect=TimeoutError()) as first_call, \
             mock.patch.object(second, "_query", return_value="next upstream") as second_call, \
             mock.patch.object(router_module.asyncio, "sleep",
                               new_callable=mock.AsyncMock) as sleep:
            self.assertEqual(await provider.text_chat(prompt="fixture"), "next upstream")
            # 第一个上游只调用一次：剩余预算不足，直接放弃重试。
            first_call.assert_awaited_once()
            # 可切换信号生效：同一次请求立刻落到下一个上游。
            second_call.assert_awaited_once()
            sleep.assert_not_awaited()

    async def test_pinned_provider_queue_has_total_deadline(self):
        provider = self.build_provider({
            "id": f"{router_module.PINNED_PROVIDER_PREFIX}1",
            "upstream_1": upstream(1),
        })
        provider.route_timeout_seconds = 0.02
        provider._semaphore = asyncio.Semaphore(0)
        with mock.patch.object(provider, "_call_once") as call:
            with self.assertRaisesRegex(RuntimeError, "超过总时限"):
                await provider.text_chat(prompt="fixture")
            call.assert_not_awaited()

    async def test_transient_health_failure_does_not_block_real_request(self):
        provider = self.build_provider({"upstream_1": upstream(1)})
        for status in (None, 429, 500, 502, 503, 504):
            with self.subTest(status=status):
                provider.health_lookup = lambda _slot: {
                    "ok": False, "checked_at": int(router_module.time.time()),
                    "http_status": status, "status": "fixture temporary failure",
                }
                with mock.patch.object(provider, "_call_once", return_value="ok") as call:
                    self.assertEqual(await provider.text_chat(prompt="fixture"), "ok")
                    call.assert_awaited_once()
        provider.health_lookup = lambda _slot: {
            "ok": False, "checked_at": int(router_module.time.time()),
            "http_status": 401, "status": "fixture authentication failure",
        }
        with mock.patch.object(provider, "_call_once") as call:
            with self.assertRaisesRegex(RuntimeError, "没有可调用的上游"):
                await provider.text_chat(prompt="fixture")
            call.assert_not_awaited()

    async def test_confirmed_quota_error_is_skipped_with_http_code(self):
        provider = self.build_provider({"upstream_1": upstream(1)})
        provider.health_lookup = lambda _slot: {
            "ok": False, "checked_at": int(router_module.time.time()),
            "http_status": 402, "status": "额度不足或欠费",
        }
        with mock.patch.object(provider, "_call_once") as call:
            with self.assertRaisesRegex(RuntimeError, "没有可调用的上游"):
                await provider.text_chat(prompt="fixture")
            call.assert_not_awaited()
        # 页面与日志都要能区分具体状态码。
        self.assertIn(
            "额度不足或欠费 / HTTP 402",
            "\n".join(provider.status_lines()),
        )
        # 指定上游模式不受健康跳过影响，仍必须真实调用。
        pinned = self.build_provider({
            "id": f"{router_module.PINNED_PROVIDER_PREFIX}1",
            "upstream_1": upstream(1),
        })
        pinned.health_lookup = provider.health_lookup
        with mock.patch.object(pinned, "_call_once", return_value="ok") as call:
            self.assertEqual(await pinned.text_chat(prompt="fixture"), "ok")
            call.assert_awaited_once()

    async def test_quota_error_probe_reports_insufficient_balance(self):
        import httpx
        from openai import APIStatusError

        plugin = object.__new__(router_module.MultiApiRouterPlugin)
        plugin.config = {"upstream_1": upstream(1)}
        error = APIStatusError(
            "quota exceeded",
            response=httpx.Response(
                402,
                request=httpx.Request(
                    "POST", "https://example.invalid/v1/chat/completions"
                ),
            ),
            body=None,
        )
        client = types.SimpleNamespace(close=mock.AsyncMock())
        with mock.patch.object(
            router_module, "AsyncOpenAI", return_value=client
        ), mock.patch.object(
            plugin, "_execute_typed_probe", side_effect=error
        ):
            result = await plugin._probe_upstream(1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["http_status"], 402)
        self.assertEqual(result["status"], "额度不足或欠费")

    def test_legacy_circuit_settings_cannot_reenable_freezing(self):
        plugin = object.__new__(router_module.MultiApiRouterPlugin)
        plugin.config = {"failure_threshold": 2, "cooldown_seconds": 300}
        self.assertEqual(plugin._coerce_global_int("failure_threshold", 2), 0)
        self.assertEqual(plugin._coerce_global_int("cooldown_seconds", 300), 0)
        provider = self.build_provider({"upstream_1": upstream(1)})
        plugin.router = provider
        strategy = plugin._strategy_data()
        self.assertFalse(strategy["circuit_breaker_enabled"])
        self.assertEqual(strategy["upstream_max_attempts"], 2)
        self.assertIn("不按连续失败冻结接口", strategy["summary"])

    async def test_configured_retry_count_interval_and_disabled_retries(self):
        # 重试间隔必须装得进总预算（默认 90s，单次 45s），否则会被新的预算前置校验跳过。
        for count, interval in ((0, 0), (3, 0.75), (5, 0.5)):
            with self.subTest(count=count, interval=interval):
                provider = self.build_provider({
                    "upstream_1": upstream(1),
                    "upstream_retry_count": count,
                    "retry_interval_seconds": interval,
                })
                client = provider.upstreams[0].client
                with mock.patch.object(client, "_prepare_chat_payload",
                                       return_value=({"model": "model-1"}, [])), \
                     mock.patch.object(client, "_query", side_effect=TimeoutError()) as call, \
                     mock.patch.object(router_module.asyncio, "sleep",
                                       new_callable=mock.AsyncMock) as sleep:
                    with self.assertRaisesRegex(RuntimeError, "路由失败"):
                        await provider.text_chat(prompt="fixture")
                    self.assertEqual(call.await_count, count + 1)
                    self.assertEqual(sleep.await_args_list, [mock.call(interval)] * count)

    async def test_fractional_retry_settings_save_read_and_reload(self):
        from copy import deepcopy

        class Config(dict):
            def save_config(self):
                self.saved = deepcopy(dict(self))

        plugin = object.__new__(router_module.MultiApiRouterPlugin)
        plugin.config = Config({
            "upstream_1": upstream(1), "max_concurrency": 7,
            "max_output_tokens": 1234, "fallback_to_main_model": False,
        })
        plugin._health_results = {}
        plugin.router = self.build_provider(dict(plugin.config))
        updates = {"request_timeout_seconds": "60", "max_attempts": "4",
                   "upstream_retry_count": "3", "retry_interval_seconds": "0.75"}

        async def reload_fixture():
            # 使用真正的 Provider 构造，替代宿主注册和其它插件自动绑定。
            plugin.router = self.build_provider(dict(plugin.config))

        with mock.patch.object(router_module, "request",
                               types.SimpleNamespace(json=mock.AsyncMock(
                                   return_value={"global": updates}))), \
             mock.patch.object(router_module, "json_response", side_effect=lambda data: data), \
             mock.patch.object(plugin, "_reload_router", side_effect=reload_fixture) as reload:
            result = await plugin.api_save_upstreams()
            self.assertTrue(result["ok"])
            reload.assert_awaited_once()
            loaded = await plugin.api_upstreams_config()

        for key, value in (("request_timeout_seconds", 60), ("max_attempts", 4),
                           ("upstream_retry_count", 3), ("retry_interval_seconds", 0.75)):
            self.assertEqual(plugin.config.saved[key], value)
            self.assertEqual(loaded["global"][key], value)
            self.assertEqual(getattr(plugin.router, key), value)
            self.assertEqual(loaded["strategy"][key], value)
        self.assertEqual(plugin.router.upstreams[0].client.client.timeout, 60)
        self.assertEqual(loaded["strategy"]["upstream_max_attempts"], 4)
        self.assertIn("间隔 0.75s", loaded["strategy"]["summary"])
        self.assertIn("重试间隔 0.75s", "\n".join(plugin.router.status_lines()))
        self.assertEqual(plugin.config.saved["max_concurrency"], 7)
        self.assertEqual(plugin.config.saved["max_output_tokens"], 1234)
        self.assertEqual(plugin.config.saved["upstream_1"], upstream(1))

    async def test_invalid_retry_settings_do_not_mutate_or_save(self):
        invalid = {
            "upstream_retry_count": (-1, 6, 1.5, "", None, True, "nan"),
            "retry_interval_seconds": (-0.1, 3601, "", None, False, "nan", "inf"),
            "request_timeout_seconds": (4, 601, 5.5),
            "max_attempts": (0, 6, 1.5),
        }
        for key, values in invalid.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    plugin = object.__new__(router_module.MultiApiRouterPlugin)
                    plugin.config = {"upstream_1": upstream(1), "upstream_retry_count": 1}
                    original = dict(plugin.config)
                    plugin._health_results = {1: {"ok": True}}
                    with mock.patch.object(router_module, "request", types.SimpleNamespace(
                        json=mock.AsyncMock(return_value={
                            "global": {"provider_id": "changed", key: value},
                            "delete_slots": [1],
                        })
                    )), mock.patch.object(
                        router_module, "error_response",
                        side_effect=lambda message, status_code: (message, status_code),
                    ), mock.patch.object(plugin, "_reload_router") as reload:
                        message, status = await plugin.api_save_upstreams()
                    self.assertEqual(status, 400)
                    self.assertIn(key, message)
                    self.assertEqual(plugin.config, original)
                    self.assertEqual(plugin._health_results, {1: {"ok": True}})
                    reload.assert_not_awaited()

    async def test_disabled_plugin_still_exposes_default_retry_settings(self):
        plugin = object.__new__(router_module.MultiApiRouterPlugin)
        plugin.config = {}
        plugin.router = None
        plugin._health_results = {}
        with mock.patch.object(router_module, "json_response", side_effect=lambda data: data):
            loaded = await plugin.api_upstreams_config()
        self.assertEqual(loaded["global"]["upstream_retry_count"], 1)
        self.assertEqual(loaded["global"]["retry_interval_seconds"], 1.0)

    def test_api_key_mask_keeps_prefix_and_suffix(self):
        mask = router_module.MultiApiRouterPlugin._mask_api_key
        self.assertEqual(mask("sk-a1b2c3d4cdef"), "sk-a1b2****cdef")
        self.assertEqual(mask("short"), "****")
        self.assertEqual(mask(""), "")
        self.assertNotIn("c3d4", mask("sk-a1b2c3d4cdef"))

    async def test_upstreams_config_masks_key_by_default(self):
        plugin = object.__new__(router_module.MultiApiRouterPlugin)
        plugin.config = {
            "upstream_1": {**upstream(1), "api_key": "sk-a1b2c3d4cdef"},
        }
        plugin.router = None
        plugin._health_results = {}
        with mock.patch.object(router_module, "json_response", side_effect=lambda data: data):
            loaded = await plugin.api_upstreams_config()
        entry = loaded["upstreams"][0]
        self.assertEqual(entry["api_key"], "sk-a1b2****cdef")
        self.assertEqual(entry["api_key_masked"], "sk-a1b2****cdef")
        self.assertTrue(entry["api_key_present"])
        self.assertNotIn("sk-a1b2c3d4cdef", json.dumps(loaded, ensure_ascii=False))

    async def test_upstreams_config_compat_switch_returns_plain_key(self):
        plugin = object.__new__(router_module.MultiApiRouterPlugin)
        plugin.config = {
            "upstream_1": {**upstream(1), "api_key": "sk-a1b2c3d4cdef"},
            "expose_plain_api_keys": True,
        }
        plugin.router = None
        plugin._health_results = {}
        with mock.patch.object(router_module, "json_response", side_effect=lambda data: data):
            loaded = await plugin.api_upstreams_config()
        self.assertEqual(loaded["upstreams"][0]["api_key"], "sk-a1b2c3d4cdef")

    async def test_save_without_api_key_field_keeps_existing_key(self):
        class Config(dict):
            def save_config(self):
                self.saved = True

        plugin = object.__new__(router_module.MultiApiRouterPlugin)
        plugin.config = Config({"upstream_1": upstream(1)})
        plugin.router = None
        plugin._health_results = {}
        payload = {
            "upstream_1": {
                key: value for key, value in upstream(1).items() if key != "api_key"
            },
        }
        with mock.patch.object(router_module, "request",
                               types.SimpleNamespace(
                                   json=mock.AsyncMock(return_value=payload))), \
             mock.patch.object(router_module, "json_response",
                               side_effect=lambda data: data), \
             mock.patch.object(plugin, "_reload_router", new_callable=mock.AsyncMock):
            result = await plugin.api_save_upstreams()
        self.assertTrue(result["ok"])
        self.assertEqual(plugin.config["upstream_1"]["api_key"], "test-key")
        self.assertTrue(plugin.config.saved)

    async def test_save_with_masked_key_keeps_existing_key(self):
        class Config(dict):
            def save_config(self):
                self.saved = True

        plugin = object.__new__(router_module.MultiApiRouterPlugin)
        plugin.config = Config({"upstream_1": upstream(1)})
        plugin.router = None
        plugin._health_results = {}
        masked = router_module.MultiApiRouterPlugin._mask_api_key("test-key")
        payload = {"upstream_1": {**upstream(1), "api_key": masked}}
        with mock.patch.object(router_module, "request",
                               types.SimpleNamespace(
                                   json=mock.AsyncMock(return_value=payload))), \
             mock.patch.object(router_module, "json_response",
                               side_effect=lambda data: data), \
             mock.patch.object(plugin, "_reload_router", new_callable=mock.AsyncMock):
            result = await plugin.api_save_upstreams()
        self.assertTrue(result["ok"])
        self.assertEqual(plugin.config["upstream_1"]["api_key"], "test-key")

    async def test_list_models_uses_saved_key_when_masked(self):
        plugin = object.__new__(router_module.MultiApiRouterPlugin)
        plugin.config = {
            "upstream_1": {**upstream(1), "api_key": "sk-a1b2c3d4cdef"},
        }
        plugin._model_list_semaphore = asyncio.Semaphore(2)
        payload = {
            "api_base": "https://example.invalid/v1",
            "api_key": "sk-a1b2****cdef",
            "slot": 1,
        }
        with mock.patch.object(router_module, "request",
                               types.SimpleNamespace(
                                   json=mock.AsyncMock(return_value=payload))), \
             mock.patch.object(plugin, "_fetch_visible_models",
                               new_callable=mock.AsyncMock,
                               return_value=(["model-x"], False)) as fetch, \
             mock.patch.object(router_module, "json_response",
                               side_effect=lambda data: data):
            result = await plugin.api_list_models()
        self.assertEqual(result["count"], 1)
        self.assertEqual(fetch.await_args.args[1], "sk-a1b2c3d4cdef")

    async def test_reveal_requires_login(self):
        plugin = object.__new__(router_module.MultiApiRouterPlugin)
        plugin.config = {"upstream_1": upstream(1)}
        with mock.patch.object(router_module, "request",
                               types.SimpleNamespace(
                                   username=None,
                                   json=mock.AsyncMock(return_value={"slot": 1}))), \
             mock.patch.object(router_module, "error_response",
                               side_effect=lambda message, status_code: (message, status_code)):
            message, status = await plugin.api_reveal_upstream_key()
        self.assertEqual(status, 401)
        self.assertIn("登录", message)

    async def test_reveal_returns_plain_key_and_writes_audit_without_key(self):
        plugin = object.__new__(router_module.MultiApiRouterPlugin)
        plugin.config = {"upstream_1": upstream(1)}
        with tempfile.TemporaryDirectory() as tmp:
            plugin._state_path = Path(tmp) / "auto_bindings.json"
            with mock.patch.object(router_module, "request",
                                   types.SimpleNamespace(
                                       username="admin",
                                       json=mock.AsyncMock(return_value={"slot": 1}),
                                       client_host="127.0.0.1",
                                       headers={"user-agent": "fixture-agent"})), \
                 mock.patch.object(router_module, "json_response",
                                   side_effect=lambda data: data):
                result = await plugin.api_reveal_upstream_key()
            self.assertEqual(result["api_key"], "test-key")
            audit_text = (Path(tmp) / router_module.API_KEY_REVEAL_AUDIT).read_text(
                encoding="utf-8"
            )
            record = json.loads(audit_text.strip())
            self.assertEqual(record["username"], "admin")
            self.assertEqual(record["slot"], 1)
            self.assertEqual(record["client_host"], "127.0.0.1")
            self.assertEqual(record["user_agent"], "fixture-agent")
            self.assertNotIn("test-key", audit_text)

    async def test_reveal_refuses_when_audit_cannot_be_written(self):
        plugin = object.__new__(router_module.MultiApiRouterPlugin)
        plugin.config = {"upstream_1": upstream(1)}
        with tempfile.TemporaryDirectory() as tmp:
            # 用一个同名文件占住目录位置，让审计日志无法创建。
            blocked = Path(tmp) / "blocked"
            blocked.write_text("", encoding="utf-8")
            plugin._state_path = blocked / "auto_bindings.json"
            with mock.patch.object(router_module, "request",
                                   types.SimpleNamespace(
                                       username="admin",
                                       json=mock.AsyncMock(return_value={"slot": 1}),
                                       client_host="127.0.0.1",
                                       headers={})), \
                 mock.patch.object(router_module, "error_response",
                                   side_effect=lambda message, status_code: (message, status_code)):
                message, status = await plugin.api_reveal_upstream_key()
        self.assertEqual(status, 500)
        self.assertNotIn("test-key", message)

    def test_startup_handles_old_or_malformed_retry_config(self):
        for value, expected in ((None, 1.0), ("nan", 1.0), ("bad", 1.0),
                                (-2, 0), (4000, 3600), ("0.75", 0.75)):
            with self.subTest(value=value):
                provider = self.build_provider({
                    "upstream_1": upstream(1), "retry_interval_seconds": value,
                })
                self.assertEqual(provider.retry_interval_seconds, expected)

    async def test_route_timeout_stops_remaining_upstreams(self):
        provider = self.build_provider(
            {
                "upstream_1": upstream(1),
                "upstream_2": upstream(2),
            }
        )
        provider.route_timeout_seconds = 0.03
        calls = []

        async def slow_call(_self, item, **_kwargs):
            calls.append(item.slot)
            await asyncio.sleep(0.2)
            return "late"

        provider._call_once = types.MethodType(slow_call, provider)
        with self.assertRaisesRegex(RuntimeError, "超过总时限"):
            await provider.text_chat(prompt="hello")

        self.assertEqual(calls, [1])

    async def test_route_timeout_also_bounds_main_model_fallback(self):
        provider = self.build_provider({"upstream_1": upstream(1)})
        provider.route_timeout_seconds = 0.03

        async def failed_call(_self, _item, **_kwargs):
            raise RuntimeError("upstream failed")

        async def slow_fallback(_self, **_kwargs):
            await asyncio.sleep(0.2)
            return "late fallback"

        provider._call_once = types.MethodType(failed_call, provider)
        provider._call_fallback = types.MethodType(slow_fallback, provider)
        with self.assertRaisesRegex(RuntimeError, "超过总时限"):
            await provider.text_chat(prompt="hello")

    async def test_route_disabled_api_remains_available_when_pinned(self):
        config = {
            "id": f"{router_module.PINNED_PROVIDER_PREFIX}1",
            "upstream_1": upstream(1, enabled=False),
        }
        provider = router_module.MultiApiRouterProvider(config)
        self.providers.append(provider)
        calls = []
        provider.health_lookup = lambda _slot: {
            "ok": False,
            "checked_at": int(router_module.time.time()),
            "status": "测试失败状态",
        }

        async def fake_call(_self, item, **_kwargs):
            calls.append(item.slot)
            return "direct response"

        provider._call_once = types.MethodType(fake_call, provider)
        result = await provider.text_chat(prompt="hello")

        self.assertEqual(result, "direct response")
        self.assertEqual(calls, [1])
        self.assertFalse(provider.upstreams[0].route_enabled)

    def test_non_chat_types_never_enter_chat_provider(self):
        provider = self.build_provider(
            {
                "upstream_1": upstream(1, api_type="chat"),
                "upstream_2": upstream(2, api_type="image_to_text"),
                "upstream_3": upstream(3, api_type="multimodal"),
                "upstream_4": upstream(4, api_type="image_generation"),
                "upstream_5": upstream(5, api_type="embedding"),
                "upstream_6": upstream(6, api_type="rerank"),
                "upstream_7": upstream(7, api_type="stt"),
                "upstream_8": upstream(8, api_type="tts"),
                "upstream_9": upstream(9, api_type="asr"),
            }
        )

        self.assertEqual(
            [item.slot for item in provider._all_upstreams],
            [1, 2, 3],
        )

    def test_legacy_enabled_field_keeps_its_route_semantics(self):
        self.assertTrue(
            router_module.MultiApiRouterProvider._route_enabled(upstream(1))
        )
        self.assertFalse(
            router_module.MultiApiRouterProvider._route_enabled(
                upstream(1, enabled=False)
            )
        )
        self.assertEqual(
            router_module.MultiApiRouterProvider._api_type(
                {"api_base": "https://example.invalid/v1"}
            ),
            "chat",
        )

    def test_max_input_chars_defaults_to_256k_and_allows_override(self):
        default_provider = self.build_provider({})
        custom_provider = self.build_provider({"max_input_chars": 300000})

        self.assertEqual(
            default_provider.max_input_chars,
            router_module.DEFAULT_MAX_INPUT_CHARS,
        )
        self.assertEqual(default_provider.max_input_chars, 262144)
        self.assertEqual(custom_provider.max_input_chars, 300000)


class FakeEndpoint:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class FakeProbeClient:
    def __init__(self):
        chat_response = types.SimpleNamespace(
            choices=[
                types.SimpleNamespace(
                    message=types.SimpleNamespace(content="OK")
                )
            ]
        )
        self.chat_endpoint = FakeEndpoint(chat_response)
        self.image_endpoint = FakeEndpoint(types.SimpleNamespace(data=[object()]))
        self.embedding_endpoint = FakeEndpoint(
            types.SimpleNamespace(
                data=[types.SimpleNamespace(embedding=[0.1, 0.2])]
            )
        )
        self.speech_endpoint = FakeEndpoint(object())
        self.transcription_endpoint = FakeEndpoint(object())
        self.chat = types.SimpleNamespace(completions=self.chat_endpoint)
        self.images = types.SimpleNamespace(generate=self.image_endpoint.create)
        self.embeddings = types.SimpleNamespace(
            create=self.embedding_endpoint.create
        )
        self.audio = types.SimpleNamespace(
            speech=types.SimpleNamespace(create=self.speech_endpoint.create),
            transcriptions=types.SimpleNamespace(
                create=self.transcription_endpoint.create
            ),
        )
        self.post_calls = []

    async def post(self, path, **kwargs):
        self.post_calls.append((path, kwargs))
        return {"results": [{"index": 0, "relevance_score": 1.0}]}


class TypedProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_gpt_image_probe_uses_supported_square_size(self):
        plugin = object.__new__(router_module.MultiApiRouterPlugin)
        for model, expected_size in (
            ("gpt-image-2.6-flare", "1024x1024"),
            ("qwen-image-3.0", "256x256"),
        ):
            with self.subTest(model=model):
                client = FakeProbeClient()
                await plugin._execute_typed_probe(
                    client, api_type="image_generation", model=model
                )
                self.assertEqual(client.image_endpoint.calls[0]["size"], expected_size)
                self.assertEqual(client.image_endpoint.calls[0]["n"], 1)

    async def test_image_probe_has_longer_timeout_than_chat(self):
        plugin = object.__new__(router_module.MultiApiRouterPlugin)
        for api_type, expected_timeout in (("image_generation", 60), ("chat", 15)):
            with self.subTest(api_type=api_type):
                plugin.config = {
                    "health_check_timeout_seconds": 15,
                    "upstream_10": upstream(10, api_type=api_type),
                }
                client = FakeProbeClient()
                client.close = mock.AsyncMock()
                with mock.patch.object(router_module, "AsyncOpenAI", return_value=client) as factory:
                    result = await plugin._probe_upstream(10)
                self.assertTrue(result["ok"])
                self.assertEqual(factory.call_args.kwargs["timeout"], expected_timeout)
                self.assertEqual(factory.call_args.kwargs["max_retries"], 0)
                client.close.assert_awaited_once()

    async def test_sdk_timeout_is_reported_as_timeout(self):
        import httpx
        plugin = object.__new__(router_module.MultiApiRouterPlugin)
        plugin.config = {"upstream_10": upstream(10)}
        client = FakeProbeClient()
        client.close = mock.AsyncMock()
        error = router_module.APITimeoutError(
            request=httpx.Request("POST", "https://example.invalid/v1/chat/completions")
        )
        with mock.patch.object(router_module, "AsyncOpenAI", return_value=client):
            with mock.patch.object(plugin, "_execute_typed_probe", side_effect=error):
                result = await plugin._probe_upstream(10)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "请求超时")
        self.assertEqual(result["error_type"], "APITimeoutError")
        client.close.assert_awaited_once()

    async def test_each_category_uses_its_own_protocol(self):
        plugin = object.__new__(router_module.MultiApiRouterPlugin)
        expected_protocols = {
            "chat": "Chat Completions",
            "image_generation": "Images Generations",
            "image_to_text": "Chat Completions",
            "embedding": "Embeddings",
            "rerank": "Rerank",
            "stt": "Audio Transcriptions",
            "tts": "Audio Speech",
            "asr": "Audio Transcriptions",
            "multimodal": "Chat Completions",
        }

        for api_type, expected in expected_protocols.items():
            with self.subTest(api_type=api_type):
                client = FakeProbeClient()
                ok, protocol = await plugin._execute_typed_probe(
                    client,
                    api_type=api_type,
                    model="test-model",
                )
                self.assertTrue(ok)
                self.assertIn(expected, protocol)

        multimodal_client = FakeProbeClient()
        ok, protocol = await plugin._execute_typed_probe(
            multimodal_client,
            api_type="image_to_text",
            model="test-model",
        )
        content = multimodal_client.chat_endpoint.calls[0]["messages"][0][
            "content"
        ]
        self.assertTrue(ok)
        self.assertIsInstance(content, str)
        self.assertIn("图转文", protocol)

    async def test_model_list_normalizes_base_deduplicates_and_sorts(self):
        class FakeModels:
            async def list(self):
                return types.SimpleNamespace(
                    data=[
                        types.SimpleNamespace(id="z-model"),
                        types.SimpleNamespace(id="A-model"),
                        types.SimpleNamespace(id="z-model"),
                        types.SimpleNamespace(id=""),
                    ]
                )

        class FakeClient:
            created = []

            def __init__(self, **kwargs):
                self.models = FakeModels()
                self.kwargs = kwargs
                self.closed = False
                self.__class__.created.append(self)

            async def close(self):
                self.closed = True

        plugin = object.__new__(router_module.MultiApiRouterPlugin)
        plugin.config = {"health_check_timeout_seconds": 15}
        normalized = plugin._normalize_model_api_base(
            "https://example.invalid/v1/models/"
        )
        with mock.patch.object(router_module, "AsyncOpenAI", FakeClient):
            models, truncated = await plugin._fetch_visible_models(
                normalized,
                "test-key",
            )

        self.assertEqual(normalized, "https://example.invalid/v1")
        self.assertEqual(models, ["A-model", "z-model"])
        self.assertFalse(truncated)
        self.assertEqual(
            FakeClient.created[0].kwargs["base_url"],
            "https://example.invalid/v1",
        )
        self.assertTrue(FakeClient.created[0].closed)

    def test_model_list_rejects_unsafe_or_invalid_base_urls(self):
        plugin = object.__new__(router_module.MultiApiRouterPlugin)
        for value in (
            "",
            "example.invalid/v1",
            "ftp://example.invalid/v1",
            "https://user:pass@example.invalid/v1",
            "https://example.invalid/v1?token=secret",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    plugin._normalize_model_api_base(value)


class ProviderRegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_route_disabled_chat_api_keeps_direct_provider(self):
        manager = types.SimpleNamespace(
            inst_map={},
            provider_insts=[],
            providers_config=[],
            provider_sources_config=[],
        )

        def register_provider(provider):
            manager.provider_insts.append(provider)

        plugin = object.__new__(router_module.MultiApiRouterPlugin)
        plugin.config = {
            "provider_id": router_module.PROVIDER_TYPE,
            "fallback_to_main_model": False,
            "upstream_1": upstream(1, enabled=True),
            "upstream_2": upstream(2, enabled=False),
            "upstream_3": upstream(3, enabled=True, api_type="embedding"),
        }
        plugin.context = types.SimpleNamespace(
            provider_manager=manager,
            register_provider=register_provider,
        )
        plugin._router_instances = []
        plugin._public_provider_configs = []
        plugin._health_results = {}
        plugin.router = None

        provider_id = await plugin._install_router_providers()
        try:
            self.assertEqual(provider_id, router_module.PROVIDER_TYPE)
            self.assertEqual(
                set(manager.inst_map),
                {
                    router_module.PROVIDER_TYPE,
                    f"{router_module.PINNED_PROVIDER_PREFIX}1",
                    f"{router_module.PINNED_PROVIDER_PREFIX}2",
                },
            )
            self.assertEqual(
                [item.slot for item in plugin.router.upstreams],
                [1],
            )
            self.assertEqual(
                [item.slot for item in plugin.router._all_upstreams],
                [1, 2],
            )
            self.assertEqual(
                {row["id"] for row in manager.provider_sources_config},
                {"多 API 路由（自动）", "api-1", "api-2"},
            )
            source_ids = {row["id"] for row in manager.provider_sources_config}
            self.assertTrue(all(
                row["provider_source_id"] in source_ids
                for row in manager.providers_config
            ))
            # 保存后的热重载不能累加重复 source 或改变现有名称。
            await plugin._install_router_providers()
            self.assertEqual(
                {row["id"] for row in manager.provider_sources_config}, source_ids
            )
            self.assertEqual(len(manager.provider_sources_config), 3)
        finally:
            for provider in plugin._router_instances:
                await plugin._close_router_instance(provider)


if __name__ == "__main__":
    unittest.main()

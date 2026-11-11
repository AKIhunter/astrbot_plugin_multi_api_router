import time
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest import mock

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError

import main as router_module


def upstream(slot):
    return {
        "enabled": True,
        "api_type": "chat",
        "name": f"api-{slot}",
        "api_base": "https://example.invalid/v1",
        "api_key": "test-key",
        "model": f"model-{slot}",
        "priority": slot * 10,
        "supports_tools": True,
    }


def status_error(status, message="fixture rejection", headers=None):
    return APIStatusError(
        message,
        response=httpx.Response(
            status,
            headers=headers or {},
            request=httpx.Request("POST", "https://example.invalid/v1"),
        ),
        body=None,
    )


class ErrorClassificationMatrixTests(unittest.TestCase):
    def test_status_code_matrix(self):
        expected = {
            400: ("invalid_request", router_module.DISPOSITION_ROTATE),
            401: (router_module.CATEGORY_AUTH, router_module.DISPOSITION_ROTATE),
            402: (router_module.CATEGORY_QUOTA, router_module.DISPOSITION_ROTATE),
            403: (router_module.CATEGORY_AUTH, router_module.DISPOSITION_ROTATE),
            404: (router_module.CATEGORY_NOT_FOUND, router_module.DISPOSITION_ROTATE),
            408: (router_module.CATEGORY_TIMEOUT, router_module.DISPOSITION_RETRY),
            418: (router_module.CATEGORY_CLIENT, router_module.DISPOSITION_ROTATE),
            422: ("invalid_request", router_module.DISPOSITION_ROTATE),
            429: (router_module.CATEGORY_HTTP_429, router_module.DISPOSITION_RETRY),
            500: (router_module.CATEGORY_HTTP_5XX, router_module.DISPOSITION_RETRY),
            502: (router_module.CATEGORY_HTTP_5XX, router_module.DISPOSITION_RETRY),
            503: (router_module.CATEGORY_HTTP_5XX, router_module.DISPOSITION_RETRY),
            504: (router_module.CATEGORY_HTTP_5XX, router_module.DISPOSITION_RETRY),
        }
        for status, pair in expected.items():
            with self.subTest(status=status):
                self.assertEqual(router_module.classify(status_error(status)), pair)

    def test_exception_type_matrix(self):
        request = httpx.Request("POST", "https://example.invalid/v1")
        cases = (
            (TimeoutError("fixture"), (router_module.CATEGORY_TIMEOUT, router_module.DISPOSITION_RETRY)),
            (APITimeoutError(request=request), (router_module.CATEGORY_TIMEOUT, router_module.DISPOSITION_RETRY)),
            (APIConnectionError(request=request), (router_module.CATEGORY_CONNECTION, router_module.DISPOSITION_RETRY)),
            (router_module.RouterConfigError("fixture"), (router_module.CATEGORY_CONFIG, router_module.DISPOSITION_FAIL)),
            (RuntimeError("fixture"), (router_module.CATEGORY_UNKNOWN, router_module.DISPOSITION_ROTATE)),
            (None, (router_module.CATEGORY_UNKNOWN, router_module.DISPOSITION_ROTATE)),
        )
        for error, pair in cases:
            with self.subTest(error=type(error).__name__):
                self.assertEqual(router_module.classify(error), pair)

    def test_mapping_inputs(self):
        cases = (
            ({"status_code": 503}, (router_module.CATEGORY_HTTP_5XX, router_module.DISPOSITION_RETRY)),
            ({"http_status": 401}, (router_module.CATEGORY_AUTH, router_module.DISPOSITION_ROTATE)),
            ({"error_type": "ConfigurationError"}, (router_module.CATEGORY_CONFIG, router_module.DISPOSITION_FAIL)),
        )
        for payload, pair in cases:
            with self.subTest(payload=payload):
                self.assertEqual(router_module.classify(payload), pair)

    def test_quota_keywords_win_over_429(self):
        cases = (
            status_error(429, '{"error":{"code":"insufficient_quota"}}'),
            status_error(429, "insufficient quota"),
            status_error(429, "账户余额不足，请充值"),
            status_error(429, "额度不足"),
            status_error(402, "fixture rejection"),
            "insufficient_quota",
        )
        for error in cases:
            with self.subTest(error=error):
                self.assertEqual(
                    router_module.classify(error),
                    (router_module.CATEGORY_QUOTA, router_module.DISPOSITION_ROTATE),
                )

    def test_intentional_errors_are_not_counted(self):
        for reason in (
            "message_recalled",
            "context_changed",
            "policy_changed",
            "no_reply",
            "proactive_silence",
        ):
            with self.subTest(reason=reason):
                self.assertEqual(
                    router_module.classify(reason),
                    (router_module.CATEGORY_INTENTIONAL, router_module.DISPOSITION_IGNORE),
                )

    def test_retry_after_parsing(self):
        self.assertEqual(
            router_module._retry_after_seconds(
                status_error(429, headers={"retry-after": "2"})
            ),
            2.0,
        )
        self.assertIsNone(
            router_module._retry_after_seconds(
                status_error(429, headers={"retry-after": "not-a-date"})
            )
        )
        self.assertIsNone(router_module._retry_after_seconds(status_error(429)))
        http_date = format_datetime(
            datetime.now(timezone.utc) + timedelta(seconds=5), usegmt=True
        )
        parsed = router_module._retry_after_seconds(
            status_error(429, headers={"retry-after": http_date})
        )
        self.assertIsNotNone(parsed)
        self.assertGreater(parsed, 2.0)
        self.assertLessEqual(parsed, 6.0)


class RouterRetryPathTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.providers = []

    def build_provider(self):
        provider = router_module.MultiApiRouterProvider(
            {
                "provider_id": router_module.PROVIDER_TYPE,
                "fallback_to_main_model": False,
                "max_attempts": 5,
                "upstream_1": upstream(1),
            }
        )
        self.providers.append(provider)
        return provider

    async def asyncTearDown(self):
        for provider in self.providers:
            for item in provider._all_upstreams:
                await item.client.client.close()

    async def _call_with_failure(self, provider, failure):
        client = provider.upstreams[0].client
        with mock.patch.object(
            client, "_prepare_chat_payload", return_value=({"model": "model-1"}, [])
        ), mock.patch.object(
            client, "_query", side_effect=[failure, "recovered"]
        ) as call, mock.patch.object(
            router_module.asyncio, "sleep", new_callable=mock.AsyncMock
        ) as sleep:
            result = await provider.text_chat(prompt="fixture")
        return result, call, sleep

    async def test_429_and_503_share_the_same_retry_path(self):
        for status in (429, 503):
            with self.subTest(status=status):
                provider = self.build_provider()
                result, call, sleep = await self._call_with_failure(
                    provider, status_error(status)
                )
                self.assertEqual(result, "recovered")
                self.assertEqual(call.await_count, 2)
                sleep.assert_awaited_once_with(1.0)
                self.assertEqual(provider.upstreams[0].last_category, "")

    async def test_quota_429_rotates_without_retry(self):
        provider = self.build_provider()
        client = provider.upstreams[0].client
        failure = status_error(429, '{"error":{"code":"insufficient_quota"}}')
        with mock.patch.object(
            client, "_prepare_chat_payload", return_value=({"model": "model-1"}, [])
        ), mock.patch.object(client, "_query", side_effect=failure) as call, mock.patch.object(
            router_module.asyncio, "sleep", new_callable=mock.AsyncMock
        ) as sleep:
            with self.assertRaisesRegex(RuntimeError, "路由失败"):
                await provider.text_chat(prompt="fixture")
            call.assert_awaited_once()
            sleep.assert_not_awaited()
        self.assertEqual(
            provider.upstreams[0].last_category, router_module.CATEGORY_QUOTA
        )
        self.assertEqual(
            provider.upstreams[0].last_disposition, router_module.DISPOSITION_ROTATE
        )

    async def test_retry_after_overrides_fixed_interval(self):
        provider = self.build_provider()
        result, call, sleep = await self._call_with_failure(
            provider, status_error(429, headers={"retry-after": "2"})
        )
        self.assertEqual(result, "recovered")
        self.assertEqual(call.await_count, 2)
        sleep.assert_awaited_once_with(2.0)

    async def test_retry_after_respects_total_route_budget(self):
        provider = self.build_provider()
        client = provider.upstreams[0].client
        failure = status_error(429, headers={"retry-after": "120"})
        with mock.patch.object(
            client, "_prepare_chat_payload", return_value=({"model": "model-1"}, [])
        ), mock.patch.object(client, "_query", side_effect=failure) as call, mock.patch.object(
            router_module.asyncio, "sleep", new_callable=mock.AsyncMock
        ) as sleep:
            with self.assertRaisesRegex(RuntimeError, "路由失败"):
                await provider.text_chat(prompt="fixture")
            call.assert_awaited_once()
            sleep.assert_not_awaited()

    async def test_health_skip_uses_disposition_not_status_code(self):
        provider = self.build_provider()
        checked_at = int(time.time())
        provider.health_lookup = lambda _slot: {
            "ok": False,
            "checked_at": checked_at,
            "http_status": 500,
            "status": "fixture",
            "disposition": router_module.DISPOSITION_ROTATE,
        }
        with mock.patch.object(provider, "_call_once") as call:
            with self.assertRaisesRegex(RuntimeError, "没有可调用的上游"):
                await provider.text_chat(prompt="fixture")
            call.assert_not_awaited()
        provider.health_lookup = lambda _slot: {
            "ok": False,
            "checked_at": checked_at,
            "http_status": 401,
            "status": "fixture",
            "disposition": router_module.DISPOSITION_RETRY,
        }
        with mock.patch.object(provider, "_call_once", return_value="ok") as call:
            self.assertEqual(await provider.text_chat(prompt="fixture"), "ok")
            call.assert_awaited_once()


class HealthSuccessRecordingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.providers = []
        self.health = {}

    def build_provider(self, *, pinned=False):
        config = {
            "provider_id": router_module.PROVIDER_TYPE,
            "fallback_to_main_model": False,
            "max_attempts": 5,
            "upstream_1": upstream(1),
        }
        if pinned:
            config["upstream_slot"] = 1
        provider = router_module.MultiApiRouterProvider(config)
        provider.health_lookup = self.health.get
        provider.health_recorder = lambda slot, result: self.health.__setitem__(
            slot, result
        )
        self.providers.append(provider)
        return provider

    async def asyncTearDown(self):
        for provider in self.providers:
            for item in provider._all_upstreams:
                await item.client.client.close()

    @staticmethod
    def skip_result(http_status=402):
        return {
            "ok": False,
            "checked_at": int(time.time()),
            "http_status": http_status,
            "status": "额度不足或欠费",
            "disposition": router_module.DISPOSITION_ROTATE,
        }

    async def test_successful_call_records_healthy_result(self):
        provider = self.build_provider()
        with mock.patch.object(provider, "_call_once", return_value="ok"):
            self.assertEqual(await provider.text_chat(prompt="fixture"), "ok")
        record = self.health[1]
        self.assertTrue(record["ok"])
        self.assertEqual(record["http_status"], 200)
        self.assertEqual(record["status"], "正常")
        self.assertEqual(record["disposition"], router_module.DISPOSITION_IGNORE)
        self.assertEqual(record["api_type"], "chat")
        self.assertEqual(record["protocol"], "Chat Completions")
        self.assertIsNone(record["error_type"])
        self.assertIn("checked_at", record)
        self.assertIn("latency_ms", record)
        self.assertEqual(provider._health_skip_reason(provider.upstreams[0]), "")

    async def test_pinned_success_unblocks_overall_router(self):
        self.health[1] = self.skip_result()
        overall = self.build_provider()
        with mock.patch.object(overall, "_call_once") as overall_call:
            with self.assertRaisesRegex(RuntimeError, "没有可调用的上游"):
                await overall.text_chat(prompt="fixture")
            overall_call.assert_not_awaited()

        pinned = self.build_provider(pinned=True)
        with mock.patch.object(
            pinned, "_call_once", return_value="pinned ok"
        ) as pinned_call:
            self.assertEqual(await pinned.text_chat(prompt="fixture"), "pinned ok")
            pinned_call.assert_awaited_once()
        self.assertTrue(self.health[1]["ok"])
        self.assertEqual(
            self.health[1]["disposition"], router_module.DISPOSITION_IGNORE
        )

        with mock.patch.object(
            overall, "_call_once", return_value="overall ok"
        ) as overall_call:
            self.assertEqual(await overall.text_chat(prompt="fixture"), "overall ok")
            overall_call.assert_awaited_once()

    async def test_other_slots_health_is_untouched(self):
        untouched = self.skip_result(http_status=401)
        self.health[2] = untouched
        provider = self.build_provider()
        with mock.patch.object(provider, "_call_once", return_value="ok"):
            self.assertEqual(await provider.text_chat(prompt="fixture"), "ok")
        self.assertIs(self.health[2], untouched)

    async def test_pinned_path_still_bypasses_health_skip(self):
        self.health[1] = self.skip_result()
        pinned = self.build_provider(pinned=True)
        self.assertEqual(pinned._health_skip_reason(pinned.upstreams[0]), "")
        with mock.patch.object(pinned, "_call_once", return_value="ok") as call:
            self.assertEqual(await pinned.text_chat(prompt="fixture"), "ok")
            call.assert_awaited_once()


class BareRateLimitError(Exception):
    """模拟拿不到响应体、也拿不到响应头的 429。"""

    status_code = 429


class RateLimitLogTests(unittest.IsolatedAsyncioTestCase):
    """[RateLimit] 留痕：只验证日志内容，同时复核重试次数与等待不变。"""

    def setUp(self):
        self.providers = []
        patcher = mock.patch.object(router_module, "logger")
        self.logger = patcher.start()
        self.addCleanup(patcher.stop)

    def build_provider(self):
        provider = router_module.MultiApiRouterProvider(
            {
                "provider_id": router_module.PROVIDER_TYPE,
                "fallback_to_main_model": False,
                "max_attempts": 5,
                "upstream_1": upstream(1),
            }
        )
        self.providers.append(provider)
        return provider

    async def asyncTearDown(self):
        for provider in self.providers:
            for item in provider._all_upstreams:
                await item.client.client.close()

    def rate_limit_lines(self):
        return [
            str(call.args[0])
            for call in self.logger.warning.call_args_list
            if call.args and str(call.args[0]).startswith("[RateLimit]")
        ]

    async def test_429_retry_path_logs_received_resolved_and_route(self):
        provider = self.build_provider()
        client = provider.upstreams[0].client
        failure = APIStatusError(
            "fixture rejection",
            response=httpx.Response(
                429,
                headers={"retry-after": "2"},
                request=httpx.Request("POST", "https://example.invalid/v1"),
            ),
            body={
                "error": {
                    "type": "rate_limit_error",
                    "code": "rate_limit_exceeded",
                    "message": "too many requests\nplease retry",
                }
            },
        )
        with mock.patch.object(
            client, "get_model", return_value="model-1"
        ), mock.patch.object(
            client, "_prepare_chat_payload", return_value=({"model": "model-1"}, [])
        ), mock.patch.object(
            client, "_query", side_effect=[failure, "recovered"]
        ) as call, mock.patch.object(
            router_module.asyncio, "sleep", new_callable=mock.AsyncMock
        ) as sleep:
            result = await provider.text_chat(prompt="fixture")

        # 行为不变：仍是一次重试、仍按 Retry-After 等 2 秒。
        self.assertEqual(result, "recovered")
        self.assertEqual(call.await_count, 2)
        sleep.assert_awaited_once_with(2.0)

        lines = self.rate_limit_lines()
        self.assertEqual(
            [line.split()[1] for line in lines],
            ["event=received", "event=resolved", "event=route"],
        )
        received, resolved, route = lines
        self.assertRegex(
            received,
            r"^\[RateLimit\] event=received ts=\S+ slot=#1 upstream=\"api-1\""
            r" model=\"model-1\" http=429 attempt=1/2"
            r" error_type=\"rate_limit_error\""
            r" error_code=\"rate_limit_exceeded\""
            r" error_message=\"too many requests please retry\""
            r" retry_after_raw=\"2\" retry_after_s=2.00 quota_hit=no$",
        )
        self.assertRegex(
            resolved,
            r"^\[RateLimit\] event=resolved ts=\S+ slot=#1 upstream=\"api-1\""
            r" model=\"model-1\" http=429 retried=yes wait_s=2.00"
            r" retry_result=success next=return_to_caller"
            r" health=unchanged_by_rate_limit$",
        )
        self.assertRegex(
            route,
            r"^\[RateLimit\] event=route ts=\S+ attempts=1"
            r" outcome=served_by final=#1$",
        )
        self.assertNotIn("test-key", " ".join(lines))
        self.assertNotIn("\n", " ".join(lines))

    async def test_quota_429_logs_no_retry_and_rotate(self):
        provider = self.build_provider()
        client = provider.upstreams[0].client
        failure = APIStatusError(
            "fixture rejection",
            response=httpx.Response(
                429,
                request=httpx.Request("POST", "https://example.invalid/v1"),
            ),
            body={"error": {"code": "insufficient_quota", "message": "quota exceeded"}},
        )
        with mock.patch.object(
            client, "get_model", return_value="model-1"
        ), mock.patch.object(
            client, "_prepare_chat_payload", return_value=({"model": "model-1"}, [])
        ), mock.patch.object(
            client, "_query", side_effect=failure
        ) as call, mock.patch.object(
            router_module.asyncio, "sleep", new_callable=mock.AsyncMock
        ) as sleep:
            with self.assertRaisesRegex(RuntimeError, "路由失败"):
                await provider.text_chat(prompt="fixture")

        # 行为不变：配额 429 不重试、不等待。
        call.assert_awaited_once()
        sleep.assert_not_awaited()

        received, resolved, route = self.rate_limit_lines()
        self.assertIn("quota_hit=yes", received)
        self.assertIn('retry_after_raw="无"', received)
        self.assertIn("retry_after_s=无", received)
        self.assertIn('error_type="不可得"', received)
        self.assertIn('error_code="insufficient_quota"', received)
        self.assertIn("retried=no", resolved)
        self.assertIn("wait_s=0.00", resolved)
        self.assertIn("retry_result=not_retried", resolved)
        self.assertIn("next=rotate", resolved)
        self.assertIn("health=unchanged_by_rate_limit", resolved)
        self.assertIn("outcome=failed", route)
        self.assertIn("final=-", route)

    async def test_429_without_response_headers_is_marked_unavailable(self):
        provider = self.build_provider()
        client = provider.upstreams[0].client
        with mock.patch.object(
            client, "get_model", return_value="model-1"
        ), mock.patch.object(
            client, "_prepare_chat_payload", return_value=({"model": "model-1"}, [])
        ), mock.patch.object(
            client, "_query", side_effect=[BareRateLimitError("raw 429"), "recovered"]
        ) as call, mock.patch.object(
            router_module.asyncio, "sleep", new_callable=mock.AsyncMock
        ) as sleep:
            result = await provider.text_chat(prompt="fixture")

        # 行为不变：无响应头时仍按固定间隔重试一次。
        self.assertEqual(result, "recovered")
        self.assertEqual(call.await_count, 2)
        sleep.assert_awaited_once_with(1.0)

        received = self.rate_limit_lines()[0]
        self.assertIn("http=429", received)
        self.assertIn('retry_after_raw="响应头不可得"', received)
        self.assertIn("retry_after_s=响应头不可得", received)
        self.assertIn('error_type="不可得"', received)
        self.assertIn('error_code="不可得"', received)
        self.assertIn('error_message="不可得"', received)


if __name__ == "__main__":
    unittest.main()

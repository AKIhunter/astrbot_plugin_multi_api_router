import asyncio
import types
import unittest

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


class ProviderRegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_route_disabled_chat_api_keeps_direct_provider(self):
        manager = types.SimpleNamespace(
            inst_map={},
            provider_insts=[],
            providers_config=[],
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
        finally:
            for provider in plugin._router_instances:
                await plugin._close_router_instance(provider)


if __name__ == "__main__":
    unittest.main()

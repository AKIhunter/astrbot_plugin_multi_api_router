"""保存失败、空槽复用和宿主持久选择的离线回归；不调用真实模型。"""

import asyncio
import json
import tempfile
import types
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import main as router
from astrbot.core.config.astrbot_config import AstrBotConfig
from test_router_features import upstream


class Config(dict):
    def __init__(self, data):
        super().__init__(deepcopy(data))
        self.disk = deepcopy(data)
        self.fail = False
        self.saves = 0

    def save_config(self):
        self.saves += 1
        if self.fail:
            raise OSError("fixture disk failure")
        self.disk = deepcopy(dict(self))


class SaveSafetyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.tmp)
        self.directory = self.root / "config"
        self.directory.mkdir()
        self.main_path = self.root / "cmd_config.json"
        self.main_path.write_text("{}")
        self.preferences = self.enterContext(patch.object(
            router.sp, "range_get_async", new=AsyncMock(return_value=[]),
        ))
        self.enterContext(patch.object(router, "logger", Mock()))
        self.enterContext(patch.object(router, "json_response", side_effect=lambda d: {
            "status": 200, "data": d,
        }))
        self.enterContext(patch.object(router, "error_response",
                                      side_effect=lambda message, status_code=400: {
                                          "status": status_code, "message": message,
                                      }))
        self.plugins = []

    def plugin(self, data=None, *, real_reload=False):
        p = object.__new__(router.MultiApiRouterPlugin)
        p.config = Config(data if data is not None else {
            "enabled": True, "fallback_to_main_model": False,
            "upstream_1": upstream(1), "upstream_2": upstream(2),
        })
        p.router = None
        p._config_dir = self.directory
        p._main_config_path = self.main_path
        p._health_results = {1: {"ok": True}, 2: {"ok": True}}
        p._health_inflight = set()
        p._save_lock = asyncio.Lock()
        p._binding_lock = asyncio.Lock()
        p._binding_state = {"version": 1, "bindings": {}}
        p._binding_messages = []
        p._state_path = self.root / f"bindings-{len(self.plugins)}.json"
        p._find_plugin_metadata = lambda name: None
        p._router_instances = []
        p._public_provider_configs = []
        manager = types.SimpleNamespace(
            inst_map={}, provider_insts=[], providers_config=[],
            provider_sources_config=[],
        )
        p.context = types.SimpleNamespace(
            provider_manager=manager,
            register_provider=lambda inst: manager.provider_insts.append(inst),
        )
        if not real_reload:
            p._reload_router = AsyncMock()
        self.plugins.append(p)
        return p

    async def asyncTearDown(self):
        for p in self.plugins:
            instances = list(p._router_instances)
            if p.router is not None and p.router not in instances:
                instances.append(p.router)
            for inst in instances:
                await p._close_router_instance(inst)

    async def save(self, p, payload):
        with patch.object(router, "request", types.SimpleNamespace(
            json=AsyncMock(return_value=payload),
        )):
            return await p.api_save_upstreams()

    def reference(self, slot=6):
        (self.directory / "abconf_fixture.json").write_text(json.dumps({
            "model": {"provider_id": f"multi_api_router:upstream_{slot}"},
        }))

    async def test_schema_restored_empty_slot_is_blocked_in_api_and_save(self):
        self.reference()
        schema = json.loads((Path(router.__file__).parent / "_conf_schema.json").read_text())
        path = self.directory / "router-fixture.json"
        path.write_text("{}")
        restored = AstrBotConfig(str(path), schema={"upstream_6": schema["upstream_6"]})
        p = self.plugin(dict(restored))
        before = deepcopy(dict(p.config))
        for method in (p.api_upstreams_config, p.api_upstreams):
            data = (await method())["data"]
            self.assertTrue(data["reference_check_complete"])
            self.assertIn(6, data["blocked_slots"])
        result = await self.save(p, {"upstream_6": upstream(6)})
        self.assertEqual(result["status"], 400)
        self.assertEqual(p.config, before)
        self.assertEqual(p.config.saves, 0)

    async def test_full_disabled_pinned_slot_is_an_edit_not_reuse(self):
        self.reference()
        p = self.plugin({"upstream_6": upstream(6, enabled=False)})
        data = (await p.api_upstreams_config())["data"]
        self.assertNotIn(6, data["blocked_slots"])
        payload = {**upstream(6, enabled=False), "name": "new display name"}
        self.assertEqual((await self.save(p, {"upstream_6": payload}))["status"], 200)

    async def test_unknown_references_block_reuse_but_allow_existing_edit(self):
        self.reference()
        reference = self.directory / "abconf_fixture.json"
        for failure in ("missing_directory", "permission", "malformed_json", "missing_main"):
            with self.subTest(failure=failure):
                p = self.plugin()
                if failure == "missing_directory":
                    p._config_dir = self.root / "missing"
                if failure == "missing_main":
                    p._main_config_path = self.root / "missing-main.json"
                original = Path.read_text

                def read(path, *args, **kwargs):
                    if path == reference and failure == "permission":
                        raise PermissionError("fixture unreadable")
                    if path == reference and failure == "malformed_json":
                        return "{"
                    return original(path, *args, **kwargs)

                with patch.object(Path, "read_text", read):
                    data = (await p.api_upstreams_config())["data"]
                    self.assertFalse(data["reference_check_complete"])
                    self.assertIn(6, data["blocked_slots"])
                    result = await self.save(p, {"upstream_6": upstream(6)})
                    self.assertEqual(result["status"], 400)
                    self.assertEqual(p.config.saves, 0)
                    result = await self.save(p, {"upstream_1": upstream(1)})
                    self.assertEqual(result["status"], 200)

    async def test_main_config_reference_blocks_reuse(self):
        self.main_path.write_text(json.dumps({
            "agent_runner": {"config": {"model": {
                "provider_id": "multi_api_router:upstream_6",
            }}},
        }))
        p = self.plugin()
        result = await self.save(p, {"upstream_6": upstream(6)})
        self.assertEqual(result["status"], 400)
        self.assertIn("cmd_config.json", result["message"])
        self.assertEqual(p.config.saves, 0)

    async def test_session_and_global_preferences_block_reuse_without_exposing_umo(self):
        for scope in ("umo", "global"):
            with self.subTest(scope=scope):
                async def values(actual_scope, scope_id, key):
                    return [types.SimpleNamespace(
                        value={"val": "multi_api_router:upstream_6"},
                        scope_id="private-session-id",
                    )] if actual_scope == scope else []
                self.preferences.side_effect = values
                p = self.plugin()
                result = await self.save(p, {"upstream_6": upstream(6)})
                self.assertEqual(result["status"], 400)
                self.assertIn("宿主偏好", result["message"])
                self.assertNotIn("private-session-id", result["message"])
        self.assertTrue(all(
            call.args[-1] in {"provider_perf_chat_completion", "curr_provider"}
            for call in self.preferences.await_args_list
        ))

    async def test_preference_failure_is_incomplete_not_empty(self):
        self.preferences.side_effect = OSError("fixture preference unavailable")
        p = self.plugin()
        data = (await p.api_upstreams())["data"]
        self.assertFalse(data["reference_check_complete"])
        self.assertEqual((await self.save(p, {"upstream_6": upstream(6)}))["status"], 400)
        self.assertEqual(p.config.saves, 0)

    async def test_non_object_reference_file_is_incomplete(self):
        for value in ([], None, "fixture"):
            with self.subTest(value=value):
                (self.directory / "abconf_fixture.json").write_text(json.dumps(value))
                p = self.plugin()
                self.assertIsNone(await p._collect_slot_references())
                self.assertEqual((await self.save(p, {"upstream_6": upstream(6)}))["status"], 400)
                self.assertEqual(p.config.saves, 0)

    async def test_host_provider_definitions_are_not_consumer_references(self):
        self.main_path.write_text(json.dumps({
            "provider": [{"id": "multi_api_router:upstream_6"}],
            "provider_sources": [{"id": "multi_api_router:upstream_6"}],
        }))
        p = self.plugin()
        self.assertEqual(await p._collect_slot_references(), {})
        self.assertEqual((await self.save(p, {"upstream_6": upstream(6)}))["status"], 200)

    async def test_all_payloads_validate_before_mutation(self):
        for payload in (
            {"delete_slots": [1, 21]},
            {"delete_slots": [1], "global": {"unknown": 1}},
            {"global": {"max_input_chars": 1001}, "upstream_2": {"unknown": 1}},
            {"upstream_1": {**upstream(1), "name": "new"}, "upstream_2": []},
        ):
            with self.subTest(payload=payload):
                p = self.plugin()
                before = deepcopy(dict(p.config))
                health = deepcopy(p._health_results)
                result = await self.save(p, payload)
                self.assertEqual(result["status"], 400)
                self.assertEqual(p.config, before)
                self.assertEqual(p.config.disk, before)
                self.assertEqual(p._health_results, health)
                self.assertEqual(p.config.saves, 0)
                p._reload_router.assert_not_awaited()
                self.assertEqual((await self.save(p, {"global": {"max_attempts": 4}}))["status"], 200)
                self.assertEqual(p.config.disk["upstream_1"], before["upstream_1"])

    async def test_disk_save_failure_keeps_memory_health_and_runtime(self):
        p = self.plugin(real_reload=True)
        await p._install_router_providers()
        old_router = p.router
        before = deepcopy(dict(p.config))
        health = deepcopy(p._health_results)
        p.config.fail = True
        result = await self.save(p, {"delete_slots": [1]})
        self.assertEqual(result["status"], 500)
        self.assertEqual(p.config, before)
        self.assertEqual(p.config.disk, before)
        self.assertEqual(p._health_results, health)
        self.assertIs(p.router, old_router)

    async def test_reload_failure_restores_disk_and_callable_provider(self):
        p = self.plugin(real_reload=True)
        await p._install_router_providers()
        before = deepcopy(dict(p.config))
        ids = set(p.context.provider_manager.inst_map)
        original = p._reload_router

        async def fail_after_replacement():
            await p._install_router_providers()
            raise RuntimeError("fixture reload failure")

        p._reload_router = fail_after_replacement
        result = await self.save(p, {"delete_slots": [1]})
        self.assertEqual(result["status"], 500)
        self.assertEqual(p.config.disk, before)
        self.assertEqual(p.config, before)
        self.assertEqual(set(p.context.provider_manager.inst_map), ids)
        pinned = p.context.provider_manager.inst_map["multi_api_router:upstream_1"]
        with patch.object(pinned, "_call_once", new=AsyncMock(return_value="ok")):
            self.assertEqual(await pinned.text_chat(prompt="fixture"), "ok")
        p._reload_router = original

    async def test_registration_failure_preserves_old_instances(self):
        p = self.plugin(real_reload=True)
        await p._install_router_providers()
        old = p.router
        manager = p.context.provider_manager
        ids = dict(manager.inst_map)
        with patch.object(p.context, "register_provider", side_effect=RuntimeError("fixture")):
            with self.assertRaises(RuntimeError):
                await p._install_router_providers()
        self.assertIs(p.router, old)
        self.assertEqual(manager.inst_map, ids)
        self.assertFalse(old._all_upstreams[0].client.client.is_closed())

    def binding(self, p, *, fail=False):
        target = Config({"SUMMARY_PROVIDER_ID": "multi_api_router"})
        target.fail = fail
        metadata = types.SimpleNamespace(name="astrbot_plugin_bilibili_ai_bot", config=target)
        p._find_plugin_metadata = lambda name: metadata if name == metadata.name else None
        p._binding_state["bindings"]["fixture"] = {
            "plugin": metadata.name, "path": ["SUMMARY_PROVIDER_ID"],
            "original": "old-model", "applied": "multi_api_router",
        }
        p._save_binding_state()
        return target

    async def test_binding_failure_rolls_back_parent_and_retains_retry_record(self):
        p = self.plugin(real_reload=True)
        await p._install_router_providers()
        target = self.binding(p, fail=True)
        state = deepcopy(p._binding_state)
        result = await self.save(p, {"global": {"enabled": False}})
        self.assertEqual(result["status"], 500)
        self.assertTrue(p.config.disk["enabled"])
        self.assertIn("multi_api_router", p.context.provider_manager.inst_map)
        self.assertEqual(target, target.disk)
        self.assertEqual(p._binding_state, state)
        self.assertEqual(json.loads(p._state_path.read_text()), state)
        target.fail = False
        self.assertEqual((await self.save(p, {"global": {"enabled": False}}))["status"], 200)
        self.assertEqual(target.disk["SUMMARY_PROVIDER_ID"], "old-model")
        self.assertFalse(p._binding_state["bindings"])
        self.assertNotIn("multi_api_router", p.context.provider_manager.inst_map)
        self.assertIn("multi_api_router:upstream_1", p.context.provider_manager.inst_map)

    async def test_binding_state_write_failure_restores_target_disk(self):
        p = self.plugin(real_reload=True)
        target = self.binding(p)
        state = deepcopy(p._binding_state)
        with patch.object(p, "_save_binding_state", side_effect=OSError("fixture")):
            with self.assertRaises(OSError):
                await p._restore_auto_bindings()
        self.assertEqual(target.disk["SUMMARY_PROVIDER_ID"], "multi_api_router")
        self.assertEqual(target, target.disk)
        self.assertEqual(p._binding_state, state)

    async def test_second_binding_failure_restores_first_and_keeps_all_records(self):
        p = self.plugin()
        first = self.binding(p)
        second = Config({"gate_provider_id": "multi_api_router"})
        second.fail = True
        metadata = {
            "astrbot_plugin_bilibili_ai_bot": types.SimpleNamespace(config=first),
            "astrbot_plugin_memewiki": types.SimpleNamespace(config=second),
        }
        p._find_plugin_metadata = metadata.get
        p._binding_state["bindings"]["second"] = {
            "plugin": "astrbot_plugin_memewiki", "path": ["gate_provider_id"],
            "original": "other-model", "applied": "multi_api_router",
        }
        p._save_binding_state()
        state = deepcopy(p._binding_state)
        with self.assertRaises(OSError):
            await p._restore_auto_bindings()
        self.assertEqual(first.disk["SUMMARY_PROVIDER_ID"], "multi_api_router")
        self.assertEqual(second.disk["gate_provider_id"], "multi_api_router")
        self.assertEqual(first, first.disk)
        self.assertEqual(second, second.disk)
        self.assertEqual(p._binding_state, state)
        self.assertEqual(json.loads(p._state_path.read_text()), state)
        second.fail = False
        await p._restore_auto_bindings()
        self.assertEqual(first.disk["SUMMARY_PROVIDER_ID"], "old-model")
        self.assertEqual(second.disk["gate_provider_id"], "other-model")
        self.assertFalse(p._binding_state["bindings"])

    async def test_async_target_save_false_is_failure_and_remains_retryable(self):
        p = self.plugin(real_reload=True)
        await p._install_router_providers()
        target = self.binding(p)
        target.save_config_async = AsyncMock(return_value=False)
        state = deepcopy(p._binding_state)
        result = await self.save(p, {"global": {"enabled": False}})
        self.assertEqual(result["status"], 500)
        self.assertTrue(p.config.disk["enabled"])
        self.assertIn("multi_api_router", p.context.provider_manager.inst_map)
        self.assertEqual(target, target.disk)
        self.assertEqual(p._binding_state, state)
        self.assertEqual(target.saves, 0)
        target.save_config_async.assert_awaited_once()

    async def test_binding_rollback_disk_failure_is_reported_incomplete(self):
        p = self.plugin(real_reload=True)
        await p._install_router_providers()
        first = self.binding(p)
        second = Config({"gate_provider_id": "multi_api_router"})
        second.fail = True
        metadata = {
            "astrbot_plugin_bilibili_ai_bot": types.SimpleNamespace(config=first),
            "astrbot_plugin_memewiki": types.SimpleNamespace(config=second),
        }
        p._find_plugin_metadata = metadata.get
        p._binding_state["bindings"]["second"] = {
            "plugin": "astrbot_plugin_memewiki", "path": ["gate_provider_id"],
            "original": "other-model", "applied": "multi_api_router",
        }
        p._save_binding_state()
        state = deepcopy(p._binding_state)
        original_save = first.save_config
        calls = []

        def save_first():
            calls.append(dict(first))
            if len(calls) == 2:
                raise OSError("fixture rollback disk failure")
            original_save()

        with patch.object(first, "save_config", side_effect=save_first):
            result = await self.save(p, {"global": {"enabled": False}})
        self.assertEqual(result["status"], 500)
        self.assertTrue(p.config.disk["enabled"])
        self.assertIn("multi_api_router", p.context.provider_manager.inst_map)
        self.assertEqual(p._binding_state, state)
        self.assertEqual(json.loads(p._state_path.read_text()), state)
        warnings = [
            call.args[0] % call.args[1:] for call in router.logger.warning.call_args_list
        ]
        self.assertTrue(any("回滚未完成" in message for message in warnings))
        self.assertFalse(any("回滚完成。" in message for message in warnings))
        second.fail = False
        await p._restore_auto_bindings()
        self.assertEqual(first.disk["SUMMARY_PROVIDER_ID"], "old-model")
        self.assertEqual(second.disk["gate_provider_id"], "other-model")
        self.assertFalse(p._binding_state["bindings"])

    async def test_cancelled_save_restores_disk_health_and_callable_provider(self):
        p = self.plugin(real_reload=True)
        await p._install_router_providers()
        before = deepcopy(dict(p.config))
        health = deepcopy(p._health_results)
        entered = asyncio.Event()
        never = asyncio.Event()

        async def reload():
            await p._install_router_providers()
            entered.set()
            await never.wait()

        p._reload_router = reload
        task = asyncio.create_task(self.save(p, {"delete_slots": [1]}))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(p.config, before)
        self.assertEqual(p.config.disk, before)
        self.assertEqual(p._health_results, health)
        self.assertFalse(p._save_lock.locked())
        pinned = p.context.provider_manager.inst_map["multi_api_router:upstream_1"]
        with patch.object(pinned, "_call_once", new=AsyncMock(return_value="ok")):
            self.assertEqual(await pinned.text_chat(prompt="fixture"), "ok")

    async def test_saves_are_serial_and_failed_update_does_not_enter_later_save(self):
        p = self.plugin()
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def reload():
            calls.append(p.config["max_attempts"])
            if len(calls) == 1:
                entered.set()
                await release.wait()
                raise RuntimeError("fixture first failed")

        p._reload_router = reload
        p.config["max_attempts"] = 3
        p.config.save_config()
        with patch.object(router, "request", types.SimpleNamespace(
            json=AsyncMock(side_effect=[
                {"global": {"max_attempts": 4}},
                {"global": {"max_input_chars": 1001}},
            ]),
        )):
            first = asyncio.create_task(p.api_save_upstreams())
            await entered.wait()
            second = asyncio.create_task(p.api_save_upstreams())
            await asyncio.sleep(0)
            self.assertEqual(calls, [4])
            release.set()
            one, two = await asyncio.gather(first, second)
        self.assertEqual(one["status"], 500)
        self.assertEqual(two["status"], 200)
        self.assertEqual(p.config.disk["max_attempts"], 3)
        self.assertEqual(p.config.disk["max_input_chars"], 1001)

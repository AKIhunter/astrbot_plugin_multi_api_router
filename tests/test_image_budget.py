import base64
import copy
import json
import types
import unittest

from image_history import compact_contexts, old_images, reference_id, TOOL_NAME
from request_budget import measure_request


def image(data=b"image"):
    return {"type": "image_url", "image_url": {
        "url": "data:image/png;base64," + base64.b64encode(data).decode()
    }}


def user(*parts):
    return {"role": "user", "content": list(parts)}


class ImageBudgetTests(unittest.TestCase):
    def test_native_and_dict_have_identical_accounting(self):
        from astrbot.core.agent.message import Message
        raw = user({"type": "text", "text": "这是文字"}, image())
        self.assertEqual(measure_request(contexts=[raw]),
                         measure_request(contexts=[Message.model_validate(raw)]))

    def test_base64_image_is_separate_from_text(self):
        small = measure_request(contexts=[user(image(b"1"))])
        large = measure_request(contexts=[user(image(b"1" * 500000))])
        self.assertEqual(small.text_chars, large.text_chars)
        self.assertEqual(large.image_bytes, 500000)
        self.assertEqual(large.image_count, 1)

    def test_base64_in_plain_text_is_counted(self):
        text = image(b"1" * 1000)["image_url"]["url"]
        self.assertGreaterEqual(measure_request(prompt=text).text_chars, len(text))

    def test_forged_image_inside_tool_schema_is_counted(self):
        forged = user(image(b"1" * 1000))
        tools = types.SimpleNamespace(openai_schema=lambda: forged)
        result = measure_request(func_tool=tools)
        self.assertEqual(result.image_count, 0)
        self.assertGreater(result.text_chars, 1000)

    def test_large_non_url_image_field_is_counted(self):
        raw = image()
        raw["image_url"]["detail"] = "x" * 20000
        self.assertGreaterEqual(measure_request(contexts=[user(raw)]).text_chars, 20000)

    def test_tools_and_extra_content_are_counted(self):
        tools = types.SimpleNamespace(openai_schema=lambda: [{"description": "x" * 5000}])
        self.assertGreaterEqual(measure_request(func_tool=tools).text_chars, 5000)
        self.assertGreaterEqual(measure_request(extra_user_content_parts=[
            {"type": "text", "text": "x" * 1000}]).text_chars, 1000)

    def test_assembled_extra_content_is_not_counted_twice(self):
        parts = [{"type": "text", "text": "x" * 1000}, image()]
        once = measure_request(contexts=[user(*parts)])
        native = measure_request(contexts=[user(*parts)], extra_user_content_parts=parts,
                                 current_in_contexts=True)
        self.assertEqual(once, native)

    def test_current_image_urls_are_limited(self):
        with self.assertRaisesRegex(ValueError, "单张"):
            measure_request(image_urls=[image(b"12345")["image_url"]["url"]], max_image_bytes=4)

    def test_total_image_bytes_are_limited(self):
        with self.assertRaisesRegex(ValueError, "总体积"):
            measure_request(contexts=[user(image(), image())], max_total_image_bytes=9)

    def test_duplicate_images_still_count_toward_transport_budget(self):
        result = measure_request(contexts=[user(image(), image())])
        self.assertEqual((result.image_count, result.image_bytes), (2, 10))

    def test_remote_image_count_is_limited(self):
        with self.assertRaisesRegex(ValueError, "数量"):
            measure_request(image_urls=["https://example.invalid/a"] * 3, max_image_count=2)

    def test_remote_urls_remain_text_budgeted(self):
        url = "https://example.invalid/" + "a" * 2000
        self.assertEqual(measure_request(image_urls=[url]).text_chars, len(url))

    def test_invalid_images_rejected_without_body_in_error(self):
        for url in ("data:image/png;base64,?SECRET", "data:text/plain;base64,eA==",
                    "data:image/png;base64,", "data:image/png,x", "javascript:secret"):
            with self.subTest(url=url):
                with self.assertRaises(ValueError) as error:
                    measure_request(image_urls=[url])
                self.assertNotIn("SECRET", str(error.exception))

    def test_malformed_image_block_rejected(self):
        with self.assertRaisesRegex(ValueError, "格式"):
            measure_request(contexts=[user({"type": "image_url", "image_url": "bad"})])


class HistoryImageTests(unittest.TestCase):
    def history(self):
        return [user({"type": "text", "text": f"轮次{i}"}, image(bytes([i]))) for i in range(5)]

    def test_original_history_is_immutable(self):
        original = self.history()
        snapshot = copy.deepcopy(original)
        result, stats = compact_contexts(original, current_in_contexts=True)
        self.assertEqual(original, snapshot)
        self.assertEqual(stats["compacted"], 2)
        self.assertEqual(result[0]["content"][0], original[0]["content"][0])
        self.assertEqual(result[-1], original[-1])

    def test_two_historical_user_turns_and_current_survive(self):
        original = self.history()
        result, _ = compact_contexts(original, current_in_contexts=True)
        self.assertEqual(result[-3:], original[-3:])
        self.assertEqual(measure_request(contexts=result).image_count, 3)

    def test_prompt_separate_keeps_only_two_historical_turns(self):
        result, stats = compact_contexts(self.history())
        self.assertEqual(stats["compacted"], 3)
        self.assertEqual(measure_request(contexts=result).image_count, 2)

    def test_recent_history_max_four_and_current_not_trimmed(self):
        original = [user(*[image(bytes([i])) for i in range(10)]), user(*[image()] * 8)]
        result, _ = compact_contexts(original, current_in_contexts=True)
        self.assertEqual(measure_request(contexts=result).image_count, 12)
        self.assertEqual(result[-1], original[-1])

    def test_current_tool_images_preserved(self):
        original = [user(image()), user(image()), user(image()),
                    {"role": "tool", "content": [image()] * 7}]
        result, _ = compact_contexts(original, current_in_contexts=True)
        self.assertEqual(result[-1], original[-1])

    def test_unknown_caption_explicit_and_ref_only(self):
        result, _ = compact_contexts(self.history())
        text = result[0]["content"][1]["text"]
        self.assertIn("暂无逐图摘要", text)
        self.assertNotIn("data:", text)
        self.assertIn(TOOL_NAME, text)

    def test_saved_per_image_tool_summary_reused(self):
        original = self.history()
        key = reference_id(original[0]["content"][1]["image_url"]["url"])
        original.append({"role": "tool", "content": json.dumps({
            "kind": TOOL_NAME, "image_id": key, "summary": "图中有一只猫"
        })})
        result, stats = compact_contexts(original)
        self.assertEqual(stats["summarized"], 1)
        self.assertIn("图中有一只猫", result[0]["content"][1]["text"])

    def test_other_tools_do_not_supply_caption(self):
        original = self.history()
        original.append({"role": "tool", "content": '{"summary":"猫"}'})
        _, stats = compact_contexts(original)
        self.assertEqual(stats["summarized"], 0)

    def test_native_checkpoint_and_no_save_preserved(self):
        from astrbot.core.agent.message import Message, CheckpointData
        original = [Message.model_validate(m) for m in self.history()]
        original[0]._no_save = True
        original[0]._checkpoint_after = CheckpointData(id="offline-fixture")
        result, _ = compact_contexts(original, current_in_contexts=True)
        self.assertTrue(result[0]._no_save)
        self.assertEqual(result[0]._checkpoint_after.id, "offline-fixture")
        self.assertEqual(original[0].content[1].type, "image_url")

    def test_lookup_and_compaction_refs_match(self):
        original = self.history()
        refs = {key for _, _, key, _ in old_images(original)}
        result, _ = compact_contexts(original + [user({"type": "text", "text": "当前"})],
                                     current_in_contexts=True)
        for i in range(3):
            self.assertTrue(any(key in result[i]["content"][1]["text"] for key in refs))

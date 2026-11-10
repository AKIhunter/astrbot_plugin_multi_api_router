"""多模态结构计量：只有实际图片内容块免于文字计数，图片另有预算。"""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_MAX_IMAGE_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_TOTAL_IMAGE_BYTES = 32 * 1024 * 1024
DEFAULT_MAX_IMAGE_COUNT = 32


@dataclass
class RequestBudget:
    max_image_bytes: int = DEFAULT_MAX_IMAGE_BYTES
    max_total_image_bytes: int = DEFAULT_MAX_TOTAL_IMAGE_BYTES
    max_image_count: int = DEFAULT_MAX_IMAGE_COUNT
    text_chars: int = 0
    image_count: int = 0
    image_bytes: int = 0
    inline_chars: int = 0

    def add_image(self, url):
        if not isinstance(url, str) or not url:
            raise ValueError("图片引用格式无效，未调用上游。")
        self.image_count += 1
        if self.image_count > self.max_image_count:
            raise ValueError(f"图片数量超过路由预算 {self.max_image_count}，未调用上游。")
        size = 0
        if url.startswith("data:"):
            header, separator, data = url.partition(",")
            if (not separator or not header.startswith("data:image/")
                    or not header.endswith(";base64") or len(header) > 160):
                raise ValueError("内联图片格式无效，未调用上游。")
            # 先约束编码长度，再校验解码；避免巨大非法输入导致无界内存分配。
            if len(data) > 4 * ((self.max_image_bytes + 2) // 3):
                raise ValueError(f"单张图片超过路由预算 {self.max_image_bytes} 字节，未调用上游。")
            try:
                size = len(base64.b64decode(data, validate=True))
            except (ValueError, binascii.Error):
                raise ValueError("内联图片 Base64 无效，未调用上游。") from None
            if not size:
                raise ValueError("内联图片内容为空，未调用上游。")
            self.inline_chars += len(url)
        else:
            self.text_chars += len(url)
            if len(url) > 4096:
                raise ValueError("图片引用地址过长，未调用上游。")
            parsed = urlsplit(url)
            if parsed.scheme in ("http", "https"):
                if not parsed.netloc:
                    raise ValueError("图片引用格式无效，未调用上游。")
                # 不额外下载远程图片；数量和地址长度受限，实际字节由上游约束。
            elif parsed.scheme == "file" or not parsed.scheme:
                path = Path(parsed.path if parsed.scheme == "file" else url)
                if path.is_file():
                    size = path.stat().st_size
            else:
                raise ValueError("图片引用协议无效，未调用上游。")
        if size > self.max_image_bytes:
            raise ValueError(f"单张图片超过路由预算 {self.max_image_bytes} 字节，未调用上游。")
        self.image_bytes += size
        if self.image_bytes > self.max_total_image_bytes:
            raise ValueError(f"图片总体积超过路由预算 {self.max_total_image_bytes} 字节，未调用上游。")

    def add(self, value, *, parts=False, messages=False):
        if value is None:
            return
        if isinstance(value, str):
            self.text_chars += len(value)
            return
        dump = getattr(value, "model_dump", None)
        if callable(dump):
            value = dump()
        if isinstance(value, dict):
            if parts and value.get("type") == "image_url":
                image = value.get("image_url")
                if not isinstance(image, dict):
                    raise ValueError("图片内容块格式无效，未调用上游。")
                self.add_image(image.get("url"))
                # detail/id 及其他字段仍占文字预算，不能借图片类型绕过限制。
                self.add({k: v for k, v in value.items() if k != "image_url"})
                self.add({k: v for k, v in image.items() if k != "url"})
                return
            message = messages and value.get("role") in {"system", "user", "assistant", "tool"}
            for key, item in value.items():
                if item is None:
                    continue
                self.add(key)
                self.add(item, parts=message and key == "content" and isinstance(item, list))
        elif isinstance(value, (list, tuple)):
            for item in value:
                self.add(item, parts=parts, messages=messages)
        else:
            self.text_chars += len(str(value))


def measure_request(prompt=None, contexts=None, system_prompt=None, *, image_urls=None,
                    extra_user_content_parts=None, func_tool=None, tool_calls_result=None,
                    current_in_contexts=False, **limits):
    budget = RequestBudget(**limits)
    budget.add(prompt)
    budget.add(contexts, messages=True)
    budget.add(system_prompt)
    for url in image_urls or []:
        budget.add_image(url)
    # 原生 runner 已把 extra parts 装入 contexts，再计一次会重复计算。
    if not current_in_contexts:
        budget.add(extra_user_content_parts, parts=True)
    if func_tool is not None:
        schema = getattr(func_tool, "openai_schema", None)
        budget.add(schema() if callable(schema) else func_tool)
    budget.add(tool_calls_result)
    return budget

"""从混有日志的连续文本流中提取 ODAS JSON 对象。"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from typing import Any, TextIO


class JSONStreamParser:
    """按大括号深度组帧，并正确处理 JSON 字符串中的括号。"""

    def __init__(self) -> None:
        self._buffer: list[str] = []
        self._depth = 0
        self._in_string = False
        self._escaped = False

    def feed(self, chunk: str) -> list[dict[str, Any]]:
        objects: list[dict[str, Any]] = []
        for char in chunk:
            if self._depth == 0:
                if char != "{":
                    continue
                self._buffer = [char]
                self._depth = 1
                self._in_string = False
                self._escaped = False
                continue

            self._buffer.append(char)
            if self._in_string:
                if self._escaped:
                    self._escaped = False
                elif char == "\\":
                    self._escaped = True
                elif char == '"':
                    self._in_string = False
                continue
            if char == '"':
                self._in_string = True
            elif char == "{":
                self._depth += 1
            elif char == "}":
                self._depth -= 1
                if self._depth == 0:
                    candidate = "".join(self._buffer)
                    self._buffer = []
                    try:
                        value = json.loads(candidate)
                    except (json.JSONDecodeError, UnicodeError):
                        continue
                    if isinstance(value, dict):
                        objects.append(value)
        return objects


def parse_chunks(chunks: Iterable[str]) -> Iterator[dict[str, Any]]:
    parser = JSONStreamParser()
    for chunk in chunks:
        yield from parser.feed(chunk)


def parse_stream(stream: TextIO, chunk_size: int = 4096) -> Iterator[dict[str, Any]]:
    parser = JSONStreamParser()
    while True:
        # readline 只决定何时取到新数据；对象仍可跨任意多行组装。
        # 对 stdbuf -oL 的 ODAS 管道而言，这比等待固定大小缓冲区更实时。
        chunk = stream.readline(chunk_size)
        if not chunk:
            return
        yield from parser.feed(chunk)

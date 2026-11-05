"""Incremental UTF-8 SSE parser with bounded line/event state."""

import codecs
from collections.abc import AsyncIterator


async def sse_data(chunks: AsyncIterator[bytes], limit: int = 65536) -> AsyncIterator[str]:
    decoder = codecs.getincrementaldecoder("utf-8-sig")("strict")
    line = ""
    data: list[str] = []
    size = 0
    skip_lf = False
    async for chunk in chunks:
        for char in decoder.decode(chunk):
            if skip_lf and char == "\n":
                skip_lf = False
                continue
            skip_lf = False
            if char in "\r\n":
                skip_lf = char == "\r"
                if not line:
                    if data:
                        yield "\n".join(data)
                    data, size = [], 0
                elif line.startswith("data:"):
                    value = line[5:]
                    data.append(value[1:] if value.startswith(" ") else value)
                    size += len(line)
                line = ""
            else:
                line += char
            if len(line) + size > limit:
                raise ValueError("SSE event exceeds limit")
    decoder.decode(b"", final=True)
    if line or data:
        raise ValueError("Truncated SSE event")

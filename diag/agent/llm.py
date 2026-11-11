"""LLM layer: one interface, complete(system, messages, tools), with provider adapters.

Neutral message format used by the loop:
  {"role": "user", "content": str}
  {"role": "assistant", "content": str, "tool_calls": [{"id", "name", "args"}]}
  {"role": "tool", "results": [{"id", "name", "content"}]}
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class LLMResponse:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    truncated: bool = False  # the reply hit the output token cap, so any tool call in it may be incomplete


class LLM(Protocol):
    model: str

    def complete(self, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse: ...


def load_dotenv(path: str = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            if v.strip():
                os.environ.setdefault(k.strip(), v.strip())


def cost_usd(tokens_in: int, tokens_out: int) -> float:
    pin = float(os.environ.get("PRICE_IN_PER_MTOK", "0") or 0)
    pout = float(os.environ.get("PRICE_OUT_PER_MTOK", "0") or 0)
    return tokens_in / 1e6 * pin + tokens_out / 1e6 * pout


def _post(url: str, headers: dict, body: dict, timeout: float = 60) -> dict:
    last: Exception | None = None
    for attempt in range(4):
        try:
            r = httpx.post(url, headers=headers, json=body, timeout=timeout)
            if r.status_code in (429, 500, 502, 503, 529):
                last = RuntimeError(f"{r.status_code}: {r.text[:300]}")
                time.sleep(2 ** attempt)
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"LLM API error {r.status_code}: {r.text[:500]}")
            return r.json()
        except httpx.HTTPError as e:
            last = e
            time.sleep(2 ** attempt)
    raise RuntimeError(f"LLM API failed after retries: {last}")


class AnthropicLLM:
    def __init__(self, model: str | None = None):
        self.model = model or os.environ.get("DIAG_MODEL", "claude-haiku-5-5")
        self.key = os.environ.get("ANTHROPIC_API_KEY")
        if not self.key:
            raise SystemExit("ANTHROPIC_API_KEY is not set (put it in .env)")

    @staticmethod
    def _convert(messages: list[dict]) -> list[dict]:
        out: list[dict] = []
        for m in messages:
            if m["role"] == "user":
                blocks = [{"type": "text", "text": m["content"]}]
                role = "user"
            elif m["role"] == "assistant":
                blocks = ([{"type": "text", "text": m["content"]}] if m.get("content") else [])
                blocks += [{"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["args"]}
                           for c in m.get("tool_calls", [])]
                role = "assistant"
            else:
                blocks = [{"type": "tool_result", "tool_use_id": r["id"], "content": r["content"]} for r in m["results"]]
                role = "user"
            if out and out[-1]["role"] == role:
                out[-1]["content"].extend(blocks)
            else:
                out.append({"role": role, "content": blocks})
        return out

    def complete(self, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        # No temperature: newer Claude models reject it ("temperature is deprecated for this model").
        # Run-to-run variance is measured by the eval's 5 runs per scenario instead.
        body = {"model": self.model, "max_tokens": 4096, "system": system,
                "messages": self._convert(messages), "tools": tools}
        data = _post("https://api.anthropic.com/v1/messages",
                     {"x-api-key": self.key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                     body)
        text = "".join(b.get("text", "") for b in data["content"] if b["type"] == "text")
        calls = [ToolCall(b["id"], b["name"], b.get("input") or {}) for b in data["content"] if b["type"] == "tool_use"]
        u = data.get("usage", {})
        return LLMResponse(text, calls, u.get("input_tokens", 0), u.get("output_tokens", 0),
                           truncated=data.get("stop_reason") == "max_tokens")


class OpenAICompatLLM:
    """OpenAI chat completions; also works for Ollama (http://localhost:11434/v1) and other compatible APIs."""

    def __init__(self, model: str | None = None, base_url: str | None = None, key: str | None = None):
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")).rstrip("/")
        self.key = key or os.environ.get("OPENAI_API_KEY", "")
        self.model = model or os.environ.get("DIAG_MODEL", "")
        if not self.model:
            raise SystemExit("set --model or DIAG_MODEL for the OpenAI-compatible adapter")

    @staticmethod
    def _convert(system: str, messages: list[dict]) -> list[dict]:
        out: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for m in messages:
            if m["role"] == "user":
                out.append({"role": "user", "content": m["content"]})
            elif m["role"] == "assistant":
                msg: dict[str, Any] = {"role": "assistant", "content": m.get("content") or None}
                if m.get("tool_calls"):
                    msg["tool_calls"] = [{"id": c["id"], "type": "function",
                                          "function": {"name": c["name"], "arguments": json.dumps(c["args"])}}
                                         for c in m["tool_calls"]]
                out.append(msg)
            else:
                out += [{"role": "tool", "tool_call_id": r["id"], "content": r["content"]} for r in m["results"]]
        return out

    def complete(self, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        body = {"model": self.model, "temperature": 0, "messages": self._convert(system, messages),
                "tools": [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                            "parameters": t["input_schema"]}} for t in tools]}
        headers = {"content-type": "application/json"}
        if self.key:
            headers["authorization"] = f"Bearer {self.key}"
        data = _post(f"{self.base_url}/chat/completions", headers, body, timeout=180)
        msg = data["choices"][0]["message"]
        calls = []
        for i, c in enumerate(msg.get("tool_calls") or []):
            try:
                args = json.loads(c["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"_unparseable": c["function"].get("arguments")}
            calls.append(ToolCall(c.get("id") or f"call_{i}", c["function"]["name"], args))
        u = data.get("usage") or {}
        return LLMResponse(msg.get("content") or "", calls, u.get("prompt_tokens", 0), u.get("completion_tokens", 0),
                           truncated=data["choices"][0].get("finish_reason") == "length")


def make_llm(kind: str, model: str | None = None) -> LLM:
    load_dotenv()
    if kind == "mock":
        from .mock_brain import MockLLM

        return MockLLM()
    if kind == "anthropic":
        return AnthropicLLM(model)
    if kind == "openai":
        return OpenAICompatLLM(model)
    if kind == "ollama":
        return OpenAICompatLLM(model or os.environ.get("OLLAMA_MODEL", "qwen2.5:7b"),
                               os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1"), "ollama")
    raise SystemExit(f"unknown --llm {kind!r}; use mock, anthropic, openai or ollama")

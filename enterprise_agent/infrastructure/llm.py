"""Real HTTP adapters for Responses / Chat Completions and bounded tool use.

No credentials or full provider responses are written to decision reports.
"""

from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


class LLMError(RuntimeError):
    """Sanitized error, safe to include in audit records."""


def load_llm_profile(path: str | None = None) -> dict:
    """Nonsecret project settings. Credentials remain environment-only."""
    explicit = path or os.getenv("MAPS_LLM_CONFIG")
    source = Path(explicit or "llm.config.json")
    if not source.exists():
        if explicit:
            raise ValueError("configured LLM profile file does not exist")
        return {}
    if source.stat().st_size > 10_000:
        raise ValueError("LLM profile exceeds size limit")
    try:
        profile = json.loads(source.read_text(encoding="utf-8-sig"))
    except (ValueError, UnicodeError):
        raise ValueError("LLM profile must contain a valid JSON object") from None
    allowed = {"provider", "base_url", "api", "model", "chat_token_parameter",
               "strict_tools", "send_parallel_tool_calls"}
    if not isinstance(profile, dict) or not set(profile) <= allowed:
        raise ValueError("LLM profile contains unsupported fields; credentials must use environment variables")
    for key, value in profile.items():
        expected_type = bool if key in {"strict_tools", "send_parallel_tool_calls"} else str
        if type(value) is not expected_type:
            raise ValueError("LLM profile contains an invalid field type")
    return profile


def boolean_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    if raw.lower() not in {"true", "false", "1", "0"}:
        raise ValueError(f"{name} must be true/false or 1/0")
    return raw.lower() in {"true", "1"}


@dataclass(frozen=True)
class LLMConfig:
    model: str
    api_key: str = field(repr=False)
    base_url: str = "https://api.openai.com/v1"
    api: str = "responses"
    timeout_seconds: float = 25
    total_seconds: float = 120
    max_rounds: int = 12
    max_tool_calls: int = 24
    max_output_tokens: int = 1600
    chat_token_parameter: str = "max_completion_tokens"
    strict_tools: bool = True
    send_parallel_tool_calls: bool = True

    def __post_init__(self):
        parsed = urlparse(self.base_url)
        local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if (parsed.scheme != "https" and not (parsed.scheme == "http" and local)
                or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment):
            raise ValueError("LLM base URL requires HTTPS or loopback HTTP, without credentials/query")
        if self.api not in {"responses", "chat_completions"}:
            raise ValueError("MAPS_LLM_API must be responses or chat_completions")
        if self.chat_token_parameter not in {"max_tokens", "max_completion_tokens"}:
            raise ValueError("chat_token_parameter must be max_tokens or max_completion_tokens")
        if type(self.strict_tools) is not bool or type(self.send_parallel_tool_calls) is not bool:
            raise ValueError("LLM compatibility flags must be boolean")
        if not self.model.strip():
            raise ValueError("configure MAPS_LLM_MODEL before using --agent llm")
        if not self.api_key and not local:
            raise ValueError("configure MAPS_LLM_API_KEY or OPENAI_API_KEY" if parsed.hostname == "api.openai.com"
                             else "configure MAPS_LLM_API_KEY for the configured provider")
        if min(self.timeout_seconds, self.total_seconds) <= 0:
            raise ValueError("LLM timeouts must be positive")
        if min(self.max_rounds, self.max_tool_calls, self.max_output_tokens) < 1:
            raise ValueError("LLM limits must be positive")

    @classmethod
    def from_env(cls, profile_path: str | None = None) -> LLMConfig:
        profile = load_llm_profile(profile_path)
        base_url = os.getenv("MAPS_LLM_BASE_URL", profile.get("base_url", "https://api.openai.com/v1")).rstrip("/")
        key = os.getenv("MAPS_LLM_API_KEY", "")
        if not key and urlparse(base_url).hostname == "api.openai.com":
            key = os.getenv("OPENAI_API_KEY", "")
        return cls(model=os.getenv("MAPS_LLM_MODEL", profile.get("model", "")),
                   api_key=key,
                   base_url=base_url,
                   api=os.getenv("MAPS_LLM_API", profile.get("api", "responses")),
                   timeout_seconds=float(os.getenv("MAPS_LLM_TIMEOUT", "25")),
                   total_seconds=float(os.getenv("MAPS_LLM_TOTAL_TIMEOUT", "120")),
                   max_rounds=int(os.getenv("MAPS_LLM_MAX_ROUNDS", "12")),
                   max_tool_calls=int(os.getenv("MAPS_LLM_MAX_TOOL_CALLS", "24")),
                   chat_token_parameter=os.getenv("MAPS_LLM_CHAT_TOKEN_PARAMETER", profile.get("chat_token_parameter", "max_completion_tokens")),
                   strict_tools=boolean_env("MAPS_LLM_STRICT_TOOLS", profile.get("strict_tools", True)),
                   send_parallel_tool_calls=boolean_env("MAPS_LLM_SEND_PARALLEL_TOOL_CALLS", profile.get("send_parallel_tool_calls", True)))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Avoid forwarding bearer credentials to an unexpected redirect target.
        return None


def http_json(url: str, payload: dict, headers: dict, timeout: float) -> dict:
    request = Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                      headers=headers, method="POST")
    try:
        with build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise LLMError("provider response exceeds size limit")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise LLMError("provider returned a non-object response")
        return result
    except HTTPError as exc:
        raise LLMError(f"provider HTTP {exc.code}; check endpoint, model and credentials") from None
    except (URLError, TimeoutError, socket.timeout, OSError):
        raise LLMError("provider network failure or timeout") from None
    except (ValueError, UnicodeError):
        raise LLMError("provider returned invalid JSON") from None


@dataclass
class ModelTurn:
    calls: list[dict]
    continuation: list[dict]
    usage: dict


class LLMClient:
    def __init__(self, config: LLMConfig, transport: Callable = http_json):
        self.config = config
        self.transport = transport

    def history(self, context: dict) -> list[dict]:
        text = json.dumps(context, ensure_ascii=False)
        if len(text) > 200_000:
            raise LLMError("scenario too large for prototype LLM context limit")
        return [{"role": "user", "content": text}]

    def complete(self, instructions: str, history: list[dict], tools: list[dict],
                 remaining_seconds: float) -> ModelTurn:
        cfg = self.config
        headers = {"Content-Type": "application/json"}
        if cfg.api_key:
            headers["Authorization"] = f"Bearer {cfg.api_key}"
        payload = {"model": cfg.model, "tool_choice": "required"}
        if cfg.api == "responses":
            payload["parallel_tool_calls"] = False
            payload.update({"instructions": instructions, "input": history, "store": False,
                            "tools": [{"type": "function", **t} for t in tools],
                            "include": ["reasoning.encrypted_content"],
                            "max_output_tokens": cfg.max_output_tokens})
            endpoint = "responses"
        else:
            definitions = [dict(t) for t in tools]
            if not cfg.strict_tools:
                for definition in definitions:
                    definition.pop("strict", None)
            if cfg.send_parallel_tool_calls:
                payload["parallel_tool_calls"] = False
            payload.update({"messages": [{"role": "system", "content": instructions}, *history],
                            "tools": [{"type": "function", "function": t} for t in definitions],
                            cfg.chat_token_parameter: cfg.max_output_tokens})
            endpoint = "chat/completions"
        raw = self.transport(cfg.base_url.rstrip("/") + "/" + endpoint, payload, headers,
                             min(cfg.timeout_seconds, remaining_seconds))
        try:
            if cfg.api == "responses":
                if raw.get("status") in {"failed", "incomplete", "cancelled"}:
                    raise LLMError("provider response did not complete")
                output = raw["output"]
                if not isinstance(output, list):
                    raise LLMError("provider output must be a list")
                calls = [{"id": item["call_id"], "name": item["name"],
                          "arguments": item["arguments"]}
                         for item in output if item.get("type") == "function_call"]
                continuation = output  # Replay reasoning items as well as function calls.
            else:
                choice = raw["choices"][0]
                if choice.get("finish_reason") in {"length", "content_filter"}:
                    raise LLMError("provider response did not complete")
                message = choice["message"]
                calls = [{"id": c["id"], "name": c["function"]["name"],
                          "arguments": c["function"]["arguments"]}
                         for c in message.get("tool_calls", [])]
                continuation = [{"role": "assistant", "content": message.get("content"),
                                 "tool_calls": message.get("tool_calls", [])}]
                # DeepSeek thinking-mode tool turns require this field on the
                # next API request. Keep it only in memory, never in audit data.
                if "reasoning_content" in message:
                    if not isinstance(message["reasoning_content"], (str, type(None))):
                        raise LLMError("provider reasoning continuation has invalid type")
                    continuation[0]["reasoning_content"] = message["reasoning_content"]
            usage = raw.get("usage", {})
            if not isinstance(usage, dict):
                usage = {}
            # Only retain numeric counts, never provider response bodies / thoughts.
            usage = {k: v for k, v in usage.items() if type(v) is int and v >= 0}
            if any(not isinstance(c["id"], str) or not isinstance(c["name"], str)
                   or not isinstance(c["arguments"], str) for c in calls):
                raise LLMError("invalid provider tool call fields")
            return ModelTurn(calls, continuation, usage)
        except (KeyError, IndexError, TypeError, AttributeError):
            raise LLMError("provider response does not match configured API protocol") from None

    def tool_output(self, call_id: str, result: dict) -> dict:
        output = json.dumps(result, ensure_ascii=False)
        if self.config.api == "responses":
            return {"type": "function_call_output", "call_id": call_id, "output": output}
        return {"role": "tool", "tool_call_id": call_id, "content": output}


def tool(name: str, description: str, properties: dict | None = None) -> dict:
    props = properties or {}
    return {"name": name, "description": description, "strict": True,
            "parameters": {"type": "object", "properties": props,
                           "required": list(props), "additionalProperties": False}}

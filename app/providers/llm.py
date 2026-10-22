"""LLM providers: Fake (tests) and the local Qwen3.8 OpenAI-compatible server."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator

from app.logging import get_logger
from app.providers.base import LLMProvider

log = get_logger("llm")


FAKE_SUMMARY = {
    "title": "针尖球心补偿验证讨论",
    "summary": "会议讨论了针尖球心补偿验证，决定重新采集5组数据并与旧算法对照。",
    "topics": ["针尖球心补偿", "数据采集"],
    "decisions": [
        {
            "text": "重新采集5组数据，并与旧版本算法进行对比。",
            "source_segment_id": None,
        }
    ],
    "action_items": [
        {
            "description": "完成针尖球心补偿验证",
            "owner": "张工",
            "deadline": "明天",
            "priority": "high",
            "source_segment_id": None,
        }
    ],
    "risks": ["球心漂移原因尚未确认"],
    "open_questions": ["球心漂移原因"],
    "follow_ups": ["联系供应商确认精度指标"],
}


class FakeLLMProvider(LLMProvider):
    """Deterministic structured JSON. Never calls a network."""

    def __init__(self, payload: dict | None = None):
        self.payload = payload or FAKE_SUMMARY
        self.calls: list[str] = []

    def complete(self, prompt: str, *, json_mode: bool = False, model: str | None = None) -> str:
        self.calls.append(prompt)
        lower = prompt.lower()
        if "segments" in lower or "纠错" in prompt or "corrected_text" in lower:
            return json.dumps({"segments": []}, ensure_ascii=False)
        if "json" in lower or json_mode or "summary" in lower or "总结" in prompt or "action" in lower:
            return json.dumps(self.payload, ensure_ascii=False)
        return json.dumps({"answer": "根据现有会议记录，建议重新验证针尖球心补偿。", "citations": []}, ensure_ascii=False)

    def stream(self, prompt: str, *, model: str | None = None, cancel_event=None) -> Iterator[str]:
        question = ""
        for line in reversed((prompt or "").splitlines()):
            stripped = line.strip()
            if stripped.startswith(("问题:", "用户问题:")):
                question = stripped.split(":", 1)[-1].strip()
                break
        if question:
            text = (
                f"好的。你刚才说：{question}。"
                "我按这个要求，结合刚才的会议内容来回应，不编造原文没有的事实。"
            )
        else:
            text = "我认为目前主要有两个风险：第一，补偿值可能漂移；第二，对照数据不足。"
        for token in text:
            if cancel_event is not None and cancel_event.is_set():
                return
            yield token


class Qwen38LocalProvider(LLMProvider):
    """The fixed local Qwen3.8 gateway; it never calls a public endpoint."""

    def __init__(self, base_url: str | None = None, model: str | None = None, api_key: str | None = None):
        self.base_url = (base_url or os.environ.get("LOCAL_LLM_BASE_URL") or "http://127.0.0.1:8000/v1").rstrip("/")
        self.model = model or os.environ.get("LOCAL_LLM_MODEL") or "mtplx-qwen38-27b-optimized-quality"
        self.api_key = api_key or os.environ.get("LOCAL_LLM_API_KEY", "not-needed")
        self.timeout_seconds = float(os.environ.get("LLM_TIMEOUT_SECONDS", "90"))
        # Conversation must start speaking promptly.  Qwen's hidden reasoning
        # consumes the token budget before any audible text is emitted, so keep
        # it off for the interactive route (structured offline work can still
        # use the ordinary complete() call).
        self.enable_thinking = os.environ.get("LLM_ENABLE_THINKING", "0").strip().lower() in {"1", "true", "yes", "on"}
        self.response_max_tokens = max(32, int(os.environ.get("AI_RESPONSE_MAX_TOKENS", "3072")))
        # Post-meeting summaries include structured evidence, participant
        # views and action items. 768 tokens routinely cuts a valid JSON
        # object in half, which then falls back to a nearly useless excerpt.
        # This is a ceiling rather than a target: short correction tasks still
        # finish at EOS, while a complete final summary has room to close JSON.
        self.background_max_tokens = max(64, int(os.environ.get("LLM_BACKGROUND_MAX_TOKENS", "3072")))
        self.interactive_first_token_timeout = max(
            3.0, float(os.environ.get("LLM_INTERACTIVE_FIRST_TOKEN_TIMEOUT_SECONDS", "12"))
        )
        self.interactive_stream_attempts = max(
            1, min(3, int(os.environ.get("LLM_INTERACTIVE_STREAM_ATTEMPTS", "2")))
        )
        self._served_model: str | None = None
        self._served_model_at = 0.0

    def _headers(self) -> dict[str, str]:
        # MTPLX deliberately ignores per-request generation controls unless a
        # non-app client opts in.  Interactive meeting turns must be able to
        # disable Qwen's hidden reasoning, otherwise the UI waits through an
        # invisible chain-of-thought before it receives any ``content``.
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "X-MTPLX-Allow-Client-Controls": "true",
        }

    def _client(self, timeout: float):
        import httpx

        # macOS System Configuration proxies (Clash/Surge/etc.) make httpx POST
        # 127.0.0.1:8000 return empty HTTP 502 in ~500ms. Never honor env/system proxy.
        return httpx.Client(timeout=timeout, trust_env=False)

    def _resolve_model(self, model: str | None = None) -> str:
        wanted = model or self.model
        now = time.time()
        if self._served_model and (now - self._served_model_at) < 30.0:
            if wanted == self._served_model or wanted == self.model:
                return self._served_model
        try:
            with self._client(2.0) as client:
                # The local MTPLX gateway protects discovery as well as chat
                # completion.  Using the same credentials avoids a needless
                # 401 (and a small first-turn delay) before every fresh worker.
                resp = client.get(f"{self.base_url}/models", headers=self._headers())
                if resp.status_code == 200:
                    ids = [m.get("id") for m in (resp.json().get("data") or []) if m.get("id")]
                    chosen = wanted if wanted in ids else (ids[0] if ids else wanted)
                    if chosen != wanted:
                        log.warning("local llm model id %s not served; using %s", wanted, chosen)
                    self._served_model = chosen
                    self._served_model_at = now
                    return chosen
        except Exception as exc:
            log.warning("local llm /v1/models lookup failed err=%s", exc)
        return wanted

    def complete(self, prompt: str, *, json_mode: bool = False, model: str | None = None) -> str:
        headers = self._headers()
        url = f"{self.base_url}/chat/completions"
        use_json = bool(json_mode)
        last_exc: Exception | None = None
        with self._client(self.timeout_seconds) as client:
            for attempt in range(5):
                body: dict = {
                    "model": self._resolve_model(model),
                    "messages": [{"role": "user", "content": prompt}],
                    "enable_thinking": self.enable_thinking,
                    "chat_template_kwargs": {"enable_thinking": self.enable_thinking},
                    "max_tokens": self.background_max_tokens,
                }
                if use_json:
                    body["response_format"] = {"type": "json_object"}
                try:
                    resp = client.post(url, headers=headers, json=body)
                    if resp.status_code in (502, 503):
                        preview = (resp.text or "")[:300]
                        log.warning(
                            "local llm %s attempt=%s json_mode=%s body=%s",
                            resp.status_code,
                            attempt + 1,
                            use_json,
                            preview,
                        )
                        if use_json:
                            use_json = False
                        if attempt < 4:
                            time.sleep(0.4 * (attempt + 1))
                            continue
                        resp.raise_for_status()
                    resp.raise_for_status()
                    return resp.json()["choices"][0]["message"]["content"]
                except Exception as exc:
                    last_exc = exc
                    code = getattr(getattr(exc, "response", None), "status_code", None)
                    log.warning(
                        "local llm request failed attempt=%s status=%s json_mode=%s err=%s",
                        attempt + 1,
                        code,
                        use_json,
                        exc,
                    )
                    if use_json:
                        use_json = False
                    if attempt < 4:
                        time.sleep(0.4 * (attempt + 1))
                        continue
                    raise
        if last_exc:
            raise last_exc
        raise RuntimeError("local llm complete failed without response")

    def stream(self, prompt: str, *, model: str | None = None, cancel_event=None) -> Iterator[str]:
        """Wake path: cancellable streaming with a bounded first-token wait."""
        headers = self._headers()
        body = {
            "model": self._resolve_model(model),
            "messages": [{"role": "user", "content": prompt}],
            "stream": True,
            "max_tokens": self.response_max_tokens,
            "enable_thinking": self.enable_thinking,
            # MTPLX also supports Qwen's documented vLLM/SGLang request
            # spelling.  Keep both forms so a gateway upgrade cannot silently
            # reinstate reasoning for live voice turns.
            "chat_template_kwargs": {"enable_thinking": self.enable_thinking},
        }
        url = f"{self.base_url}/chat/completions"
        last_exc: Exception | None = None
        attempts = self.interactive_stream_attempts if cancel_event is not None else 5
        for attempt in range(attempts):
            if cancel_event is not None and cancel_event.is_set():
                return
            # A fresh client per retry makes a closed stale stream unable to
            # retain the HTTP connection or a local-model queue slot.
            with self._client(self.timeout_seconds) as client:
                # Closing a synchronous httpx Response from another thread
                # does *not* reliably unblock ``iter_lines`` on macOS.  The
                # previous watchdog therefore announced a 12-second timeout
                # but could leave a turn blocked in a 90-second socket read.
                # A read timeout is enforced by the transport itself.  Qwen
                # normally emits at least one SSE event every <1 s, so it also
                # detects a genuinely stalled stream after the first token.
                import httpx

                read_timeout = min(self.timeout_seconds, self.interactive_first_token_timeout)
                stream_timeout = httpx.Timeout(
                    connect=min(5.0, read_timeout),
                    read=read_timeout,
                    write=min(10.0, self.timeout_seconds),
                    pool=min(5.0, self.timeout_seconds),
                )
                abort_reason: str | None = None
                got_token = False
                saw_reasoning = False
                try:
                    with client.stream("POST", url, headers=headers, json=body, timeout=stream_timeout) as resp:
                        if resp.status_code in (502, 503):
                            preview = ""
                            try:
                                preview = (resp.read() or b"").decode("utf-8", "replace")[:300]
                            except Exception:
                                preview = ""
                            log.warning(
                                "local llm stream %s attempt=%s body=%s",
                                resp.status_code,
                                attempt + 1,
                                preview,
                            )
                            if attempt < attempts - 1:
                                time.sleep(0.4 * (attempt + 1))
                                continue
                            resp.raise_for_status()
                        resp.raise_for_status()
                        first_content_deadline = time.monotonic() + self.interactive_first_token_timeout
                        for line in resp.iter_lines():
                            if cancel_event is not None and cancel_event.is_set():
                                return
                            # Queue keep-alives are valid SSE but are not a
                            # response.  They reset the transport read timer,
                            # so enforce a separate visible-content deadline.
                            if not got_token and time.monotonic() >= first_content_deadline:
                                abort_reason = "first_content_timeout"
                                raise TimeoutError("interactive response produced no visible content in time")
                            if not line or not line.startswith("data:"):
                                continue
                            payload = line[5:].strip()
                            if payload == "[DONE]":
                                break
                            try:
                                chunk = json.loads(payload)
                                delta_obj = chunk["choices"][0]["delta"]
                                delta = delta_obj.get("content") or ""
                                if not delta and delta_obj.get("reasoning_content"):
                                    saw_reasoning = True
                                    if not got_token and time.monotonic() >= first_content_deadline:
                                        abort_reason = "first_content_timeout"
                                        raise TimeoutError("interactive response emitted reasoning but no visible content")
                                if delta:
                                    got_token = True
                                    yield delta
                            except (json.JSONDecodeError, KeyError, IndexError):
                                continue
                    if got_token or abort_reason is None:
                        return
                    if abort_reason == "first_content_timeout":
                        last_exc = TimeoutError("interactive first token timed out")
                        log.warning(
                            "local llm stream visible content timed out after %.1fs attempt=%s reasoning_events=%s",
                            self.interactive_first_token_timeout,
                            attempt + 1,
                            saw_reasoning,
                        )
                except Exception as exc:
                    last_exc = exc
                    if cancel_event is not None and cancel_event.is_set():
                        log.info("local llm stream cancelled before completion")
                        return
                    if got_token:
                        # Never retry after visible text: a retry would append
                        # a second answer to the live transcript.  The caller
                        # can still speak this coherent partial answer.
                        log.warning("local llm stream ended after visible content attempt=%s err=%s", attempt + 1, exc)
                        return
                    if isinstance(exc, (TimeoutError, httpx.ReadTimeout)) or abort_reason == "first_content_timeout":
                        log.warning(
                            "local llm stream first token timed out after %.1fs attempt=%s",
                            self.interactive_first_token_timeout,
                            attempt + 1,
                        )
                    else:
                        log.warning("local llm stream failed attempt=%s err=%s", attempt + 1, exc)
                    if attempt < attempts - 1:
                        time.sleep(0.4 * (attempt + 1))
                        continue
        # Interactive timeout should produce the caller's spoken fallback,
        # rather than look like a wake-word failure after 90 seconds.
        if cancel_event is not None and last_exc is not None:
            return
        if last_exc:
            raise last_exc

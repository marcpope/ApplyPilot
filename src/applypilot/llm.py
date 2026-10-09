"""
Unified LLM client for ApplyPilot.

Auto-detects provider from environment:
  LLM_URL         -> Any OpenAI-compatible endpoint (Ollama, llama.cpp,
                     DeepSeek, OpenRouter...). LLM_API_KEY is sent if set.
  GEMINI_API_KEY  -> Google Gemini (default: gemini-2.5-flash)
  OPENAI_API_KEY  -> OpenAI (default: gpt-4o-mini)

LLM_MODEL env var overrides the model name for any provider.
"""

import logging
import os
import re
import threading
import time
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)

# gemini-2.0-flash returns 404 for API keys created after early 2026.
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"

_OLLAMA_PORT = 11434

# ---------------------------------------------------------------------------
# Provider detection
# ---------------------------------------------------------------------------

def _normalize_local_url(url: str) -> str:
    """Return the OpenAI-compatible base URL for a user-supplied LLM_URL.

    Ollama serves the OpenAI-compatible API under /v1, but users often paste
    the bare server address (http://localhost:11434), which makes every
    request 404 on /chat/completions.
    """
    url = url.rstrip("/")
    parsed = urlparse(url)
    if parsed.port == _OLLAMA_PORT and parsed.path in ("", "/"):
        return f"{url}/v1"
    return url


def _detect_provider() -> tuple[str, str, str]:
    """Return (base_url, model, api_key) based on environment variables.

    Reads env at call time (not module import time) so that load_env() called
    in _bootstrap() is always visible here.
    """
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    local_url = os.environ.get("LLM_URL", "")
    model_override = os.environ.get("LLM_MODEL", "")

    if gemini_key and not local_url:
        return (
            "https://generativelanguage.googleapis.com/v1beta/openai",
            model_override or DEFAULT_GEMINI_MODEL,
            gemini_key,
        )

    if openai_key and not local_url:
        return (
            "https://api.openai.com/v1",
            model_override or "gpt-4o-mini",
            openai_key,
        )

    if local_url:
        return (
            _normalize_local_url(local_url),
            model_override or "local-model",
            os.environ.get("LLM_API_KEY", ""),
        )

    raise RuntimeError(
        "No LLM provider configured. "
        "Set GEMINI_API_KEY, OPENAI_API_KEY, or LLM_URL in your environment."
    )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

_MAX_RETRIES = 5
_TIMEOUT = 120  # seconds

# Base wait on first 429/503 (doubles each retry, caps at 60s).
# Gemini free tier is 15 RPM = 4s minimum between requests; 10s gives headroom.
_RATE_LIMIT_BASE_WAIT = 10


_GEMINI_COMPAT_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"
_GEMINI_NATIVE_BASE = "https://generativelanguage.googleapis.com/v1beta"


class LLMClient:
    """Thin LLM client supporting OpenAI-compatible and native Gemini endpoints.

    For Gemini keys, starts on the OpenAI-compat layer. On a 403 (which
    happens with preview/experimental models not exposed via compat), it
    automatically switches to the native generateContent API and stays there
    for the lifetime of the process.
    """

    def __init__(self, base_url: str, model: str, api_key: str) -> None:
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self._client = httpx.Client(timeout=_TIMEOUT)
        # True once we've confirmed the native Gemini API works for this model
        self._use_native_gemini: bool = False
        self._is_gemini: bool = base_url.startswith(_GEMINI_COMPAT_BASE)
        # Set when the endpoint rejects response_format; JSON mode is then
        # skipped and callers fall back to parsing JSON out of free text.
        self._json_mode_unsupported: bool = False
        # Optional client-side pacing for free tiers (LLM_RPM requests/minute).
        rpm = float(os.environ.get("LLM_RPM", "0") or 0)
        self._min_interval: float = 60.0 / rpm if rpm > 0 else 0.0
        self._last_request: float = 0.0
        self._pace_lock = threading.Lock()

    def _pace(self) -> None:
        """Sleep as needed so requests stay under LLM_RPM."""
        if not self._min_interval:
            return
        with self._pace_lock:
            wait = self._last_request + self._min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()

    # -- Native Gemini API --------------------------------------------------

    def _chat_native_gemini(
        self,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
        json_mode: bool = False,
    ) -> str:
        """Call the native Gemini generateContent API.

        Used automatically when the OpenAI-compat endpoint returns 403,
        which happens for preview/experimental models not exposed via compat.

        Converts OpenAI-style messages to Gemini's contents/systemInstruction
        format transparently.
        """
        contents: list[dict] = []
        system_parts: list[dict] = []

        for msg in messages:
            role = msg["role"]
            text = msg.get("content", "")
            if role == "system":
                system_parts.append({"text": text})
            elif role == "user":
                contents.append({"role": "user", "parts": [{"text": text}]})
            elif role == "assistant":
                # Gemini uses "model" instead of "assistant"
                contents.append({"role": "model", "parts": [{"text": text}]})

        payload: dict = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        if system_parts:
            payload["systemInstruction"] = {"parts": system_parts}
        if json_mode:
            payload["generationConfig"]["responseMimeType"] = "application/json"

        url = f"{_GEMINI_NATIVE_BASE}/models/{self.model}:generateContent"
        resp = self._client.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
            params={"key": self.api_key},
        )
        resp.raise_for_status()
        data = resp.json()
        candidate = (data.get("candidates") or [{}])[0]
        parts = (candidate.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts)
        return _finish_text(text, candidate.get("finishReason"))

    # -- OpenAI-compat API --------------------------------------------------

    def _chat_compat(
        self,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
        json_mode: bool = False,
    ) -> str:
        """Call the OpenAI-compatible endpoint."""
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        use_json = json_mode and not self._json_mode_unsupported
        if use_json:
            payload["response_format"] = {"type": "json_object"}

        resp = self._client.post(
            f"{self.base_url}/chat/completions",
            json=payload,
            headers=headers,
        )

        # Some OpenAI-compatible servers reject response_format. Remember that
        # and retry once without it; the caller's JSON parsing still applies.
        if use_json and resp.status_code in (400, 422):
            log.info("Endpoint rejected response_format; continuing without JSON mode.")
            self._json_mode_unsupported = True
            payload.pop("response_format")
            resp = self._client.post(f"{self.base_url}/chat/completions", json=payload, headers=headers)

        # 403 on Gemini compat = model not available on compat layer.
        # Raise a specific sentinel so chat() can switch to native API.
        if resp.status_code == 403 and self._is_gemini:
            raise _GeminiCompatForbidden(resp)

        return self._handle_compat_response(resp)

    @staticmethod
    def _handle_compat_response(resp: httpx.Response) -> str:
        resp.raise_for_status()
        data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        content = (choice.get("message") or {}).get("content")
        return _finish_text(content, choice.get("finish_reason"))

    # -- public API ---------------------------------------------------------

    def chat(
        self,
        messages: list[dict],
        temperature: float = 0.0,
        max_tokens: int = 4096,
        json_mode: bool = False,
    ) -> str:
        """Send a chat completion request and return the assistant message text.

        json_mode asks the provider to return a bare JSON object (OpenAI
        response_format / Gemini responseMimeType). Gemini otherwise wraps JSON
        in markdown fences and occasionally emits malformed JSON.
        """
        # Qwen3 optimization: prepend /no_think to the first user message to
        # skip chain-of-thought reasoning, saving tokens on structured tasks.
        # Most prompts lead with a system message, so look past it.
        if "qwen" in self.model.lower():
            for i, msg in enumerate(messages):
                if msg.get("role") == "user":
                    if not msg["content"].startswith("/no_think"):
                        messages = list(messages)
                        messages[i] = {**msg, "content": f"/no_think\n{msg['content']}"}
                    break

        for attempt in range(_MAX_RETRIES):
            try:
                self._pace()
                # Route to native Gemini if we've already confirmed it's needed
                if self._use_native_gemini:
                    return self._chat_native_gemini(messages, temperature, max_tokens, json_mode)

                return self._chat_compat(messages, temperature, max_tokens, json_mode)

            except _GeminiCompatForbidden as exc:
                # Model not available on OpenAI-compat layer — switch to native.
                log.warning(
                    "Gemini compat endpoint returned 403 for model '%s'. "
                    "Switching to native generateContent API. "
                    "(Preview/experimental models are often compat-only on native.)",
                    self.model,
                )
                self._use_native_gemini = True
                # Retry immediately with native — don't count as a rate-limit wait
                try:
                    return self._chat_native_gemini(messages, temperature, max_tokens, json_mode)
                except httpx.HTTPStatusError as native_exc:
                    raise RuntimeError(
                        f"Both Gemini endpoints failed. Compat: 403 Forbidden. "
                        f"Native: {native_exc.response.status_code} — "
                        f"{native_exc.response.text[:200]}"
                    ) from native_exc

            except httpx.HTTPStatusError as exc:
                resp = exc.response
                if resp.status_code == 429 and _is_daily_quota(resp.text):
                    # Retrying can't help until the quota resets (midnight Pacific
                    # for Gemini); fail now so the stage stops quickly.
                    raise RuntimeError(
                        f"Daily request quota exhausted for model '{self.model}'. It resets "
                        "tomorrow; switch LLM_MODEL or provider to continue today."
                    ) from exc
                if resp.status_code in (429, 503) and attempt < _MAX_RETRIES - 1:
                    wait = _retry_wait(resp, attempt)
                    log.warning(
                        "LLM rate limited (HTTP %s). Waiting %ds before retry %d/%d. "
                        "Tip: set LLM_RPM in .env to pace requests under a free-tier limit.",
                        resp.status_code, wait, attempt + 1, _MAX_RETRIES,
                    )
                    time.sleep(wait)
                    continue
                # Surface the provider's explanation (e.g. "model is no longer
                # available to new users") instead of a bare status line.
                raise RuntimeError(
                    f"LLM request failed: HTTP {resp.status_code} from {resp.request.url.host} "
                    f"(model '{self.model}'): {resp.text[:300]}"
                ) from exc

            except httpx.TimeoutException:
                if attempt < _MAX_RETRIES - 1:
                    wait = min(_RATE_LIMIT_BASE_WAIT * (2 ** attempt), 60)
                    log.warning(
                        "LLM request timed out, retrying in %ds (attempt %d/%d)",
                        wait, attempt + 1, _MAX_RETRIES,
                    )
                    time.sleep(wait)
                    continue
                raise

        raise RuntimeError("LLM request failed after all retries")

    def check(self) -> str:
        """Send one tiny request without retries; raise with the reason on failure.

        Used by `applypilot doctor` so a dead model name, bad key or exhausted
        quota shows up before a run instead of as hundreds of failed jobs.
        """
        messages = [{"role": "user", "content": "Reply with the single word OK."}]
        try:
            if self._use_native_gemini:
                return self._chat_native_gemini(messages, 0.0, 1024)
            return self._chat_compat(messages, 0.0, 1024)
        except _GeminiCompatForbidden:
            self._use_native_gemini = True
            return self._chat_native_gemini(messages, 0.0, 1024)
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(f"HTTP {exc.response.status_code}: {exc.response.text[:200]}") from exc

    def ask(self, prompt: str, **kwargs) -> str:
        """Convenience: single user prompt -> assistant response."""
        return self.chat([{"role": "user", "content": prompt}], **kwargs)

    def close(self) -> None:
        self._client.close()


_RETRY_DELAY = re.compile(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"')


def _is_daily_quota(body: str) -> bool:
    """True when a 429 is a per-day quota (Gemini quotaId ...PerDay...)."""
    return "PerDay" in body


def _retry_wait(resp: httpx.Response, attempt: int) -> float:
    """Seconds to wait before retrying a 429/503.

    Gemini puts the real delay in the error body (RetryInfo.retryDelay)
    rather than a Retry-After header; fall back to exponential backoff.
    """
    match = _RETRY_DELAY.search(resp.text)
    header = resp.headers.get("Retry-After") or resp.headers.get("X-RateLimit-Reset-Requests")
    for candidate in (match.group(1) if match else None, header):
        if candidate:
            try:
                return min(float(candidate) + 1, 120)
            except ValueError:
                pass
    return min(_RATE_LIMIT_BASE_WAIT * (2 ** attempt), 60)


_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.S | re.I)


def _finish_text(content: str | None, finish_reason: str | None) -> str:
    """Clean a completion and fail loudly when it came back empty.

    Reasoning models (qwen3, deepseek-r1, Gemini 2.5) can spend the whole
    token budget thinking and return no visible text. Callers used to parse
    that as a garbage result (e.g. a fit score of 0), so raise instead and let
    the stage leave the job pending.
    """
    text = _THINK_BLOCK.sub("", content or "").strip()
    if not text:
        raise RuntimeError(
            f"LLM returned an empty response (finish_reason={finish_reason}). "
            "Reasoning models can use up the token budget before answering; "
            "try a non-reasoning model or a larger model."
        )
    return text


class _GeminiCompatForbidden(Exception):
    """Sentinel: Gemini OpenAI-compat returned 403. Switch to native API."""
    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        super().__init__(f"Gemini compat 403: {response.text[:200]}")


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_instance: LLMClient | None = None


def get_client() -> LLMClient:
    """Return (or create) the module-level LLMClient singleton."""
    global _instance
    if _instance is None:
        base_url, model, api_key = _detect_provider()
        log.info("LLM provider: %s  model: %s", base_url, model)
        _instance = LLMClient(base_url, model, api_key)
    return _instance

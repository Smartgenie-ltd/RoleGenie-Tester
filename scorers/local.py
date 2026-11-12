"""
E5 - Ollama adapter.

Two transports, one schema.

  openai (default)  POST http://localhost:11434/v1/chat/completions
  native            POST http://localhost:11434/api/chat

Both are given the same full JSON schema object, never the string "json". The
field differs because the wire formats differ: the OpenAI-compatible endpoint
carries a schema in `response_format.json_schema.schema`, and Ollama's native
endpoint carries it in `format`. Same constraint, same object, different
envelope - so the transport is not a hidden variable, and the run file records
which one was used.

Constrained decoding is the point. It removes parse failure as a confound, so
gate check 4 measures the harness rather than the model.

Model identity is taken from the API response, never from config. SPEC.md 5.1:
the machine currently serves `qwen3-vl:8b`, which is not `qwen3:8b`, and a run
reported under the wrong name is worse than no run.
"""

from __future__ import annotations

import time
from urllib.parse import urlparse

import requests

from . import RESPONSE_SCHEMA, SCHEMA_NAME, Sample, parse_response

DEFAULT_BASE_URL = "http://localhost:11434/v1"
DEFAULT_MODEL = "qwen3:8b"
TIMEOUT = 300

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}


def assert_local(url: str) -> None:
    """The harness runs offline against frozen fixtures. A 'local' scorer
    pointing at someone else's machine is a live dependency wearing a disguise."""
    host = urlparse(url).hostname or ""
    if host not in LOCAL_HOSTS:
        raise ValueError(
            f"local scorer refuses a non-local endpoint: {url!r}. "
            f"Expected one of {sorted(LOCAL_HOSTS)}."
        )


class LocalScorer:
    """Implements the Scorer protocol against Ollama."""

    kind = "local"
    constrained = True
    price_per_mtok = (0.0, 0.0)      # electricity is not modelled

    def __init__(self, model: str = DEFAULT_MODEL,
                 base_url: str = DEFAULT_BASE_URL,
                 temperature: float = 0.0,
                 api: str = "openai",
                 timeout: int = TIMEOUT,
                 think: str = "auto",
                 max_tokens: int | None = None) -> None:
        if api not in ("openai", "native"):
            raise ValueError(f"api must be 'openai' or 'native', got {api!r}")
        if think not in ("auto", "on", "off"):
            raise ValueError(f"think must be 'auto', 'on' or 'off', got {think!r}")
        if think != "auto" and api == "openai":
            raise ValueError(
                "Ollama's OpenAI-compatible endpoint ignores thinking controls "
                "(chat_template_kwargs is silently dropped). Use --api native to "
                "set --think."
            )
        assert_local(base_url)

        self.name = model
        self.base_url = base_url.rstrip("/")
        self.temperature = temperature
        self.api = api
        self.timeout = timeout
        self.think = think
        self.max_tokens = max_tokens

    # -- wire formats -------------------------------------------------------

    def _openai_request(self, prompt: str, seed: int | None) -> tuple[str, dict]:
        body = {
            "model": self.name,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self.temperature,
            "seed": seed,
            "stream": False,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": SCHEMA_NAME,
                    "strict": True,
                    "schema": RESPONSE_SCHEMA,
                },
            },
        }
        if self.max_tokens:
            body["max_tokens"] = self.max_tokens
        return f"{self.base_url}/chat/completions", body

    def _native_request(self, prompt: str, seed: int | None) -> tuple[str, dict]:
        root = self.base_url[:-3] if self.base_url.endswith("/v1") else self.base_url
        options = {"temperature": self.temperature, "seed": seed}
        if self.max_tokens:
            options["num_predict"] = self.max_tokens
        body = {
            "model": self.name,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "format": RESPONSE_SCHEMA,
            "options": options,
        }
        if self.think != "auto":
            body["think"] = self.think == "on"
        return f"{root}/api/chat", body

    # -- readers ------------------------------------------------------------
    # A thinking model can put the whole answer in its reasoning field and leave
    # content empty. Observed with qwen3-vl:8b under `think: false`: valid
    # schema-conformant JSON in `thinking`, nothing in `content`, done_reason
    # "stop". The answer is there, misrouted, so the reasoning field is tried as
    # a fallback and which field was used is recorded on the sample. A genuine
    # reasoning block is prose and fails parse_response anyway, so this cannot
    # turn thinking-out-loud into an answer.

    @staticmethod
    def _read_openai(data: dict) -> dict:
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message", {}) or {}
        usage = data.get("usage") or {}
        text, source = msg.get("content") or "", "content"
        if not text.strip() and (msg.get("reasoning") or "").strip():
            text, source = msg["reasoning"], "reasoning"
        return {"text": text, "source": source,
                "tokens_in": usage.get("prompt_tokens", 0) or 0,
                "tokens_out": usage.get("completion_tokens", 0) or 0,
                "model": data.get("model", ""),
                "finish_reason": choice.get("finish_reason", "") or "",
                "reasoning_chars": len(msg.get("reasoning") or "")}

    @staticmethod
    def _read_native(data: dict) -> dict:
        msg = data.get("message", {}) or {}
        text, source = msg.get("content") or "", "content"
        if not text.strip() and (msg.get("thinking") or "").strip():
            text, source = msg["thinking"], "thinking"
        return {"text": text, "source": source,
                "tokens_in": data.get("prompt_eval_count", 0) or 0,
                "tokens_out": data.get("eval_count", 0) or 0,
                "model": data.get("model", ""),
                "finish_reason": data.get("done_reason", "") or "",
                "reasoning_chars": len(msg.get("thinking") or "")}

    # -- the protocol -------------------------------------------------------

    def sample(self, prompt: str, seed: int | None) -> Sample:
        url, payload = (self._openai_request(prompt, seed) if self.api == "openai"
                        else self._native_request(prompt, seed))
        started = time.perf_counter()
        try:
            r = requests.post(url, json=payload, timeout=self.timeout)
            r.raise_for_status()
            data = r.json()
        except Exception as exc:                          # noqa: BLE001
            elapsed = int((time.perf_counter() - started) * 1000)
            return Sample(seed=seed, band=None, score=None, rationale="",
                          tokens_in=0, tokens_out=0, latency_ms=elapsed,
                          parse_ok=False, error=f"{type(exc).__name__}: {exc}",
                          model_reported="")

        elapsed = int((time.perf_counter() - started) * 1000)
        reader = self._read_openai if self.api == "openai" else self._read_native
        got = reader(data)
        band, score, rationale, ok, err = parse_response(got["text"])

        # "empty response" on its own sends you hunting. Say what actually
        # happened: a thinking model that spent the whole budget reasoning looks
        # completely different from a model that returned nothing at all.
        if not ok and not (got["text"] or "").strip():
            err = (f"no content (finish_reason={got['finish_reason'] or 'unknown'}, "
                   f"{got['reasoning_chars']} chars of reasoning, "
                   f"{got['tokens_out']} output tokens)")
            if got["finish_reason"] == "length":
                err += ". The output budget went on reasoning. Try --api native "
                err += "--think off, or raise --max-tokens."

        return Sample(seed=seed, band=band, score=score, rationale=rationale,
                      tokens_in=got["tokens_in"], tokens_out=got["tokens_out"],
                      latency_ms=elapsed, parse_ok=ok, error=err,
                      model_reported=got["model"] or "",
                      finish_reason=got["finish_reason"],
                      content_from=got["source"], raw=got["text"])


def available_models(base_url: str = DEFAULT_BASE_URL, timeout: int = 10) -> list[str]:
    """What Ollama is actually serving. Used by the runner to fail early with a
    useful message rather than after the first call."""
    assert_local(base_url)
    root = base_url[:-3] if base_url.endswith("/v1") else base_url
    r = requests.get(f"{root}/api/tags", timeout=timeout)
    r.raise_for_status()
    return sorted(m.get("name", "") for m in r.json().get("models", []))

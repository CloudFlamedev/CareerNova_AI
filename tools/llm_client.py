"""Groq multi-model rotation with empty-response failover and persistent cooldowns."""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from langchain_groq import ChatGroq

load_dotenv()

_API_KEY = os.getenv("GROQ_API_KEY")
if not _API_KEY:
    raise RuntimeError("GROQ_API_KEY is missing. Add it to your .env file.")

# General-purpose models only. Configure the exact models enabled for your
# Groq account in GROQ_MODELS; unsupported IDs will be cooled down after errors.
_DEFAULT_MODELS = [
    "openai/gpt-oss-120b",
    "qwen/qwen3.8-27b",
    "openai/gpt-oss-20b",
]

_models_setting = os.getenv("GROQ_MODELS", "").strip()
MODEL_ORDER = list(dict.fromkeys(
    item.strip()
    for item in (_models_setting.split(",") if _models_setting else _DEFAULT_MODELS)
    if item.strip()
)) or _DEFAULT_MODELS.copy()

# 500 gives the evaluator enough room to finish a valid JSON object while its
# prompt limits skill lists. Set GROQ_MAX_OUTPUT_TOKENS to override this value.
MAX_OUTPUT_TOKENS = int(os.getenv("GROQ_MAX_OUTPUT_TOKENS", "500"))
REQUEST_TIMEOUT = float(os.getenv("GROQ_REQUEST_TIMEOUT", "45"))
EMPTY_RESPONSE_COOLDOWN = int(os.getenv("GROQ_EMPTY_RESPONSE_COOLDOWN", "60"))
COOLDOWN_FILE = Path(os.getenv("GROQ_COOLDOWN_FILE", ".groq_model_cooldowns.json"))


def extract_response_text(response: Any) -> str:
    """Extract text from LangChain AIMessage content in string or block-list form."""
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                value = block.get("text")
                if isinstance(value, str):
                    parts.append(value)
        return "\n".join(part for part in parts if part).strip()
    return str(content or "").strip()


def _make_model(model_name: str) -> ChatGroq:
    return ChatGroq(
        model=model_name,
        api_key=_API_KEY,
        temperature=0.1,
        max_tokens=MAX_OUTPUT_TOKENS,
        max_retries=0,
        timeout=REQUEST_TIMEOUT,
    )


def _load_cooldowns() -> dict[str, float]:
    try:
        data = json.loads(COOLDOWN_FILE.read_text(encoding="utf-8"))
        now = time.time()
        return {
            str(model): float(until)
            for model, until in data.items()
            if float(until) > now
        }
    except (OSError, ValueError, TypeError, json.JSONDecodeError, AttributeError):
        return {}


class GroqModelRouter:
    """Try every currently available model, failing over on errors or blank output."""

    def __init__(self, model_names: list[str]):
        self.model_names = list(dict.fromkeys(model_names))
        self.models = {name: _make_model(name) for name in self.model_names}
        self.cooldowns = _load_cooldowns()
        self.next_index = 0
        self._lock = asyncio.Lock()

    def _save_cooldowns(self) -> None:
        try:
            COOLDOWN_FILE.write_text(json.dumps(self.cooldowns, indent=2), encoding="utf-8")
        except OSError as exc:
            print(f"   ⚠️ Could not save model cooldowns: {exc}")

    @staticmethod
    def _cooldown_seconds(error_text: str) -> int:
        text = error_text.lower()
        composite = re.search(
            r"(?:try again in|retry after)\s*(?:(\d+)h)?(?:(\d+)m)?(\d+(?:\.\d+)?)s",
            text,
        )
        explicit_seconds = None
        if composite and any(composite.groups()):
            hours = int(composite.group(1) or 0)
            minutes = int(composite.group(2) or 0)
            seconds = float(composite.group(3) or 0)
            explicit_seconds = max(2, int(hours * 3600 + minutes * 60 + seconds) + 2)

        if any(term in text for term in (
            "tokens per day", "token per day", "requests per day",
            "daily limit", "tpd", "rpd",
        )):
            return explicit_seconds if explicit_seconds is not None else 24 * 60 * 60

        if explicit_seconds is not None:
            return explicit_seconds

        match = re.search(
            r"(?:try again in|retry after)\s*(\d+(?:\.\d+)?)\s*"
            r"(ms|milliseconds?|s|sec(?:onds?)?|m|mins?|minutes?|h|hours?)",
            text,
        )
        if match:
            value = float(match.group(1))
            unit = match.group(2)
            if unit.startswith("ms") or unit.startswith("millisecond"):
                return max(2, int(value / 1000) + 2)
            if unit.startswith("m"):
                return max(5, int(value * 60) + 2)
            if unit.startswith("h"):
                return max(60, int(value * 3600) + 2)
            return max(2, int(value) + 2)

        if "429" in text or "rate limit" in text or "too many requests" in text:
            return 60
        if any(term in text for term in ("model_not_found", "decommission", "not available")):
            return 6 * 60 * 60
        return 30

    def _mark_cooldown(self, model_name: str, seconds: int, reason: str) -> None:
        self.cooldowns[model_name] = time.time() + seconds
        self._save_cooldowns()
        print(
            f"   ⏸️ Model {model_name} cooling down for about "
            f"{max(1, seconds // 60)} minute(s): {reason[:180]}"
        )

    async def ainvoke(self, prompt: Any, config: Any = None, **kwargs: Any) -> Any:
        """Invoke a model; blank responses are failures and trigger immediate failover."""
        async with self._lock:
            now = time.time()
            active = [name for name in self.model_names if self.cooldowns.get(name, 0) <= now]
            if not active:
                soonest = min(
                    (self.cooldowns.get(name, now + 86400) for name in self.model_names),
                    default=now + 86400,
                )
                wait_seconds = max(0, int(soonest - now))
                if 0 < wait_seconds <= 10:
                    print(f"   ⏳ All models briefly rate-limited; waiting {wait_seconds}s.")
                    await asyncio.sleep(wait_seconds + 1)
                    now = time.time()
                    active = [name for name in self.model_names if self.cooldowns.get(name, 0) <= now]
                if not active:
                    raise RuntimeError(
                        "All configured Groq models are cooling down or unavailable. "
                        f"Earliest retry in about {max(1, wait_seconds // 60)} minute(s)."
                    )

            start = self.next_index % len(self.model_names)
            ordered = self.model_names[start:] + self.model_names[:start]
            self.next_index = (self.next_index + 1) % len(self.model_names)
            candidates = [name for name in ordered if name in active]
            errors: list[str] = []

            for model_name in candidates:
                try:
                    response = await self.models[model_name].ainvoke(prompt, config=config, **kwargs)
                    content = extract_response_text(response)
                    if not content:
                        reason = "Empty response from the model"
                        self._mark_cooldown(model_name, EMPTY_RESPONSE_COOLDOWN, reason)
                        errors.append(f"{model_name}: {reason}")
                        continue
                    print(f"   🤖 Groq model used: {model_name}")
                    return response
                except Exception as exc:
                    error_text = f"{type(exc).__name__}: {exc}"
                    cooldown = self._cooldown_seconds(error_text)
                    self._mark_cooldown(model_name, cooldown, error_text)
                    errors.append(f"{model_name}: {error_text[:220]}")

            raise RuntimeError(
                "All available Groq models failed for this request. " + " | ".join(errors)
            )


# Evaluation and feedback share one router, so cooldowns apply to every LLM task.
llm = GroqModelRouter(MODEL_ORDER)

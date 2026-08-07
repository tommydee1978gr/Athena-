from __future__ import annotations

import json
import os
import re
import secrets
from pathlib import Path
from typing import Any

import httpx

from .config import CLIPROXY_CONFIG_PATH, CLIPROXY_DIR, CLIPROXY_LOG_DIR, CLIPROXY_SECRETS_PATH

CLIPROXY_INTERNAL_BASE_URL = os.getenv("ATHENA_CLIPROXY_BASE_URL", "http://127.0.0.1:8317").rstrip("/")

BRAIN_FAMILIES = ("claude", "codex_openai", "gemini", "grok")
BRAIN_FAMILY_LABELS = {
    "claude": "Claude",
    "codex_openai": "Codex/OpenAI",
    "gemini": "Gemini",
    "grok": "Grok",
}


class CLIProxyError(RuntimeError):
    def __init__(self, status: str, message: str, *, details: Any = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.details = details


def bootstrap() -> dict[str, str]:
    """Create the persistent CLIProxyAPI runtime without personal credentials."""
    CLIPROXY_DIR.mkdir(parents=True, exist_ok=True)
    (CLIPROXY_DIR / "auths").mkdir(parents=True, exist_ok=True)
    CLIPROXY_LOG_DIR.mkdir(parents=True, exist_ok=True)

    if CLIPROXY_SECRETS_PATH.exists():
        data = json.loads(CLIPROXY_SECRETS_PATH.read_text(encoding="utf-8"))
    else:
        data = {
            "api_key": secrets.token_urlsafe(48),
            "management_key": secrets.token_urlsafe(48),
        }
        CLIPROXY_SECRETS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(CLIPROXY_SECRETS_PATH, 0o600)

    required = ("api_key", "management_key")
    if not all(isinstance(data.get(key), str) and len(data[key]) >= 32 for key in required):
        raise RuntimeError("CLIProxyAPI secrets file is invalid")

    if not CLIPROXY_CONFIG_PATH.exists():
        config = f'''host: "0.0.0.0"
port: 8317
tls:
  enable: false
  cert: ""
  key: ""
remote-management:
  allow-remote: true
  secret-key: "{data['management_key']}"
  disable-control-panel: false
  disable-auto-update-panel: false
auth-dir: "/config/cliproxy/auths"
api-keys:
  - "{data['api_key']}"
debug: false
commercial-mode: false
logging-to-file: true
logs-max-total-size-mb: 100
error-logs-max-files: 25
usage-statistics-enabled: false
proxy-url: ""
request-retry: 3
'''
        CLIPROXY_CONFIG_PATH.write_text(config, encoding="utf-8")
        os.chmod(CLIPROXY_CONFIG_PATH, 0o600)

    return {"api_key": data["api_key"], "management_key": data["management_key"]}


def secrets_data() -> dict[str, str] | None:
    try:
        data = json.loads(CLIPROXY_SECRETS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not data.get("api_key") or not data.get("management_key"):
        return None
    return {"api_key": str(data["api_key"]), "management_key": str(data["management_key"])}


def api_headers() -> dict[str, str]:
    data = secrets_data()
    if not data:
        raise CLIProxyError("not_configured", "CLIProxyAPI runtime secrets are not initialized")
    return {"Authorization": f"Bearer {data['api_key']}", "Content-Type": "application/json"}


def model_family(model_id: str) -> str | None:
    """Map a discovered CLIProxyAPI model id to one of ATHENA's four brain families."""
    value = model_id.strip().lower()
    if not value:
        return None
    if "claude" in value or "anthropic" in value:
        return "claude"
    if "gemini" in value or "google" in value:
        return "gemini"
    if "grok" in value or "xai" in value or "x-ai" in value:
        return "grok"
    if any(token in value for token in ("codex", "openai", "chatgpt", "gpt-", "gpt_")):
        return "codex_openai"
    if re.search(r"(?:^|[-_/])(o1|o3|o4)(?:$|[-_/])", value):
        return "codex_openai"
    return None


def _rank_model(model_id: str, *, prefer_fast: bool) -> tuple[int, str]:
    value = model_id.lower()
    score = 0
    strong_tokens = ("opus", "pro", "max", "reasoning", "thinking", "gpt-5", "gpt-4.1", "o3", "o4")
    fast_tokens = ("mini", "nano", "flash", "haiku", "lite", "fast")
    if prefer_fast:
        score += sum(8 for token in fast_tokens if token in value)
        score -= sum(3 for token in strong_tokens if token in value)
    else:
        score += sum(8 for token in strong_tokens if token in value)
        score -= sum(3 for token in fast_tokens if token in value)
    if "latest" in value:
        score += 2
    return score, value


def _select_family_model(models: list[str], *, prefer_fast: bool) -> str:
    return max(models, key=lambda item: _rank_model(item, prefer_fast=prefer_fast))


def _task_profile(question: str) -> dict[str, Any]:
    """Tommy's own framing (2026-08-07): Gemini takes the light stuff, Claude is the
    organizer (reasoning/planning/coordination), Codex/OpenAI is the worker (execution,
    coding, doing the actual task). Grok stays the realtime/social specialist. Claude
    remains the default fallback for anything ambiguous or substantial — an organizer's
    job when nothing else clearly claims the task."""
    value = question.lower()
    execution_work = (
        "code", "python", "docker", "github", "unraid", "api", "yaml", "json", "sql", "bug", "compile",
        "build", "fix", "implement", "write", "create", "send", "upload", "run", "execute", "deploy",
        "κώδικ", "προγραμματ", "σφάλμα", "workflow", "script", "φτιάξ", "στείλ", "ανέβασ", "εκτέλεσ",
        "κάνε", "διόρθωσ", "γράψε",
    )
    google_multimodal = (
        "image", "photo", "video", "pdf", "vision", "map", "youtube", "gmail", "calendar", "google",
        "εικόν", "φωτο", "βίντεο", "χάρτ", "youtube", "gmail", "ημερολόγ",
    )
    realtime_social = (
        "latest", "today", "now", "news", "trend", "twitter", "x.com", "social", "tiktok", "current",
        "σήμερα", "τώρα", "τελευταί", "νέα", "είδηση", "τάση", "κοινωνικ", "tiktok",
    )
    deep_reasoning = (
        "analyze", "analysis", "architecture", "strategy", "compare", "explain", "plan", "research", "reasoning",
        "why", "should", "decide", "recommend",
        "ανάλυσ", "αρχιτεκτον", "στρατηγ", "σύγκριν", "εξήγη", "σχέδιο", "έρευνα", "σχεδιασ",
        "γιατί", "πρέπει", "αποφάσ", "προτείν",
    )
    light_quick = (
        "hi", "hello", "thanks", "thank you", "what time", "what is", "quick", "simple", "translate", "spell", "define",
        "γεια", "ευχαριστ", "τι ώρα", "τι είναι", "γρήγορ", "απλ", "μετάφρασ", "ορθογραφ", "ορισμ",
    )
    scores = {
        "codex_openai": sum(1 for token in execution_work if token in value),
        "gemini": sum(1 for token in google_multimodal if token in value),
        "grok": sum(1 for token in realtime_social if token in value),
        "claude": sum(1 for token in deep_reasoning if token in value),
    }
    light_score = sum(1 for token in light_quick if token in value)
    if max(scores.values(), default=0) == 0:
        if light_score > 0 or len(question) < 60:
            # Nothing substantial claimed it and it reads short/simple — this is exactly
            # the "light topic" Gemini should take, not something worth Claude's reasoning.
            primary_family = "gemini"
            reason = "light_quick_task"
        else:
            primary_family = "claude"
            reason = "general_coordination"
    else:
        priority = {"codex_openai": 4, "gemini": 3, "grok": 2, "claude": 1}
        primary_family = max(scores, key=lambda family: (scores[family], priority[family]))
        reason = {
            "codex_openai": "execution_or_worker_task",
            "gemini": "google_multimodal_or_light_task",
            "grok": "realtime_or_social_task",
            "claude": "deep_reasoning_or_organizer_task",
        }[primary_family]
    complexity = "complex" if len(question) >= 1200 or sum(scores.values()) >= 4 else "normal"
    return {
        "primary_family": primary_family,
        "reason": reason,
        "complexity": complexity,
        "prefer_fast": complexity == "normal" and len(question) < 280,
    }


async def model_catalog() -> dict[str, Any]:
    data = secrets_data()
    if not data or not CLIPROXY_CONFIG_PATH.exists():
        return {
            "status": "not_configured",
            "selection": "athena_automatic",
            "models": [],
            "families": {family: [] for family in BRAIN_FAMILIES},
            "connected_families": [],
            "missing_families": list(BRAIN_FAMILIES),
        }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(f"{CLIPROXY_INTERNAL_BASE_URL}/v1/models", headers=api_headers())
    except httpx.RequestError as exc:
        return {
            "status": "provider_unavailable",
            "selection": "athena_automatic",
            "models": [],
            "families": {family: [] for family in BRAIN_FAMILIES},
            "connected_families": [],
            "missing_families": list(BRAIN_FAMILIES),
            "error": str(exc),
        }
    if response.status_code in {401, 403}:
        return {
            "status": "authorization_required",
            "selection": "athena_automatic",
            "models": [],
            "families": {family: [] for family in BRAIN_FAMILIES},
            "connected_families": [],
            "missing_families": list(BRAIN_FAMILIES),
            "error": response.text[:1000],
        }
    if response.status_code >= 400:
        return {
            "status": "error",
            "selection": "athena_automatic",
            "models": [],
            "families": {family: [] for family in BRAIN_FAMILIES},
            "connected_families": [],
            "missing_families": list(BRAIN_FAMILIES),
            "provider_http_status": response.status_code,
            "error": response.text[:1000],
        }
    try:
        payload = response.json()
        models = sorted({str(item["id"]) for item in payload.get("data", []) if isinstance(item, dict) and item.get("id")})
    except (ValueError, TypeError, KeyError) as exc:
        return {
            "status": "error",
            "selection": "athena_automatic",
            "models": [],
            "families": {family: [] for family in BRAIN_FAMILIES},
            "connected_families": [],
            "missing_families": list(BRAIN_FAMILIES),
            "error": f"Invalid /v1/models response: {exc}",
        }

    families: dict[str, list[str]] = {family: [] for family in BRAIN_FAMILIES}
    unclassified: list[str] = []
    for model in models:
        family = model_family(model)
        if family:
            families[family].append(model)
        else:
            unclassified.append(model)
    connected = [family for family in BRAIN_FAMILIES if families[family]]
    missing = [family for family in BRAIN_FAMILIES if not families[family]]
    if not models:
        status = "authorization_required"
        detail = "no_llm_accounts_connected"
    elif not connected:
        status = "model_unavailable"
        detail = "no_recognized_athena_brain_family"
    elif missing:
        status = "degraded"
        detail = "athena_brain_missing_one_or_more_llm_families"
    else:
        status = "connected"
        detail = "athena_four_family_brain_ready"
    return {
        "status": status,
        "selection": "athena_automatic",
        "models": models,
        "families": families,
        "connected_families": connected,
        "missing_families": missing,
        "unclassified_models": unclassified,
        "detail": detail,
    }


async def automatic_route(question: str) -> dict[str, Any]:
    """Build an internal route. No caller or user may select a model."""
    catalog = await model_catalog()
    if catalog["status"] not in {"connected", "degraded"}:
        raise CLIProxyError(
            catalog["status"],
            catalog.get("error") or catalog.get("detail") or "ATHENA LLM brain is not ready",
            details=catalog,
        )
    profile = _task_profile(question)
    families: dict[str, list[str]] = catalog["families"]
    primary = profile["primary_family"]
    fallback_order = {
        "claude": ["codex_openai", "gemini", "grok"],
        "codex_openai": ["claude", "gemini", "grok"],
        "gemini": ["claude", "codex_openai", "grok"],
        "grok": ["gemini", "claude", "codex_openai"],
    }[primary]
    family_order = [primary, *fallback_order]
    available_order = [family for family in family_order if families.get(family)]
    if not available_order:
        raise CLIProxyError("model_unavailable", "No recognized ATHENA brain model is available", details=catalog)
    selected_family = available_order[0]
    ordered_models = [
        _select_family_model(families[family], prefer_fast=bool(profile["prefer_fast"]))
        for family in available_order
    ]
    return {
        "selection": "athena_automatic",
        "primary_family": selected_family,
        "primary_model": ordered_models[0],
        "fallback_models": ordered_models[1:],
        "family_order": available_order,
        "reason": profile["reason"],
        "complexity": profile["complexity"],
        "brain_status": catalog["status"],
        "connected_families": catalog["connected_families"],
        "missing_families": catalog["missing_families"],
    }


async def chat_completions(payload: dict[str, Any], timeout: float = 180.0) -> httpx.Response:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await client.post(
                f"{CLIPROXY_INTERNAL_BASE_URL}/v1/chat/completions",
                headers=api_headers(),
                json=payload,
            )
    except httpx.RequestError as exc:
        raise CLIProxyError("provider_unavailable", str(exc)) from exc


async def chat_completions_stream(payload: dict[str, Any], timeout: float = 180.0):
    """Async generator over /v1/chat/completions with stream=True — yields each
    parsed SSE chunk dict as it arrives instead of waiting for the full
    response. This is what lets ATHENA show text as it's generated rather than
    only after the whole answer (and any tool-calling round before it) finishes.
    Raises CLIProxyError (before yielding anything) on a failed connection or
    an error status from the very first response."""
    payload = dict(payload)
    payload["stream"] = True
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST",
                f"{CLIPROXY_INTERNAL_BASE_URL}/v1/chat/completions",
                headers=api_headers(),
                json=payload,
            ) as response:
                if response.status_code >= 400:
                    body = await response.aread()
                    status = "provider_unavailable" if response.status_code in (401, 403) else "provider_http_error"
                    raise CLIProxyError(status, body.decode("utf-8", "replace")[:500], details={"status_code": response.status_code})
                async for line in response.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        break
                    try:
                        yield json.loads(data)
                    except ValueError:
                        continue
    except httpx.RequestError as exc:
        raise CLIProxyError("provider_unavailable", str(exc)) from exc


async def generate_image(prompt: str, size: str = "1024x1024", timeout: float = 120.0) -> httpx.Response:
    """POST /v1/images/generations on the embedded CLIProxyAPI. Backed by the
    Codex/ChatGPT OAuth connection (GPT Image) — no separate API key, uses
    whatever "GPT Image 2 Base Model" is configured server-side in CLIProxyAPI
    when no model is given, so we deliberately omit the "model" field rather
    than guess an exact image-model id."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await client.post(
                f"{CLIPROXY_INTERNAL_BASE_URL}/v1/images/generations",
                headers=api_headers(),
                json={"prompt": prompt, "n": 1, "size": size},
            )
    except httpx.RequestError as exc:
        raise CLIProxyError("provider_unavailable", str(exc)) from exc

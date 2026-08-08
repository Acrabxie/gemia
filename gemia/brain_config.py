"""大脑(编排 LLM) provider 配置的读/写/自检中枢。

Setup UI 与 CLI 都经此模块，保证：
  1. 密钥永不回传前端——read_status 只给 has_key 布尔。
  2. 只白名单大脑相关字段——绝不碰 config.json 里的 smtp/cloudflare/oauth 等敏感块。
  3. 写入即设 env → 新会话即时生效（无需重启，与既有 /config POST 行为一致）。

字段口径与 gemini_client.py 的 provider 解析完全对齐（见其 docstring）。
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

OPENAI_SUBSCRIPTION_BRIDGE_ROOT = os.environ.get(
    "LUMERI_CODEX_BRIDGE_ROOT", "http://127.0.0.1:7808"
).rstrip("/")
OPENAI_SUBSCRIPTION_BASE_URL = f"{OPENAI_SUBSCRIPTION_BRIDGE_ROOT}/v1/chat/completions"
OPENAI_SUBSCRIPTION_MODE = "subscription"

# 常见供应商目录：前端据此渲染卡片；custom = OpenAI 兼容自定义 base_url。
PROVIDERS: list[dict[str, Any]] = [
    {
        "id": "vertex",
        "label": "Google Vertex AI",
        "hint": "用 GCP ADC 鉴权（gcloud auth）+ 项目/区域；Gemini 与 Vertex 版 Claude 均走此路",
        "fields": ["vertex_project", "vertex_location", "model"],
        "key_field": None,  # Vertex 用 ADC，不需明文 key
        "recommended_model": "google/gemini-3.5-flash",
        "model_presets": [
            "google/gemini-2.5-pro",
            "google/gemini-3.5-flash",
            "anthropic/claude-sonnet-5",
        ],
    },
    {
        "id": "gemini",
        "label": "Google Gemini API",
        "hint": "AI Studio 的 GEMINI_API_KEY（generativelanguage 端点）",
        "fields": ["model"],
        "key_field": "gemini_api_key",
        "recommended_model": "gemini-2.5-pro",
        "model_presets": ["gemini-2.5-pro", "gemini-2.5-flash"],
    },
    {
        "id": "openai",
        "label": "OpenAI",
        "hint": "OPENAI_API_KEY（可选自定义 base_url，指向兼容网关）",
        "fields": ["model", "base_url"],
        "key_field": "openai_api_key",
        "recommended_model": "gpt-5.6-sol",
        "model_presets": ["gpt-5.6-sol", "gpt-5.5"],
    },
    {
        "id": "openai_subscription",
        "label": "OpenAI 订阅额度",
        "hint": "使用本机 Codex 登录的 ChatGPT 订阅额度，无需 API Key",
        "fields": ["model"],
        "key_field": None,
        "recommended_model": "gpt-5.6-sol",
        "model_presets": ["gpt-5.6-sol", "gpt-5.5", "gpt-5.4"],
    },
    {
        "id": "claude",
        "label": "Anthropic Claude",
        "hint": "ANTHROPIC_API_KEY（api.anthropic.com）",
        "fields": ["model"],
        "key_field": "anthropic_api_key",
        "recommended_model": "claude-opus-4-8",
        "model_presets": ["claude-opus-4-8", "claude-sonnet-5", "claude-fable-5"],
    },
    {
        "id": "openrouter",
        "label": "OpenRouter",
        "hint": "OPENROUTER_API_KEY（聚合网关，一个 key 通多家）",
        "fields": ["model"],
        "key_field": "openrouter_api_key",
        "recommended_model": "anthropic/claude-opus-4.8",
        "model_presets": [
            "anthropic/claude-fable-5",
            "anthropic/claude-opus-4.8",
            "openai/gpt-5.5",
            "google/gemini-2.5-pro",
        ],
    },
    {
        "id": "custom",
        "label": "自定义（OpenAI 兼容）",
        "hint": "任意 OpenAI 兼容端点：填 base_url + key + 模型名（走 openai 通道）",
        "fields": ["base_url", "model"],
        "key_field": "openai_api_key",
        "recommended_model": "",
        "model_presets": [],
    },
]

EFFORTS = ["none", "low", "medium", "high", "xhigh"]

# body 字段 → (config.json 键, 环境变量名)。仅这些字段会被写入。
_STR_FIELDS = {
    "provider": ("lumeri_v3_provider", "LUMERI_V3_PROVIDER"),
    "model": ("lumeri_v3_model", "LUMERI_V3_MODEL"),
    "effort": ("lumeri_v3_effort", "LUMERI_V3_EFFORT"),
    "location": ("lumeri_v3_location", "LUMERI_V3_LOCATION"),
    "vertex_project": ("vertex_project", "VERTEX_PROJECT"),
    "vertex_location": ("vertex_location", "VERTEX_LOCATION"),
    "base_url": ("lumeri_openai_base_url", "LUMERI_OPENAI_BASE_URL"),
    "openai_auth_mode": ("lumeri_openai_auth_mode", "LUMERI_OPENAI_AUTH_MODE"),
}
# 密钥字段：仅当非空才覆盖（避免留空表单误清已存 key）。
_KEY_FIELDS = {
    "openrouter_api_key": "OPENROUTER_API_KEY",
    "gemini_api_key": "GEMINI_API_KEY",
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "openai_api_key": "OPENAI_API_KEY",
}
_LEGACY_STRONGEST_KEYS = (
    "lumeri_v3_force_strongest",
    "lumeri_v3_strongest_model",
    "lumeri_v3_strongest_provider",
    "lumeri_v3_strongest_effort",
)
_PROFILE_CONFIG_KEY = "brain_provider_profiles"
_ACTIVE_PROFILE_KEY = "brain_active_profile"
_FIXED_PROFILE_IDS = {
    "vertex",
    "gemini",
    "openai",
    "openai_subscription",
    "claude",
    "openrouter",
}


def _custom_profile_id(name: str, existing: dict[str, Any]) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(name or "").strip().lower()).strip("-")
    base = f"custom:{slug or 'custom'}"
    candidate = base
    ordinal = 2
    while candidate in existing:
        candidate = f"{base}-{ordinal}"
        ordinal += 1
    return candidate


def _legacy_provider(config: dict[str, Any]) -> str:
    provider = str(config.get("lumeri_v3_provider") or "").strip()
    auth_mode = str(config.get("lumeri_openai_auth_mode") or "").strip()
    base_url = str(config.get("lumeri_openai_base_url") or "").strip()
    if provider == "openai" and (
        auth_mode == OPENAI_SUBSCRIPTION_MODE
        or base_url.rstrip("/") == OPENAI_SUBSCRIPTION_BASE_URL
    ):
        return "openai_subscription"
    return provider


def _legacy_profiles(config: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], str]:
    """Build isolated profiles without mutating the legacy config."""
    active_provider = _legacy_provider(config)
    active_profile = active_provider if active_provider in _FIXED_PROFILE_IDS else ""
    profiles: dict[str, dict[str, Any]] = {}

    for provider in _FIXED_PROFILE_IDS:
        profile: dict[str, Any] = {
            "provider": provider,
            "name": str((provider_spec(provider) or {}).get("label") or provider),
            "model": (
                str(config.get("lumeri_v3_model") or "")
                if active_provider == provider
                else ""
            ),
            "effort": (
                str(config.get("lumeri_v3_effort") or "medium")
                if active_provider == provider
                else "medium"
            ),
        }
        if provider == "vertex":
            profile.update(
                {
                    "vertex_project": str(config.get("vertex_project") or ""),
                    "vertex_location": str(config.get("vertex_location") or ""),
                    "location": str(config.get("lumeri_v3_location") or "global"),
                }
            )
        elif provider == "gemini":
            profile["api_key"] = str(config.get("gemini_api_key") or "")
        elif provider == "claude":
            profile["api_key"] = str(config.get("anthropic_api_key") or "")
        elif provider == "openrouter":
            profile["api_key"] = str(config.get("openrouter_api_key") or "")
        elif provider == "openai":
            profile["api_key"] = str(config.get("openai_api_key") or "")
            if active_provider == provider:
                profile["base_url"] = str(config.get("lumeri_openai_base_url") or "")
            profile["auth_mode"] = "api_key"
        elif provider == "openai_subscription":
            profile["base_url"] = OPENAI_SUBSCRIPTION_BASE_URL
            profile["auth_mode"] = OPENAI_SUBSCRIPTION_MODE
        profiles[provider] = profile

    if active_provider == "custom":
        profile_id = "custom:default"
        profiles[profile_id] = {
            "provider": "custom",
            "name": "自定义",
            "model": str(config.get("lumeri_v3_model") or ""),
            "effort": str(config.get("lumeri_v3_effort") or "medium"),
            "base_url": str(config.get("lumeri_openai_base_url") or ""),
            "auth_mode": "api_key",
            "api_key": str(config.get("openai_api_key") or ""),
        }
        active_profile = profile_id

    # Older Sisyphus fields already express a distinct named custom instance.
    sisyphus_base = str(config.get("sisyphus_base_url") or "").strip()
    sisyphus_model = str(config.get("sisyphus_model") or "").strip()
    sisyphus_key = str(config.get("sisyphus_api_key") or "").strip()
    if sisyphus_base or sisyphus_model or sisyphus_key:
        profile_id = "custom:sisyphus"
        profiles.setdefault(
            profile_id,
            {
                "provider": "custom",
                "name": "Sisyphus",
                "model": sisyphus_model,
                "effort": "medium",
                "base_url": sisyphus_base,
                "auth_mode": "api_key",
                "api_key": sisyphus_key,
            },
        )

    if not active_profile:
        active_profile = "openrouter"
    return profiles, active_profile


def provider_profiles(config: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], str]:
    raw = config.get(_PROFILE_CONFIG_KEY)
    if isinstance(raw, dict) and raw:
        profiles = {
            str(profile_id): dict(profile)
            for profile_id, profile in raw.items()
            if isinstance(profile, dict)
        }
        active = str(config.get(_ACTIVE_PROFILE_KEY) or "")
        if active in profiles:
            return profiles, active
    return _legacy_profiles(config)


def ensure_provider_profiles(config: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], str]:
    profiles, active = provider_profiles(config)
    config[_PROFILE_CONFIG_KEY] = profiles
    config[_ACTIVE_PROFILE_KEY] = active
    return profiles, active


def resolve_runtime_config(
    config: dict[str, Any],
    *,
    profile_id: str | None = None,
) -> dict[str, Any]:
    """Return a flattened snapshot containing only one provider's fields."""
    profiles, active = provider_profiles(config)
    selected = str(profile_id or active)
    profile = dict(profiles.get(selected) or {})
    provider = str(profile.get("provider") or selected)
    out = {
        key: value
        for key, value in config.items()
        if key
        not in {
            "lumeri_v3_provider",
            "lumeri_v3_model",
            "lumeri_v3_effort",
            "lumeri_v3_location",
            "vertex_project",
            "vertex_location",
            "lumeri_openai_base_url",
            "lumeri_openai_auth_mode",
            "openrouter_api_key",
            "gemini_api_key",
            "anthropic_api_key",
            "openai_api_key",
        }
    }
    out["brain_profile_isolated"] = True
    out["brain_profile_id"] = selected
    out["brain_logical_provider"] = provider
    out["lumeri_v3_provider"] = (
        "openai" if provider == "openai_subscription" else provider
    )
    out["lumeri_v3_model"] = str(profile.get("model") or "")
    out["lumeri_v3_effort"] = str(profile.get("effort") or "medium")
    out["lumeri_v3_location"] = str(profile.get("location") or "")
    out["vertex_project"] = str(profile.get("vertex_project") or "")
    out["vertex_location"] = str(profile.get("vertex_location") or "")
    out["lumeri_openai_base_url"] = str(profile.get("base_url") or "")
    out["lumeri_openai_auth_mode"] = str(profile.get("auth_mode") or "")
    api_key = str(profile.get("api_key") or "")
    key_field = {
        "openrouter": "openrouter_api_key",
        "gemini": "gemini_api_key",
        "claude": "anthropic_api_key",
        "openai": "openai_api_key",
        "custom": "openai_api_key",
    }.get(provider)
    if key_field:
        out[key_field] = api_key
    return out


def provider_spec(provider: str) -> dict[str, Any] | None:
    return next((item for item in PROVIDERS if item["id"] == provider), None)


def recommended_model(provider: str, models: list[dict[str, Any]] | None = None) -> str:
    """Return the provider-local recommended model, never a model from another provider."""
    spec = provider_spec(provider)
    preferred = str((spec or {}).get("recommended_model") or "").strip()
    if models is None:
        return preferred
    ids = [str(item.get("id") or "").strip() for item in models if isinstance(item, dict)]
    ids = [model_id for model_id in ids if model_id]
    if preferred and preferred in ids:
        return preferred
    for preset in (spec or {}).get("model_presets") or []:
        if preset in ids:
            return preset
    return ids[0] if ids else preferred


def model_matches_provider(provider: str, model: str) -> bool:
    """Reject only clearly cross-provider model ids; custom ids remain possible."""
    value = str(model or "").strip().lower()
    if not value:
        return False
    if provider == "vertex":
        return value.startswith(("google/gemini-", "anthropic/claude-"))
    if provider == "gemini":
        return value.startswith("gemini-")
    if provider == "claude":
        return value.startswith("claude-")
    if provider in {"openai", "openai_subscription"}:
        return "/" not in value and not value.startswith(("gemini-", "claude-"))
    if provider == "openrouter":
        return "/" in value
    return True


def _has(config: dict, key: str) -> bool:
    v = config.get(key)
    return bool(isinstance(v, str) and v.strip())


def read_status(config: dict) -> dict[str, Any]:
    """返回脱敏的大脑配置现状（密钥只给布尔）。供 GET /config 用。"""
    profiles, active_profile = provider_profiles(config)
    active = profiles.get(active_profile) or {}
    provider = str(active.get("provider") or "")
    sanitized_profiles = {}
    for profile_id, profile in profiles.items():
        sanitized_profiles[profile_id] = {
            "id": profile_id,
            "provider": str(profile.get("provider") or ""),
            "name": str(profile.get("name") or profile_id),
            "model": str(profile.get("model") or ""),
            "effort": str(profile.get("effort") or "medium"),
            "location": str(profile.get("location") or ""),
            "vertex_project": str(profile.get("vertex_project") or ""),
            "vertex_location": str(profile.get("vertex_location") or ""),
            "base_url": str(profile.get("base_url") or ""),
            "has_key": bool(str(profile.get("api_key") or "").strip()),
        }
    return {
        "provider": provider,
        "active_profile": active_profile,
        "profiles": sanitized_profiles,
        "model": str(active.get("model") or ""),
        "recommended_model": recommended_model(provider),
        "effort": str(active.get("effort") or "medium"),
        "location": str(active.get("location") or "global"),
        "vertex_project": str((profiles.get("vertex") or {}).get("vertex_project") or ""),
        "vertex_location": str((profiles.get("vertex") or {}).get("vertex_location") or ""),
        "base_url": str(active.get("base_url") or ""),
        "has_key": {
            "openrouter": bool(str((profiles.get("openrouter") or {}).get("api_key") or "").strip()),
            "gemini": bool(str((profiles.get("gemini") or {}).get("api_key") or "").strip()),
            "anthropic": bool(str((profiles.get("claude") or {}).get("api_key") or "").strip()),
            "openai": bool(str((profiles.get("openai") or {}).get("api_key") or "").strip()),
        },
        "providers": PROVIDERS,
        "efforts": EFFORTS,
    }


def apply_update(
    config: dict,
    body: dict,
    *,
    sync_env: bool = True,
) -> tuple[dict, list[str]]:
    """把 body 里的白名单大脑字段合并进 config，并同步设置 env。

    返回 (更新后的 config, 变更字段名列表)。就地修改 config 并返回它。
    """
    body = dict(body)
    profiles, current_profile_id = ensure_provider_profiles(config)
    requested_provider = str(body.get("provider") or "").strip()
    requested_profile_id = str(body.get("profile_id") or "").strip()
    if requested_provider == "custom" and (
        not requested_profile_id or requested_profile_id == "custom:new"
    ):
        requested_profile_id = _custom_profile_id(
            str(body.get("profile_name") or "自定义"), profiles
        )
    if not requested_profile_id:
        requested_profile_id = requested_provider or current_profile_id
    current_provider = str(
        (profiles.get(current_profile_id) or {}).get("provider") or ""
    )
    provider_changed = bool(requested_provider and requested_provider != current_provider)
    requested_model = str(body.get("model") or "").strip()
    if requested_provider and (
        (provider_changed and not requested_model)
        or (requested_model and not model_matches_provider(requested_provider, requested_model))
    ):
        body["model"] = recommended_model(requested_provider)

    if requested_provider == "openai_subscription":
        body["base_url"] = OPENAI_SUBSCRIPTION_BASE_URL
        body["openai_auth_mode"] = OPENAI_SUBSCRIPTION_MODE
    elif requested_provider in {"openai", "custom"}:
        body["openai_auth_mode"] = "api_key"

    valid_providers = {
        "vertex", "gemini", "openai", "openai_subscription",
        "claude", "openrouter", "custom",
    }
    if requested_provider and requested_provider not in valid_providers:
        return config, []

    profile = dict(profiles.get(requested_profile_id) or {})
    profile_provider = requested_provider or str(profile.get("provider") or "")
    if not profile_provider:
        return config, []
    profile["provider"] = profile_provider
    profile["name"] = str(
        body.get("profile_name")
        or profile.get("name")
        or (provider_spec(profile_provider) or {}).get("label")
        or requested_profile_id
    ).strip()
    profile_field_map = {
        "model": "model",
        "effort": "effort",
        "location": "location",
        "vertex_project": "vertex_project",
        "vertex_location": "vertex_location",
        "base_url": "base_url",
        "openai_auth_mode": "auth_mode",
    }
    for field, profile_key in profile_field_map.items():
        if field in body:
            profile[profile_key] = str(body.get(field) or "").strip()
    key_field = (provider_spec(profile_provider) or {}).get("key_field")
    if key_field and key_field in body:
        value = str(body.get(key_field) or "").strip()
        if value:
            profile["api_key"] = value
    profiles[requested_profile_id] = profile
    config[_PROFILE_CONFIG_KEY] = profiles
    config[_ACTIVE_PROFILE_KEY] = requested_profile_id

    # Backward-compatible active-view projection. These fields are outputs,
    # never the source once profiles exist; credentials stay profile-only.
    runtime_provider = (
        "openai" if profile_provider == "openai_subscription" else profile_provider
    )
    projection = {
        "lumeri_v3_provider": runtime_provider,
        "lumeri_v3_model": str(profile.get("model") or ""),
        "lumeri_v3_effort": str(profile.get("effort") or "medium"),
        "lumeri_v3_location": str(profile.get("location") or ""),
        "vertex_project": str(profile.get("vertex_project") or ""),
        "vertex_location": str(profile.get("vertex_location") or ""),
        "lumeri_openai_base_url": str(profile.get("base_url") or ""),
        "lumeri_openai_auth_mode": str(profile.get("auth_mode") or ""),
    }
    for cfg_key, value in projection.items():
        config[cfg_key] = value
    if sync_env:
        for cfg_key, env_key in (
            ("lumeri_v3_provider", "LUMERI_V3_PROVIDER"),
            ("lumeri_v3_model", "LUMERI_V3_MODEL"),
            ("lumeri_v3_effort", "LUMERI_V3_EFFORT"),
            ("lumeri_v3_location", "LUMERI_V3_LOCATION"),
            ("vertex_project", "VERTEX_PROJECT"),
            ("vertex_location", "VERTEX_LOCATION"),
            ("lumeri_openai_base_url", "LUMERI_OPENAI_BASE_URL"),
            ("lumeri_openai_auth_mode", "LUMERI_OPENAI_AUTH_MODE"),
        ):
            value = str(projection.get(cfg_key) or "")
            if value:
                os.environ[env_key] = value
            else:
                os.environ.pop(env_key, None)

    changed: list[str] = []
    for field, (cfg_key, env_key) in _STR_FIELDS.items():
        if field not in body:
            continue
        # Provider-owned values live only in the selected profile. Legacy flat
        # fields remain untouched as rollback evidence and are never synced
        # into the daemon environment after profile migration.
        if field in {
            "provider", "model", "effort", "location", "vertex_project",
            "vertex_location", "base_url", "openai_auth_mode",
        }:
            continue
        val = str(body.get(field) or "").strip()
        # provider 必须在已知运行时集合内。
        if field == "provider" and val and val not in valid_providers:
            continue
        config[cfg_key] = val
        if sync_env:
            if val:
                os.environ[env_key] = val
            else:
                os.environ.pop(env_key, None)
        changed.append(cfg_key)

    for key_field, env_key in _KEY_FIELDS.items():
        if key_field not in body:
            continue
        # Keys are already stored in the selected profile above.
        continue

    changed.extend([*projection.keys(), _PROFILE_CONFIG_KEY, _ACTIVE_PROFILE_KEY])

    if requested_provider or "model" in body:
        for legacy_key in _LEGACY_STRONGEST_KEYS:
            if legacy_key in config:
                config.pop(legacy_key, None)
                changed.append(legacy_key)

    return config, changed


def list_models(provider: str, config: dict, proxy: str | None = None) -> dict[str, Any]:
    """查询 provider 的可用模型列表。返回 {ok, models: [{id, name?}], error?}。"""
    if (
        provider == "custom"
        and not isinstance(config.get(_PROFILE_CONFIG_KEY), dict)
        and config.get("lumeri_openai_base_url")
    ):
        config = {
            **config,
            "lumeri_v3_provider": "custom",
            "lumeri_openai_auth_mode": "api_key",
        }
    cloud_mode = False
    try:
        from gemia import cloud_accounts

        cloud_mode = cloud_accounts.enabled()
        if cloud_mode:
            config = cloud_accounts.runtime_model_config(config)
            selected_config = resolve_runtime_config(config)
            selected_provider = str(
                selected_config.get("brain_logical_provider")
                or selected_config.get("lumeri_v3_provider")
                or ""
            )
            requested_provider = "custom" if provider.startswith("custom:") else provider
            if requested_provider != selected_provider:
                return {
                    "ok": False,
                    "error": "provider does not match the cloud account",
                    "models": [],
                }
            if selected_provider == "vertex":
                return {
                    "ok": False,
                    "error": "provider is unavailable in cloud-account mode",
                    "models": [],
                }
    except Exception as exc:
        if cloud_mode or os.environ.get("LUMERI_CLOUD_ACCOUNTS", "").strip().lower() in {
            "1", "true", "yes",
        }:
            return {
                "ok": False,
                "error": f"cloud account resolver unavailable: {str(exc)[:120]}",
                "models": [],
            }
    profiles, active = provider_profiles(config)
    profile_id = provider if provider in profiles else active
    if provider.startswith("custom:"):
        profile_id = provider
    runtime_config = resolve_runtime_config(config, profile_id=profile_id)
    provider = str(
        runtime_config.get("brain_logical_provider")
        or runtime_config.get("lumeri_v3_provider")
        or provider
    )
    config = runtime_config
    try:
        allow_env_fallback = not cloud_mode and not cloud_accounts.enabled()
    except Exception:
        allow_env_fallback = os.environ.get("LUMERI_CLOUD_ACCOUNTS", "").strip().lower() not in {
            "1", "true", "yes",
        }
    if provider == "vertex":
        return _list_vertex_models(config, proxy=proxy)

    if provider == "openai_subscription":
        try:
            # Loopback requests must not be sent through the outbound proxy.
            status, payload = _loopback_json("GET", "/v1/models", timeout=8)
            if status >= 400:
                return {"ok": False, "error": f"HTTP {status}", "models": []}
            data = payload.get("data") or []
            models = [
                {"id": m["id"], **({"name": m["name"]} if m.get("name") else {})}
                for m in data
                if isinstance(m, dict) and "id" in m
            ]
            return {
                "ok": True,
                "models": models,
                "recommended_model": recommended_model(provider, models),
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:200], "models": []}

    try:
        if provider in ("openai", "custom"):
            key = config.get("openai_api_key") or (
                os.environ.get("OPENAI_API_KEY") if allow_env_fallback else ""
            ) or ""
            base = config.get("lumeri_openai_base_url") or (
                os.environ.get("LUMERI_OPENAI_BASE_URL") if allow_env_fallback else ""
            ) or "https://api.openai.com/v1/chat/completions"
            root = base.split("/v1/")[0] if "/v1/" in base else base.rstrip("/")
            url = f"{root}/v1/models"
            headers = {"Authorization": f"Bearer {key}"} if key else {}
            request = urllib.request.Request(url, headers=headers)
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"https": proxy}) if proxy
                else urllib.request.ProxyHandler({})
            )
            with opener.open(request, timeout=15) as response:
                data = json.loads(response.read()).get("data") or []
            models = [{"id": m["id"]} for m in data if isinstance(m, dict) and "id" in m]
            models.sort(key=lambda m: m["id"])
            return {
                "ok": True,
                "models": models,
                "recommended_model": recommended_model(provider, models),
            }

        import httpx

        timeout = httpx.Timeout(15, connect=8)
        transport_kw: dict[str, Any] = {}
        if proxy:
            transport_kw["proxy"] = proxy

        if provider == "openrouter":
            key = config.get("openrouter_api_key") or (
                os.environ.get("OPENROUTER_API_KEY") if allow_env_fallback else ""
            ) or ""
            headers = {"Authorization": f"Bearer {key}"} if key else {}
            r = httpx.get("https://openrouter.ai/api/v1/models", headers=headers, timeout=timeout, **transport_kw)
            r.raise_for_status()
            data = r.json().get("data") or []
            models = [{"id": m["id"], "name": m.get("name", "")} for m in data if isinstance(m, dict) and "id" in m]
            return {
                "ok": True,
                "models": models,
                "recommended_model": recommended_model(provider, models),
            }

        if provider == "gemini":
            key = config.get("gemini_api_key") or (
                os.environ.get("GEMINI_API_KEY") if allow_env_fallback else ""
            ) or ""
            r = httpx.get(f"https://generativelanguage.googleapis.com/v1beta/models?key={key}", timeout=timeout, **transport_kw)
            r.raise_for_status()
            raw = r.json().get("models") or []
            models = [{"id": m.get("name", "").replace("models/", ""), "name": m.get("displayName", "")} for m in raw if isinstance(m, dict)]
            return {
                "ok": True,
                "models": models,
                "recommended_model": recommended_model(provider, models),
            }

        # Claude 没有模型 listing API，回退到该 provider 的维护目录。
        p = provider_spec(provider)
        presets = p["model_presets"] if p else []
        models = [{"id": m} for m in presets]
        return {
            "ok": True,
            "models": models,
            "recommended_model": recommended_model(provider, models),
            "from_presets": True,
        }

    except Exception as exc:
        if exc.__class__.__name__ == "HTTPStatusError" and getattr(exc, "response", None) is not None:
            return {"ok": False, "error": f"HTTP {exc.response.status_code}", "models": []}
        return {"ok": False, "error": str(exc)[:200], "models": []}


def _list_vertex_models(config: dict, proxy: str | None = None) -> dict[str, Any]:
    """Read the complete Vertex Model Garden catalog, following every page token."""
    try:
        from gemia.gemini_client import _vertex_access_token

        project = str(
            config.get("vertex_project") or os.environ.get("VERTEX_PROJECT") or ""
        ).strip()
        if not project:
            return {"ok": False, "error": "缺少 GCP 项目 ID", "models": []}
        location = str(
            config.get("vertex_location")
            or config.get("lumeri_v3_location")
            or os.environ.get("VERTEX_LOCATION")
            or "us-central1"
        ).strip()
        # Model Garden listing is regional even when inference uses `global`.
        catalog_location = "us-central1" if location == "global" else location
        root = f"https://{catalog_location}-aiplatform.googleapis.com"
        token = _vertex_access_token(proxy)
        proxy_handler = (
            urllib.request.ProxyHandler({"https": proxy, "http": proxy})
            if proxy
            else urllib.request.ProxyHandler({})
        )
        opener = urllib.request.build_opener(proxy_handler)
        pages = 0
        found: dict[str, dict[str, str]] = {}

        # Query only the two publishers supported by Lumeri's Vertex chat path.
        # The wildcard catalog includes unrelated vision/embedding vendors and
        # is both much larger and slower while adding no selectable brain models.
        for catalog_publisher in ("google", "anthropic"):
            page_token = ""
            publisher_pages = 0
            while publisher_pages < 100:
                params = {
                    "listAllVersions": "true",
                    "pageSize": "100",
                }
                if page_token:
                    params["pageToken"] = page_token
                url = (
                    f"{root}/v1beta1/publishers/{catalog_publisher}/models?"
                    f"{urllib.parse.urlencode(params)}"
                )
                request = urllib.request.Request(
                    url,
                    headers={
                        "Accept": "application/json",
                        "Authorization": f"Bearer {token}",
                        "x-goog-user-project": project,
                    },
                )
                with opener.open(request, timeout=30) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                pages += 1
                publisher_pages += 1
                for raw in payload.get("publisherModels") or []:
                    if not isinstance(raw, dict):
                        continue
                    name = str(raw.get("name") or "")
                    parts = name.split("/")
                    if len(parts) < 4 or parts[0] != "publishers" or parts[2] != "models":
                        continue
                    publisher, model_id = parts[1], "/".join(parts[3:])
                    if publisher == "google" and model_id.startswith("gemini-"):
                        normalized = f"google/{model_id}"
                    elif publisher == "anthropic" and model_id.startswith("claude-"):
                        normalized = f"anthropic/{model_id}"
                    else:
                        continue
                    found[normalized] = {
                        "id": normalized,
                        **(
                            {"name": str(raw.get("displayName"))}
                            if raw.get("displayName")
                            else {}
                        ),
                    }
                page_token = str(payload.get("nextPageToken") or "")
                if not page_token:
                    break

        models = list(found.values())
        preferred = recommended_model("vertex", models)
        models.sort(key=lambda item: (item["id"] != preferred, item["id"]))
        return {
            "ok": True,
            "models": models,
            "recommended_model": preferred,
            "pages": pages,
            "from_catalog": True,
        }
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("error", {}).get("message")
        except Exception:
            detail = ""
        return {
            "ok": False,
            "error": f"Vertex HTTP {exc.code}{': ' + detail if detail else ''}"[:300],
            "models": [],
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300], "models": []}


def _loopback_json(method: str, path: str, *, timeout: int) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        f"{OPENAI_SUBSCRIPTION_BRIDGE_ROOT}{path}",
        data=b"" if method == "POST" else None,
        headers={"Accept": "application/json"},
        method=method,
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            status = int(getattr(response, "status", response.getcode()))
            raw = response.read()
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        raw = exc.read()
    try:
        payload = json.loads(raw.decode("utf-8")) if raw else {}
    except Exception:
        payload = {"error": "Codex bridge returned invalid JSON"}
    if not isinstance(payload, dict):
        payload = {"error": "Codex bridge returned an invalid response"}
    return status, payload


def codex_login_bridge(method: str) -> tuple[int, dict[str, Any]]:
    """Start or inspect the loopback Codex OAuth flow without exposing tokens."""
    normalized = method.strip().upper()
    if normalized == "POST":
        path = "/v1/auth/login"
    elif normalized == "GET":
        path = "/v1/auth/status"
    else:
        return 405, {"error": "unsupported method"}
    try:
        return _loopback_json(normalized, path, timeout=8)
    except Exception as exc:
        return 502, {"error": f"Codex bridge unavailable: {str(exc)[:160]}"}


def test_provider(
    proxy: str | None = None,
    *,
    config: dict[str, Any] | None = None,
    provider_override: str | None = None,
) -> dict[str, Any]:
    """用当前(env/config)配置建一个临时客户端，发极小探针，验证连通与鉴权。

    驱动 stream_turn（客户端自解析 provider/url/key），拿到首个 text_delta 即判成功、
    error 即判失败。不产生副作用、不入会话。返回 {ok, provider, model, sample|error}。
    """
    import asyncio

    try:
        from gemia.gemini_client import GeminiClientV3
    except Exception as exc:  # pragma: no cover - import 环境问题
        return {"ok": False, "error": f"client import 失败: {exc}"}
    try:
        client = GeminiClientV3(
            proxy=proxy,
            config=config,
            cloud_provider_override=provider_override,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    pv = getattr(client, "provider", "?")
    mdl = getattr(client, "model", "?")

    async def _probe() -> dict[str, Any]:
        sample = ""
        async for ev in client.stream_turn([{"role": "user", "content": "hi"}]):
            kind = ev.get("kind")
            if kind == "text_delta":
                sample += ev.get("text", "")
                if len(sample) >= 1:
                    return {"ok": True, "provider": pv, "model": mdl, "sample": sample[:40]}
            elif kind == "error":
                return {"ok": False, "provider": pv, "model": mdl, "error": str(ev.get("error"))[:300]}
            elif kind == "finish":
                return {"ok": True, "provider": pv, "model": mdl, "sample": sample[:40] or "(空)"}
        return {"ok": True, "provider": pv, "model": mdl, "sample": sample[:40] or "(无输出)"}

    try:
        return asyncio.run(asyncio.wait_for(_probe(), timeout=90))
    except asyncio.TimeoutError:
        return {"ok": False, "provider": pv, "model": mdl, "error": "探针超时(90s)"}
    except Exception as exc:
        return {"ok": False, "provider": pv, "model": mdl, "error": str(exc)[:300]}

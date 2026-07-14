"""大脑(编排 LLM) provider 配置的读/写/自检中枢。

Setup UI 与 CLI 都经此模块，保证：
  1. 密钥永不回传前端——read_status 只给 has_key 布尔。
  2. 只白名单大脑相关字段——绝不碰 config.json 里的 smtp/cloudflare/oauth 等敏感块。
  3. 写入即设 env → 新会话即时生效（无需重启，与既有 /config POST 行为一致）。

字段口径与 gemini_client.py 的 provider 解析完全对齐（见其 docstring）。
"""
from __future__ import annotations

import os
from typing import Any

# 常见供应商目录：前端据此渲染卡片；custom = OpenAI 兼容自定义 base_url。
PROVIDERS: list[dict[str, Any]] = [
    {
        "id": "vertex",
        "label": "Google Vertex AI",
        "hint": "用 GCP ADC 鉴权（gcloud auth）+ 项目/区域；Gemini 与 Vertex 版 Claude 均走此路",
        "fields": ["vertex_project", "vertex_location", "model"],
        "key_field": None,  # Vertex 用 ADC，不需明文 key
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
        "model_presets": ["gemini-2.5-pro", "gemini-2.5-flash"],
    },
    {
        "id": "openai",
        "label": "OpenAI",
        "hint": "OPENAI_API_KEY（可选自定义 base_url，指向兼容网关）",
        "fields": ["model", "base_url"],
        "key_field": "openai_api_key",
        "model_presets": ["gpt-5.5", "gpt-5.6-sol"],
    },
    {
        "id": "claude",
        "label": "Anthropic Claude",
        "hint": "ANTHROPIC_API_KEY（api.anthropic.com）",
        "fields": ["model"],
        "key_field": "anthropic_api_key",
        "model_presets": ["claude-opus-4-8", "claude-sonnet-5", "claude-fable-5"],
    },
    {
        "id": "openrouter",
        "label": "OpenRouter",
        "hint": "OPENROUTER_API_KEY（聚合网关，一个 key 通多家）",
        "fields": ["model"],
        "key_field": "openrouter_api_key",
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
}
# 密钥字段：仅当非空才覆盖（避免留空表单误清已存 key）。
_KEY_FIELDS = {
    "openrouter_api_key": "OPENROUTER_API_KEY",
    "gemini_api_key": "GEMINI_API_KEY",
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "openai_api_key": "OPENAI_API_KEY",
}


def _has(config: dict, key: str) -> bool:
    v = config.get(key)
    return bool(isinstance(v, str) and v.strip())


def read_status(config: dict) -> dict[str, Any]:
    """返回脱敏的大脑配置现状（密钥只给布尔）。供 GET /config 用。"""
    return {
        "provider": config.get("lumeri_v3_provider") or "",
        "model": config.get("lumeri_v3_model") or "",
        "effort": config.get("lumeri_v3_effort") or "medium",
        "location": config.get("lumeri_v3_location") or "global",
        "vertex_project": config.get("vertex_project") or "",
        "vertex_location": config.get("vertex_location") or "",
        "base_url": config.get("lumeri_openai_base_url") or "",
        "has_key": {
            "openrouter": _has(config, "openrouter_api_key"),
            "gemini": _has(config, "gemini_api_key"),
            "anthropic": _has(config, "anthropic_api_key"),
            "openai": _has(config, "openai_api_key"),
        },
        "providers": PROVIDERS,
        "efforts": EFFORTS,
    }


def apply_update(config: dict, body: dict) -> tuple[dict, list[str]]:
    """把 body 里的白名单大脑字段合并进 config，并同步设置 env。

    返回 (更新后的 config, 变更字段名列表)。就地修改 config 并返回它。
    """
    changed: list[str] = []
    for field, (cfg_key, env_key) in _STR_FIELDS.items():
        if field not in body:
            continue
        val = str(body.get(field) or "").strip()
        # provider 必须在已知集合内（含 custom 会在下方翻译）
        if field == "provider" and val and val not in {p["id"] for p in PROVIDERS}:
            continue
        # custom 只是 UI 概念 → 实际走 openai 通道 + 自定义 base_url
        if field == "provider" and val == "custom":
            val = "openai"
        config[cfg_key] = val
        if val:
            os.environ[env_key] = val
        else:
            os.environ.pop(env_key, None)
        changed.append(cfg_key)

    for key_field, env_key in _KEY_FIELDS.items():
        if key_field not in body:
            continue
        val = str(body.get(key_field) or "").strip()
        if not val:  # 留空不清 key（要清需显式传特殊标记，此处从简）
            continue
        config[key_field] = val
        os.environ[env_key] = val
        changed.append(key_field)

    return config, changed


def list_models(provider: str, config: dict, proxy: str | None = None) -> dict[str, Any]:
    """查询 provider 的可用模型列表。返回 {ok, models: [{id, name?}], error?}。"""
    import httpx

    timeout = httpx.Timeout(15, connect=8)
    transport_kw: dict[str, Any] = {}
    if proxy:
        transport_kw["proxy"] = proxy

    try:
        if provider in ("openai", "custom"):
            key = config.get("openai_api_key") or os.environ.get("OPENAI_API_KEY") or ""
            base = config.get("lumeri_openai_base_url") or os.environ.get("LUMERI_OPENAI_BASE_URL") or "https://api.openai.com/v1/chat/completions"
            root = base.split("/v1/")[0] if "/v1/" in base else base.rstrip("/")
            url = f"{root}/v1/models"
            headers = {"Authorization": f"Bearer {key}"} if key else {}
            r = httpx.get(url, headers=headers, timeout=timeout, **transport_kw)
            r.raise_for_status()
            data = r.json().get("data") or []
            models = [{"id": m["id"]} for m in data if isinstance(m, dict) and "id" in m]
            models.sort(key=lambda m: m["id"])
            return {"ok": True, "models": models}

        if provider == "openrouter":
            key = config.get("openrouter_api_key") or os.environ.get("OPENROUTER_API_KEY") or ""
            headers = {"Authorization": f"Bearer {key}"} if key else {}
            r = httpx.get("https://openrouter.ai/api/v1/models", headers=headers, timeout=timeout, **transport_kw)
            r.raise_for_status()
            data = r.json().get("data") or []
            models = [{"id": m["id"], "name": m.get("name", "")} for m in data if isinstance(m, dict) and "id" in m]
            return {"ok": True, "models": models}

        if provider == "gemini":
            key = config.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY") or ""
            r = httpx.get(f"https://generativelanguage.googleapis.com/v1beta/models?key={key}", timeout=timeout, **transport_kw)
            r.raise_for_status()
            raw = r.json().get("models") or []
            models = [{"id": m.get("name", "").replace("models/", ""), "name": m.get("displayName", "")} for m in raw if isinstance(m, dict)]
            return {"ok": True, "models": models}

        # vertex / claude — 无简便 listing API，返回预设
        p = next((x for x in PROVIDERS if x["id"] == provider), None)
        presets = p["model_presets"] if p else []
        return {"ok": True, "models": [{"id": m} for m in presets], "from_presets": True}

    except httpx.HTTPStatusError as exc:
        return {"ok": False, "error": f"HTTP {exc.response.status_code}", "models": []}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200], "models": []}


def test_provider(proxy: str | None = None) -> dict[str, Any]:
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
        client = GeminiClientV3(proxy=proxy)
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

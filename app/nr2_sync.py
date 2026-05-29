"""Read-only Nr2 tenant knowledge sync for the Nr3 workspace.

Nr2 remains the customer/operator dashboard and the canonical runtime for
tenant Company knowledge. Nr3 should show Calvin what the tenant has already
configured there, without copying secrets or inventing placeholder state.
"""
from __future__ import annotations

import os
import json
import re
import tempfile
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from app.tenants import get_tenant_client_data


@dataclass(frozen=True)
class Nr2KnowledgeSync:
    status: str
    source_url: str = ""
    error: str = ""
    fetched_at: str = ""
    cached: bool = False
    sot_blocks: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    info_updates: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    knowledge_files: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    knowledge_media: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    runtime_prompt_manifest: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def total_items(self) -> int:
        return (
            len(self.sot_blocks)
            + len(self.info_updates)
            + len(self.knowledge_files)
            + len(self.knowledge_media)
        )


@dataclass(frozen=True)
class Nr2AutoBlockSync:
    status: str
    source_url: str = ""
    error: str = ""
    settings: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def _api_base_for_tenant(tenant_id: str) -> str:
    template = os.getenv(
        "NR3_TENANT_API_BASE_TEMPLATE",
        "http://wtyj-{tenant}:8001/dashboard/api",
    ).strip()
    return template.format(tenant=tenant_id).rstrip("/")


def _cache_path() -> Path:
    return Path(os.getenv("NR3_NR2_KNOWLEDGE_CACHE_PATH", "data/nr2_knowledge_cache.json"))


def _cache_ttl_seconds() -> int:
    try:
        ttl = int(os.getenv("NR3_NR2_KNOWLEDGE_CACHE_TTL_SECONDS", "900"))
    except ValueError:
        return 900
    return max(0, ttl)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _parse_dt(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _load_cache() -> dict[str, Any]:
    path = _cache_path()
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_cache(data: dict[str, Any]) -> None:
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=".nr2_knowledge_cache.", suffix=".json", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _sync_from_cache(raw: Any) -> Nr2KnowledgeSync | None:
    if not isinstance(raw, dict):
        return None
    try:
        return Nr2KnowledgeSync(
            status=str(raw.get("status") or "cached"),
            source_url=str(raw.get("source_url") or ""),
            error=str(raw.get("error") or ""),
            fetched_at=str(raw.get("fetched_at") or ""),
            cached=True,
            sot_blocks=tuple(raw.get("sot_blocks") or ()),
            info_updates=tuple(raw.get("info_updates") or ()),
            knowledge_files=tuple(raw.get("knowledge_files") or ()),
            knowledge_media=tuple(raw.get("knowledge_media") or ()),
            runtime_prompt_manifest=(
                raw.get("runtime_prompt_manifest")
                if isinstance(raw.get("runtime_prompt_manifest"), dict)
                else {}
            ),
        )
    except (TypeError, ValueError):
        return None


def _fresh_cached_sync(tenant_id: str) -> Nr2KnowledgeSync | None:
    raw = _load_cache().get(tenant_id)
    sync = _sync_from_cache(raw)
    if sync is None:
        return None
    fetched = _parse_dt(sync.fetched_at)
    ttl = _cache_ttl_seconds()
    if ttl <= 0 or fetched is None:
        return None
    if (_utc_now() - fetched).total_seconds() <= ttl:
        return sync
    return None


def _write_tenant_cache(tenant_id: str, sync: Nr2KnowledgeSync) -> None:
    cache = _load_cache()
    cache[tenant_id] = asdict(replace(sync, cached=False))
    _save_cache(cache)


def _clean_text(value: Any, max_len: int = 1200) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if len(text) > max_len:
        return text[: max_len - 1] + "…"
    return text


_SECRET_LINE_RE = re.compile(
    r"\b(password|access[_-]?key|api[_-]?key|token|secret|webhook[_-]?secret|client[_-]?secret)\b",
    re.IGNORECASE,
)


def _redact_secret_lines(text: str) -> str:
    safe_lines: list[str] = []
    for line in text.splitlines():
        if _SECRET_LINE_RE.search(line):
            if ":" in line:
                prefix = line.split(":", 1)[0].strip()
                safe_lines.append(f"{prefix}: [REDACTED]")
            elif "=" in line:
                prefix = line.split("=", 1)[0].strip()
                safe_lines.append(f"{prefix}=[REDACTED]")
            else:
                safe_lines.append("[REDACTED]")
        else:
            safe_lines.append(line)
    return "\n".join(safe_lines)


def _tenant_password(tenant_id: str) -> str:
    data = get_tenant_client_data(tenant_id)
    for key in ("password", "dashboard_access_key", "access_key"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _safe_sot_blocks(raw: Any) -> tuple[dict[str, Any], ...]:
    blocks = raw.get("blocks") if isinstance(raw, dict) else raw
    if not isinstance(blocks, list):
        return tuple()
    out: list[dict[str, Any]] = []
    for block in blocks[:20]:
        if not isinstance(block, dict):
            continue
        title = _clean_text(block.get("title"), 160)
        content = _clean_text(block.get("content"), 900)
        items = block.get("items")
        clean_items: list[str] = []
        if isinstance(items, list):
            clean_items = [_clean_text(item, 300) for item in items[:12] if _clean_text(item, 300)]
        subsections = block.get("subsections")
        clean_subsections: list[dict[str, Any]] = []
        if isinstance(subsections, list):
            for sub in subsections[:8]:
                if not isinstance(sub, dict):
                    continue
                clean_subsections.append({
                    "title": _clean_text(sub.get("title"), 160),
                    "content": _clean_text(sub.get("content"), 500),
                })
        if title or content or clean_items or clean_subsections:
            out.append({
                "title": title or "Untitled",
                "content": content,
                "items": tuple(clean_items),
                "subsections": tuple(clean_subsections),
            })
    return tuple(out)


def _safe_info_updates(raw: Any) -> tuple[dict[str, Any], ...]:
    updates = raw.get("updates") if isinstance(raw, dict) else raw
    if not isinstance(updates, list):
        return tuple()
    out: list[dict[str, Any]] = []
    for update in updates[:40]:
        if not isinstance(update, dict):
            continue
        text = _clean_text(update.get("text"), 900)
        if not text:
            continue
        out.append({
            "id": _clean_text(update.get("id"), 80),
            "type": _clean_text(update.get("type"), 40) or "general",
            "text": text,
            "active": update.get("active") is not False,
            "created_at": _clean_text(update.get("createdAt") or update.get("created_at"), 80),
        })
    return tuple(out)


def _safe_files(raw: Any) -> tuple[dict[str, Any], ...]:
    files = raw.get("files") if isinstance(raw, dict) else raw
    if not isinstance(files, list):
        return tuple()
    out: list[dict[str, Any]] = []
    for item in files[:40]:
        if not isinstance(item, dict):
            continue
        name = _clean_text(item.get("filename") or item.get("name"), 180)
        if not name:
            continue
        out.append({
            "filename": name,
            "status": _clean_text(item.get("status"), 40) or "unknown",
            "size_bytes": int(item.get("sizeBytes") or item.get("size_bytes") or 0),
            "uploaded_at": _clean_text(item.get("uploadedAt") or item.get("uploaded_at"), 80),
        })
    return tuple(out)


def _safe_media(raw: Any) -> tuple[dict[str, Any], ...]:
    media = raw.get("media") if isinstance(raw, dict) else raw
    if not isinstance(media, list):
        return tuple()
    out: list[dict[str, Any]] = []
    for item in media[:60]:
        if not isinstance(item, dict):
            continue
        url = _clean_text(item.get("url"), 500)
        caption = _clean_text(item.get("caption"), 180)
        filename = _clean_text(item.get("originalFilename") or item.get("filename"), 180)
        if not (url or caption or filename):
            continue
        out.append({
            "caption": caption or filename or "Image",
            "filename": filename,
            "url": url,
            "knowledge_id": _clean_text(item.get("knowledgeId") or item.get("knowledge_id"), 80),
            "uploaded_at": _clean_text(item.get("uploadedAt") or item.get("uploaded_at"), 80),
        })
    return tuple(out)


def _safe_runtime_prompt_manifest(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    sources = raw.get("sources")
    if not isinstance(sources, list):
        return {}
    safe_sources: list[dict[str, Any]] = []
    for item in sources[:30]:
        if not isinstance(item, dict):
            continue
        text = _redact_secret_lines(_clean_text(item.get("text"), 20000))
        name = _clean_text(item.get("name"), 180)
        source_id = _clean_text(item.get("id"), 160)
        if not (name and source_id):
            continue
        safe_sources.append({
            "id": source_id,
            "name": name,
            "source_location": _clean_text(item.get("source_location"), 300),
            "used_in": [
                _clean_text(value, 80)
                for value in (item.get("used_in") if isinstance(item.get("used_in"), list) else [])
                if _clean_text(value, 80)
            ][:20],
            "prompt_kind": _clean_text(item.get("prompt_kind"), 80),
            "priority": _clean_text(item.get("priority"), 80) or "soft_preferences",
            "status": _clean_text(item.get("status"), 80) or "indexed",
            "partial_reason": _clean_text(item.get("partial_reason"), 300),
            "text": text,
        })
    return {
        "schema_version": raw.get("schema_version") or 1,
        "generated_at": _clean_text(raw.get("generated_at"), 80),
        "tenant": raw.get("tenant") if isinstance(raw.get("tenant"), dict) else {},
        "sources": safe_sources,
        "partial": bool(raw.get("partial")),
        "limitations": [
            _clean_text(value, 300)
            for value in (raw.get("limitations") if isinstance(raw.get("limitations"), list) else [])
            if _clean_text(value, 300)
        ][:20],
    }


def _get_json(client: httpx.Client, base: str, path: str, token: str) -> Any:
    response = client.get(
        f"{base}{path}",
        headers={"Authorization": f"Bearer {token}"},
    )
    response.raise_for_status()
    return response.json()


def fetch_nr2_knowledge(
    tenant_id: str,
    *,
    client: httpx.Client | None = None,
    refresh: bool = False,
) -> Nr2KnowledgeSync:
    """Pull live Company knowledge from one tenant's Nr2 runtime."""
    if not refresh and client is None:
        cached = _fresh_cached_sync(tenant_id)
        if cached is not None:
            return cached

    stale_cache = _sync_from_cache(_load_cache().get(tenant_id))
    password = _tenant_password(tenant_id)
    base = _api_base_for_tenant(tenant_id)
    if not password:
        return Nr2KnowledgeSync(
            status="missing_credentials",
            source_url=base,
            error="No dashboard password/access key found in tenant client.json.",
        )

    owns_client = client is None
    http = client or httpx.Client(timeout=3)
    try:
        login = http.post(f"{base}/login", json={"password": password})
        login.raise_for_status()
        token = login.json().get("token")
        if not isinstance(token, str) or not token:
            return Nr2KnowledgeSync(status="unavailable", source_url=base, error="Nr2 login returned no token.")

        errors: list[str] = []

        def optional(path: str) -> Any:
            try:
                return _get_json(http, base, path, token)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    errors.append(f"{path} missing")
                    return {}
                raise

        sot = optional("/source-of-truth")
        updates = optional("/settings/info-updates")
        files = optional("/knowledge/files")
        media = optional("/knowledge/media")
        runtime_prompt_manifest = optional("/runtime-prompt-manifest")

        status = "ok" if not errors else "partial"
        sync = Nr2KnowledgeSync(
            status=status,
            source_url=base,
            error="; ".join(errors),
            fetched_at=_utc_now().isoformat(),
            sot_blocks=_safe_sot_blocks(sot),
            info_updates=_safe_info_updates(updates),
            knowledge_files=_safe_files(files),
            knowledge_media=_safe_media(media),
            runtime_prompt_manifest=_safe_runtime_prompt_manifest(runtime_prompt_manifest),
        )
        if client is None and sync.status in {"ok", "partial"}:
            _write_tenant_cache(tenant_id, sync)
        return sync
    except (httpx.ConnectError, httpx.TimeoutException):
        if stale_cache is not None:
            return replace(
                stale_cache,
                cached=True,
                error=(stale_cache.error + "; " if stale_cache.error else "")
                + "Tenant runtime is offline or unreachable; showing cached data.",
            )
        return Nr2KnowledgeSync(status="offline", source_url=base, error="Tenant runtime is offline or unreachable.")
    except httpx.HTTPStatusError as exc:
        if stale_cache is not None:
            return replace(
                stale_cache,
                cached=True,
                error=(stale_cache.error + "; " if stale_cache.error else "")
                + f"Nr2 returned HTTP {exc.response.status_code}; showing cached data.",
            )
        return Nr2KnowledgeSync(
            status="auth_failed" if exc.response.status_code in {401, 403, 405} else "unavailable",
            source_url=base,
            error=f"Nr2 returned HTTP {exc.response.status_code}.",
        )
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        if stale_cache is not None:
            return replace(
                stale_cache,
                cached=True,
                error=(stale_cache.error + "; " if stale_cache.error else "")
                + "Nr2 sync failed; showing cached data.",
            )
        return Nr2KnowledgeSync(status="unavailable", source_url=base, error=str(exc)[:220])


def _login_token(http: httpx.Client, base: str, tenant_id: str) -> tuple[str, str]:
    password = _tenant_password(tenant_id)
    if not password:
        return "", "No dashboard password/access key found in tenant client.json."
    login = http.post(f"{base}/login", json={"password": password})
    login.raise_for_status()
    token = login.json().get("token")
    if not isinstance(token, str) or not token:
        return "", "Nr2 login returned no token."
    return token, ""


def fetch_auto_block_settings(
    tenant_id: str,
    *,
    client: httpx.Client | None = None,
) -> Nr2AutoBlockSync:
    base = _api_base_for_tenant(tenant_id)
    owns_client = client is None
    http = client or httpx.Client(timeout=3)
    try:
        token, error = _login_token(http, base, tenant_id)
        if error:
            return Nr2AutoBlockSync(status="missing_credentials", source_url=base, error=error)
        data = _get_json(http, base, "/settings/auto-block", token)
        return Nr2AutoBlockSync(status="ok", source_url=base, settings=data if isinstance(data, dict) else {})
    except (httpx.ConnectError, httpx.TimeoutException):
        return Nr2AutoBlockSync(status="offline", source_url=base, error="Tenant runtime is offline or unreachable.")
    except httpx.HTTPStatusError as exc:
        return Nr2AutoBlockSync(
            status="auth_failed" if exc.response.status_code in {401, 403, 405} else "unavailable",
            source_url=base,
            error=f"Nr2 returned HTTP {exc.response.status_code}.",
        )
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        return Nr2AutoBlockSync(status="unavailable", source_url=base, error=str(exc)[:220])
    finally:
        if owns_client:
            http.close()


def update_auto_block_settings(
    tenant_id: str,
    settings: dict[str, Any],
    *,
    client: httpx.Client | None = None,
) -> Nr2AutoBlockSync:
    base = _api_base_for_tenant(tenant_id)
    owns_client = client is None
    http = client or httpx.Client(timeout=3)
    try:
        token, error = _login_token(http, base, tenant_id)
        if error:
            return Nr2AutoBlockSync(status="missing_credentials", source_url=base, error=error)
        response = http.put(
            f"{base}/settings/auto-block",
            headers={"Authorization": f"Bearer {token}"},
            json=settings,
        )
        response.raise_for_status()
        data = response.json()
        return Nr2AutoBlockSync(status="ok", source_url=base, settings=data if isinstance(data, dict) else {})
    except (httpx.ConnectError, httpx.TimeoutException):
        return Nr2AutoBlockSync(status="offline", source_url=base, error="Tenant runtime is offline or unreachable.")
    except httpx.HTTPStatusError as exc:
        return Nr2AutoBlockSync(
            status="auth_failed" if exc.response.status_code in {401, 403, 405} else "unavailable",
            source_url=base,
            error=f"Nr2 returned HTTP {exc.response.status_code}.",
        )
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        return Nr2AutoBlockSync(status="unavailable", source_url=base, error=str(exc)[:220])
    finally:
        if owns_client:
            http.close()

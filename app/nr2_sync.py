"""Read-only Nr2 tenant knowledge sync for the Nr3 workspace.

Nr2 remains the customer/operator dashboard and the canonical runtime for
tenant Company knowledge. Nr3 should show Calvin what the tenant has already
configured there, without copying secrets or inventing placeholder state.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.tenants import get_tenant_client_data


@dataclass(frozen=True)
class Nr2KnowledgeSync:
    status: str
    source_url: str = ""
    error: str = ""
    sot_blocks: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    info_updates: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    knowledge_files: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    knowledge_media: tuple[dict[str, Any], ...] = field(default_factory=tuple)

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


def _api_base_for_tenant(tenant_id: str) -> str:
    template = os.getenv(
        "NR3_TENANT_API_BASE_TEMPLATE",
        "http://wtyj-{tenant}:8001/dashboard/api",
    ).strip()
    return template.format(tenant=tenant_id).rstrip("/")


def _clean_text(value: Any, max_len: int = 1200) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if len(text) > max_len:
        return text[: max_len - 1] + "…"
    return text


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


def _get_json(client: httpx.Client, base: str, path: str, token: str) -> Any:
    response = client.get(
        f"{base}{path}",
        headers={"Authorization": f"Bearer {token}"},
    )
    response.raise_for_status()
    return response.json()


def fetch_nr2_knowledge(tenant_id: str, *, client: httpx.Client | None = None) -> Nr2KnowledgeSync:
    """Pull live Company knowledge from one tenant's Nr2 runtime."""
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

        status = "ok" if not errors else "partial"
        return Nr2KnowledgeSync(
            status=status,
            source_url=base,
            error="; ".join(errors),
            sot_blocks=_safe_sot_blocks(sot),
            info_updates=_safe_info_updates(updates),
            knowledge_files=_safe_files(files),
            knowledge_media=_safe_media(media),
        )
    except (httpx.ConnectError, httpx.TimeoutException):
        return Nr2KnowledgeSync(status="offline", source_url=base, error="Tenant runtime is offline or unreachable.")
    except httpx.HTTPStatusError as exc:
        return Nr2KnowledgeSync(
            status="auth_failed" if exc.response.status_code in {401, 403, 405} else "unavailable",
            source_url=base,
            error=f"Nr2 returned HTTP {exc.response.status_code}.",
        )
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        return Nr2KnowledgeSync(status="unavailable", source_url=base, error=str(exc)[:220])
    finally:
        if owns_client:
            http.close()

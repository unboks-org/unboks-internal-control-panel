"""Calvin-only internal todo storage for the Nr3 admin panel."""

from __future__ import annotations

import html
import re
import secrets
import sqlite3
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

from app.channel_connections import utc_now
from app.config import get_settings


MAX_TODO_HTML_BYTES = 2_000_000
MAX_TODO_IMAGE_BYTES = 1_000_000
MAX_TODO_IMAGES = 4
MAX_TODO_PLAIN_CHARS = 8_000


@dataclass(frozen=True)
class TodoItem:
    id: str
    content_html: str
    content_plain: str
    is_done: bool
    created_at: str
    updated_at: str


def _connect() -> sqlite3.Connection:
    settings = get_settings()
    db_path = Path(settings.db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS internal_todos (
                id TEXT PRIMARY KEY,
                content_html TEXT NOT NULL,
                content_plain TEXT NOT NULL,
                is_done INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_internal_todos_done_updated
            ON internal_todos (is_done, updated_at)
            """
        )


class _TodoHtmlSanitizer(HTMLParser):
    allowed_tags = {
        "a",
        "b",
        "blockquote",
        "br",
        "code",
        "div",
        "em",
        "h1",
        "h2",
        "h3",
        "h4",
        "i",
        "img",
        "li",
        "ol",
        "p",
        "pre",
        "s",
        "span",
        "strong",
        "u",
        "ul",
    }
    void_tags = {"br", "img"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.out: list[str] = []
        self.image_count = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag not in self.allowed_tags:
            return
        if tag == "a":
            href = _safe_href(_attr(attrs, "href"))
            if href:
                self.out.append(
                    f'<a href="{html.escape(href, quote=True)}" target="_blank" rel="noopener noreferrer">'
                )
            else:
                self.out.append("<span>")
            return
        if tag == "img":
            if self.image_count >= MAX_TODO_IMAGES:
                raise ValueError(f"Todo can include up to {MAX_TODO_IMAGES} pasted images.")
            src = _safe_img_src(_attr(attrs, "src"))
            if not src:
                return
            self.image_count += 1
            alt = html.escape(_attr(attrs, "alt") or "Pasted image", quote=True)
            self.out.append(f'<img src="{html.escape(src, quote=True)}" alt="{alt}">')
            return
        self.out.append(f"<{tag}>")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "a":
            self.out.append("</a>")
            return
        if tag in self.allowed_tags and tag not in self.void_tags:
            self.out.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        self.out.append(html.escape(data))

    def handle_entityref(self, name: str) -> None:
        self.out.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.out.append(f"&#{name};")

    def result(self) -> str:
        return "".join(self.out).strip()


def _attr(attrs: list[tuple[str, str | None]], name: str) -> str:
    for key, value in attrs:
        if key.lower() == name and value:
            return value.strip()
    return ""


def _safe_href(value: str) -> str:
    if not value:
        return ""
    lowered = value.lower()
    if lowered.startswith(("https://", "http://", "mailto:")):
        return value
    return ""


def _safe_img_src(value: str) -> str:
    if not value:
        return ""
    lowered = value.lower()
    if lowered.startswith(("https://", "http://")):
        return value
    if re.match(r"^data:image/(png|jpe?g|gif|webp);base64,[a-z0-9+/=\s]+$", lowered):
        compact = re.sub(r"\s+", "", value)
        _, _, encoded = compact.partition(",")
        padding = encoded.count("=")
        estimated_bytes = max(0, (len(encoded) * 3) // 4 - padding)
        if estimated_bytes > MAX_TODO_IMAGE_BYTES:
            raise ValueError("Pasted image is too large. Use an image under 1 MB.")
        return compact
    return ""


def _byte_len(value: str) -> int:
    return len(value.encode("utf-8"))


def sanitize_html(raw_html: str, plain_text: str = "") -> str:
    raw_html = (raw_html or "").strip()
    plain_text = (plain_text or "").strip()
    if _byte_len(raw_html) > MAX_TODO_HTML_BYTES:
        raise ValueError("Todo is too large. Keep one todo under 2 MB.")
    if not raw_html and plain_text:
        return html.escape(plain_text).replace("\n", "<br>")
    parser = _TodoHtmlSanitizer()
    parser.feed(raw_html)
    clean = parser.result()
    if clean:
        return clean
    return html.escape(plain_text).replace("\n", "<br>")


def clean_plain_text(raw: str, fallback_html: str = "") -> str:
    text = (raw or "").strip()
    if text:
        return text[:MAX_TODO_PLAIN_CHARS]
    without_tags = re.sub(r"<[^>]+>", " ", fallback_html or "")
    return html.unescape(re.sub(r"\s+", " ", without_tags)).strip()[
        :MAX_TODO_PLAIN_CHARS
    ]


def create_todo(content_html: str, content_plain: str) -> TodoItem:
    init_db()
    clean_html = sanitize_html(content_html, content_plain)
    if _byte_len(clean_html) > MAX_TODO_HTML_BYTES:
        raise ValueError("Todo is too large. Keep one todo under 2 MB.")
    clean_plain = clean_plain_text(content_plain, clean_html)
    if not clean_plain and "<img " not in clean_html:
        raise ValueError("Todo cannot be empty.")
    now = utc_now()
    todo_id = f"todo_{secrets.token_urlsafe(18)}"
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO internal_todos (
                id,
                content_html,
                content_plain,
                is_done,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, 0, ?, ?)
            """,
            (todo_id, clean_html, clean_plain, now, now),
        )
        row = conn.execute(
            "SELECT * FROM internal_todos WHERE id = ?",
            (todo_id,),
        ).fetchone()
    return row_to_todo(row)


def list_todos() -> tuple[TodoItem, ...]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM internal_todos
            ORDER BY is_done ASC, updated_at DESC, created_at DESC
            """
        ).fetchall()
    return tuple(row_to_todo(row) for row in rows)


def toggle_todo(todo_id: str) -> TodoItem | None:
    init_db()
    now = utc_now()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM internal_todos WHERE id = ?",
            (todo_id,),
        ).fetchone()
        if row is None:
            return None
        next_done = 0 if int(row["is_done"]) else 1
        conn.execute(
            """
            UPDATE internal_todos
            SET is_done = ?, updated_at = ?
            WHERE id = ?
            """,
            (next_done, now, todo_id),
        )
        updated = conn.execute(
            "SELECT * FROM internal_todos WHERE id = ?",
            (todo_id,),
        ).fetchone()
    return row_to_todo(updated)


def delete_todo(todo_id: str) -> bool:
    init_db()
    with _connect() as conn:
        result = conn.execute(
            "DELETE FROM internal_todos WHERE id = ?",
            (todo_id,),
        )
    return result.rowcount > 0


def row_to_todo(row: sqlite3.Row) -> TodoItem:
    return TodoItem(
        id=str(row["id"]),
        content_html=str(row["content_html"]),
        content_plain=str(row["content_plain"]),
        is_done=bool(row["is_done"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )

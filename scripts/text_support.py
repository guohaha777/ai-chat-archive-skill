#!/usr/bin/env python3
"""Shared HTML and structured text extraction; no file-system mutations."""

from __future__ import annotations

import html
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from typing import Any


SUPPORTED_TEXT = {".json", ".jsonl", ".html", ".htm", ".md", ".txt"}
ATTACHMENT_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg",
    ".pdf", ".mp3", ".wav", ".m4a", ".mp4", ".mov", ".avi",
    ".zip", ".7z", ".rar", ".doc", ".docx", ".ppt", ".pptx",
    ".xls", ".xlsx",
}
BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "br", "div", "dl", "dt",
    "dd", "fieldset", "figcaption", "figure", "footer", "form", "h1",
    "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main", "ol",
    "p", "section", "table", "tbody", "td", "tfoot", "th", "thead", "tr",
    "ul",
}
UNSUPPORTED_FENCED_BLOCK = re.compile(
    r"(?ms)^[ \t]*```[^\r\n]*\r?\n"
    r"[ \t]*This block is not supported on your current device yet\.[ \t]*\r?\n"
    r"[ \t]*```[ \t]*(?:\r?\n|$)"
)
UNSUPPORTED_BLOCK_LINE = re.compile(
    r"(?mi)^[ \t]*This block is not supported on your current device yet\.[ \t]*"
    r"(?:\r?\n|$)"
)
TOOL_TEXT_KEYS = {
    "text", "content", "value", "file_text", "old_str", "new_str",
    "code", "command", "query", "prompt", "markdown", "output",
    "widget_code", "control", "question", "options", "body", "label",
}
TOOL_TEXT_CONTAINER_KEYS = {
    "input", "result", "data", "structured_content", "json_block", "edits",
}
TOOL_METADATA_KEYS = {
    "path", "paths", "filepath", "filepaths", "url", "urls", "id", "name",
    "description", "start_timestamp", "stop_timestamp", "flags", "type",
    "message", "integration_name", "integration_icon_url", "icon_name",
    "approval_options", "approval_key", "approval_key_legacy", "mcp_server_url",
}
EMBEDDED_ATTACHMENT_TEXT_KEYS = ("extracted_content", "text_content")


class VisibleHTML(HTMLParser):
    """Extract visible text while preserving coarse document and code structure."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0
        self.in_pre = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "template"}:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag == "pre":
            self.in_pre = True
            self.parts.append("\n~~~text\n")
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "template"}:
            if self.skip_depth:
                self.skip_depth -= 1
            return
        if self.skip_depth:
            return
        if tag == "pre":
            self.parts.append("\n~~~\n")
            self.in_pre = False
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        if self.in_pre:
            self.parts.append(data)
        else:
            self.parts.append(re.sub(r"[ \t\r\f\v]+", " ", data))

    def text(self) -> str:
        value = html.unescape("".join(self.parts))
        value = re.sub(r"[ \t]+\n", "\n", value)
        value = re.sub(r"\n[ \t]+", "\n", value)
        value = re.sub(r"\n{3,}", "\n\n", value)
        return value.strip()


def clean_html(value: str) -> str:
    parser = VisibleHTML()
    parser.feed(value)
    parser.close()
    return parser.text()


def value_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [value_to_text(item) for item in value]
        return "\n".join(part for part in parts if part)
    if isinstance(value, dict):
        for key in ("text", "content", "value"):
            if key in value:
                candidate = value_to_text(value[key])
                if candidate:
                    return candidate
        return json.dumps(value, ensure_ascii=False, indent=2)
    return str(value)


def strip_unsupported_block_placeholders(value: str) -> str:
    cleaned = UNSUPPORTED_FENCED_BLOCK.sub("", value)
    cleaned = UNSUPPORTED_BLOCK_LINE.sub("", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def structured_text_fragments(value: Any, prefix: str) -> list[tuple[str, str]]:
    """Extract explicit text-bearing fields without dumping tool metadata."""
    fragments: list[tuple[str, str]] = []
    if value is None:
        return fragments
    if isinstance(value, str):
        text = strip_unsupported_block_placeholders(value)
        if text:
            fragments.append((prefix, text))
        return fragments
    if isinstance(value, list):
        for index, item in enumerate(value):
            fragments.extend(structured_text_fragments(item, f"{prefix}[{index}]"))
        return fragments
    if isinstance(value, dict):
        for key, item in value.items():
            if key in TOOL_METADATA_KEYS:
                continue
            if key in TOOL_TEXT_KEYS:
                fragments.extend(structured_text_fragments(item, f"{prefix}.{key}"))
            elif isinstance(item, dict):
                fragments.extend(structured_text_fragments(item, f"{prefix}.{key}"))
            elif key in TOOL_TEXT_CONTAINER_KEYS and isinstance(item, list):
                fragments.extend(structured_text_fragments(item, f"{prefix}.{key}"))
            elif isinstance(item, list) and any(
                isinstance(child, dict) for child in item
            ):
                fragments.extend(structured_text_fragments(item, f"{prefix}.{key}"))
    return fragments


DOCUMENT_SUFFIXES = {".md", ".markdown", ".txt", ".rst"}
DOCUMENT_TYPES = {"text/markdown", "text/plain"}
ASK_TOOL = re.compile(r"ask_user|askuser|user_input|request_user|question", re.IGNORECASE)
PROCESS_BLOCK_TYPES = {"thinking", "redacted_thinking", "token_budget"}


def tool_block_role(block: dict[str, Any]) -> tuple[str, str, str]:
    """Classify a tool call as 'ask', 'document' or 'process'.

    'ask' is a structured question put to the person, and their answer is part of the
    dialogue. 'document' is prose the assistant wrote as the deliverable (a Markdown or
    text file, a text artifact), which is the answer itself. Everything else (reading
    files, running commands, searching, editing code, rendering widgets) is working
    process. Returns (role, document_name, document_body).
    """
    name = str(block.get("name") or "")
    data = block.get("input") if isinstance(block.get("input"), dict) else {}
    if ASK_TOOL.search(name):
        return "ask", "", ""
    path = str(data.get("path") or data.get("file_path") or data.get("filename") or data.get("title") or "")
    body = next((data[key] for key in ("file_text", "content", "text", "markdown")
                 if isinstance(data.get(key), str) and data[key].strip()), "")
    kind = str(data.get("type") or "").lower()
    if body and (Path(path).suffix.lower() in DOCUMENT_SUFFIXES or kind in DOCUMENT_TYPES):
        return "document", Path(path).name or "document", body
    return "process", "", ""


def render_content_blocks(
    content: Any,
    conversation_only: bool = False,
    omitted: dict[str, int] | None = None,
) -> tuple[list[tuple[str, str]], int]:
    """Turn content blocks into labelled text fragments.

    With conversation_only, thinking and tool traffic are left out and counted in
    `omitted`; questions put to the person and documents written for them are kept.
    """
    if not isinstance(content, list):
        content = [content]
    fragments: list[tuple[str, str]] = []
    recovered_tool_blocks = 0

    def leave_out(category: str, block: Any) -> None:
        if omitted is not None:
            omitted[category] = omitted.get(category, 0) + len(json.dumps(block, ensure_ascii=False))

    for index, block in enumerate(content):
        if not isinstance(block, dict):
            fragments.extend(structured_text_fragments(block, f"content[{index}]"))
            continue
        block_type = str(block.get("type") or "unknown")
        block_fragments: list[tuple[str, str]] = []
        if block_type == "text":
            block_fragments.extend(
                structured_text_fragments(block.get("text"), f"content[{index}].text")
            )
        elif conversation_only and block_type in PROCESS_BLOCK_TYPES:
            leave_out("thinking", block)
            continue
        elif block_type == "tool_use":
            role, doc_name, doc_body = tool_block_role(block) if conversation_only else ("full", "", "")
            if role == "process":
                leave_out("tool_use", block)
                continue
            if role == "document":
                text = strip_unsupported_block_placeholders(doc_body)
                if text:
                    block_fragments.append((f"document written by assistant | {doc_name}", text))
            else:
                block_fragments.extend(
                    structured_text_fragments(block.get("input"), f"content[{index}].input")
                )
        elif block_type == "tool_result":
            if conversation_only and not ASK_TOOL.search(str(block.get("name") or "")):
                leave_out("tool_result", block)
                continue
            block_fragments.extend(
                structured_text_fragments(block.get("content"), f"content[{index}].content")
            )
            block_fragments.extend(
                structured_text_fragments(
                    block.get("structured_content"),
                    f"content[{index}].structured_content",
                )
            )
        else:
            block_fragments.extend(structured_text_fragments(block, f"content[{index}]"))
        if block_fragments and block_type in {"tool_use", "tool_result"}:
            recovered_tool_blocks += 1
        fragments.extend(block_fragments)
    return fragments, recovered_tool_blocks


def embedded_text_attachment(item: Any) -> tuple[str, dict[str, Any]]:
    if not isinstance(item, dict):
        return "", {"value": str(item)}
    metadata = {
        key: value
        for key, value in item.items()
        if key not in EMBEDDED_ATTACHMENT_TEXT_KEYS
    }
    file_name = str(item.get("file_name") or item.get("name") or "")
    file_type = str(item.get("file_type") or item.get("mime_type") or "").lower()
    is_supported_text = (
        Path(file_name).suffix.lower() in SUPPORTED_TEXT
        or file_type.startswith("text/")
        or file_type in {"markdown", "text", "json", "html"}
    )
    if not is_supported_text:
        return "", metadata
    for key in EMBEDDED_ATTACHMENT_TEXT_KEYS:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip(), metadata
    return "", metadata


def render_chat_messages(
    messages: list[Any],
    conversation_only: bool = False,
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    rendered: list[str] = []
    attachments: list[dict[str, Any]] = []
    stats = {
        "omitted_chars": {},
        "recovered_tool_block_count": 0,
        "recovered_placeholder_message_count": 0,
        "unrecovered_placeholder_message_count": 0,
        "processed_embedded_text_attachment_count": 0,
    }
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            rendered.append(f"[message {index}]\n{value_to_text(message)}")
            continue
        role = message.get("sender") or message.get("role") or "unknown"
        raw_top_level_text = value_to_text(message.get("text")).strip()
        top_level_text = strip_unsupported_block_placeholders(raw_top_level_text)
        content_fragments, recovered_tool_blocks = render_content_blocks(
            message.get("content"), conversation_only, stats["omitted_chars"]
        )
        stats["recovered_tool_block_count"] += recovered_tool_blocks
        had_placeholder = raw_top_level_text != top_level_text
        body_parts: list[str] = []
        seen: set[str] = set()

        def add_body_part(label: str | None, text: str) -> None:
            normalized = text.strip()
            if not normalized or normalized in seen:
                return
            seen.add(normalized)
            if label:
                body_parts.append(f"[{label}]\n{normalized}")
            else:
                body_parts.append(normalized)

        if top_level_text:
            add_body_part(None, top_level_text)
        for label, fragment in content_fragments:
            add_body_part(label, fragment)
        if had_placeholder and content_fragments:
            stats["recovered_placeholder_message_count"] += 1
        elif had_placeholder:
            stats["unrecovered_placeholder_message_count"] += 1
        timestamp = message.get("created_at") or message.get("timestamp")
        header = f"[message {index} | {role}"
        if timestamp:
            header += f" | {timestamp}"
        header += "]"
        for field in ("attachments", "files"):
            raw_items = message.get(field)
            if not raw_items:
                continue
            if not isinstance(raw_items, list):
                raw_items = [raw_items]
            for item_index, item in enumerate(raw_items):
                attachment_text, metadata = embedded_text_attachment(item)
                file_name = metadata.get("file_name") or metadata.get("name")
                attachment_label = f"attachment {field}[{item_index}]"
                if file_name:
                    attachment_label += f" | {file_name}"
                if attachment_text:
                    add_body_part(attachment_label, attachment_text)
                    stats["processed_embedded_text_attachment_count"] += 1
                attachments.append(
                    {
                        "message_index": index,
                        "field": field,
                        "item_index": item_index,
                        "metadata": metadata,
                        "embedded_text": attachment_text or None,
                    }
                )
        body = "\n\n".join(body_parts)
        rendered.append(f"{header}\n{body}".rstrip())
    return "\n\n".join(rendered).strip(), attachments, stats



# --- Activity-log HTML (one card per prompt/response, no conversation IDs) ---

ACTIVITY_CELL = '<div class="outer-cell'
ACTIVITY_BODY = re.compile(r'<div class="content-cell[^"]*mdl-typography--body-1">')
MONTHS = {name: number for number, names in enumerate([
    (), ("jan", "january"), ("feb", "february"), ("mar", "march"),
    ("apr", "april"), ("may",), ("jun", "june"), ("jul", "july"),
    ("aug", "august"), ("sep", "sept", "september"), ("oct", "october"),
    ("nov", "november"), ("dec", "december")]) for name in names}
TIMESTAMP_PATTERNS = [
    # 1 Jul 2026, 14:39:29
    (re.compile(r"^(\d{1,2}) ([A-Za-z]{3,9})\.? (\d{4}),? (\d{1,2}):(\d{2}):(\d{2})\s*([AaPp][Mm])?"),
     lambda m: (m[3], MONTHS.get(m[2].lower()), m[1], m[4], m[5], m[6], m[7])),
    # Jul 1, 2026, 2:39:29 PM
    (re.compile(r"^([A-Za-z]{3,9})\.? (\d{1,2}), (\d{4}),? (\d{1,2}):(\d{2}):(\d{2})\s*([AaPp][Mm])?"),
     lambda m: (m[3], MONTHS.get(m[1].lower()), m[2], m[4], m[5], m[6], m[7])),
    # 2026年7月1日 下午2:39:29
    (re.compile(r"^(\d{4})年(\d{1,2})月(\d{1,2})日\s*(上午|下午)?\s*(\d{1,2}):(\d{2}):(\d{2})"),
     lambda m: (m[1], m[2], m[3], m[5], m[6], m[7], {"上午": "am", "下午": "pm"}.get(m[4]))),
    # ISO-like: year-month-day, then a 24-hour time
    (re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})[ ,T]+(\d{1,2}):(\d{2}):(\d{2})\s*([AaPp][Mm])?"),
     lambda m: (m[1], m[2], m[3], m[4], m[5], m[6], m[7])),
]


def parse_activity_timestamp(value: str) -> str | None:
    """Return a sortable local 'YYYY-MM-DDTHH:MM:SS' or None. Time zones are not converted."""
    value = value.replace(" ", " ").replace("\xa0", " ").strip()
    for pattern, fields in TIMESTAMP_PATTERNS:
        match = pattern.match(value)
        if not match:
            continue
        year, month, day, hour, minute, second, half = fields(match)
        if not month:
            continue
        hour = int(hour)
        if half and half.lower() == "pm" and hour < 12:
            hour += 12
        if half and half.lower() == "am" and hour == 12:
            hour = 0
        try:
            return f"{int(year):04d}-{int(month):02d}-{int(day):02d}T{hour:02d}:{int(minute):02d}:{int(second):02d}"
        except ValueError:
            return None
    return None


def parse_activity_html(value: str) -> list[dict[str, Any]]:
    """Split an activity-log export into prompt/response records, in document order.

    Returns [] when the page is not an activity log. Each record has verb, prompt,
    timestamp (or None), response and attachment file names. Session boundaries are
    not present in this format; callers must infer or leave them unknown.
    """
    if ACTIVITY_CELL not in value:
        return []
    records: list[dict[str, Any]] = []
    for cell in value.split(ACTIVITY_CELL)[1:]:
        pieces = ACTIVITY_BODY.split(cell, 1)
        if len(pieces) < 2:
            continue
        body = pieces[1].split('<div class="content-cell', 1)[0]
        segments = body.split("<br>")
        head: list[str] = []
        timestamp, response_index = None, None
        for index, segment in enumerate(segments[:60]):
            plain = clean_html(segment)
            parsed = parse_activity_timestamp(plain) if len(plain) < 60 else None
            if parsed:
                timestamp, response_index = parsed, index + 1
                break
            head.append(segment)
        if response_index is None:
            head, response = segments, ""
        else:
            response = clean_html("<br>".join(segments[response_index:]))
        attachments, prompt_parts = [], []
        for segment in head:
            plain = clean_html(segment)
            if not plain:
                continue
            names = re.findall(r'<a href="[^"]*">([^<]+)</a>', segment)
            if names and plain.lstrip("-  \xa0").strip() in names:
                attachments.extend(names)
            elif re.fullmatch(r"Attached \d+ files?\.?|已附加 ?\d+ ?个文件。?", plain):
                continue
            else:
                prompt_parts.append(plain)
        prompt = "\n".join(prompt_parts)
        verb, _, rest = prompt.partition(" ")
        if verb in {"Prompted", "Created", "Used", "Asked", "Said"}:
            prompt = rest.strip()
        else:
            verb = ""
        records.append({"verb": verb, "prompt": prompt, "timestamp": timestamp,
                        "response": response, "attachments": attachments})
    return records


def group_activity_sessions(records: list[dict[str, Any]], gap_minutes: int) -> list[list[dict[str, Any]]]:
    """Group activity records into inferred sessions by time gap.

    Records without a timestamp cannot be ordered reliably; when any are present the
    original order is kept and each record becomes its own session.
    """
    from datetime import datetime
    if not records:
        return []
    if any(r["timestamp"] is None for r in records):
        return [[r] for r in records]
    ordered = sorted(records, key=lambda r: r["timestamp"])
    sessions, current, last = [], [], None
    for record in ordered:
        moment = datetime.fromisoformat(record["timestamp"])
        if last is not None and (moment - last).total_seconds() > gap_minutes * 60:
            sessions.append(current)
            current = []
        current.append(record)
        last = moment
    if current:
        sessions.append(current)
    return sessions

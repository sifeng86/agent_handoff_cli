"""Frozen dotenv grammar and span-preserving patches; never evaluate shell code."""
from dataclasses import dataclass, field
import json
import re

from ..contracts import RuntimeFailure


KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
ASSIGNMENT = re.compile(r"[ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)[ \t]*=[ \t]*")
EXPANSION = re.compile(r"\$(?:\{|\(|[A-Za-z_])|`")
ESCAPES = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", '"': '"'}


@dataclass(frozen=True)
class Entry:
    start: int
    end: int
    value_start: int
    value_end: int


@dataclass(frozen=True, repr=False)
class Document:
    raw: str = field(repr=False)
    values: dict[str, str] = field(repr=False)
    entries: dict[str, Entry] = field(repr=False)


def parse(raw: str) -> Document:
    if "\x00" in raw or raw.startswith("\ufeff"):
        raise RuntimeFailure("Unsupported dotenv encoding or NUL character.")
    values, entries = {}, {}
    position = 0
    while position < len(raw):
        start = position
        line_end = raw.find("\n", position)
        if line_end == -1:
            line_end = len(raw)
        text = raw[position:line_end].strip()
        if not text or text.startswith("#"):
            position = min(line_end + 1, len(raw))
            continue
        match = ASSIGNMENT.match(raw, position)
        if not match:
            raise RuntimeFailure("Unsupported dotenv assignment syntax.")
        key = match[1]
        if key in values:
            raise RuntimeFailure("Duplicate dotenv key.")
        value_start = position = match.end()
        if position < len(raw) and raw[position] in ("'", '"'):
            quote = raw[position]
            position += 1
            characters = []
            while position < len(raw) and raw[position] != quote:
                char = raw[position]
                if quote == '"' and char == "\\":
                    position += 1
                    if position >= len(raw) or raw[position] not in ESCAPES:
                        raise RuntimeFailure("Unsupported dotenv escape.")
                    char = ESCAPES[raw[position]]
                characters.append(char)
                position += 1
            if position >= len(raw):
                raise RuntimeFailure("Unterminated dotenv quoted value.")
            value = "".join(characters).replace("\r\n", "\n")
            position += 1
            value_end = position
            while position < len(raw) and raw[position] in " \t":
                position += 1
            if position < len(raw) and raw[position] == "#":
                newline = raw.find("\n", position)
                position = len(raw) if newline == -1 else newline
            elif position < len(raw) and raw[position] not in "\r\n":
                raise RuntimeFailure("Unexpected text after dotenv value.")
        else:
            ending = raw.find("\n", position)
            ending = len(raw) if ending == -1 else ending
            segment = raw[position:ending].rstrip("\r")
            if segment.startswith("#") and match[0].endswith((" ", "\t")):
                segment = ""
            comment = re.search(r"[ \t]+#", segment)
            segment = segment[:comment.start()] if comment else segment
            value = segment.rstrip(" \t")
            if any(char in value for char in ("'", '"')):
                raise RuntimeFailure("Unsupported quotes in unquoted dotenv value.")
            value_end = value_start + len(value)
            position = ending
        if EXPANSION.search(value):
            raise RuntimeFailure("Dotenv expansion and command substitution are unsupported.")
        if position < len(raw) and raw[position] == "\r":
            position += 1
            if position >= len(raw) or raw[position] != "\n":
                raise RuntimeFailure("Bare CR line endings are unsupported.")
        if position < len(raw) and raw[position] == "\n":
            position += 1
        values[key] = value
        entries[key] = Entry(start, position, value_start, value_end)
    return Document(raw, values, entries)


def patch(document: Document, changes: dict[str, str | None]) -> str:
    edits, additions = [], []
    newline = "\r\n" if "\r\n" in document.raw else "\n"
    for key, value in changes.items():
        if not KEY.fullmatch(key) or (value is not None and not isinstance(value, str)):
            raise RuntimeFailure("Invalid dotenv patch.")
        if value is not None and (EXPANSION.search(value) or "\x00" in value):
            raise RuntimeFailure("Dotenv value cannot be represented by the supported grammar.")
        entry = document.entries.get(key)
        if value is None:
            if entry:
                edits.append((entry.start, entry.end, ""))
        elif entry:
            edits.append((entry.value_start, entry.value_end, json.dumps(value, ensure_ascii=False)))
        else:
            additions.append(key + "=" + json.dumps(value, ensure_ascii=False) + newline)
    raw = document.raw
    for start, end, replacement in sorted(edits, reverse=True):
        raw = raw[:start] + replacement + raw[end:]
    if additions:
        if raw and not raw.endswith("\n"):
            raw += newline
        raw += "".join(additions)
    verified = parse(raw)
    expected = dict(document.values)
    for key, value in changes.items():
        if value is None:
            expected.pop(key, None)
        else:
            expected[key] = value.replace("\r\n", "\n")
    if verified.values != expected:
        raise RuntimeFailure("Dotenv patch failed round-trip verification.")
    return raw

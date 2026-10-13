"""omp's config.yml: read the YAML subset omp writes, and reconcile the discovery keys tstack needs with
line edits that keep every other line, comment and the file's quoting style.

Errors name the key path, the construct and the line, never a value. An edit is refused when the file uses
a construct a line edit cannot handle safely, and an edit is kept only when Bun's YAML parser reads the new
file as the old one with exactly the intended changes."""

from __future__ import annotations

import copy
import json
import os
import re
import shutil

from .util import HOME, Die, run

KEY = re.compile(r"""^("(?:[^"\\]|\\.)*"|'(?:[^']|'')*'|[^"'\s#][^:#]*?)\s*:(?:\s+(.*))?$""")
BLOCK_SCALAR = re.compile(r"^[|>][+-]?\d*$")
TOUCHED = (("skills", "enabled"), ("skills", "enablePiUser"), ("skills", "customDirectories"), ("disabledProviders",))


class ConfigError(Die):
    """A config.yml construct tstack cannot read or edit safely."""

    def __init__(self, path: str, construct: str, line: int | None = None):
        super().__init__(f"{path or '(top level)'} ({construct}{f', line {line}' if line else ''})")


class Unsupported(Exception):
    """A construct inside one value or key; the parser adds the key path and line."""


# parsing: nodes keep the file line they came from, so edits touch only those lines

def strip_comment(text: str) -> str:
    quote = None
    for i, ch in enumerate(text):
        if quote:
            if ch == quote and (quote == "'" or text[i - 1] != "\\"):
                quote = None
        elif ch in "\"'" and (i == 0 or text[i - 1] in " \t:[{,-"):
            quote = ch
        elif ch == "#" and (i == 0 or text[i - 1] in " \t"):
            return text[:i].rstrip()
    return text.rstrip()


def unquote(token: str) -> str:
    if token.startswith('"'):
        try:
            return json.loads(token)
        except ValueError:
            raise Unsupported("malformed quoted text")
    if token.startswith("'"):
        if len(token) < 2 or not token.endswith("'"):
            raise Unsupported("malformed quoted text")
        return token[1:-1].replace("''", "'")
    for mark, construct in (("&", "anchor"), ("*", "alias"), ("!", "tag")):
        if token.startswith(mark):
            raise Unsupported(construct)
    return token


def scalar(text: str):
    text = text.strip()
    if not text or text in ("~", "null", "Null", "NULL"):
        return None
    if text[0] in "\"'&*!":
        return unquote(text)
    if text[0] in "[{":
        try:
            return json.loads(text)
        except ValueError:
            parts = [part for part in text[1:-1].split(",") if part.strip()]
            if text[0] == "{" or not text.endswith("]") or any(ch in part for part in parts for ch in "[]{}\"'&*!"):
                raise Unsupported("flow mapping" if text[0] == "{" else "flow sequence")
            return [scalar(part) for part in parts]
    lowered = text.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?\d+\.\d*", text):
        return float(text)
    return text


def is_item(content: str) -> bool:
    return content == "-" or content.startswith("- ")


def child(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


class Parser:
    def __init__(self, text: str):
        self.raw = text.split("\n")
        self.lines = []
        self.flow = []  # (key path, construct, line) of flow collections other than a bare []
        for n, line in enumerate(self.raw):
            content = strip_comment(line.strip())
            if not content:
                continue
            if "\t" in line[: len(line) - len(line.lstrip())]:
                raise ConfigError("", "tab indentation", n + 1)
            if content.startswith("%"):
                raise ConfigError("", "directive", n + 1)
            if content == "---" and not self.lines:
                continue
            if content.startswith(("---", "...")) and content.rstrip(".-") == "":
                raise ConfigError("", "document marker", n + 1)
            indent = len(line) - len(line.lstrip(" "))
            self.lines.append({"n": n, "indent": indent, "col": indent, "content": content})

    def scalar(self, text: str, n: int, path: str):
        text = text.strip()
        if text[:1] == "{" or text[:1] == "[" and text != "[]":
            self.flow.append((path, "flow mapping" if text[0] == "{" else "flow sequence", n + 1))
        try:
            return {"kind": "scalar", "value": scalar(text), "line": n}
        except Unsupported as error:
            raise ConfigError(path, str(error), n + 1)

    def key(self, raw_key: str, n: int, path: str) -> str:
        try:
            return unquote(raw_key)
        except Unsupported as error:
            raise ConfigError(path, f"{error} in a key", n + 1)

    def parse(self):
        if not self.lines:
            return {"kind": "map", "entries": [], "indent": 0}
        node, i = self.node(0, self.lines[0]["indent"], "")
        if i < len(self.lines):
            raise ConfigError("", "unexpected indentation", self.lines[i]["n"] + 1)
        return node

    def node(self, i: int, indent: int, path: str):
        content = self.lines[i]["content"]
        if is_item(content):
            return self.seq(i, indent, path)
        if KEY.match(content):
            return self.map(i, indent, path)
        return self.scalar(content, self.lines[i]["n"], path), i + 1

    def map(self, i: int, indent: int, path: str):
        entries, seen = [], set()
        while i < len(self.lines) and self.lines[i]["indent"] == indent and not is_item(self.lines[i]["content"]):
            line = self.lines[i]
            match = KEY.match(line["content"])
            if not match:
                raise ConfigError(path, "expected `key: value`", line["n"] + 1)
            raw_key, rest = match.group(1), (match.group(2) or "").strip()
            key = self.key(raw_key, line["n"], path)
            here = child(path, key)
            if key in seen:
                raise ConfigError(here, "duplicate key", line["n"] + 1)
            seen.add(key)
            entry = {"key": key, "raw_key": raw_key, "line": line["n"], "col": line["col"], "indent": indent}
            i += 1
            if BLOCK_SCALAR.match(rest):
                block = []
                while i < len(self.lines) and self.lines[i]["indent"] > indent:
                    block.append(self.raw[self.lines[i]["n"]].strip())
                    i += 1
                value = {"kind": "scalar", "value": "\n".join(block), "line": line["n"]}
            elif rest:
                value = self.scalar(rest, line["n"], here)
            elif i < len(self.lines) and self.lines[i]["indent"] > indent:
                value, i = self.node(i, self.lines[i]["indent"], here)
            elif i < len(self.lines) and self.lines[i]["indent"] == indent and is_item(self.lines[i]["content"]):
                value, i = self.seq(i, indent, here)
            else:
                value = {"kind": "scalar", "value": None, "line": line["n"]}
            entry["value"], entry["last"] = value, self.lines[i - 1]["n"]
            entries.append(entry)
        return {"kind": "map", "entries": entries, "indent": indent}, i

    def seq(self, i: int, indent: int, path: str):
        items = []
        while i < len(self.lines) and self.lines[i]["indent"] == indent and is_item(self.lines[i]["content"]):
            line = self.lines[i]
            here = f"{path}[{len(items)}]"
            rest = line["content"][1:].strip()
            if not rest:
                i += 1
                if i < len(self.lines) and self.lines[i]["indent"] > indent:
                    value, i = self.node(i, self.lines[i]["indent"], here)
                else:
                    value = {"kind": "scalar", "value": None, "line": line["n"]}
            elif KEY.match(rest) and rest[0] not in "[{":
                col = line["col"] + len(line["content"]) - len(rest)
                self.lines[i] = {**line, "indent": col, "col": col, "content": rest}
                value, i = self.map(i, col, here)
            else:
                value = self.scalar(rest, line["n"], here)
                i += 1
            items.append({"line": line["n"], "value": value})
        return {"kind": "seq", "items": items, "indent": indent}, i


def plain(node):
    if node["kind"] == "map":
        return {e["key"]: plain(e["value"]) for e in node["entries"]}
    if node["kind"] == "seq":
        return [plain(item["value"]) for item in node["items"]]
    return node["value"]


def load(text: str) -> dict:
    value = plain(Parser(text).parse())
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError("", "document is not a mapping", 1)
    return value


# reconciling

def entry(node, key: str):
    if node and node["kind"] == "map":
        return next((e for e in node["entries"] if e["key"] == key), None)
    return None


def quoted_like(sibling_keys: list[str], key: str) -> str:
    if any(k.startswith('"') for k in sibling_keys):
        return json.dumps(key)
    if any(k.startswith("'") for k in sibling_keys):
        return f"'{key}'"
    return key


def names_native(value) -> bool:
    """Whether a value names the native provider in any form: a string (alone or in a list such as
    "native, claude"), a list item or a mapping key or value."""
    if isinstance(value, str):
        return "native" in re.split(r"[\s,]+", value.strip().lower())
    if isinstance(value, list):
        return any(names_native(v) for v in value)
    if isinstance(value, dict):
        return any(names_native(k) or names_native(v) for k, v in value.items())
    return False


def reconcile(text: str, keep_directory) -> tuple[str, list[str]]:
    """Make `skills.enabled` and `skills.enablePiUser` true, drop `skills.customDirectories` entries for which
    `keep_directory(path)` is false, and drop global `native` entries from `disabledProviders`. Returns the
    new text and what changed; other lines stay byte for byte. Raises ConfigError (manual edit needed) for a
    path-scoped `disabledProviders` entry naming native, and for an edit in a file with anchors, aliases,
    tags or flow collections other than a bare `[]`, or one Bun's YAML parser does not confirm."""
    parser = Parser(text)
    tree = parser.parse()
    raw = parser.raw
    if tree["kind"] == "scalar" and tree["value"] is None:
        tree = {"kind": "map", "entries": [], "indent": 0}
    if tree["kind"] != "map":
        raise ConfigError("", parser.flow[0][1] if parser.flow else "document is not a mapping", 1)
    replace, delete, insert, changes = {}, set(), {}, []
    top_keys = [e["raw_key"] for e in tree["entries"]]
    unit = next((ln["indent"] for ln in parser.lines if ln["indent"] > 0), 2)

    def rewrite_entry(e, value: str):
        replace[e["line"]] = raw[e["line"]][: e["col"]] + e["raw_key"] + ": " + value

    def prune(e, path: str, drop):
        """Delete the one-line block items for which drop(value) holds; `key: []` once none are left."""
        node = e["value"]
        if node["kind"] != "seq":
            raise ConfigError(path, "flow sequence" if isinstance(node["value"], list) else "not a list", e["line"] + 1)
        gone = []
        for item in node["items"]:
            value = plain(item["value"])
            if drop(value):
                if item["value"]["kind"] != "scalar" or item["value"]["line"] != item["line"]:
                    raise ConfigError(path, "multi-line list item", item["line"] + 1)
                gone.append(item)
                delete.add(item["line"])
                changes.append(f"{path} drops {value}")
        if gone and len(gone) == len(node["items"]):
            rewrite_entry(e, "[]")

    disabled = entry(tree, "disabledProviders")
    if disabled:
        value = plain(disabled["value"])
        if isinstance(value, list):
            for index, item in enumerate(value):
                if not isinstance(item, str) and names_native(item):
                    line = disabled["value"]["items"][index]["line"] if disabled["value"]["kind"] == "seq" else disabled["line"]
                    raise ConfigError(f"disabledProviders[{index}]", "path-scoped entry names native", line + 1)
            if "native" in value:
                prune(disabled, "disabledProviders", lambda item: item == "native")
        elif names_native(value):
            raise ConfigError("disabledProviders", "names native but is not a list", disabled["line"] + 1)

    skills = entry(tree, "skills")
    if skills is None:
        key = quoted_like(top_keys, "skills")
        children = [f"{' ' * unit}{quoted_like(top_keys, name)}: true" for name in ("enabled", "enablePiUser")]
        insert.setdefault(len(raw) - 1 if raw and raw[-1] == "" else len(raw), []).extend([f"{key}:", *children])
        changes.extend(["skills.enabled set true", "skills.enablePiUser set true"])
    else:
        node = skills["value"]
        siblings = [e["raw_key"] for e in node.get("entries", [])] or top_keys
        indent = node.get("indent", skills["indent"] + unit) if node["kind"] == "map" else skills["indent"] + unit
        if node["kind"] != "map":
            if node["kind"] != "scalar" or node["value"] is not None:
                raise ConfigError("skills", "not a mapping", skills["line"] + 1)
            replace[skills["line"]] = raw[skills["line"]][: skills["col"]] + skills["raw_key"] + ":"
        for name in ("enabled", "enablePiUser"):
            found = entry(node, name)
            if found is None:
                insert.setdefault(skills["last"] + 1, []).append(f"{' ' * indent}{quoted_like(siblings, name)}: true")
                changes.append(f"skills.{name} set true")
            elif found["value"]["kind"] != "scalar" or found["value"]["value"] is not True:
                rewrite_entry(found, "true")
                changes.append(f"skills.{name} set true")
        directories = entry(node, "customDirectories")
        if directories:
            value = plain(directories["value"])
            if isinstance(value, list) and any(not (isinstance(d, str) and keep_directory(d)) for d in value):
                prune(directories, "skills.customDirectories", lambda d: not (isinstance(d, str) and keep_directory(d)))
            elif value is not None and not isinstance(value, list):
                raise ConfigError("skills.customDirectories", "not a list", directories["line"] + 1)
    if not changes:
        verify(text, text, keep_directory)
        return text, []
    if parser.flow:
        raise ConfigError(*parser.flow[0])
    out = []
    for n, line in enumerate(raw):
        out += insert.get(n, [])
        if n not in delete:
            out.append(replace.get(n, line))
    out += insert.get(len(raw), [])
    new = "\n".join(out)
    verify(text, new, keep_directory)
    return new, changes


# verifying an edit with an independent YAML parser

BUN_PARSE = ("const docs = JSON.parse(await Bun.stdin.text());"
             "console.log(JSON.stringify(docs.map((t) => { try { return { ok: true, value: Bun.YAML.parse(t) ?? null }; }"
             " catch { return { ok: false }; } })));")


def bun_parse(*texts: str) -> list[dict]:
    """Bun.YAML.parse of each text as {"ok", "value"}. Parse errors carry no detail, so no value leaks."""
    bun = shutil.which("bun") or str(HOME / ".bun" / "bin" / "bun")
    if not os.access(bun, os.X_OK):
        raise ConfigError("", "bun not found to check the file with Bun's YAML parser")
    result = run([bun, "-e", BUN_PARSE], input=json.dumps(texts), check=False, timeout=60)
    if result.returncode != 0:
        raise ConfigError("", f"Bun's YAML parser failed to run (exit {result.returncode})")
    return json.loads(result.stdout)


def dig(doc, path: tuple[str, ...]):
    for key in path:
        doc = doc.get(key) if isinstance(doc, dict) else None
    return doc


def intended(doc, keep_directory) -> dict:
    """The document after reconcile, computed on parsed values."""
    doc = copy.deepcopy(doc) if isinstance(doc, dict) else {}
    skills = dict(doc["skills"]) if isinstance(doc.get("skills"), dict) else {}
    skills["enabled"] = skills["enablePiUser"] = True
    if isinstance(skills.get("customDirectories"), list):
        skills["customDirectories"] = [d for d in skills["customDirectories"] if isinstance(d, str) and keep_directory(d)]
    doc["skills"] = skills
    if isinstance(doc.get("disabledProviders"), list):
        doc["disabledProviders"] = [p for p in doc["disabledProviders"] if p != "native"]
    return doc


def verify(old: str, new: str, keep_directory) -> None:
    """Accept `new` (the edited file, or the file as it is when no edit is needed) only if Bun's YAML parser
    reads it as `old` with exactly the intended changes, and this reader agrees with Bun on every touched key."""
    before, after = bun_parse(old, new) if new != old else bun_parse(old) * 2
    if not before["ok"]:
        raise ConfigError("", "Bun's YAML parser rejects the current file")
    if not after["ok"]:
        raise ConfigError("", "the edited file is not valid YAML for Bun")
    got, mine, expected = after["value"] or {}, load(new), intended(before["value"], keep_directory)
    for path in TOUCHED:
        if not dig(got, path) == dig(mine, path) == dig(expected, path):
            raise ConfigError(".".join(path), "reads differently under Bun's YAML parser")
    if got != expected:
        differs = next((k for k in sorted(set(got) | set(expected), key=str) if got.get(k) != expected.get(k)), "")
        raise ConfigError(str(differs), "the edit changes a key it should not")

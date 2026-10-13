"""omp's config.yml: read the YAML subset omp writes, and reconcile the discovery keys tstack needs with
line edits that keep every other line, comment and the file's quoting style."""

from __future__ import annotations

import json
import re

from .util import Die

KEY = re.compile(r"""^("(?:[^"\\]|\\.)*"|'(?:[^']|'')*'|[^"'\s#][^:#]*?)\s*:(?:\s+(.*))?$""")
BLOCK_SCALAR = re.compile(r"^[|>][+-]?\d*$")


class ConfigError(Die):
    pass


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
            raise ConfigError(f"bad quoted text {token[:40]!r}")
    if token.startswith("'"):
        return token[1:-1].replace("''", "'")
    return token


def scalar(text: str):
    text = text.strip()
    if not text or text in ("~", "null", "Null", "NULL"):
        return None
    if text[0] in "\"'":
        return unquote(text)
    if text[0] in "[{":
        try:
            return json.loads(text)
        except ValueError:
            inner = text[1:-1].strip()
            if text[0] == "{":
                if inner:
                    raise ConfigError(f"unsupported flow mapping {text[:40]!r}")
                return {}
            return [scalar(part) for part in inner.split(",")] if inner else []
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


class Parser:
    def __init__(self, text: str):
        self.raw = text.split("\n")
        self.lines = []
        for n, line in enumerate(self.raw):
            if "\t" in line[: len(line) - len(line.lstrip())]:
                raise ConfigError(f"line {n + 1}: tab indentation")
            content = strip_comment(line.strip())
            if content and content != "---":
                indent = len(line) - len(line.lstrip(" "))
                self.lines.append({"n": n, "indent": indent, "col": indent, "content": content})

    def parse(self):
        if not self.lines:
            return {"kind": "map", "entries": []}
        node, i = self.node(0, self.lines[0]["indent"])
        if i < len(self.lines):
            raise ConfigError(f"line {self.lines[i]['n'] + 1}: unexpected indentation")
        return node

    def node(self, i: int, indent: int):
        content = self.lines[i]["content"]
        if is_item(content):
            return self.seq(i, indent)
        if KEY.match(content):
            return self.map(i, indent)
        return {"kind": "scalar", "value": scalar(content), "line": self.lines[i]["n"]}, i + 1

    def map(self, i: int, indent: int):
        entries = []
        while i < len(self.lines) and self.lines[i]["indent"] == indent and not is_item(self.lines[i]["content"]):
            line = self.lines[i]
            match = KEY.match(line["content"])
            if not match:
                raise ConfigError(f"line {line['n'] + 1}: expected `key: value`")
            raw_key, rest = match.group(1), (match.group(2) or "").strip()
            entry = {"key": unquote(raw_key), "raw_key": raw_key, "line": line["n"], "col": line["col"], "indent": indent}
            i += 1
            if BLOCK_SCALAR.match(rest):
                block = []
                while i < len(self.lines) and self.lines[i]["indent"] > indent:
                    block.append(self.raw[self.lines[i]["n"]].strip())
                    i += 1
                value = {"kind": "scalar", "value": "\n".join(block), "line": line["n"]}
            elif rest:
                value = {"kind": "scalar", "value": scalar(rest), "line": line["n"]}
            elif i < len(self.lines) and self.lines[i]["indent"] > indent:
                value, i = self.node(i, self.lines[i]["indent"])
            elif i < len(self.lines) and self.lines[i]["indent"] == indent and is_item(self.lines[i]["content"]):
                value, i = self.seq(i, indent)
            else:
                value = {"kind": "scalar", "value": None, "line": line["n"]}
            entry["value"], entry["last"] = value, self.lines[i - 1]["n"]
            entries.append(entry)
        return {"kind": "map", "entries": entries, "indent": indent}, i

    def seq(self, i: int, indent: int):
        items = []
        while i < len(self.lines) and self.lines[i]["indent"] == indent and is_item(self.lines[i]["content"]):
            line = self.lines[i]
            rest = line["content"][1:].strip()
            if not rest:
                i += 1
                if i < len(self.lines) and self.lines[i]["indent"] > indent:
                    value, i = self.node(i, self.lines[i]["indent"])
                else:
                    value = {"kind": "scalar", "value": None, "line": line["n"]}
            elif KEY.match(rest) and rest[0] not in "[{":
                col = line["col"] + len(line["content"]) - len(rest)
                self.lines[i] = {**line, "indent": col, "col": col, "content": rest}
                value, i = self.map(i, col)
            else:
                value = {"kind": "scalar", "value": scalar(rest), "line": line["n"]}
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
        raise ConfigError("config.yml is not a mapping")
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


def reconcile(text: str, keep_directory) -> tuple[str, list[str]]:
    """Make `skills.enabled` and `skills.enablePiUser` true, drop `skills.customDirectories` entries for which
    `keep_directory(path)` is false, and drop `native` from `disabledProviders` (path-scoped lists too).
    Returns the new text and what changed; other lines stay byte for byte."""
    parser = Parser(text)
    tree = parser.parse()
    raw = parser.raw
    replace, delete, insert, changes = {}, set(), {}, []
    top_keys = [e["raw_key"] for e in tree.get("entries", [])]
    unit = next((ln["indent"] for ln in parser.lines if ln["indent"] > 0), 2)

    def rewrite_entry(e, value: str):
        replace[e["line"]] = raw[e["line"]][: e["col"]] + e["raw_key"] + ": " + value

    def prune(e, keep, label: str):
        node = e["value"]
        if node["kind"] == "seq":
            gone = [item for item in node["items"] if item["value"]["kind"] == "scalar"
                    and item["value"]["line"] == item["line"] and not keep(item["value"]["value"])]
            for item in gone:
                delete.add(item["line"])
                changes.append(f"{label} drops {item['value']['value']}")
            if gone and len(gone) == len(node["items"]):
                rewrite_entry(e, "[]")
        elif node["kind"] == "scalar" and isinstance(node["value"], list):
            kept = [v for v in node["value"] if keep(v)]
            if kept != node["value"]:
                changes.extend(f"{label} drops {v}" for v in node["value"] if not keep(v))
                if node["line"] == e["line"]:
                    rewrite_entry(e, json.dumps(kept))
                else:
                    line = raw[node["line"]]
                    replace[node["line"]] = line[: len(line) - len(line.lstrip())] + json.dumps(kept)

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
            if node["value"] not in (None, {}):
                raise ConfigError("config.yml: skills is not a mapping")
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
            prune(directories, keep_directory, "skills.customDirectories")
    disabled = entry(tree, "disabledProviders")
    if disabled:
        prune(disabled, lambda value: value != "native", "disabledProviders")
        if disabled["value"]["kind"] == "seq":
            for item in disabled["value"]["items"]:
                for name in ("providers", "values", "items"):
                    scoped = entry(item["value"], name)
                    if scoped:
                        prune(scoped, lambda value: value != "native", f"disabledProviders {name}")
    if not changes:
        return text, []
    out = []
    for n, line in enumerate(raw):
        out += insert.get(n, [])
        if n not in delete:
            out.append(replace.get(n, line))
    out += insert.get(len(raw), [])
    return "\n".join(out), changes

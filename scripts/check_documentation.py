"""检查自有文档的本地链接和读者入口，不访问网络。"""

from __future__ import annotations

import html
import re
import unicodedata
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import unquote, urlsplit

MARKDOWN_LINK = re.compile(r"!?\[[^]]*]\(([^)]+)\)")
FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")


def _prose_lines(text: str) -> Iterator[str]:
    """围栏和 HTML 注释不是可见导航或章节。"""
    fence = ""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    for line in text.splitlines():
        marker = FENCE.match(line)
        if marker:
            token = marker.group(1)
            if not fence:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = ""
            continue
        if not fence:
            yield line


def _destinations(text: str) -> list[str]:
    """维护导航使用行内 Markdown 链接。"""
    destinations: list[str] = []
    for line in _prose_lines(text):
        for match in MARKDOWN_LINK.finditer(line):
            value = match.group(1).strip()
            if value.startswith("<"):
                destinations.append(value[1:].split(">", 1)[0])
            elif value:
                destinations.append(value.split()[0])
    return destinations


def document_anchors(text: str) -> set[str]:
    """解析本仓库的 ATX/Setext 标题与显式 HTML 锚点。"""
    anchors: set[str] = set()
    headings: set[str] = set()
    previous = ""
    for line in _prose_lines(text):
        anchors.update(re.findall(r"<a\s+(?:id|name)=[\"\']([^\"\']+)[\"\']", line))
        heading = re.match(r"^ {0,3}#{1,6}\s+(.+?)\s*#*\s*$", line)
        title = heading.group(1) if heading else ""
        if not title and previous.strip() and re.fullmatch(r" {0,3}(?:=+|-+)\s*", line):
            title = previous.strip()
        previous = line
        if not title:
            continue
        title = re.sub(r"!?\[([^]]*)]\([^)]*\)", r"\1", title)
        title = html.unescape(re.sub(r"<[^>]*>", "", title)).lower()
        slug = "".join(
            char
            for char in title
            if char in "-_ " or unicodedata.category(char)[0] in {"L", "N", "M"}
        ).replace(" ", "-")
        candidate = slug
        suffix = 0
        while candidate in headings:
            suffix += 1
            candidate = f"{slug}-{suffix}"
        headings.add(candidate)
        anchors.add(candidate)
    return anchors


def check_documentation(root: Path, files: list[Path]) -> list[str]:
    """返回失效链接、越界路径和无法从读者入口到达的文档。"""
    root = root.resolve()
    documents = {
        path for path in files if path.suffix.lower() == ".md" and path.parts[0] != "vendor"
    }
    inputs = set(files)
    graph: dict[Path, set[Path]] = {path: set() for path in documents}
    anchors = {
        path: document_anchors((root / path).read_text(encoding="utf-8")) for path in documents
    }
    errors: list[str] = []
    for path in sorted(documents):
        for destination in _destinations((root / path).read_text(encoding="utf-8")):
            parts = urlsplit(destination)
            if parts.scheme or parts.netloc:
                continue
            local_path = unquote(parts.path)
            target = (root / path.parent / local_path).resolve() if local_path else root / path
            try:
                relative = target.relative_to(root)
            except ValueError:
                errors.append(f"Markdown link escapes repository: {path} -> {local_path}")
                continue
            if not target.exists():
                errors.append(f"broken local Markdown link: {path} -> {local_path}")
                continue
            if target.is_dir():
                if not any(item.is_relative_to(relative) for item in inputs):
                    errors.append(
                        f"Markdown target not in repository inputs: {path} -> {destination}"
                    )
                    continue
                relative /= "README.md"
            elif relative not in inputs:
                errors.append(f"Markdown target not in repository inputs: {path} -> {destination}")
                continue
            if relative in documents:
                graph[path].add(relative)
            fragment = unquote(parts.fragment)
            if fragment and relative in anchors and fragment not in anchors[relative]:
                errors.append(f"broken Markdown fragment: {path} -> {destination}")

    pending = list({Path("README.md"), Path("AGENTS.md")} & documents)
    reached: set[Path] = set()
    while pending:
        current = pending.pop()
        if current in reached:
            continue
        reached.add(current)
        pending.extend(graph[current] - reached)
    errors.extend(
        f"unindexed Markdown document: {path}; link from a reachable owner"
        for path in sorted(documents - reached)
    )
    return errors

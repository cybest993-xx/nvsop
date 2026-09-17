"""检查自有文档的本地链接和读者入口，不访问网络。"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

MARKDOWN_LINK = re.compile(r"!?\[[^]]*]\(([^)]+)\)")
FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")


def _destinations(text: str) -> list[str]:
    """导航使用行内 Markdown 链接，代码围栏内的示例不算入口。"""
    destinations: list[str] = []
    fence = ""
    for line in text.splitlines():
        marker = FENCE.match(line)
        if marker:
            token = marker.group(1)
            if not fence:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = ""
            continue
        if fence:
            continue
        for match in MARKDOWN_LINK.finditer(line):
            value = match.group(1).strip()
            if value.startswith("<"):
                destinations.append(value[1:].split(">", 1)[0])
            elif value:
                destinations.append(value.split()[0])
    return destinations


def check_documentation(root: Path, files: list[Path]) -> list[str]:
    """返回失效链接、越界路径和无法从读者入口到达的文档。"""
    root = root.resolve()
    documents = {
        path for path in files if path.suffix.lower() == ".md" and path.parts[0] != "vendor"
    }
    graph: dict[Path, set[Path]] = {path: set() for path in documents}
    errors: list[str] = []
    for path in sorted(documents):
        for destination in _destinations((root / path).read_text(encoding="utf-8")):
            parts = urlsplit(destination)
            if parts.scheme or parts.netloc or not parts.path:
                continue
            local_path = unquote(parts.path)
            target = (root / path.parent / local_path).resolve()
            try:
                relative = target.relative_to(root)
            except ValueError:
                errors.append(f"Markdown link escapes repository: {path} -> {local_path}")
                continue
            if not target.exists():
                errors.append(f"broken local Markdown link: {path} -> {local_path}")
                continue
            if target.is_dir():
                relative /= "README.md"
            if relative in documents:
                graph[path].add(relative)

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

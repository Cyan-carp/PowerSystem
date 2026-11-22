"""Shared link parsing and resolution for the note governance tools.

`check_notes.py`（校验）与 `tools/note-graph/build_graph.py`（图谱）共用本模块，
避免同一份 Vault 出现“检查器说没问题、图谱说断链”的两套解析规则。

设计要点：

- **提取与判定分开。** `iter_links()` 只按语法找出链接；锚点是否成立由
  `anchor_status(text, subpath, dialect)` 判定。Markdown 与 Obsidian 的锚点
  语义不同，不能共用一套归一化：
  - `markdown`：GitHub 规则。片段是 URL 解码后的标题 slug，重复标题按出现顺序
    追加 `-1`、`-2`；标题里的行内代码按渲染后的文字参与 slug。
  - `obsidian`：双链按标题原文（含层级 `#` 与块 `^id`）匹配，不做 slug 归一化。
- Markdown 链接**只**按源文件相对路径解析，不退回“全库同名文件”兜底；
  Obsidian 双链才允许“短名唯一匹配”。这是语义差异，不是宽松度差异。
- 相对路径、Vault 根路径、URL 编码、尖括号、表格转义别名、带括号文件名
  统一在这里处理，调用方只拿到解析结果。

支持的链接语法（其余形式不声明支持，遇到也不会被当成链接）：

- 行内链接与行内图片：`[text](target)`、`![alt](target)`
- 完整引用：`[text][label]`、`![alt][label]`
- 折叠引用：`[text][]`、`![alt][]`
- 简写引用：`[label]`（仅当 `[label]: target` 已定义）
- Obsidian 双链与嵌入：`[[note#标题|别名]]`、`![[file]]`
- 围栏代码块（```/~~~）内的内容不参与扫描；行内代码在提取链接时剔除，
  但在标题文字里保留其内容（GitHub 会把代码文字计入 slug）。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

EXTERNAL_PREFIXES = (
    "http://",
    "https://",
    "file://",
    "mailto:",
    "obsidian://",
    "data:",
    "tel:",
    "ftp://",
)

WIKILINK = re.compile(r"(!?)\[\[([^\[\]]+?)\]\]")
REFERENCE_DEF = re.compile(r"^\s{0,3}\[([^\]]+)\]:\s*(\S+)(?:\s+[\"'(].*)?$")
# 一个正则同时吃掉 `[text][label]` 的两个括号：分开匹配会把 label 再当简写引用计一次。
REFERENCE = re.compile(r"(!?)\[([^\[\]]*)\](?:\[([^\[\]]*)\])?")
FENCE = re.compile(r"^\s{0,3}(```+|~~~+)")
INLINE_CODE = re.compile(r"`[^`\n]*`")
ATX_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
BLOCK_ID = re.compile(r"(?:^|\s)\^([A-Za-z0-9-]+)\s*$")
MD_ESCAPE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|])")
HTML_TAG = re.compile(r"</?[A-Za-z][^>]*>")


@dataclass(frozen=True)
class Link:
    """A link found in a Markdown file, with its 1-based source line."""

    raw: str
    kind: str  # wikilink | markdown | reference
    line: int
    target: str  # path part only (no alias / anchor)
    subpath: str  # anchor or block id, without '#'
    image: bool = False

    @property
    def display(self) -> str:
        return self.raw


@dataclass(frozen=True)
class Heading:
    """One ATX heading: raw source text, rendered text, and its line."""

    level: int
    raw: str
    plain: str
    line: int


@dataclass(frozen=True)
class Resolution:
    """Outcome of resolving one link target."""

    status: str  # ok | external | empty | missing | ambiguous | case | outside
    path: Path | None = None
    detail: str = ""


def is_external(target: str) -> bool:
    return target.strip().lower().startswith(EXTERNAL_PREFIXES)


def normalise(target: str) -> str:
    """Strip Markdown/Obsidian wrappers that are not part of the path."""

    value = target.strip()
    if value.startswith("<") and value.endswith(">"):
        value = value[1:-1]
    return unquote(value).replace("\\", "/").strip()


def split_target(raw: str) -> tuple[str, str, str]:
    """Split `path#subpath|alias` (already unescaped) into its three parts."""

    value = raw.replace("\\|", "|")
    alias = ""
    if "|" in value:
        value, alias = value.split("|", 1)
    path, _, subpath = value.partition("#")
    return path.strip(), subpath.strip(), alias.strip()


def dialect_of(kind: str) -> str:
    """Which anchor rules apply to a link of this kind."""

    return "obsidian" if kind == "wikilink" else "markdown"


def scan_lines(text: str) -> list[tuple[int, str, str]]:
    """(line number, raw line, code-free line) outside fenced code blocks.

    The raw line is kept for heading parsing: GitHub counts the *rendered* text of
    a heading, so ``## Use `pip``` must slug to `use-pip`, not `use-`. Link
    extraction uses the code-free copy, because a link inside backticks is an
    example rather than a reference.
    """

    kept: list[tuple[int, str, str]] = []
    fence: str | None = None
    for number, line in enumerate(text.splitlines(), 1):
        match = FENCE.match(line)
        if match:
            marker = match.group(1)[0] * 3
            if fence is None:
                fence = marker
            elif marker == fence:
                fence = None
            continue
        if fence is not None:
            continue
        kept.append((number, line, INLINE_CODE.sub(" ", line)))
    return kept


def iter_links(text: str) -> list[Link]:
    """All Markdown and Obsidian links in `text`, with line numbers."""

    links: list[Link] = []
    lines = scan_lines(text)
    definitions: dict[str, tuple[str, int]] = {}
    definition_lines: set[int] = set()
    for number, _, code_free in lines:
        found = REFERENCE_DEF.match(code_free)
        if found:
            label = found.group(1).strip().casefold()
            definitions.setdefault(label, (found.group(2).strip(), number))
            definition_lines.add(number)

    for number, _, code_free in lines:
        for image, raw in WIKILINK.findall(code_free):
            target, subpath, _ = split_target(raw)
            links.append(Link(f"[[{raw}]]", "wikilink", number, target, subpath, bool(image)))
        for image, raw in _inline_links(code_free):
            target, subpath, _ = split_target(raw)
            links.append(Link(f"({raw})", "markdown", number, target, subpath, image))
        if number in definition_lines:
            # 定义行本身不是引用；把它当简写引用会重复计数同一目标。
            continue
        for image, raw in _reference_links(code_free, definitions):
            target, subpath, _ = split_target(raw)
            links.append(Link(f"({raw})", "reference", number, target, subpath, image))
    return links


def _inline_links(line: str) -> list[tuple[bool, str]]:
    """`](...)` targets with balanced parentheses, plus whether it is an image."""

    targets: list[tuple[bool, str]] = []
    start = 0
    while True:
        index = line.find("](", start)
        if index < 0:
            return targets
        # `!` 在 `[` 之前，不在 `]` 之前：从 `]` 向前找配对的 `[` 再判断。
        opener = line.rfind("[", 0, index)
        image = opener > 0 and line[opener - 1] == "!"
        cursor, depth = index + 2, 1
        if cursor < len(line) and line[cursor] == "<":
            close = line.find(">", cursor)
            if close > 0:
                targets.append((image, line[cursor : close + 1]))
                start = close + 1
                continue
        while cursor < len(line):
            char = line[cursor]
            if char == "\\":
                cursor += 2
                continue
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    targets.append((image, line[index + 2 : cursor]))
                    start = cursor + 1
                    break
            cursor += 1
        else:
            return targets


def _markdown_targets(line: str) -> list[str]:
    """Backward-compatible view of `_inline_links` used by the parser tests."""

    return [target for _, target in _inline_links(line)]


def _reference_links(
    line: str, definitions: dict[str, tuple[str, int]]
) -> list[tuple[bool, str]]:
    """Full, collapsed and shortcut references on one line.

    `[text](url)` is skipped because it is an inline link; `[[note]]` is masked
    because Obsidian's brackets are not reference labels.
    """

    found: list[tuple[bool, str]] = []
    masked = WIKILINK.sub(lambda match: "\0" * len(match.group(0)), line)
    for match in REFERENCE.finditer(masked):
        image, text, label = bool(match.group(1)), match.group(2), match.group(3)
        if label is None and masked[match.end() :].startswith("("):
            # 行内链接/图片，交给 `_inline_links`；这里只处理引用式。
            continue
        # 折叠引用 `[text][]` 的 label 为空，按 CommonMark 取 text 作标签。
        key = (label or text).strip().casefold()
        if not key:
            continue
        entry = definitions.get(key)
        if entry:
            found.append((image, entry[0]))
    return found


def heading_text(raw: str) -> str:
    """Rendered heading text: inline code kept, other Markdown removed."""

    codes: list[str] = []

    def stash(match: re.Match[str]) -> str:
        codes.append(match.group(0)[1:-1])
        return f"\0{len(codes) - 1}\0"

    value = INLINE_CODE.sub(stash, raw)
    value = MD_ESCAPE.sub(r"\1", value)
    value = re.sub(r"!?\[\[([^\[\]|]+)(?:\|([^\]]*))?\]\]", lambda m: m.group(2) or m.group(1), value)
    value = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", value)
    value = HTML_TAG.sub("", value)
    value = re.sub(r"[*_~]+", "", value)
    value = re.sub(r"\0(\d+)\0", lambda m: codes[int(m.group(1))], value)
    return value.strip()


def headings(text: str) -> list[Heading]:
    """ATX headings outside fenced blocks."""

    result: list[Heading] = []
    for number, raw, _ in scan_lines(text):
        match = ATX_HEADING.match(raw)
        if match:
            title = match.group(2).strip()
            result.append(Heading(len(match.group(1)), title, heading_text(title), number))
    return result


def slug(text: str) -> str:
    """GitHub-style anchor slug: lowercase, drop punctuation, spaces to hyphens."""

    value = text.strip().lower()
    value = re.sub(r"[^\w\u3400-\u9fff\- ]", "", value, flags=re.UNICODE)
    return value.replace(" ", "-")


def markdown_slugs(text: str) -> dict[str, int]:
    """Every fragment GitHub generates for this page, including `-1` duplicates."""

    used: set[str] = set()
    result: dict[str, int] = {}
    for item in headings(text):
        base = slug(item.plain) or "section"
        candidate = base
        index = 0
        while candidate in used:
            index += 1
            candidate = f"{base}-{index}"
        used.add(candidate)
        result[candidate] = item.line
    return result


def block_ids(text: str) -> set[str]:
    found: set[str] = set()
    for _, line, _ in scan_lines(text):
        match = BLOCK_ID.search(line)
        if match:
            found.add(match.group(1).casefold())
    return found


def anchor_status(target_text: str, subpath: str, dialect: str = "obsidian") -> str:
    """Check a fragment against one page: ok | missing | ambiguous.

    The dialect is not cosmetic: GitHub accepts only the generated slug (with
    `-1` numbering for duplicates), while Obsidian matches the heading as written.
    Mixing them either hides a broken link or invents one.
    """

    if not subpath:
        return "ok"
    wanted = unquote(subpath.strip())
    if not wanted:
        return "ok"
    if dialect == "markdown":
        return _markdown_anchor(target_text, wanted)
    return _obsidian_anchor(target_text, wanted)


def _markdown_anchor(target_text: str, fragment: str) -> str:
    value = fragment.lstrip("#")
    if value.startswith("user-content-"):
        value = value[len("user-content-") :]
    if not value:
        return "ok"
    return "ok" if value.casefold() in markdown_slugs(target_text) else "missing"


def _obsidian_anchor(target_text: str, fragment: str) -> str:
    levels = [part for part in fragment.split("#") if part]
    if not levels:
        return "ok"
    last = levels[-1]
    if last.startswith("^"):
        return "ok" if last[1:].casefold() in block_ids(target_text) else "missing"
    hits = [
        item
        for item in headings(target_text)
        if item.plain.casefold() == last.casefold() or item.raw.casefold() == last.casefold()
    ]
    if not hits:
        return "missing"
    return "ok" if len(hits) == 1 else "ambiguous"


def _candidate_paths(source: Path, target: str, vault: Path | None, kind: str) -> list[Path]:
    raw = Path(target)
    bases: list[Path] = []
    if not raw.is_absolute():
        bases.append(source.parent / raw)
        if vault is not None and kind == "wikilink":
            bases.append(vault / raw)
    else:
        bases.append(raw)
    candidates: list[Path] = []
    for base in bases:
        # `..` must be collapsed before walking components: no directory lists
        # a literal ".." entry, so an unnormalised path would look missing.
        base = Path(os.path.normpath(str(base)))
        candidates.append(base)
        if base.suffix.casefold() != ".md":
            candidates.append(Path(str(base) + ".md"))
        if not base.suffix:
            candidates.append(base / "README.md")
            candidates.append(Path(str(base) + ".md"))
    return candidates


def _directory_names(path: Path) -> set[str]:
    try:
        return {entry.name for entry in path.iterdir()}
    except OSError:
        return set()


_DIR_CACHE: dict[str, set[str]] = {}
_EXISTS_CACHE: dict[str, bool] = {}


def reset_cache() -> None:
    """Call between independent roots (tests, or a second vault in one process)."""

    _DIR_CACHE.clear()
    _EXISTS_CACHE.clear()


def cached_is_file(path: Path) -> bool:
    """Memoised `is_file()`: the same index pages are linked thousands of times."""

    key = str(path)
    value = _EXISTS_CACHE.get(key)
    if value is None:
        value = path.is_file()
        _EXISTS_CACHE[key] = value
    return value


def exact_exists(path: Path) -> bool:
    """True only when every path component matches on-disk case exactly.

    `Path.exists()` is case-insensitive on Windows and would silently accept
    `notes/casedir/note.md` for `notes/CaseDir/Note.md`; such a link still breaks
    on Linux, in GitHub and in a fresh clone.
    """

    if not cached_is_file(path):
        return False
    current = Path(path.anchor) if path.anchor else Path()
    parts = path.parts[1:] if path.anchor else path.parts
    for part in parts:
        key = str(current)
        names = _DIR_CACHE.get(key)
        if names is None:
            names = _directory_names(current)
            _DIR_CACHE[key] = names
        if part not in names:
            return False
        current = current / part
    return True


def _case_insensitive_hit(path: Path) -> Path | None:
    """Return the real path when `path` only matches by case, else None.

    Do not short-circuit on `path.exists()`: on Windows a wrong-case path exists,
    yet the same link fails on Linux and in a fresh clone.
    """

    current = Path(path.anchor) if path.anchor else Path()
    parts = path.parts[1:] if path.anchor else path.parts
    differs = False
    for part in parts:
        key = str(current)
        names = _DIR_CACHE.get(key)
        if names is None:
            names = _directory_names(current)
            _DIR_CACHE[key] = names
        if part in names:
            current = current / part
            continue
        real = next((name for name in names if name.casefold() == part.casefold()), None)
        if real is None:
            return None
        differs = True
        current = current / real
    return current if differs and cached_is_file(current) else None


def resolve(
    target: str,
    source: Path,
    by_stem: dict[str, list[Path]],
    *,
    vault: Path | None = None,
    kind: str = "wikilink",
    notes_only: bool = False,
    root: Path | None = None,
) -> Resolution:
    """Resolve one link target.

    `notes_only` restricts wikilink short-name matching to Markdown notes, which is
    what the graph builder needs; the checker keeps every file so that image and
    attachment links are validated too.
    """

    value = normalise(target)
    if not value:
        return Resolution("empty")
    if is_external(value):
        return Resolution("external")
    if value.startswith("/"):
        return Resolution("outside", detail="absolute path")
    candidates = _candidate_paths(source, value, vault, kind)
    for candidate in candidates:
        if cached_is_file(candidate) and exact_exists(candidate):
            if notes_only and candidate.suffix.casefold() != ".md":
                continue
            if root is not None and not _inside(candidate, root):
                return Resolution("outside", detail=str(candidate))
            return Resolution("ok", candidate)
    for candidate in candidates:
        if notes_only and candidate.suffix.casefold() != ".md":
            continue
        real = _case_insensitive_hit(candidate)
        if real is not None:
            return Resolution("case", real, detail=candidate.name)
    if kind == "wikilink" and "/" not in value:
        stem = Path(value).stem.casefold()
        matches = [
            item
            for item in by_stem.get(stem, [])
            if not notes_only or item.suffix.casefold() == ".md"
        ]
        if len(matches) == 1:
            return Resolution("ok", matches[0])
        if len(matches) > 1:
            return Resolution("ambiguous", None, detail=f"{len(matches)} files share the name")
    hint = Path(os.path.normpath(str(source.parent / value)))
    if root is not None and _inside(hint, root):
        return Resolution("missing", hint)
    return Resolution("outside", detail=value)


def _inside(path: Path, root: Path) -> bool:
    """String-level containment check; `resolve()` per link made the scan ~10x slower."""

    try:
        return os.path.commonpath([os.path.abspath(path), os.path.abspath(root)]) == os.path.abspath(root)
    except ValueError:
        return False

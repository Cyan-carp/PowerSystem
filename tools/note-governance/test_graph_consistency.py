#!/usr/bin/env python3
"""图谱与检查器一致性测试。

复核 R3 的核心断言是：目标页存在、但章节不存在时，图谱必须和检查器一样报失效；
并且 `inputDigest` 要随源笔记字节变化，而不是随派生统计变化。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "note-graph"))
import build_graph as bg  # noqa: E402
import check_notes as cn  # noqa: E402

FRONT = "---\ntags: [测试]\nstatus: 现行\nscope: 跨版本\nupdated: 2026-11-22\n---\n\n"


def note(title: str, body: str = "") -> str:
    return FRONT + title + "\n" + body


class GraphFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.write("notes/00-项目笔记总入口.md", note("# 项目笔记总入口"))
        self.write("notes/00-当前系统状态.md", note("# 当前系统状态"))
        self.write("notes/02-工程规范边界与验收治理/项目验收与收尾清单/00-索引.md", note("# 验收清单"))
        self.write("tools/note-governance/workspace-links.json", "[]")
        self.write("tools/note-governance/migration-map.json", '{"moves": [], "files": {}}')
        self.write("site/template.html", "<html><script>const DATA = __DATA__;</script></html>")

    def write(self, relative: str, text: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def build(self) -> dict:
        bg.main()
        return json.loads((bg.SITE / "graph-data.json").read_text(encoding="utf-8"))

    def check(self) -> list[str]:
        issues, _ = cn.run(root=self.root, ci_mode=False, verbose=True)
        return [issue.kind for issue in issues]


class GraphConsistencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fixture = GraphFixture(self.root)
        self.original = (bg.ROOT, bg.VAULT, bg.SITE)
        bg.ROOT = self.root
        bg.VAULT = self.root / "notes"
        bg.SITE = self.root / "site"

    def tearDown(self) -> None:
        bg.ROOT, bg.VAULT, bg.SITE = self.original
        self.tmp.cleanup()

    def test_missing_anchor_is_broken_in_graph_and_checker(self):
        self.fixture.write("notes/01-target.md", note("# 目标", "## 现有章节\n"))
        self.fixture.write("notes/02-source.md", note("# 来源", "[[01-target#不存在的章节]]\n"))

        data = self.fixture.build()
        self.assertEqual(data["stats"]["brokenTargets"], 0, "目标页存在，不应算目标缺失")
        self.assertEqual(data["stats"]["brokenAnchors"], 1)
        self.assertEqual(data["broken"][0]["reason"], "missing-anchor")
        self.assertIn("broken-anchor", self.fixture.check())

    def test_fixed_anchor_passes_both(self):
        self.fixture.write("notes/01-target.md", note("# 目标", "## 现有章节\n"))
        self.fixture.write("notes/02-source.md", note("# 来源", "[[01-target#现有章节]]\n"))

        data = self.fixture.build()
        self.assertEqual(data["stats"]["broken"], 0)
        self.assertNotIn("broken-anchor", self.fixture.check())

    def test_same_page_anchor_is_checked(self):
        self.fixture.write("notes/01-note.md", note("# 页面", "## 现有\n\n[[#不存在]]\n"))
        data = self.fixture.build()
        self.assertEqual(data["stats"]["brokenAnchors"], 1)
        self.assertIn("broken-anchor", self.fixture.check())

    def test_missing_target_is_reported(self):
        self.fixture.write("notes/02-source.md", note("# 来源", "[[99-gone]]\n"))
        data = self.fixture.build()
        self.assertEqual(data["stats"]["brokenTargets"], 1)

    def test_digest_follows_source_bytes_not_derived_stats(self):
        """等长正文改动不会改变任何统计，但输入摘要必须变化。"""

        self.fixture.write("notes/03-note.md", note("# 三", "AAAA\n"))
        first = self.fixture.build()["inputDigest"]
        self.fixture.write("notes/03-note.md", note("# 三", "BBBB\n"))
        second = self.fixture.build()["inputDigest"]
        self.assertNotEqual(first, second)

    def test_repeated_build_is_byte_identical(self):
        self.fixture.write("notes/01-target.md", note("# 目标", "## 现有章节\n"))
        self.fixture.build()
        first = (bg.SITE / "graph-data.json").read_bytes()
        self.fixture.build()
        second = (bg.SITE / "graph-data.json").read_bytes()
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()

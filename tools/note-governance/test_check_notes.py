#!/usr/bin/env python3
"""解析与回归测试：每条用例在临时夹具里制造一种真实缺陷，禁止移动真实笔记。

运行：
    python -m unittest discover -s tools/note-governance -p 'test_*.py'

用例对应 2026-11-22 文档治理第三批列出的八类问题：目录迁移断链、旧标题锚点、
中文括号文件名、表格转义别名、代码示例误报、Markdown 同名误解析、大小写差异、
私有证据 CI 边界。前两类同时复现第一批实际修过的两种目录迁移错误。
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_notes  # noqa: E402
import note_links as nl  # noqa: E402

FRONT = "---\ntags: [测试]\nstatus: {status}\nscope: 跨版本\nupdated: 2026-11-22\n---\n\n"


def note(title: str = "# 页面", body: str = "", status: str = "现行") -> str:
    return FRONT.format(status=status) + title + "\n" + body


def entry(body: str) -> str:
    return note("# 项目笔记总入口", body)


class Fixture:
    """最小的仓库骨架：只要 Vault、验收清单、工作区清单。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.write("notes/00-项目笔记总入口.md", entry(""))
        self.write("notes/00-当前系统状态.md", note("# 当前系统状态"))
        self.write("notes/02-工程规范边界与验收治理/项目验收与收尾清单/00-索引.md", note("# 验收清单"))
        self.write("tools/note-governance/workspace-links.json", "[]")
        self.write("tools/note-governance/migration-map.json", '{"moves": [], "files": {}}')

    def write(self, relative: str, text: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def check(self, ci: bool = False):
        issues, stats = check_notes.run(root=self.root, ci_mode=ci, verbose=True)
        return issues, stats

    def kinds(self, ci: bool = False) -> list[str]:
        return [issue.kind for issue in self.check(ci)[0]]


class LinkParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fixture = Fixture(self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_markdown_link_does_not_fall_back_to_unique_name(self):
        """同名文件唯一时，Markdown 链接也不能靠全库兜底解析。"""

        self.fixture.write("notes/other/01-x.md", note("# X"))
        self.fixture.write("notes/sub/02-note.md", note("# 二", "[x](01-x.md)\n"))
        issues, _ = self.fixture.check()
        broken = [issue for issue in issues if issue.kind == "broken-link" and issue.path.endswith("02-note.md")]
        self.assertEqual(len(broken), 1, "Markdown 链接不应解析到 other/01-x.md")

    def test_markdown_link_to_sibling_resolves(self):
        self.fixture.write("notes/sub/01-x.md", note("# X"))
        self.fixture.write("notes/sub/02-note.md", note("# 二", "[x](01-x.md)\n"))
        self.assertNotIn("broken-link", self.fixture.kinds())

    def test_chinese_parentheses_filename(self):
        """文件名带中文括号时，`](...)` 的括号要成对消费。"""

        self.fixture.write("notes/sub/报告（2026）版.md", note("# 报告"))
        self.fixture.write("notes/sub/02-note.md", note("# 二", "[报告](报告（2026）版.md)\n"))
        self.assertNotIn("broken-link", self.fixture.kinds())

    def test_escaped_alias_in_table(self):
        """表格里的 `[[路径\\|别名]]` 只把反斜杠当转义。"""

        self.fixture.write("notes/sub/01-x.md", note("# X"))
        self.fixture.write("notes/sub/02-note.md", note("# 二", "| 链接 |\n| --- |\n| [[01-x\\|别名]] |\n"))
        self.assertNotIn("broken-link", self.fixture.kinds())

    def test_code_examples_are_not_links(self):
        """围栏代码与行内代码里的示例不能当正文链接。"""

        body = "```text\n[x](missing.md)\n```\n\n行内 `[y](missing2.md)` 示例。\n"
        self.fixture.write("notes/sub/02-note.md", note("# 二", body))
        self.assertNotIn("broken-link", self.fixture.kinds())


class AnchorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fixture = Fixture(self.root)
        self.fixture.write("notes/sub/01-target.md", note("# 目标", "## 新标题\n"))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_old_heading_anchor_fails(self):
        self.fixture.write("notes/sub/02-note.md", note("# 二", "[[01-target#旧标题]]\n"))
        self.assertIn("broken-anchor", self.fixture.kinds())

    def test_current_heading_anchor_passes(self):
        self.fixture.write("notes/sub/02-note.md", note("# 二", "[[01-target#新标题]]\n"))
        self.assertNotIn("broken-anchor", self.fixture.kinds())

    def test_duplicate_heading_is_ambiguous(self):
        self.fixture.write("notes/sub/01-target.md", note("# 目标", "## 同一个\n## 同一个\n"))
        self.fixture.write("notes/sub/02-note.md", note("# 二", "[[01-target#同一个]]\n"))
        kinds = self.fixture.kinds()
        self.assertIn("ambiguous-anchor", kinds)
        self.assertNotIn("broken-anchor", kinds)

    def test_vault_link_to_outside_markdown_checks_anchor(self):
        """指向库外 README 的章节同样会失效，不能因为目标在 Vault 外就跳过。"""

        self.fixture.write("stages/svc/README.md", "# 服务\n\n## 现有章节\n")
        self.fixture.write("notes/sub/02-note.md", note("# 二", "[服务](../../stages/svc/README.md#不存在)\n"))
        issues, _ = self.fixture.check()
        self.assertTrue(any(issue.kind == "broken-anchor" and "stages/svc/README.md" in issue.target for issue in issues))

    def test_vault_link_to_outside_markdown_fixed_passes(self):
        self.fixture.write("stages/svc/README.md", "# 服务\n\n## 现有章节\n")
        self.fixture.write("notes/sub/02-note.md", note("# 二", "[服务](../../stages/svc/README.md#现有章节)\n"))
        self.assertNotIn("broken-anchor", self.fixture.kinds())


class WorkspaceAnchorTests(unittest.TestCase):
    """R1：库外 Markdown 的同页与跨页章节此前被整体跳过。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fixture = Fixture(self.root)
        self.fixture.write("stages/svc/README.md", "# 服务\n\n## 现有章节\n")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_same_page_anchor_missing_fails(self):
        self.fixture.write("README.md", "# 项目\n\n## 现有章节\n\n[缺失](#不存在)\n")
        issues, _ = self.fixture.check()
        self.assertTrue(any(issue.kind == "broken-anchor" and issue.path == "README.md" for issue in issues))

    def test_same_page_anchor_present_passes(self):
        self.fixture.write("README.md", "# 项目\n\n## 现有章节\n\n[跳转](#现有章节)\n")
        self.assertNotIn("broken-anchor", self.fixture.kinds())

    def test_cross_page_anchor_missing_fails(self):
        self.fixture.write("README.md", "# 项目\n\n[服务](stages/svc/README.md#不存在)\n")
        issues, _ = self.fixture.check()
        self.assertTrue(any(issue.kind == "broken-anchor" for issue in issues))

    def test_cross_page_anchor_fixed_passes(self):
        self.fixture.write("README.md", "# 项目\n\n[服务](stages/svc/README.md#现有章节)\n")
        self.assertNotIn("broken-anchor", self.fixture.kinds())


class MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fixture = Fixture(self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_directory_rename_residue_reports_new_path(self):
        """复现第一批修过的目录改名断链：旧目录名仍被现行页面引用。"""

        self.fixture.write("tools/note-governance/migration-map.json",
                           '{"moves": [{"from": "notes/old-grid", "to": "notes/new-grid"}], "files": {}}')
        self.fixture.write("notes/new-grid/01-x.md", note("# X"))
        self.fixture.write("notes/sub/02-note.md", note("# 二", "[[old-grid/01-x]]\n"))
        issues, _ = self.fixture.check()
        residue = [issue for issue in issues if issue.kind == "migration-residue"]
        self.assertEqual(len(residue), 1)
        self.assertIn("notes/new-grid/01-x.md", residue[0].message)

    def test_nested_file_move_residue(self):
        """嵌套文件改位置（如日志 69 从分组根移入子目录）同样要给出新路径。"""

        self.fixture.write(
            "tools/note-governance/migration-map.json",
            '{"moves": [], "files": {"notes/logs/69-report.md": "notes/logs/ui/69-report.md"}}',
        )
        self.fixture.write("notes/logs/ui/69-report.md", note("# 69"))
        self.fixture.write("notes/sub/02-note.md", note("# 二", "[[logs/69-report]]\n"))
        issues, _ = self.fixture.check()
        self.assertTrue(any(issue.kind == "migration-residue" and "logs/ui/69-report.md" in issue.message for issue in issues))


class CaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fixture = Fixture(self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_wrong_case_is_detected_even_on_windows(self):
        self.fixture.write("notes/CaseDir/Note.md", note("# Note"))
        self.fixture.write("notes/sub/02-note.md", note("# 二", "[[casedir/note]]\n"))
        self.assertIn("case-mismatch", self.fixture.kinds())

    def test_exact_case_passes(self):
        self.fixture.write("notes/CaseDir/Note.md", note("# Note"))
        self.fixture.write("notes/sub/02-note.md", note("# 二", "[[CaseDir/Note]]\n"))
        self.assertNotIn("case-mismatch", self.fixture.kinds())


class PrivateEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fixture = Fixture(self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_repo_artifacts_missing_fails_strict_and_passes_ci(self):
        self.fixture.write("notes/sub/02-note.md", note("# 二", "[证据](../../artifacts/evidence/x.zip)\n"))
        self.assertIn("broken-link", self.fixture.kinds(ci=False))
        self.assertNotIn("broken-link", self.fixture.kinds(ci=True))

    def test_artifacts_in_name_only_still_fails(self):
        """路径里含 artifacts 但不在仓库根 artifacts/ 下，CI 也不能放行。"""

        self.fixture.write("notes/sub/02-note.md", note("# 二", "[证据](artifacts/x.zip)\n"))
        self.assertIn("broken-link", self.fixture.kinds(ci=True))


class StructureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fixture = Fixture(self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_multi_note_directory_without_index(self):
        self.fixture.write("notes/group/01-a.md", note("# A"))
        self.fixture.write("notes/group/02-b.md", note("# B"))
        self.assertIn("missing-index", self.fixture.kinds())

    def test_unreachable_page_is_warning_not_error(self):
        self.fixture.write("notes/group/00-索引·组.md", note("# 组", "[[01-a]]\n"))
        self.fixture.write("notes/group/01-a.md", note("# A"))
        issues, stats = self.fixture.check()
        self.assertTrue(any(issue.kind == "index-unlinked" for issue in issues))
        self.assertFalse([issue for issue in issues if issue.severity == "error"])

    def test_metadata_and_size_limits(self):
        self.fixture.write("notes/sub/02-note.md", FRONT.format(status="在用") + "# 二\n")
        self.fixture.write("notes/sub/03-big.md", note("# 大", "x" * 15_001))
        kinds = self.fixture.kinds()
        self.assertEqual(kinds.count("metadata"), 1)
        self.assertIn("oversized", kinds)


class ParserUnitTests(unittest.TestCase):
    def test_markdown_targets_keep_balanced_parentheses(self):
        self.assertEqual(nl._markdown_targets("![a](图（1）.png)"), ["图（1）.png"])

    def test_heading_inline_code_participates_in_slug(self):
        """标题里的行内代码是渲染文字的一部分：`Use pip` 的 slug 是 use-pip。"""

        text = "# 页面\n\n## Use `pip` to install\n"
        self.assertEqual(nl.markdown_slugs(text), {"页面": 1, "use-pip-to-install": 3})
        self.assertEqual(nl.anchor_status(text, "use-pip-to-install", "markdown"), "ok")
        self.assertEqual(nl.anchor_status(text, "use--to-install", "markdown"), "missing")

    def test_duplicate_headings_get_github_numbering(self):
        text = "## Intro\n\n## Intro\n\n## Intro\n"
        self.assertEqual(set(nl.markdown_slugs(text)), {"intro", "intro-1", "intro-2"})
        self.assertEqual(nl.anchor_status(text, "intro-1", "markdown"), "ok")
        self.assertEqual(nl.anchor_status(text, "intro-2", "markdown"), "ok")
        self.assertEqual(nl.anchor_status(text, "intro-3", "markdown"), "missing")

    def test_markdown_and_obsidian_dialects_stay_separate(self):
        """同一个标题，GitHub 只认 slug，Obsidian 只认原文。"""

        text = "## 一、结果与范围\n"
        self.assertEqual(nl.anchor_status(text, "一结果与范围", "markdown"), "ok")
        self.assertEqual(nl.anchor_status(text, "一、结果与范围", "markdown"), "missing")
        self.assertEqual(nl.anchor_status(text, "一、结果与范围", "obsidian"), "ok")
        self.assertEqual(nl.anchor_status(text, "一结果与范围", "obsidian"), "missing")

    def test_percent_encoded_fragment_is_decoded(self):
        text = "## 结果 范围\n"
        self.assertEqual(nl.anchor_status(text, "%E7%BB%93%E6%9E%9C-%E8%8C%83%E5%9B%B4", "markdown"), "ok")

    def test_obsidian_block_id(self):
        text = "## 标题\n\n段落 ^blk-1\n"
        self.assertEqual(nl.anchor_status(text, "^blk-1", "obsidian"), "ok")
        self.assertEqual(nl.anchor_status(text, "^missing", "obsidian"), "missing")

    def test_reference_forms_are_all_detected(self):
        text = (
            "[a][full]\n"
            "[b][]\n"
            "[c]\n"
            "![img][pic]\n"
            "![inline](x.png)\n\n"
            "[full]: 01-a.md\n"
            "[b]: 01-b.md\n"
            "[c]: 01-c.md\n"
            '[pic]: x.png "带标题的定义"\n'
        )
        links = nl.iter_links(text)
        self.assertEqual([link.target for link in links], ["01-a.md", "01-b.md", "01-c.md", "x.png", "x.png"])
        self.assertEqual([link.kind for link in links], ["reference", "reference", "reference", "reference", "markdown"])
        self.assertTrue(links[3].image, "引用式图片要标记为 image")
        self.assertTrue(links[4].image, "行内图片要标记为 image")
        self.assertFalse(links[0].image)

    def test_definition_line_is_not_counted_as_a_use(self):
        text = "[c]: 01-c.md\n\n[c]\n"
        links = nl.iter_links(text)
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].line, 3)

    def test_code_fence_suppresses_links(self):
        text = "```\n[[a]]\n```\n[[b]]\n"
        self.assertEqual([link.target for link in nl.iter_links(text)], ["b"])


class GitTrackingTests(unittest.TestCase):
    """Linux 的 git ls-files 默认把中文路径转义，CI 会误判为未跟踪。"""

    def test_non_ascii_paths_are_not_quoted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "core.quotePath", "true"], cwd=root, check=True)
            target = root / "deploy" / "独立目录" / "脚本.sh"
            target.parent.mkdir(parents=True)
            target.write_text("x\n", encoding="utf-8")
            subprocess.run(["git", "add", "--", "deploy/独立目录/脚本.sh"], cwd=root, check=True)
            self.assertIn("deploy/独立目录/脚本.sh", check_notes.git_tracked_files(root))


if __name__ == "__main__":
    unittest.main()

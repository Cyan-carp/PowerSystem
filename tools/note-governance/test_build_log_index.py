#!/usr/bin/env python3
"""生成器回归测试：`--check` 必须在“有待生成内容”时失败。

只用临时目录，不触碰真实部署日志。用例对应 2026-11-22 复核 R4：
旧实现无论是否有待生成文件都返回 0，CI 也就无法发现漏生成。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_log_index as blog  # noqa: E402

ENTRY = """# 部署与运维日志

手写开头。

<!-- BEGIN GENERATED:timeline -->
## 四、最近批次

旧内容
<!-- END GENERATED:timeline -->

手写结尾。
"""


class LogIndexFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.layout = blog.Layout.at(root)
        self.page = "04-系统部署发布备份与变更记录/部署与运维日志/01-a.md"
        self.write(self.layout.vault / self.page, "# 01 · A\n")
        self.write(
            self.layout.catalog,
            json.dumps(
                {
                    "schema": 1,
                    "updated": "2026-11-22",
                    "entries": [
                        {
                            "number": "01",
                            "page": self.page,
                            "title": "01 · A",
                            "date": "2026-09-29",
                            "version": "V1",
                            "batch": "`a585fe1`",
                            "conclusion": "首版",
                            "month": "2026-09",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
        )
        self.write(self.layout.entry_page, ENTRY)
        self.write(
            self.layout.provenance,
            json.dumps(
                {
                    "schema": 1,
                    "source": {"rows": 1},
                    "rows": [
                        {
                            "row": 1,
                            "number": "01",
                            "catalog": {"number": "01", "page": self.page},
                            "line": "| [[01-a\\|01]] | 2026-09-29 | V1 | `a585fe1` | 首版 |",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
        )

    def write(self, path: Path, text: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def generate(self) -> tuple[list[str], int]:
        return blog.run(self.layout, check=False)

    def check(self) -> tuple[list[str], int]:
        return blog.run(self.layout, check=True)

    def add_merged_pair(self) -> None:
        """One page carrying two original numbers, as `33-（原33+36）` does."""

        data = json.loads(self.layout.catalog.read_text(encoding="utf-8"))
        page = data["entries"][0]["page"]
        data["entries"].append(
            {
                "number": "02",
                "page": page,
                "title": "02 · 合并页第二段",
                "date": "2026-09-29",
                "version": "V1",
                "batch": "`b`",
                "conclusion": "合并页第二段",
                "month": "2026-09",
            }
        )
        self.write(self.layout.catalog, json.dumps(data, ensure_ascii=False))
        prov = json.loads(self.layout.provenance.read_text(encoding="utf-8"))
        prov["source"]["rows"] = 2
        prov["rows"].append(
            {
                "row": 2,
                "number": "02",
                "catalog": {"number": "02", "page": page},
                "line": "| 02 · [[01-a]] | 2026-09-29 | V1 | `b` | 合并页第二段 |",
            }
        )
        self.write(self.layout.provenance, json.dumps(prov, ensure_ascii=False))


class GenerationCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.fixture = LogIndexFixture(Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_check_reports_every_missing_or_stale_file(self):
        problems, changed = self.fixture.check()
        self.assertEqual(problems, [])
        # 月份页、时间轴索引、入口页生成块，三处都需要生成。
        self.assertEqual(changed, 3)

    def test_generate_then_check_is_clean(self):
        _, generated = self.fixture.generate()
        self.assertEqual(generated, 3)
        problems, changed = self.fixture.check()
        self.assertEqual(problems, [])
        self.assertEqual(changed, 0, "生成后不应再有待生成文件")

    def test_stale_month_page_is_detected(self):
        self.fixture.generate()
        month = self.fixture.layout.timeline_dir / "2026-09-部署批次时间轴.md"
        month.write_text(month.read_text(encoding="utf-8") + "\n手工改动\n", encoding="utf-8")
        problems, changed = self.fixture.check()
        self.assertEqual(problems, [])
        self.assertEqual(changed, 1)

    def test_missing_month_page_is_detected(self):
        self.fixture.generate()
        (self.fixture.layout.timeline_dir / "2026-09-部署批次时间轴.md").unlink()
        problems, changed = self.fixture.check()
        self.assertEqual(problems, [])
        self.assertEqual(changed, 1)

    def test_handwritten_text_survives_generation(self):
        self.fixture.generate()
        text = self.fixture.layout.entry_page.read_text(encoding="utf-8")
        self.assertIn("手写开头", text)
        self.assertIn("手写结尾", text)
        self.assertNotIn("旧内容", text)

    def test_missing_marker_blocks_instead_of_overwriting(self):
        self.fixture.layout.entry_page.write_text("# 只有手写内容\n", encoding="utf-8")
        problems, changed = self.fixture.check()
        self.assertTrue(any("生成标记" in item for item in problems))
        self.assertEqual(changed, 0)

    def test_stale_provenance_mapping_blocks(self):
        data = json.loads(self.fixture.layout.provenance.read_text(encoding="utf-8"))
        data["rows"][0]["catalog"]["page"] = "04-系统部署发布备份与变更记录/部署与运维日志/99-gone.md"
        self.fixture.layout.provenance.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        problems, changed = self.fixture.check()
        self.assertTrue(any("映射已失效" in item for item in problems))
        self.assertEqual(changed, 0)

    def test_provenance_row_count_must_match_declaration(self):
        data = json.loads(self.fixture.layout.provenance.read_text(encoding="utf-8"))
        data["source"]["rows"] = 2
        self.fixture.layout.provenance.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        problems, _ = self.fixture.check()
        self.assertTrue(any("行数与声明不一致" in item for item in problems))

    def test_merged_page_with_correct_number_passes(self):
        self.fixture.add_merged_pair()
        problems, changed = self.fixture.check()
        self.assertEqual(problems, [])
        self.assertEqual(changed, 3)

    def test_merged_page_mapped_to_other_batch_blocks(self):
        """原文写 02、目录也有 02，但映射自身也写成 01：只有比对原文才拦得住。"""

        self.fixture.add_merged_pair()
        data = json.loads(self.fixture.layout.provenance.read_text(encoding="utf-8"))
        data["rows"][1]["number"] = "01"
        data["rows"][1]["catalog"]["number"] = "01"
        self.fixture.layout.provenance.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        problems, changed = self.fixture.check()
        self.assertTrue(any("误配另一批次" in item for item in problems), problems)
        self.assertEqual(changed, 0)


if __name__ == "__main__":
    unittest.main()

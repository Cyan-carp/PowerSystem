import importlib.util
from pathlib import Path
import httpx
import pytest
from rag_agent.knowledge import Knowledge
from test_engine import config

spec = importlib.util.spec_from_file_location("import_knowledge", Path(__file__).parents[1] / "scripts/knowledge.py")
ingest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ingest)


class Tokenizer:
    def encode(self, text, **kwargs): return list(text)
    def decode(self, tokens): return "".join(tokens)


def test_chunks_bound_tokens_and_preserve_tail():
    text = "甲" * 1000 + "最后一段"
    rows = list(ingest.chunks([("标题", text, 7)], Tokenizer()))
    assert len(rows) > 2 and all(len(row[1]) <= 400 for row in rows)
    assert rows[-1][1].endswith("最后一段\n") and all(row[2] == 7 for row in rows)
    assert rows[0][1][-60:] in rows[1][1]


@pytest.mark.asyncio
async def test_historical_source_reads_retained_collection():
    cfg = config()
    def handler(request):
        path = request.url.path
        if path == "/collections":
            return httpx.Response(200, json={"result":{"collections":[{"name":"unrelated_private"},{"name":"powersystem_rag_old_123"}]}})
        if "powersystem_rag_old_123" in path:
            return httpx.Response(200, json={"result":{"payload":{"version":"old","content":"历史章节"}}})
        if "unrelated_private" in path: pytest.fail("must not search unrelated collections")
        return httpx.Response(404)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert (await Knowledge(cfg, client).source("K" + "a" * 20))["version"] == "old"


def test_extract_document_types():
    # Keep generated files in the ignored test assets directory.
    folder = Path("artifacts/stage8-agent-rag/test-documents")
    folder.mkdir(parents=True, exist_ok=True)
    from docx import Document
    document = Document(); document.add_heading("设备资料", 1); document.add_paragraph("风扇用于散热")
    table = document.add_table(rows=1, cols=2); table.cell(0,0).text = "型号"; table.cell(0,1).text = "样例"
    document.save(folder / "device.docx")
    assert "风扇" in str(ingest.extract(folder / "device.docx")) and "样例" in str(ingest.extract(folder / "device.docx"))
    (folder / "sample.md").write_text("# 标题\n\n测试段落", encoding="utf-8")
    (folder / "sample.txt").write_text("测试文本", encoding="utf-8")
    assert ingest.extract(folder / "sample.md")[0][0] == "标题"
    assert "测试文本" in str(ingest.extract(folder / "sample.txt"))
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
    writer = PdfWriter(); page = writer.add_blank_page(width=200, height=200)
    font = DictionaryObject({NameObject("/Type"):NameObject("/Font"), NameObject("/Subtype"):NameObject("/Type1"),NameObject("/BaseFont"):NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"):DictionaryObject({NameObject("/F1"):writer._add_object(font)})})
    stream = DecodedStreamObject(); stream.set_data(b"BT /F1 12 Tf 10 100 Td (Cooling manual) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    writer.add_blank_page(width=200,height=200)
    writer.write(folder / "manual.pdf")
    sections = ingest.extract(folder / "manual.pdf")
    assert "Cooling manual" in sections[0][1] and sections[0][2] == 1
    assert sections[1][1] == "" and not list(ingest.chunks([sections[1]], Tokenizer()))

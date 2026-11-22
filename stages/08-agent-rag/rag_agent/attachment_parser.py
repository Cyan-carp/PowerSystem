"""Bounded extraction in a throwaway process. Returns plain text with local locators."""
import csv
import io
import json
import posixpath
import re
import sys
import zipfile
from pathlib import PurePosixPath
from xml.etree import ElementTree as ET


MAX_UPLOAD = 5 << 20
MAX_EXPANDED = 32 << 20
MAX_ROWS = 12_000
MAX_TEXT = 500_000
MAX_CHUNKS = 1_800
FORMATS = {'.pdf', '.docx', '.xlsx', '.csv', '.txt', '.log'}
CONTROL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')


class AttachmentError(ValueError):
    pass


def parts(archive):
    entries = archive.infolist()
    if len(entries) > 2500 or sum(entry.file_size for entry in entries) > MAX_EXPANDED:
        raise AttachmentError('文件解压后超过处理上限')
    names = {entry.filename for entry in entries}
    if len(names) != len(entries):
        raise AttachmentError('文件包含重复的文档部件')
    return names


def xml(archive, name):
    raw = archive.read(name)
    if b'\x00' in raw or re.search(br'<!\s*(?:DOCTYPE|ENTITY)\b', raw, re.I):
        raise AttachmentError('文件包含不支持的 XML 内容')
    return ET.fromstring(raw)


def word_parts(archive):
    body = xml(archive, 'word/document.xml')
    for number, paragraph in enumerate(body.iterfind('.//{*}p'), 1):
        yield f'第 {number} 段', ''.join(node.text or '' for node in paragraph.iterfind('.//{*}t'))


def worksheet_parts(archive, names):
    shared = []
    if 'xl/sharedStrings.xml' in names:
        shared = [''.join(node.text or '' for node in item.iterfind('.//{*}t'))
                  for item in xml(archive, 'xl/sharedStrings.xml').findall('{*}si')]
    relationships = {}
    for item in xml(archive, 'xl/_rels/workbook.xml.rels'):
        if item.get('TargetMode') == 'External':
            continue
        target = item.get('Target', '')
        path = posixpath.normpath(target.lstrip('/') if target.startswith('/') else 'xl/' + target)
        if path.startswith('xl/worksheets/') and path in names:
            relationships[item.get('Id')] = path
    for sheet in xml(archive, 'xl/workbook.xml').iterfind('.//{*}sheet'):
        relationship_id = next((value for key, value in sheet.attrib.items() if key.endswith('}id')), None)
        path = relationships.get(relationship_id)
        if not path:
            continue
        name = sheet.get('name', '工作表')
        for number, row in enumerate(xml(archive, path).iterfind('.//{*}row'), 1):
            cells = []
            for cell in row.findall('{*}c'):
                value = cell.findtext('{*}v', '')
                if cell.get('t') == 's' and value:
                    index = int(value)
                    if index < 0 or index >= len(shared):
                        raise AttachmentError('表格字符串索引无效')
                    value = shared[index]
                if cell.get('t') == 'inlineStr':
                    value = ''.join(node.text or '' for node in cell.iterfind('.//{*}t'))
                if value:
                    cells.append(f"{cell.get('r', '')} {value}".strip())
            yield f'{name} · 第 {row.get("r", number)} 行', ' | '.join(cells)


def text_parts(data, suffix):
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError:
        text = data.decode('gb18030')
    if CONTROL.search(text):
        raise AttachmentError('请选择文本日志或表格文件')
    if suffix != '.csv':
        lines = text.splitlines()
        for start in range(0, len(lines), 4):
            end = min(start + 4, len(lines))
            yield f'第 {start + 1}–{end} 行', '\n'.join(lines[start:end])
        return
    reader = csv.reader(io.StringIO(text, newline=''), strict=True)
    headings = next(reader, None)
    if headings is None:
        return
    yield '第 1 行', ' | '.join(headings)
    previous_line = reader.line_num
    for row in reader:
        start, end = previous_line + 1, reader.line_num
        previous_line = end
        location = str(start) if start == end else f'{start}–{end}'
        yield f'第 {location} 行', ' | '.join(
            f'{headings[index]} {value}'.strip() if index < len(headings) else value for index, value in enumerate(row))


def pdf_parts(data):
    from pypdf import PdfReader

    pdf = PdfReader(io.BytesIO(data), strict=False)
    if pdf.is_encrypted:
        raise AttachmentError('暂不支持加密 PDF')
    if len(pdf.pages) > 120:
        raise AttachmentError('PDF 超过 120 页处理上限')
    for index, page in enumerate(pdf.pages, 1):
        yield f'第 {index} 页', page.extract_text() or ''


def collect(sections):
    excerpts, total = [], 0
    for number, (location, value) in enumerate(sections, 1):
        value = CONTROL.sub('', value).strip()
        total += len(value)
        if number > MAX_ROWS or total > MAX_TEXT:
            raise AttachmentError('可检索内容超过首版处理上限，请拆分文件')
        for start in range(0, len(value), 650):
            excerpts.append({'location': CONTROL.sub('', location)[:220], 'text': value[start:start + 720]})
        if len(excerpts) > MAX_CHUNKS:
            raise AttachmentError('文件包含过多可检索片段，请拆分文件')
    if not excerpts:
        raise AttachmentError('没有可检索的文本。扫描件请先转成带文字层的文档')
    return excerpts


def parse(data, suffix):
    if not data or len(data) > MAX_UPLOAD or suffix not in FORMATS:
        raise AttachmentError('文件为空、过大或格式不受支持')
    try:
        if suffix == '.pdf':
            return collect(pdf_parts(data))
        if suffix in ('.docx', '.xlsx'):
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                names = parts(archive)
                return collect(word_parts(archive) if suffix == '.docx' else worksheet_parts(archive, names))
        return collect(text_parts(data, suffix))
    except AttachmentError:
        raise
    except Exception as error:
        raise AttachmentError('文件无法解析，可能损坏、加密或与扩展名不符') from error


def file_suffix(name):
    if not name or len(name) > 180 or '/' in name or '\\' in name or CONTROL.search(name):
        raise AttachmentError('文件名不正确')
    suffix = PurePosixPath(name).suffix.lower()
    if suffix not in FORMATS:
        raise AttachmentError('支持 PDF、DOCX、XLSX、CSV、TXT 和 LOG')
    return suffix


def main():
    if sys.platform != 'win32':
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (512 << 20, 512 << 20))
        resource.setrlimit(resource.RLIMIT_CPU, (8, 9))
    try:
        result = {'excerpts': parse(sys.stdin.buffer.read(MAX_UPLOAD + 1), sys.argv[1])}
    except AttachmentError as error:
        result = {'error': str(error)}
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode('utf-8'))


if __name__ == '__main__':
    main()

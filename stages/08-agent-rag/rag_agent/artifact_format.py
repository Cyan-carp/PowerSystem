"""S05: turn backend CSV bytes into an XLSX artifact without recomputing anything.

Contract (03-架构与接口契约, S05 artifact section):

* 输入是后端报表导出的真实 CSV 字节（utf-8-sig），本模块只做格式转换。
* 字段数、行序、单元格取值与 CSV 完全一致：不重算、不补行、不裁剪、不排序、不填充缺失值。
* 所有单元格一律写成 inlineStr 文本，因此 `=SUM(A1)`、`+1`、`-1`、`@x` 只是普通文本，
  永远不会变成 Excel 公式；CSV 里带前导单引号的 `'=1+1` 原样保留。
* 内容按纯文本写入，HTML 片段不会被解释或执行。
* Excel 不接受的控制字符直接失败（ValueError），不做静默替换或删除。
* 上限：总行数（含表头）200001、列数 256、输入与生成文件各 32MiB、单单元格 32767 字符；
  任一超限都明确失败，不产出会被 Excel 判为损坏或静默截断的文件。

实现只依赖标准库（zipfile + xml 字符串拼装）：仓库 requirements 中没有 openpyxl，
这里也不引入新依赖。
"""
import csv
import io
import re
import zipfile

MAX_ROWS = 200001          # 含表头；与遥测导出 200000 行 + 表头一致
MAX_COLUMNS = 256
MAX_BYTES = 32 * 1024 * 1024
MAX_CELL_CHARS = 32767     # Excel 单元格字符硬上限；超过后 Excel 直接拒绝打开文件

# csv 模块默认单字段上限 128KiB，比 MAX_CELL_CHARS 更早触发且报错含糊；
# 输入字节本身已限 32MiB，这里把字段上限放宽到同一预算，让超长单元格由本模块明确报错。
csv.field_size_limit(MAX_BYTES)

_SHEET_NAME = 'Sheet1'
_XML_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
_NS_MAIN = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
_NS_REL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
_NS_PKG_REL = 'http://schemas.openxmlformats.org/package/2006/relationships'

# XML 1.0 不允许的 C0 控制符（保留 \t \n \r），以及 Excel 会拒收的 C1 控制符与 U+FFFE/U+FFFF。
_ILLEGAL_PATTERN = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ufffe\uffff]')

_CONTENT_TYPES = (
    _XML_HEAD
    + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    '<Override PartName="/xl/worksheets/sheet1.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
    '<Override PartName="/xl/styles.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
    '</Types>'
)

_ROOT_RELS = (
    _XML_HEAD
    + '<Relationships xmlns="' + _NS_PKG_REL + '">'
    '<Relationship Id="rId1" Type="' + _NS_REL + '/officeDocument" Target="xl/workbook.xml"/>'
    '</Relationships>'
)

_WORKBOOK = (
    _XML_HEAD
    + '<workbook xmlns="' + _NS_MAIN + '" xmlns:r="' + _NS_REL + '">'
    '<sheets><sheet name="' + _SHEET_NAME + '" sheetId="1" r:id="rId1"/></sheets>'
    '</workbook>'
)

_WORKBOOK_RELS = (
    _XML_HEAD
    + '<Relationships xmlns="' + _NS_PKG_REL + '">'
    '<Relationship Id="rId1" Type="' + _NS_REL + '/worksheet" Target="worksheets/sheet1.xml"/>'
    '<Relationship Id="rId2" Type="' + _NS_REL + '/styles" Target="styles.xml"/>'
    '</Relationships>'
)

_STYLES = (
    _XML_HEAD
    + '<styleSheet xmlns="' + _NS_MAIN + '">'
    '<fonts count="1"><font><sz val="11"/><color rgb="FF000000"/><name val="Calibri"/><family val="2"/></font></fonts>'
    '<fills count="2"><fill><patternFill patternType="none"/></fill>'
    '<fill><patternFill patternType="gray125"/></fill></fills>'
    '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>'
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    '</styleSheet>'
)


def _column_name(index):
    """1 -> A, 26 -> Z, 27 -> AA, 256 -> IV."""
    name = ''
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


_COLUMN_NAMES = tuple(_column_name(i) for i in range(1, MAX_COLUMNS + 1))


def _escape(text):
    """XML 文本转义；\\r 用字符引用保留，否则解析时会被规范化为 \\n。"""
    return (text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            .replace('\r', '&#13;'))


def _check_cells(rows):
    for row_number, row in enumerate(rows, 1):
        for column_number, value in enumerate(row, 1):
            illegal = _ILLEGAL_PATTERN.search(value)
            if illegal:
                raise ValueError(
                    'CSV 第%d行第%d列包含 Excel 不接受的控制字符 U+%04X，'
                    '拒绝生成 XLSX（不做静默替换）'
                    % (row_number, column_number, ord(illegal.group())))
            if len(value) > MAX_CELL_CHARS:
                raise ValueError(
                    'CSV 第%d行第%d列有 %d 个字符，超过 Excel 单元格 %d 字符上限，'
                    '拒绝生成打不开的 XLSX' % (row_number, column_number, len(value), MAX_CELL_CHARS))


def _parse(csv_bytes):
    if not isinstance(csv_bytes, (bytes, bytearray, memoryview)):
        raise TypeError('render_csv_xlsx 只接受 CSV 原始字节')
    payload = bytes(csv_bytes)
    if len(payload) > MAX_BYTES:
        raise ValueError('CSV 输入 %d 字节，超过 %d 字节上限' % (len(payload), MAX_BYTES))
    try:
        text = payload.decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise ValueError('CSV 不是合法的 UTF-8/utf-8-sig 编码：%s' % exc) from exc
    rows = []
    try:
        for row in csv.reader(io.StringIO(text, newline=''), strict=True):
            if len(row) > MAX_COLUMNS:
                raise ValueError('CSV 第%d行有 %d 列，超过 %d 列上限'
                                 % (len(rows) + 1, len(row), MAX_COLUMNS))
            if len(rows) >= MAX_ROWS:
                raise ValueError('CSV 行数超过 %d 行上限' % MAX_ROWS)
            rows.append(row)
    except csv.Error as exc:
        raise ValueError('CSV 格式错误（第%d行附近）：%s' % (len(rows) + 1, exc)) from exc
    if not rows or not any(cell.strip() for row in rows for cell in row):
        raise ValueError('CSV 为空，没有可交付的表头或数据行')
    _check_cells(rows)
    return rows


def _sheet_xml(rows):
    width = max(len(row) for row in rows)
    parts = [
        _XML_HEAD,
        '<worksheet xmlns="' + _NS_MAIN + '" xmlns:r="' + _NS_REL + '">',
        '<dimension ref="A1:%s%d"/>' % (_COLUMN_NAMES[width - 1], len(rows)),
        '<sheetViews><sheetView workbookViewId="0"/></sheetViews>',
        '<sheetFormatPr defaultRowHeight="15"/>',
        '<sheetData>',
    ]
    estimated = sum(len(x.encode('utf-8')) for x in parts)
    package_overhead = sum(len(x.encode('utf-8')) for x in (_CONTENT_TYPES, _ROOT_RELS, _WORKBOOK, _WORKBOOK_RELS, _STYLES)) + len('</sheetData></worksheet>')
    for row_number, row in enumerate(rows, 1):
        row_tag = '<row r="%d">' % row_number
        parts.append(row_tag)
        estimated += len(row_tag) + len('</row>')
        for column_number, value in enumerate(row, 1):
            cell = '<c r="%s%d" t="inlineStr"><is><t xml:space="preserve">%s</t></is></c>' % (_COLUMN_NAMES[column_number - 1], row_number, _escape(value))
            estimated += len(cell.encode('utf-8'))
            if estimated + package_overhead > MAX_BYTES:
                raise ValueError('生成的 XLSX 未压缩内容超过 %d 字节上限' % MAX_BYTES)
            parts.append(cell)
        parts.append('</row>')
    parts.append('</sheetData></worksheet>')
    return ''.join(parts)


def render_csv_xlsx(csv_bytes):
    """把后端 CSV 原始字节转换为可打开的 XLSX 字节；任何不合规输入抛 ValueError。"""
    rows = _parse(csv_bytes)
    sheet = _sheet_xml(rows).encode('utf-8')
    parts = [
        ('[Content_Types].xml', _CONTENT_TYPES.encode('utf-8')),
        ('_rels/.rels', _ROOT_RELS.encode('utf-8')),
        ('xl/workbook.xml', _WORKBOOK.encode('utf-8')),
        ('xl/_rels/workbook.xml.rels', _WORKBOOK_RELS.encode('utf-8')),
        ('xl/styles.xml', _STYLES.encode('utf-8')),
        ('xl/worksheets/sheet1.xml', sheet),
    ]
    if sum(len(body) for _, body in parts) > MAX_BYTES:
        raise ValueError('生成的 XLSX 未压缩内容超过 %d 字节上限' % MAX_BYTES)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, body in parts:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            archive.writestr(info, body)
    xlsx = buffer.getvalue()
    if len(xlsx) > MAX_BYTES:
        raise ValueError('生成的 XLSX %d 字节，超过 %d 字节上限' % (len(xlsx), MAX_BYTES))
    return xlsx

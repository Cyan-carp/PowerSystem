"""S05 产物格式：CSV -> XLSX 的字段/行/值一致性与防护边界。

测试直接打开生成的 XLSX 包（zipfile + XML），按单元格引用还原表格，
因此校验的是真实文件内容，而不是生成函数自报的结果。
"""
import io
import xml.etree.ElementTree as ET
import zipfile

import pytest

from rag_agent.artifact_format import (MAX_BYTES, MAX_CELL_CHARS, MAX_COLUMNS, MAX_ROWS,
                                       render_csv_xlsx)

MAIN = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
NS = {'m': MAIN}


def _parts(blob):
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        assert archive.testzip() is None
        return {name: archive.read(name) for name in archive.namelist()}


def _column_index(reference):
    letters = ''.join(ch for ch in reference if ch.isalpha())
    index = 0
    for ch in letters:
        index = index * 26 + (ord(ch) - 64)
    return index


def _sheet(blob):
    """还原为 {(row, column): (type, value)}，并返回工作簿的 XML 根节点。"""
    parts = _parts(blob)
    root = ET.fromstring(parts['xl/worksheets/sheet1.xml'])
    cells = {}
    for row in root.findall('m:sheetData/m:row', NS):
        row_number = int(row.get('r'))
        for cell in row.findall('m:c', NS):
            reference = cell.get('r')
            assert int(reference[len(''.join(ch for ch in reference if ch.isalpha())):]) == row_number
            text = ''.join(node.text or '' for node in cell.iter('{%s}t' % MAIN))
            cells[(row_number, _column_index(reference))] = (cell.get('t'), text)
    return cells, root, parts


def _values(blob):
    cells, _, _ = _sheet(blob)
    rows = {}
    for (row_number, column_number), (_, value) in cells.items():
        rows.setdefault(row_number, {})[column_number] = value
    return [dict(sorted(rows[key].items())) for key in sorted(rows)]


def _csv(text):
    return text.encode('utf-8-sig')


def test_columns_rows_and_values_round_trip_unchanged():
    source = ('device_id,station,availability,remark\n'
              '11,ST-01,99.87,"正常, 无需处理"\n'
              '12,ST-02,,未采集\n'
              '13,ST-03,100.00,"多行\n备注"\n')
    blob = render_csv_xlsx(_csv(source))
    assert _values(blob) == [
        {1: 'device_id', 2: 'station', 3: 'availability', 4: 'remark'},
        {1: '11', 2: 'ST-01', 3: '99.87', 4: '正常, 无需处理'},
        {1: '12', 2: 'ST-02', 3: '', 4: '未采集'},
        {1: '13', 2: 'ST-03', 3: '100.00', 4: '多行\n备注'},
    ]
    cells, root, _ = _sheet(blob)
    assert {kind for kind, _ in cells.values()} == {'inlineStr'}
    assert root.findall('.//m:f', NS) == []


def test_utf8_bom_is_not_part_of_the_first_header_cell():
    blob = render_csv_xlsx(b'\xef\xbb\xbfdevice_id,kwh\r\n11,280\r\n')
    assert _values(blob) == [{1: 'device_id', 2: 'kwh'}, {1: '11', 2: '280'}]


@pytest.mark.parametrize('value', [
    '=SUM(A1:A2)', '+1+1', '-1', '@SUM(A1)', "'=1+1", '=HYPERLINK("http://x","y")',
])
def test_formula_like_text_stays_text_and_is_never_a_formula(value):
    blob = render_csv_xlsx(_csv('value\n"%s"\n' % value.replace('"', '""')))
    cells, root, _ = _sheet(blob)
    assert cells[(2, 1)] == ('inlineStr', value)
    assert root.findall('.//m:f', NS) == []
    assert b'<f>' not in _parts(blob)['xl/worksheets/sheet1.xml']


def test_leading_quote_formula_keeps_the_quote_character():
    blob = render_csv_xlsx(_csv("value\n'=1+1\n"))
    assert _values(blob)[1] == {1: "'=1+1"}


def test_html_and_xml_markup_are_written_as_plain_text():
    payload = '<script>alert(1)</script> & <b>x</b>'
    blob = render_csv_xlsx(_csv('remark\n"%s"\n' % payload))
    sheet = _parts(blob)['xl/worksheets/sheet1.xml']
    assert b'<script' not in sheet and b'&lt;script&gt;' in sheet
    assert _values(blob)[1] == {1: payload}


def test_unicode_whitespace_and_crlf_are_preserved_exactly():
    payload = '  光伏 & 逆变器\t\n第二行\r\n第三行 '
    blob = render_csv_xlsx(_csv('text\n"%s"\n' % payload))
    assert _values(blob)[1] == {1: payload}


def test_ragged_rows_are_not_padded_with_invented_cells():
    blob = render_csv_xlsx(_csv('a,b,c\n1\n2,,3\n'))
    cells, root, _ = _sheet(blob)
    assert cells[(2, 1)] == ('inlineStr', '1')
    assert (2, 2) not in cells and (2, 3) not in cells
    assert cells[(3, 2)] == ('inlineStr', '')
    assert root.find('m:dimension', NS).get('ref') == 'A1:C3'


def test_package_structure_and_relationships_are_complete():
    parts = _parts(render_csv_xlsx(_csv('a,b\n1,2\n')))
    assert list(parts)[0] == '[Content_Types].xml'
    assert set(parts) == {'[Content_Types].xml', '_rels/.rels', 'xl/workbook.xml',
                          'xl/_rels/workbook.xml.rels', 'xl/styles.xml',
                          'xl/worksheets/sheet1.xml'}
    assert 'xl/sharedStrings.xml' not in parts
    for name, body in parts.items():
        ET.fromstring(body)  # 每个部件都必须是良构 XML
    types = ET.fromstring(parts['[Content_Types].xml'])
    overrides = {node.get('PartName') for node in types
                 if node.tag.endswith('Override')}
    assert overrides == {'/xl/workbook.xml', '/xl/worksheets/sheet1.xml', '/xl/styles.xml'}
    workbook = ET.fromstring(parts['xl/workbook.xml'])
    sheet = workbook.find('m:sheets/m:sheet', NS)
    assert sheet.get('name') == 'Sheet1' and sheet.get('sheetId') == '1'
    rels = ET.fromstring(parts['xl/_rels/workbook.xml.rels'])
    targets = {node.get('Target') for node in rels}
    assert targets == {'worksheets/sheet1.xml', 'styles.xml'}


def test_same_input_produces_identical_bytes():
    source = _csv('a,b\n1,2\n')
    assert render_csv_xlsx(source) == render_csv_xlsx(source)


@pytest.mark.parametrize('payload', [b'', b'\xef\xbb\xbf', b'\n\n', b' \n\t\n'])
def test_empty_csv_fails_with_value_error(payload):
    with pytest.raises(ValueError):
        render_csv_xlsx(payload)


@pytest.mark.parametrize('payload', [
    b'\xff\xfe\x00a',                 # 非 UTF-8
    b'a,b\n1,"2\n3,4\n',              # 引号未闭合
    b'a,b\n1,2\n"x"y,3\n',            # 引号后紧跟内容
])
def test_malformed_csv_fails_with_value_error(payload):
    with pytest.raises(ValueError):
        render_csv_xlsx(payload)


def test_non_bytes_input_is_rejected():
    with pytest.raises(TypeError):
        render_csv_xlsx('a,b\n1,2\n')


@pytest.mark.parametrize('char', ['\x00', '\x0b', '\x0c', '\x1f', '\x7f', '\x85'])
def test_excel_illegal_control_characters_fail_instead_of_being_rewritten(char):
    payload = ('header\n正常,%s\n' % char).encode('utf-8')
    with pytest.raises(ValueError) as error:
        render_csv_xlsx(payload)
    assert 'U+%04X' % ord(char) in str(error.value)
    assert '第2行第2列' in str(error.value)


def test_column_limit_is_256():
    header = ','.join('c%d' % i for i in range(MAX_COLUMNS))
    blob = render_csv_xlsx(_csv(header + '\n'))
    _, root, _ = _sheet(blob)
    assert root.find('m:dimension', NS).get('ref') == 'A1:IV1'
    with pytest.raises(ValueError):
        render_csv_xlsx(_csv(header + ',overflow\n'))


def test_row_limit_counts_the_header_row():
    blob = render_csv_xlsx(_csv('id,value\n' + '1,x\n' * (MAX_ROWS - 1)))
    cells, root, _ = _sheet(blob)
    assert root.find('m:dimension', NS).get('ref') == 'A1:B%d' % MAX_ROWS
    assert cells[(MAX_ROWS, 1)] == ('inlineStr', '1')
    with pytest.raises(ValueError):
        render_csv_xlsx(_csv('id,value\n' + '1,x\n' * MAX_ROWS))


def test_input_byte_limit_is_enforced_before_parsing():
    with pytest.raises(ValueError) as error:
        render_csv_xlsx(b'a' * (MAX_BYTES + 1))
    assert str(MAX_BYTES) in str(error.value)


def test_cell_at_the_excel_character_limit_is_accepted():
    # 32767 是 Excel 单元格硬上限；该长度也远高于 csv 默认 128KiB 字段上限的常见误伤区。
    longest = 'x' * MAX_CELL_CHARS
    blob = render_csv_xlsx(_csv('remark\n"%s"\n' % longest))
    assert _values(blob)[1] == {1: longest}


def test_cell_over_the_excel_character_limit_fails_instead_of_producing_a_broken_file():
    # 真实 Excel 打开超过 32767 字符的单元格会直接报错，因此必须在生成前失败。
    with pytest.raises(ValueError) as error:
        render_csv_xlsx(_csv('remark\n"%s"\n' % ('x' * (MAX_CELL_CHARS + 1))))
    assert str(MAX_CELL_CHARS) in str(error.value)
    assert '第2行第1列' in str(error.value)


def test_output_budget_rejects_content_that_expands_past_the_limit():
    # 每个 '<' 转义成 '&lt;'（4 字节）；300 行 × 30000 字符 = 9MiB 输入，膨胀成 36MiB 未压缩内容。
    row = b'<' * 30000 + b'\n'
    payload = b'value\n' + row * 300
    assert len(payload) < MAX_BYTES
    with pytest.raises(ValueError) as error:
        render_csv_xlsx(payload)
    assert str(MAX_BYTES) in str(error.value)

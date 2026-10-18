"""Markdown 录题解析器（docs/OJ对接设计.md 第十章）

纯函数零 DB：把单文件 markdown 解析为题目字段。
约定代码块标签：input-sample/output-sample（公开样例）、
input/output（隐藏测试点，信息串可带 points=N）、spj-python（特殊判定）。

安全（14.7 测试数据保密链）：statement_md 由「剔除全部受保护块 +
标题行」后的正文重建，不允许原始文件原文落库再下发。
"""
import ast
import re

from rest_framework.exceptions import ValidationError

# 受保护代码块标签集合（这些块的内容进入 tests/samples/spj_code，永不下发）
PROTECTED_TAGS = frozenset([
    'input-sample', 'output-sample', 'input', 'output', 'spj-python',
])

# 数据围栏标签：内容按「文件语义」保留末尾换行。
# md 里数据围栏的最后一个换行属于文件内容的一部分（与程序 stdout 的
# 行尾逐字节一致），exact 比对时若丢失会导致「输出正确却 WA」；
# spj-python 是代码块而非数据文件，不在其列。
DATA_TAGS = frozenset(['input-sample', 'output-sample', 'input', 'output'])

MAX_TEST_GROUPS = 20             # 隐藏测试点上限（第十章）
MAX_GROUP_BYTES = 256 * 1024     # 单组 input+output 上限 256KB（第十章）
TOTAL_POINTS = 100               # 未标注分值测试点平分的总分（第十章）
MAX_MARKDOWN_BYTES = 6 * 1024 * 1024  # md 文件总上限（20 组 × 256KB 留余量）

# 围栏代码块起始行：```标签 信息串（标签可为空 = 普通代码块，保留进正文）
FENCE_START_RE = re.compile(r'^```([^\s`]*)([ \t]*.*)$')
FENCE_END_RE = re.compile(r'^```\s*$')
# 一级标题行（title 字段来源，正文中仅第一个生效）
TITLE_RE = re.compile(r'^#\s+(.+?)\s*$')


def extractBlocks(mdText):
    """逐行状态机：提取受保护代码块，其余正文（含普通代码块）原样保留

    返回 (bodyLines, protectedBlocks)；protectedBlocks 为
    [(tag, info, content)]，bodyLines 不含任何受保护块。
    """
    lines = mdText.splitlines()
    bodyLines = []
    protectedBlocks = []
    index = 0
    while index < len(lines):
        match = FENCE_START_RE.match(lines[index])
        if match is None:
            bodyLines.append(lines[index])
            index += 1
            continue
        blockStart = index
        tag, info = match.group(1), match.group(2).strip()
        contentLines = []
        index += 1
        while index < len(lines) and FENCE_END_RE.match(lines[index]) is None:
            contentLines.append(lines[index])
            index += 1
        if index >= len(lines):
            raise ValidationError('代码块未闭合：缺少结尾 ```')
        if tag in PROTECTED_TAGS:
            content = '\n'.join(contentLines)
            # 数据围栏补回内容末尾换行：splitlines 丢掉了最后一个换行符，
            # 而它属于文件内容（见 DATA_TAGS 注释）；空块不补
            if contentLines and tag in DATA_TAGS:
                content += '\n'
            protectedBlocks.append((tag, info, content))
        else:
            # 普通代码块（如题面里的示例代码）原样放回正文
            bodyLines.extend(lines[blockStart:index + 1])
        index += 1
    return bodyLines, protectedBlocks


def parsePoints(info):
    """信息串解析 points=N（1 ≤ N ≤ 100）；未标注返回 None（由调用方均分）"""
    if not info:
        return None
    for token in info.split():
        if token.startswith('points='):
            raw = token[len('points='):]
            if not raw.isdigit() or not (1 <= int(raw) <= TOTAL_POINTS):
                raise ValidationError(
                    f'points 必须是 1~{TOTAL_POINTS} 的整数')
            return int(raw)
    return None


def pairBlocks(protectedBlocks):
    """相邻配对：input-sample → output-sample、input → output；
    spj-python 独立（至多一个）；不配对/顺序错误 → 400 拒绝整题

    返回 (samples, tests, spjCode)。
    """
    pairKeys = {'input-sample': 'output-sample', 'input': 'output'}
    samples = []
    tests = []
    spjCode = None
    index = 0
    while index < len(protectedBlocks):
        tag, info, content = protectedBlocks[index]
        if tag == 'spj-python':
            if spjCode is not None:
                raise ValidationError('spj-python 代码块只能出现一次')
            spjCode = content
            index += 1
            continue
        partner = pairKeys.get(tag)
        if partner is None:
            raise ValidationError(
                f'{tag} 代码块必须紧跟在其配对的输入块之后')
        if index + 1 >= len(protectedBlocks) \
                or protectedBlocks[index + 1][0] != partner:
            raise ValidationError(f'{tag} 代码块必须紧跟配对的 {partner} 代码块')
        output = protectedBlocks[index + 1][2]
        if tag == 'input':
            tests.append({'input': content, 'output': output,
                          'points': parsePoints(info)})
        else:
            samples.append({'input': content, 'output': output})
        index += 2
    return samples, tests, spjCode


def assignPoints(tests):
    """分值分配（第十章）：标注点用标注值；未标注点平分 100 分
    （除不尽时余数补给前几个未标注点，保证总和恰为 100）"""
    unmarked = [i for i, test in enumerate(tests) if test['points'] is None]
    if not unmarked:
        return
    share = TOTAL_POINTS // len(unmarked)
    remainder = TOTAL_POINTS % len(unmarked)
    for offset, index in enumerate(unmarked):
        tests[index]['points'] = share + (1 if offset < remainder else 0)


def parseProblemMarkdown(mdText):
    """解析录题 markdown → {title, statement_md, samples, tests, spj_code, has_spj}

    校验失败抛 DRF ValidationError（400 拒绝整题，第十章约束）。
    """
    if not mdText or not mdText.strip():
        raise ValidationError('题目内容为空')
    if len(mdText.encode('utf-8')) > MAX_MARKDOWN_BYTES:
        raise ValidationError('题目文件超过 6MB 上限')
    bodyLines, protectedBlocks = extractBlocks(mdText)
    # 标题：第一个一级标题行，正文重建时剔除该行（title 进独立字段）
    title = None
    bodyWithoutTitle = []
    for line in bodyLines:
        match = TITLE_RE.match(line)
        if title is None and match is not None:
            title = match.group(1)
            continue
        bodyWithoutTitle.append(line)
    if title is None:
        raise ValidationError('缺少一级标题（# 题目名称）')
    if len(title) > 100:
        raise ValidationError('标题不能超过 100 字符')
    samples, tests, spjCode = pairBlocks(protectedBlocks)
    # 隐藏测试点约束（第十章）
    if not tests:
        raise ValidationError('至少需要 1 组测试点（input/output 代码块）')
    if len(tests) > MAX_TEST_GROUPS:
        raise ValidationError(f'测试点最多 {MAX_TEST_GROUPS} 组')
    for test in tests:
        if len((test['input'] + test['output']).encode('utf-8')) \
                > MAX_GROUP_BYTES:
            raise ValidationError('单个测试点数据不能超过 256KB')
    if len(samples) > MAX_TEST_GROUPS:
        raise ValidationError(f'公开样例最多 {MAX_TEST_GROUPS} 组')
    assignPoints(tests)
    # spj 入库前语法校验（第十章）
    if spjCode is not None:
        try:
            ast.parse(spjCode)
        except SyntaxError:
            raise ValidationError('spj-python 代码语法错误')
    return {
        'title': title,
        'statement_md': '\n'.join(bodyWithoutTitle).strip(),
        'samples': samples,
        'tests': tests,
        'spj_code': spjCode or '',
        'has_spj': spjCode is not None,
    }

"""题库 ZIP 批量导入服务（管理员公共题库、教师本人题库）

导入包结构：
- manifest.xlsx / manifest.csv：清单（file 必填；code/tier/languages/
  compareStrategy 可选）
- *.md：每题一个文件，沿用 problem_markdown 的录题格式

设计要点：
- 全程在内存中读取 ZIP，不落盘；逐项校验路径穿越、加密、大小上限
- 复用 parseAndApply 解析 markdown 与元数据，和单题录题走同一套校验
- source_code 在公共题库/教师本人题库内唯一；source_hash 识别内容变化
  → 未变化跳过；变化且 overwrite=True 更新并 revision+1；否则报错
- 同步执行，AsyncTask 仅作为导入历史记录（约 1000 题秒级完成）

清单列约定（表头大小写/下划线不敏感）：
  file | code | tier | languages | compareStrategy
  示例：p0001.md | C0001 | basic | c,python | exact
"""
import csv
import hashlib
import io
import logging
import posixpath
import re
import zipfile

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone
from openpyxl import load_workbook
from rest_framework.exceptions import PermissionDenied, ValidationError

from tasks.models import AsyncTask
from teaching.models import Problem
from teaching.problem_markdown import MAX_MARKDOWN_BYTES
from teaching.problem_service import parseAndApply, validateMetadata
from users.permissions import hasPerm

logger = logging.getLogger('codemind.problem')

# 单批导入上限：约 1000 题留出余量，防止误传超大文件拖垮服务
MAX_IMPORT_PROBLEMS = 2000
MAX_IMPORT_ENTRIES = 2200
MAX_ZIP_BYTES = 64 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
MAX_MANIFEST_BYTES = 10 * 1024 * 1024
MANIFEST_NAMES = frozenset(['manifest.xlsx', 'manifest.csv'])

# 中文/英文别名：管理员在清单里写中文也能识别
TIER_ALIASES = {
    'beginner': 'beginner', '入门': 'beginner',
    'basic': 'basic', '基础': 'basic',
    'advanced': 'advanced', '进阶': 'advanced',
}
STRATEGY_ALIASES = {
    'exact': 'exact', '精确比对': 'exact', '精确': 'exact',
    'trailing': 'trailing', '忽略行尾空白': 'trailing', '忽略行尾': 'trailing',
    'loose': 'loose', '忽略全部空白': 'loose', '忽略空白': 'loose',
}
LANGUAGE_ALIASES = {'c': 'c', 'python': 'python', 'py': 'python'}

# 题目内容字段：重复导入时只要任一字段变化或源文件哈希变化，就视为内容变化
CONTENT_FIELDS = [
    'title', 'statement_md', 'samples', 'tests', 'languages', 'tier',
    'compare_strategy', 'has_spj', 'spj_code',
]

# 覆盖导入时从新题目复制的内容字段
APPLY_FIELDS = CONTENT_FIELDS + ['source_hash']

# bulk_update 需要写回的字段（含 revision 与更新时间）
UPDATE_FIELDS = APPLY_FIELDS + ['revision', 'updated_at']


def formatErrorDetail(exc):
    """把 DRF ValidationError 的 detail 转成单行中文原因（写进导入报告）"""
    detail = getattr(exc, 'detail', exc)
    if isinstance(detail, dict):
        text = '；'.join(f'{key}: {value}' for key, value in detail.items())
    elif isinstance(detail, (list, tuple)):
        text = '；'.join(str(item) for item in detail)
    else:
        text = str(detail)
    return text[:300] or '未知错误'


def normalizeZipPath(rawName):
    """规范化 ZIP 内路径并拒绝绝对路径/路径穿越（不解压，仅做安全校验）"""
    if not isinstance(rawName, str) or not rawName.strip():
        raise ValidationError('ZIP 内存在空文件名')
    name = rawName.replace('\\', '/').strip()
    if '\x00' in name:
        raise ValidationError('文件名包含非法字符')
    if name.startswith('/') or re.match(r'^[A-Za-z]:', name):
        raise ValidationError(f'非法绝对路径：{rawName}')
    if any(part == '..' for part in name.split('/')):
        raise ValidationError(f'非法文件路径：{rawName}')
    normalized = posixpath.normpath(name)
    if normalized in ('.', '..') or normalized.startswith('/'):
        raise ValidationError(f'非法文件路径：{rawName}')
    return normalized


def getZipNameCandidates(info):
    """返回 ZIP 条目的候选文件名

    Windows 资源管理器打的 ZIP 可能未置 UTF-8 标志，中文名会被 Python
    按 cp437 解码。这里额外尝试 cp437→GBK 还原一份候选名，避免清单
    里写中文文件名时匹配不上。
    """
    candidates = [info.filename]
    if not (info.flag_bits & 0x800):
        try:
            decoded = info.filename.encode('cp437').decode('gbk')
        except (UnicodeEncodeError, UnicodeDecodeError):
            decoded = None
        if decoded and decoded != info.filename:
            candidates.append(decoded)
    return candidates


def manifestCellToStr(value):
    """清单单元格转字符串：防止 Excel 把纯数字编号变成 1.0"""
    if value is None:
        return ''
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def canonicalHeader(value):
    """表头规范化：忽略大小写、空格、下划线、连字符"""
    return str(value or '').strip().lower().replace('_', '') \
        .replace('-', '').replace(' ', '')


def readManifestRows(rawBytes, fileName):
    """读取 manifest.xlsx / manifest.csv 的全部原始行（含表头）"""
    if fileName.lower().endswith('.xlsx'):
        try:
            workbook = load_workbook(io.BytesIO(rawBytes), read_only=True,
                                     data_only=True)
            try:
                sheet = workbook.active
                return [list(row) for row in sheet.iter_rows(values_only=True)]
            finally:
                workbook.close()
        except Exception as exc:
            raise ValidationError(f'清单文件无法解析：{exc}')
    try:
        text = rawBytes.decode('utf-8-sig')
    except UnicodeDecodeError:
        raise ValidationError('清单 CSV 必须是 UTF-8 编码')
    return [row for row in csv.reader(io.StringIO(text))]


def getManifestCell(row, columnIndex, key):
    """按规范化列名读取单元格（缺列/缺单元格返回空串）"""
    index = columnIndex.get(canonicalHeader(key))
    if index is None or index >= len(row):
        return ''
    return manifestCellToStr(row[index])


def parseManifestRow(row, lineNo, columnIndex, seenFiles, seenCodes,
                     allFiles):
    """解析一行清单；校验失败抛 ValidationError（由调用方汇总为行错误）"""
    filePath = normalizeZipPath(getManifestCell(row, columnIndex, 'file'))
    if not filePath.lower().endswith('.md'):
        raise ValidationError('file 必须指向 .md 文件')
    allFiles.add(filePath.lower())
    code = getManifestCell(row, columnIndex, 'code') or filePath
    if len(code) > 128:
        raise ValidationError('code 不能超过 128 字符')
    if filePath in seenFiles:
        raise ValidationError('清单中 file 重复')
    if code in seenCodes:
        raise ValidationError(f'清单中 code 重复：{code}')
    tierRaw = getManifestCell(row, columnIndex, 'tier')
    tier = TIER_ALIASES.get(tierRaw.lower(), tierRaw) \
        if tierRaw else Problem.TIER_BASIC
    languageRaw = getManifestCell(row, columnIndex, 'languages')
    if languageRaw:
        normalized = languageRaw.replace('，', ',') \
            .replace('、', ',').replace('/', ',').replace(';', ',')
        languages = [
            LANGUAGE_ALIASES.get(item.strip().lower(), item.strip())
            for item in normalized.split(',') if item.strip()
        ]
    else:
        languages = [Problem.LANGUAGE_C, Problem.LANGUAGE_PYTHON]
    strategyRaw = getManifestCell(row, columnIndex, 'compareStrategy')
    strategy = STRATEGY_ALIASES.get(strategyRaw.lower(), strategyRaw) \
        if strategyRaw else Problem.COMPARE_EXACT
    # 复用单题录题的元数据白名单校验
    languages, tier, strategy = validateMetadata({
        'languages': languages,
        'tier': tier,
        'compareStrategy': strategy,
    })
    return {
        'file': filePath,
        'code': code,
        'line': lineNo,
        'meta': {
            'languages': languages,
            'tier': tier,
            'compareStrategy': strategy,
        },
    }


def parseManifest(rawBytes, fileName):
    """解析清单 → (records, errors, allFiles)

    records: [{file, code, meta, line}]
    errors:  [{file, reason}]（单行元数据错误不终止整批导入）
    allFiles: 清单中出现过的全部合法文件路径（用于识别未登记文件）
    结构性错误（缺 file 列）直接抛 ValidationError。
    """
    rows = readManifestRows(rawBytes, fileName)
    if not rows:
        raise ValidationError('清单文件为空')
    headers = [canonicalHeader(cell) for cell in rows[0]]
    if 'file' not in headers:
        raise ValidationError('清单缺少 file 列')
    nonEmptyHeaders = [header for header in headers if header]
    if len(set(nonEmptyHeaders)) != len(nonEmptyHeaders):
        raise ValidationError('清单存在重复列名')
    columnIndex = {name: index for index, name in enumerate(headers) if name}

    records = []
    errors = []
    allFiles = set()
    seenFiles = set()
    seenCodes = set()
    dataRowCount = 0
    for lineNo, row in enumerate(rows[1:], start=2):
        if not any(manifestCellToStr(cell) for cell in row):
            continue  # 跳过空行
        dataRowCount += 1
        if dataRowCount > MAX_IMPORT_PROBLEMS:
            raise ValidationError(f'单批最多导入 {MAX_IMPORT_PROBLEMS} 道题')
        rawFile = getManifestCell(row, columnIndex, 'file')
        try:
            record = parseManifestRow(row, lineNo, columnIndex,
                                      seenFiles, seenCodes, allFiles)
        except ValidationError as exc:
            errors.append({
                'file': rawFile or f'第 {lineNo} 行',
                'reason': formatErrorDetail(exc),
            })
            continue
        seenFiles.add(record['file'])
        seenCodes.add(record['code'])
        records.append(record)
    return records, errors, allFiles


def readZipEntry(zipFile, info, maxBytes):
    """读取单个 ZIP 条目，超过上限立即拒绝（防 zip bomb）"""
    if info.flag_bits & 0x1:
        raise ValidationError('文件已加密，请先解除密码')
    try:
        with zipFile.open(info) as fileObj:
            data = fileObj.read(maxBytes + 1)
    except RuntimeError:
        raise ValidationError('文件已加密，请先解除密码')
    except (zipfile.BadZipFile, NotImplementedError, OSError) as exc:
        raise ValidationError(f'ZIP 条目读取失败：{exc}')
    if len(data) > maxBytes:
        raise ValidationError('文件超过大小上限')
    return data


def collectZipEntries(zipFile):
    """收集 ZIP 内可导入的文件并做全局安全校验

    返回 (entries, manifestInfo, manifestName)：
    entries 为 [{info, names}]，names 是去重后的规范化候选路径。
    """
    entries = []
    manifestMatches = []
    totalBytes = 0
    for info in zipFile.infolist():
        if info.is_dir():
            continue
        if info.flag_bits & 0x1:
            raise ValidationError('ZIP 内含加密文件，请先解除密码')
        names = []
        for candidate in getZipNameCandidates(info):
            try:
                normalized = normalizeZipPath(candidate)
            except ValidationError:
                # 未使用的恶意路径不应阻断整批，但也不能静默忽略；
                # 这里直接拒绝，避免管理员误以为导入成功
                raise
            if normalized not in names:
                names.append(normalized)
        lowered = {name.lower() for name in names}
        # 忽略 macOS/Windows 常见垃圾文件（必须在识别 manifest 之前跳过）
        if any(name.startswith('__MACOSX/') or
               posixpath.basename(name).lower() in ('.ds_store', 'thumbs.db')
               for name in names):
            continue
        basename = posixpath.basename(names[0]).lower()
        if basename in MANIFEST_NAMES:
            manifestMatches.append((info, names[0]))
            continue
        # 只导入 markdown；README、图片等附属文件直接忽略
        if not any(name.lower().endswith('.md') for name in names):
            continue
        totalBytes += info.file_size
        entries.append({'info': info, 'names': names, 'lowered': lowered})
    if totalBytes > MAX_UNCOMPRESSED_BYTES:
        raise ValidationError('ZIP 解压后总大小超过 256MB 上限')
    if len(entries) > MAX_IMPORT_ENTRIES:
        raise ValidationError(f'ZIP 内文件数超过 {MAX_IMPORT_ENTRIES} 个上限')
    if len(manifestMatches) != 1:
        raise ValidationError('ZIP 根目录必须包含且只包含一个 manifest.xlsx 或 manifest.csv')
    manifestInfo, manifestName = manifestMatches[0]
    if manifestInfo.file_size > MAX_MANIFEST_BYTES:
        raise ValidationError('清单文件超过 10MB 上限')
    return entries, manifestInfo, manifestName


def failBatch(batch, message):
    """把导入批次标记为失败（保留脱敏原因，管理页可查）"""
    batch.status = AsyncTask.STATUS_FAILED
    batch.error = message[:500]
    batch.finished_at = timezone.now()
    batch.save(update_fields=['status', 'error', 'finished_at'])


def importProblemZip(user, zipFile, overwrite=False, *, isPublic=True):
    """批量导入题库 ZIP → 结果字典

    返回值：
      {total, createdCount, updatedCount, skippedCount, errorCount, errors}
    预期内的文件/题目错误以 errors 逐条返回；整体失败抛 ValidationError。
    """
    if (isPublic and not user.is_superuser) or (
            not isPublic and not hasPerm(user, 'homework:bank')):
        raise PermissionDenied('没有题库导入权限')
    if getattr(zipFile, 'size', 0) > MAX_ZIP_BYTES:
        raise ValidationError('ZIP 文件超过 64MB 上限')
    batch = AsyncTask.objects.create(
        type=AsyncTask.TYPE_IMPORT,
        status=AsyncTask.STATUS_RUNNING,
        created_by=user,
        started_at=timezone.now(),
        result={'scope': 'public' if isPublic else 'personal'},
    )
    try:
        result = runImport(zipFile, user, overwrite, isPublic=isPublic)
    except ValidationError as exc:
        failBatch(batch, formatErrorDetail(exc))
        raise
    except Exception:
        logger.exception('题库批量导入失败')
        failBatch(batch, '导入失败：服务器内部错误')
        raise ValidationError('导入失败：服务器内部错误')
    batch.total = result['total']
    batch.progress = 100
    result['batchId'] = batch.id
    result['scope'] = 'public' if isPublic else 'personal'
    batch.result = result
    batch.status = AsyncTask.STATUS_DONE
    batch.finished_at = timezone.now()
    batch.save(update_fields=['total', 'progress', 'result', 'status',
                              'finished_at'])
    return result


def runImport(zipFile, user, overwrite, *, isPublic=True):
    """执行导入（不含批次状态处理，便于单元测试直接调用）"""
    zipFile.seek(0)
    try:
        with zipfile.ZipFile(zipFile) as archive:
            entries, manifestInfo, manifestName = collectZipEntries(archive)
            manifestRaw = readZipEntry(archive, manifestInfo,
                                       MAX_MANIFEST_BYTES)
            records, errors, manifestFiles = parseManifest(manifestRaw,
                                                           manifestName)
            # 清单行数 = 合法记录 + 清单层错误（未通过文件/元数据校验的行）
            totalRows = len(records) + len(errors)
            return processRecords(archive, entries, records, errors,
                                  manifestFiles, totalRows, user, overwrite,
                                  isPublic=isPublic)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError):
        raise ValidationError('不是有效的 ZIP 文件')


def buildProblemFromEntry(archive, info, record, user, *, isPublic=True):
    """读取并解析单个 md → 未落库的 Problem（校验失败抛 ValidationError）"""
    rawBytes = readZipEntry(archive, info, MAX_MARKDOWN_BYTES)
    try:
        markdown = rawBytes.decode('utf-8-sig')
    except UnicodeDecodeError:
        raise ValidationError('markdown 必须是 UTF-8 编码')
    problem = Problem(owner=user, is_public=isPublic, revision=1,
                      source_code=record['code'])
    problem.source_hash = hashlib.sha256(rawBytes).hexdigest()
    parseAndApply(problem, {'markdown': markdown, **record['meta']})
    return problem


def applyExistingProblem(existing, problem, overwrite, now):
    """把新解析结果合并到已存在题目：返回 (动作, 原因)

    动作：skip（未变化）/ update（覆盖）/ error（不可覆盖）
    """
    changed = (
        existing.source_hash != problem.source_hash
        or any(getattr(existing, field) != getattr(problem, field)
               for field in CONTENT_FIELDS)
    )
    if not changed:
        return 'skip', ''
    if not overwrite:
        return 'error', '题目已存在且内容有变化（未勾选覆盖）'
    for field in APPLY_FIELDS:
        setattr(existing, field, getattr(problem, field))
    existing.revision += 1
    existing.updated_at = now
    return 'update', ''


def collectProblemChanges(archive, entryByPath, records, errors, user,
                          overwrite, now, *, isPublic=True):
    """逐题解析与去重：返回 (created, updated, skippedCount)"""
    existingQuery = Problem.objects.select_for_update().filter(is_public=isPublic)
    if not isPublic:
        existingQuery = existingQuery.filter(owner=user)
    existingByCode = {
        problem.source_code: problem
        for problem in existingQuery.filter(
            source_code__in=[record['code'] for record in records])
    }
    created = []
    updated = []
    skippedCount = 0
    for record in records:
        entry = entryByPath.get(record['file'].lower())
        if entry is None:
            errors.append({'file': record['file'],
                           'reason': '清单引用的文件不存在'})
            continue
        try:
            problem = buildProblemFromEntry(archive, entry['info'], record,
                                            user, isPublic=isPublic)
        except ValidationError as exc:
            errors.append({'file': record['file'],
                           'reason': formatErrorDetail(exc)})
            continue
        existing = existingByCode.get(record['code'])
        if existing is None:
            problem.created_at = now
            problem.updated_at = now
            created.append(problem)
            continue
        action, reason = applyExistingProblem(existing, problem, overwrite,
                                              now)
        if action == 'update':
            updated.append(existing)
        elif action == 'skip':
            skippedCount += 1
        else:
            errors.append({'file': record['file'], 'reason': reason})
    return created, updated, skippedCount


def processRecords(archive, entries, records, errors, manifestFiles,
                   totalRows, user, overwrite, *, isPublic=True):
    """建立条目索引 + 未登记检查 + 批量落库"""
    # 建立 ZIP 条目索引：精确路径 + 小写路径（兼容大小写差异）
    entryByPath = {}
    for entry in entries:
        for name in entry['names']:
            key = name.lower()
            if key in entryByPath and entryByPath[key] is not entry:
                raise ValidationError(f'ZIP 内文件重名：{name}')
            entryByPath[key] = entry

    # 清单里出现、但 ZIP 中没有的 md → 逐条报错
    for entry in entries:
        if not entry['lowered'] & manifestFiles:
            errors.append({
                'file': entry['names'][0],
                'reason': '文件未在清单中登记',
            })

    now = timezone.now()
    try:
        with transaction.atomic():
            get_user_model().objects.filter(pk=user.pk).update(id=F('id'))
            created, updated, skippedCount = collectProblemChanges(
                archive, entryByPath, records, errors, user, overwrite, now,
                isPublic=isPublic)
            if created:
                Problem.objects.bulk_create(created, batch_size=200)
            if updated:
                Problem.objects.bulk_update(updated, fields=UPDATE_FIELDS,
                                            batch_size=200)
    except IntegrityError:
        raise ValidationError('题库发生并发修改，请重新导入')
    return {
        'total': totalRows,
        'createdCount': len(created),
        'updatedCount': len(updated),
        'skippedCount': skippedCount,
        'errorCount': len(errors),
        'errors': errors,
    }

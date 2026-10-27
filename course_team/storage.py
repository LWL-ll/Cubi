"""课程组文件保存、限额和确定性文本解析，不调用外部服务。"""
import csv
import io
import json
import logging
from pathlib import Path, PurePosixPath
import uuid
from xml.etree import ElementTree
import zipfile

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from django.http import Http404
from rest_framework.exceptions import ValidationError

logger = logging.getLogger(__name__)
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_TEXT_CHARS = 1_000_000
MAX_DOCUMENT_BYTES = 8 * 1024 * 1024
MAX_DOCX_UNPACKED = 32 * 1024 * 1024
DOCUMENT_SUFFIXES = {'.txt', '.md', '.csv', '.json', '.docx'}
MATERIAL_SUFFIXES = {
    '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
    '.txt', '.md', '.csv', '.json', '.zip', '.c', '.py'}


def privateStorage():
    return FileSystemStorage(location=Path(settings.PRIVATE_MATERIAL_ROOT) / 'course_team')


def readUpload(uploaded, *, document=False):
    if uploaded is None or not hasattr(uploaded, 'read'):
        raise ValidationError({'file': '请上传文件'})
    name = PurePosixPath(uploaded.name.replace('\\', '/')).name
    suffixes = DOCUMENT_SUFFIXES if document else MATERIAL_SUFFIXES
    if (not name or len(name) > 255 or '\x00' in name
            or PurePosixPath(name).suffix.lower() not in suffixes):
        raise ValidationError({'file': '不支持的文件名或文件类型'})
    try:
        name.encode('utf-8')
    except UnicodeEncodeError:
        raise ValidationError({'file': '文件名含无效字符'}) from None
    limit = MAX_DOCUMENT_BYTES if document else MAX_FILE_BYTES
    if not 0 < uploaded.size <= limit:
        raise ValidationError({'file': f'文件不能为空且不能超过 {limit // (1024 * 1024)}MB'})
    content = uploaded.read(limit + 1)
    if not content or len(content) > limit:
        raise ValidationError({'file': '文件为空或超过允许大小'})
    return name, content


def storeFile(originalName, content):
    name = uuid.uuid4().hex + PurePosixPath(originalName).suffix.lower()
    try:
        return privateStorage().save(name, ContentFile(content))
    except OSError:
        deleteFile(name)
        raise ValidationError({'file': '文件保存失败，请稍后重试'}) from None


def deleteFile(name):
    if not name:
        return
    try:
        privateStorage().delete(name)
    except OSError:
        logger.exception('课程组私有文件清理失败：%s', name)


def openFile(obj):
    # 调用者必须先完成对象权限检查；物理路径从不返回客户端。
    if not obj.storage_name:
        raise Http404('文件不存在')
    try:
        return privateStorage().open(obj.storage_name, 'rb')
    except FileNotFoundError:
        raise Http404('文件不存在，请联系课程组重新上传') from None


def _docxText(content):
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        entries = archive.infolist()
        if (len(entries) > 2000
                or sum(entry.file_size for entry in entries) > MAX_DOCX_UNPACKED
                or any(entry.flag_bits & 1 for entry in entries)):
            raise ValueError('DOCX 解压后过大或包含加密内容')
        document = archive.read('word/document.xml')
    # DOCX 只提取标准正文 XML；不执行宏、不解包路径、不处理外链及 OCR。
    documentText = document.decode('utf-8-sig')
    if '<!DOCTYPE' in documentText.upper() or '<!ENTITY' in documentText.upper():
        raise ValueError('DOCX XML 包含不支持的实体声明')
    tree = ElementTree.fromstring(documentText)
    ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
    return '\n'.join(''.join(p.itertext()) for p in tree.findall('.//w:p', ns))


def parseDocument(originalName, content):
    """返回正文；预期解析错误只暴露固定业务信息，不返回库栈或物理路径。"""
    suffix = PurePosixPath(originalName).suffix.lower()
    try:
        if suffix == '.docx':
            text = _docxText(content)
        else:
            text = content.decode('utf-8-sig')
            if suffix == '.json':
                text = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
            elif suffix == '.csv':
                csv.field_size_limit(MAX_TEXT_CHARS)
                rows = csv.reader(io.StringIO(text))
                text = '\n'.join('\t'.join(row) for row in rows)
        if '\x00' in text or not text.strip() or len(text) > MAX_TEXT_CHARS:
            raise ValueError('正文为空、包含无效字符或超过一百万字')
        text.encode('utf-8')
        return text.strip(), ''
    except (UnicodeError, ValueError, TypeError, csv.Error, zipfile.BadZipFile,
            KeyError, ElementTree.ParseError, RecursionError, RuntimeError, OSError):
        return '', '无法解析文档：请使用有效 UTF-8 文本/CSV/JSON 或标准 DOCX，正文须非空且不超过一百万字'

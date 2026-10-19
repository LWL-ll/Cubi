"""题库 ZIP 批量导入测试（服务层 + 管理员 API + /admin/ 页面）

覆盖：
- 清单 xlsx/csv、中文别名、逐题解析
- 重复导入跳过、覆盖导入 revision+1、坏文件逐条报错
- ZIP 路径穿越/缺清单/缺文件等安全与完整性校验
- 权限：仅超级用户；教师/课程组/教务/学生一律 403
- 导入后教师可选题（公共题库可见）
"""
import csv
import io
import zipfile

from django.contrib.auth.models import Group
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from openpyxl import Workbook
from rest_framework.test import APIClient

from tasks.models import AsyncTask
from teaching.homework_service import loadSelectableProblems
from teaching.models import Problem
from users.models import User

PASSWORD = 'Test12345'
IMPORT_URL = '/api/admin/problems/import/'


def buildMd(title='两数之和', body='给定两个整数，输出它们的和。',
            samples=None, tests=None):
    """组装一份最小可用的录题 markdown"""
    lines = [f'# {title}', '', body]
    for sampleInput, sampleOutput in (samples or [('1 2', '3')]):
        lines += ['```input-sample', sampleInput, '```',
                  '```output-sample', sampleOutput, '```']
    for testInput, testOutput in (tests or [('1 2', '3')]):
        lines += ['```input', testInput, '```',
                  '```output', testOutput, '```']
    return '\n'.join(lines)


def manifestXlsx(rows):
    """把二维数组写成 xlsx 字节"""
    workbook = Workbook()
    sheet = workbook.active
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def manifestCsv(rows):
    """把二维数组写成 UTF-8 BOM CSV 字节（Excel 打开不乱码）"""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerows(rows)
    return ('﻿' + output.getvalue()).encode('utf-8')


class ProblemImportTests(TestCase):
    """导入核心行为与权限矩阵"""

    @classmethod
    def setUpTestData(cls):
        cls.superUser = User.objects.create_superuser(
            username='SU9201', password=PASSWORD)
        cls.teacher = User.objects.create_user(
            username='T9201', password=PASSWORD)
        cls.teacher.groups.add(Group.objects.get(name='教师'))
        cls.courseTeam = User.objects.create_user(
            username='C9201', password=PASSWORD)
        cls.courseTeam.groups.add(Group.objects.get(name='课程组'),
                                  Group.objects.get(name='教师'))
        cls.student = User.objects.create_user(
            username='S9201', password=PASSWORD)
        cls.student.groups.add(Group.objects.get(name='学生'))
        cls.adminUser = User.objects.create_user(
            username='A9201', password=PASSWORD)
        cls.adminUser.groups.add(Group.objects.get(name='教务'))
        # 有 admin 入口但非超级用户：导入页也必须拒绝
        cls.staffUser = User.objects.create_user(
            username='ST9201', password=PASSWORD, is_staff=True)

    def setUp(self):
        self.client = APIClient()

    def _zip(self, files, name='problems.zip'):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
            for path, content in files.items():
                archive.writestr(path, content)
        return SimpleUploadedFile(
            name, buffer.getvalue(), content_type='application/zip')

    def _singleZip(self, markdown=None, title='两数之和', code='C001',
                   tier='basic', languages='c,python',
                   compareStrategy='exact', manifestName='manifest.xlsx'):
        """构造一份单题导入包"""
        rows = [
            ['file', 'code', 'tier', 'languages', 'compareStrategy'],
            ['p0001.md', code, tier, languages, compareStrategy],
        ]
        manifest = manifestXlsx(rows) if manifestName.endswith('.xlsx') \
            else manifestCsv(rows)
        return self._zip({
            manifestName: manifest,
            'p0001.md': (markdown or buildMd(title)).encode('utf-8'),
        })

    def _import(self, zipFile, overwrite=False):
        self.client.force_login(self.superUser)
        payload = {'file': zipFile}
        if overwrite:
            payload['overwrite'] = '1'
        return self.client.post(IMPORT_URL, payload, format='multipart')

    # ---------- 基本导入 ----------

    def test_import_xlsx_manifest(self):
        """xlsx 清单：两道题成功入库，字段与批次记录正确"""
        zipFile = self._zip({
            'manifest.xlsx': manifestXlsx([
                ['file', 'code', 'tier', 'languages', 'compareStrategy'],
                ['p0001.md', 'C001', 'basic', 'c,python', 'exact'],
                ['p0002.md', 'C002', 'advanced', 'c', 'trailing'],
            ]),
            'p0001.md': buildMd('两数之和').encode('utf-8'),
            'p0002.md': buildMd('数组求和').encode('utf-8'),
        })
        resp = self._import(zipFile)
        self.assertEqual(resp.status_code, 200, msg=resp.data)
        self.assertEqual(resp.data['createdCount'], 2)
        self.assertEqual(resp.data['errorCount'], 0)
        problem = Problem.objects.get(source_code='C001')
        self.assertTrue(problem.is_public)
        self.assertEqual(problem.title, '两数之和')
        self.assertEqual(problem.tier, 'basic')
        self.assertEqual(problem.languages, ['c', 'python'])
        self.assertEqual(len(problem.source_hash), 64)
        advanced = Problem.objects.get(source_code='C002')
        self.assertEqual(advanced.tier, 'advanced')
        self.assertEqual(advanced.languages, ['c'])
        self.assertEqual(advanced.compare_strategy, 'trailing')
        batch = AsyncTask.objects.get(id=resp.data['batchId'])
        self.assertEqual(batch.status, AsyncTask.STATUS_DONE)
        self.assertEqual(batch.total, 2)

    def test_import_csv_manifest(self):
        """csv 清单同样可用（UTF-8 BOM）"""
        resp = self._import(self._singleZip(manifestName='manifest.csv'))
        self.assertEqual(resp.status_code, 200, msg=resp.data)
        self.assertEqual(resp.data['createdCount'], 1)

    def test_manifest_chinese_aliases(self):
        """清单支持中文档位/语言分隔符/比对策略"""
        resp = self._import(self._singleZip(
            tier='进阶', languages='C、Python', compareStrategy='忽略行尾空白'))
        self.assertEqual(resp.status_code, 200, msg=resp.data)
        problem = Problem.objects.get(source_code='C001')
        self.assertEqual(problem.tier, 'advanced')
        self.assertEqual(problem.languages, ['c', 'python'])
        self.assertEqual(problem.compare_strategy, 'trailing')

    def test_reimport_same_content_skipped(self):
        """同一批文件重复导入：内容未变全部跳过，不产生重复题"""
        self.assertEqual(self._import(self._singleZip()).status_code, 200)
        resp = self._import(self._singleZip())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['createdCount'], 0)
        self.assertEqual(resp.data['skippedCount'], 1)
        self.assertEqual(Problem.objects.count(), 1)

    def test_overwrite_updates_revision(self):
        """内容变化 + 勾选覆盖：更新题目且 revision 在原值上 +1"""
        self._import(self._singleZip(title='旧标题'))
        Problem.objects.filter(source_code='C001').update(revision=5)
        resp = self._import(self._singleZip(title='新标题'), overwrite=True)
        self.assertEqual(resp.status_code, 200, msg=resp.data)
        self.assertEqual(resp.data['updatedCount'], 1)
        problem = Problem.objects.get(source_code='C001')
        self.assertEqual(problem.title, '新标题')
        self.assertEqual(problem.revision, 6)

    def test_changed_without_overwrite_reports_error(self):
        """内容变化但未勾选覆盖：报错且原题不变"""
        self._import(self._singleZip(title='旧标题'))
        resp = self._import(self._singleZip(title='新标题'))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['updatedCount'], 0)
        self.assertEqual(resp.data['errorCount'], 1)
        problem = Problem.objects.get(source_code='C001')
        self.assertEqual(problem.title, '旧标题')
        self.assertEqual(problem.revision, 1)

    def test_manual_edit_detected_on_reimport(self):
        """题目被后台/API 手工改过：重复导入能识别内容变化（不只靠源文件哈希）"""
        self._import(self._singleZip(title='原始标题'))
        Problem.objects.filter(source_code='C001').update(title='手工改标题')
        resp = self._import(self._singleZip(title='原始标题'), overwrite=True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['updatedCount'], 1)
        problem = Problem.objects.get(source_code='C001')
        self.assertEqual(problem.title, '原始标题')

    # ---------- 逐题错误 ----------

    def test_invalid_markdown_row_error(self):
        """坏 md 只影响该行，其他题正常导入"""
        zipFile = self._zip({
            'manifest.xlsx': manifestXlsx([
                ['file', 'code'],
                ['good.md', 'C001'],
                ['bad.md', 'C002'],
            ]),
            'good.md': buildMd('好题').encode('utf-8'),
            'bad.md': '没有标题也没有测试点'.encode('utf-8'),
        })
        resp = self._import(zipFile)
        self.assertEqual(resp.status_code, 200, msg=resp.data)
        self.assertEqual(resp.data['createdCount'], 1)
        self.assertEqual(resp.data['errorCount'], 1)
        self.assertTrue(Problem.objects.filter(source_code='C001').exists())

    def test_manifest_references_missing_file(self):
        """清单引用 ZIP 中不存在的文件 → 该行报错"""
        zipFile = self._zip({
            'manifest.xlsx': manifestXlsx([
                ['file', 'code'],
                ['missing.md', 'C001'],
            ]),
        })
        resp = self._import(zipFile)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['createdCount'], 0)
        self.assertEqual(resp.data['errorCount'], 1)
        self.assertIn('不存在', resp.data['errors'][0]['reason'])

    def test_unlisted_markdown_reports_error(self):
        """ZIP 中存在但清单未登记的 md → 报错不导入"""
        zipFile = self._zip({
            'manifest.xlsx': manifestXlsx([
                ['file', 'code'],
                ['p0001.md', 'C001'],
            ]),
            'p0001.md': buildMd('登记题').encode('utf-8'),
            'extra.md': buildMd('未登记题').encode('utf-8'),
        })
        resp = self._import(zipFile)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['createdCount'], 1)
        self.assertEqual(resp.data['errorCount'], 1)
        self.assertEqual(Problem.objects.count(), 1)

    def test_duplicate_code_in_manifest(self):
        """清单内 code 重复：第二条报错，第一条正常导入"""
        zipFile = self._zip({
            'manifest.xlsx': manifestXlsx([
                ['file', 'code'],
                ['p0001.md', 'C001'],
                ['p0002.md', 'C001'],
            ]),
            'p0001.md': buildMd('题一').encode('utf-8'),
            'p0002.md': buildMd('题二').encode('utf-8'),
        })
        resp = self._import(zipFile)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['createdCount'], 1)
        self.assertEqual(resp.data['errorCount'], 1)
        self.assertEqual(Problem.objects.count(), 1)

    # ---------- ZIP/清单安全与完整性 ----------

    def test_missing_manifest_rejected(self):
        """没有清单 → 400，批次记录为失败"""
        zipFile = self._zip({'p0001.md': buildMd().encode('utf-8')})
        resp = self._import(zipFile)
        self.assertEqual(resp.status_code, 400)
        batch = AsyncTask.objects.filter(type=AsyncTask.TYPE_IMPORT).first()
        self.assertEqual(batch.status, AsyncTask.STATUS_FAILED)

    def test_manifest_missing_file_column_rejected(self):
        """清单缺 file 列 → 400"""
        zipFile = self._zip({
            'manifest.xlsx': manifestXlsx([['name', 'code']]),
        })
        resp = self._import(zipFile)
        self.assertEqual(resp.status_code, 400)

    def test_path_traversal_rejected(self):
        """ZIP 内 ../ 路径穿越 → 400"""
        zipFile = self._zip({
            'manifest.xlsx': manifestXlsx([
                ['file', 'code'],
                ['../evil.md', 'C001'],
            ]),
            '../evil.md': buildMd().encode('utf-8'),
        })
        resp = self._import(zipFile)
        self.assertEqual(resp.status_code, 400)

    def test_bad_zip_rejected(self):
        """不是 ZIP 的文件 → 400"""
        bad = SimpleUploadedFile('bad.zip', b'not a zip',
                                 content_type='application/zip')
        resp = self._import(bad)
        self.assertEqual(resp.status_code, 400)

    # ---------- 权限 ----------

    def test_only_superuser_can_import(self):
        """教师/课程组/教务/学生调用导入 API 一律 403"""
        for user in (self.teacher, self.courseTeam, self.adminUser,
                     self.student):
            self.client.force_login(user)
            resp = self.client.post(
                IMPORT_URL, {'file': self._singleZip()},
                format='multipart')
            self.assertEqual(resp.status_code, 403, msg=user.username)

    def test_anonymous_cannot_import(self):
        """未登录 403"""
        resp = APIClient().post(IMPORT_URL, {'file': self._singleZip()},
                                format='multipart')
        self.assertEqual(resp.status_code, 403)

    def test_admin_page_only_superuser(self):
        """有 staff 权限但非超级用户：后台导入页 403"""
        self.client.force_login(self.staffUser)
        resp = self.client.get(reverse('admin:teaching_problem_import'))
        self.assertEqual(resp.status_code, 403)

    def test_admin_page_imports(self):
        """超级用户可从 /admin/ 导入页上传 ZIP"""
        self.client.force_login(self.superUser)
        resp = self.client.post(
            reverse('admin:teaching_problem_import'),
            {'file': self._singleZip()})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(Problem.objects.count(), 1)

    def test_admin_changelist_has_import_entry(self):
        """题目列表页渲染正常，并显示批量导入入口"""
        self.client.force_login(self.superUser)
        resp = self.client.get('/admin/teaching/problem/')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, '批量导入题库')

    # ---------- 导入后可选题 ----------

    def test_imported_problem_selectable_by_teacher(self):
        """导入的公共题对教师可见，可加入作业选题集合"""
        resp = self._import(self._singleZip())
        problem = Problem.objects.get(source_code='C001')
        selectable = loadSelectableProblems(
            self.teacher, [problem.id])
        self.assertEqual([item.id for item in selectable], [problem.id])
        self.assertEqual(
            [item.id for item in loadSelectableProblems(
                self.courseTeam, [problem.id])],
            [problem.id])

    def test_import_1000_problems(self):
        """大体量验收：一次导入 1000 题全部成功"""
        rows = [['file', 'code']]
        files = {}
        for index in range(1000):
            fileName = f'p{index:04d}.md'
            rows.append([fileName, f'C{index:04d}'])
            files[fileName] = buildMd(f'题目{index}').encode('utf-8')
        files['manifest.xlsx'] = manifestXlsx(rows)
        resp = self._import(self._zip(files))
        self.assertEqual(resp.status_code, 200, msg=resp.data)
        self.assertEqual(resp.data['createdCount'], 1000)
        self.assertEqual(resp.data['errorCount'], 0)
        self.assertEqual(Problem.objects.count(), 1000)


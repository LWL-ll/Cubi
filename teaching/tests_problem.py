"""录题 API 与 markdown 解析器测试

规格：docs/OJ对接设计.md 第十章（markdown 录题格式）、
12.1（题库两级归属）、12.2（题目接口）、12.3（端点决定归属）、
6.2（保密链：tests/spj_code 永不下发）。

覆盖两大部分：
- 解析器矩阵：配对/分值/上限/异常拒绝（纯函数，不落库）
- API 矩阵：教师建个人题、课程组建公共题、编辑/删除对象级 403/404、
  学生题面白名单、脏输入 400、越权 403
"""
from django.contrib.auth.models import Group
from django.test import TestCase
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient

from teaching.models import Problem
from teaching.problem_markdown import parseProblemMarkdown
from users.models import User

PASSWORD = 'Test12345'


def buildMd(title='两数之和', body='给定两个整数，输出它们的和。',
            samples=None, tests=None, spjCode=None):
    """组装录题 markdown 文本（tests 元素为 (input, output, info串)）"""
    lines = [f'# {title}', '', body]
    for sampleInput, sampleOutput in (samples or []):
        lines += ['```input-sample', sampleInput, '```',
                  '```output-sample', sampleOutput, '```']
    for testInput, testOutput, info in (tests or []):
        lines += [f'```input{info}', testInput, '```',
                  '```output', testOutput, '```']
    if spjCode is not None:
        lines += ['```spj-python', spjCode, '```']
    return '\n'.join(lines)


class ProblemMarkdownTests(TestCase):
    """markdown 解析器矩阵（第十章约定，纯函数无 DB）"""

    def test_parse_basic(self):
        """正常解析：标题/正文/样例/标注分值测试点"""
        parsed = parseProblemMarkdown(buildMd(
            samples=[('1 2', '3')],
            tests=[('1 2', '3', ' points=60'), ('10 20', '30', ' points=40')]))
        self.assertEqual(parsed['title'], '两数之和')
        self.assertIn('给定两个整数', parsed['statement_md'])
        self.assertEqual(parsed['samples'], [{'input': '1 2', 'output': '3'}])
        self.assertEqual(parsed['tests'][0]['points'], 60)
        self.assertEqual(parsed['tests'][1]['points'], 40)
        self.assertFalse(parsed['has_spj'])
        self.assertEqual(parsed['spj_code'], '')

    def test_unmarked_points_split_evenly(self):
        """未标注点平分 100：3 个点 → 34/33/33；2 个点 → 50/50"""
        parsed3 = parseProblemMarkdown(buildMd(
            tests=[('1', '1', ''), ('2', '2', ''), ('3', '3', '')]))
        self.assertEqual([t['points'] for t in parsed3['tests']], [34, 33, 33])
        parsed2 = parseProblemMarkdown(buildMd(
            tests=[('1', '1', ''), ('2', '2', '')]))
        self.assertEqual([t['points'] for t in parsed2['tests']], [50, 50])

    def test_mixed_points(self):
        """混合分值：标注点保持标注值，未标注点平分 100"""
        parsed = parseProblemMarkdown(buildMd(
            tests=[('1', '1', ' points=40'), ('2', '2', ''),
                   ('3', '3', '')]))
        self.assertEqual([t['points'] for t in parsed['tests']], [40, 50, 50])

    def test_points_out_of_range_rejected(self):
        """points=0 / points=101 越界拒绝"""
        for badInfo in (' points=0', ' points=101'):
            with self.assertRaises(ValidationError):
                parseProblemMarkdown(buildMd(tests=[('1', '1', badInfo)]))

    def test_points_not_integer_rejected(self):
        """points=abc 非整数拒绝"""
        with self.assertRaises(ValidationError):
            parseProblemMarkdown(buildMd(tests=[('1', '1', ' points=abc')]))

    def test_unpaired_input_rejected(self):
        """input 后必须紧跟 output，单块/错配拒绝"""
        with self.assertRaises(ValidationError):
            parseProblemMarkdown('# 题\n\n```input\n1\n```\n')
        # input 后跟 output-sample 属于错配
        mdText = '# 题\n\n```input\n1\n```\n```output-sample\n1\n```\n'
        with self.assertRaises(ValidationError):
            parseProblemMarkdown(mdText)

    def test_unclosed_fence_rejected(self):
        """代码块未闭合拒绝"""
        mdText = '# 题\n\n```input\n1\n'
        with self.assertRaises(ValidationError):
            parseProblemMarkdown(mdText)

    def test_no_tests_rejected(self):
        """没有任何隐藏测试点拒绝"""
        with self.assertRaises(ValidationError):
            parseProblemMarkdown(buildMd(samples=[('1 2', '3')]))

    def test_too_many_tests_rejected(self):
        """超过 20 组隐藏测试点拒绝"""
        tests = [(str(i), str(i), '') for i in range(21)]
        with self.assertRaises(ValidationError):
            parseProblemMarkdown(buildMd(tests=tests))

    def test_group_too_large_rejected(self):
        """单组 input+output 超过 256KB 拒绝"""
        big = 'x' * (130 * 1024)  # 130KB × 2 = 260KB > 256KB
        with self.assertRaises(ValidationError):
            parseProblemMarkdown(buildMd(tests=[(big, big, '')]))

    def test_too_many_samples_rejected(self):
        """公开样例超过 20 组拒绝"""
        samples = [('1', '1')] * 21
        with self.assertRaises(ValidationError):
            parseProblemMarkdown(buildMd(
                samples=samples, tests=[('1', '1', '')]))

    def test_missing_title_rejected(self):
        """缺少一级标题拒绝"""
        with self.assertRaises(ValidationError):
            parseProblemMarkdown('```input\n1\n```\n```output\n1\n```\n')

    def test_empty_markdown_rejected(self):
        """空内容 / 纯空白拒绝"""
        for empty in ('', '   \n  '):
            with self.assertRaises(ValidationError):
                parseProblemMarkdown(empty)

    def test_title_too_long_rejected(self):
        """标题超过 100 字符拒绝"""
        with self.assertRaises(ValidationError):
            parseProblemMarkdown(buildMd(title='长' * 101,
                                         tests=[('1', '1', '')]))

    def test_spj_syntax_error_rejected(self):
        """spj-python 语法错误入库前拒绝"""
        with self.assertRaises(ValidationError):
            parseProblemMarkdown(buildMd(tests=[('1', '1', '')],
                                         spjCode='def broken(:'))

    def test_spj_parsed(self):
        """合法 spj 入库：has_spj=True、spj_code 原文保留"""
        spjCode = 'import sys\nscore = 100\nprint(score)\n'
        parsed = parseProblemMarkdown(buildMd(tests=[('1', '1', '')],
                                              spjCode=spjCode))
        self.assertTrue(parsed['has_spj'])
        self.assertEqual(parsed['spj_code'], spjCode)

    def test_spj_twice_rejected(self):
        """spj-python 代码块只能出现一次"""
        mdText = buildMd(tests=[('1', '1', '')], spjCode='print(100)\n')
        mdText += '\n```spj-python\nprint(50)\n```\n'
        with self.assertRaises(ValidationError):
            parseProblemMarkdown(mdText)

    def test_statement_excludes_protected_blocks(self):
        """保密链：statement_md 不含任何受保护块内容（14.7）"""
        parsed = parseProblemMarkdown(buildMd(
            body='正文', samples=[('SAMPLE_INPUT', 'SAMPLE_OUTPUT')],
            tests=[('SECRET_INPUT', 'SECRET_OUTPUT', '')]))
        for secret in ('SAMPLE_INPUT', 'SAMPLE_OUTPUT',
                       'SECRET_INPUT', 'SECRET_OUTPUT'):
            self.assertNotIn(secret, parsed['statement_md'])

    def test_plain_code_block_kept_in_body(self):
        """普通代码块（无受保护标签）原样保留进正文"""
        parsed = parseProblemMarkdown(buildMd(
            body='说明\n```c\nint main(){return 0;}\n```',
            tests=[('1', '1', '')]))
        self.assertIn('```c', parsed['statement_md'])
        self.assertIn('int main', parsed['statement_md'])

    def test_oversize_markdown_rejected(self):
        """md 超过 6MB 上限拒绝"""
        huge = buildMd(body='x' * (6 * 1024 * 1024), tests=[('1', '1', '')])
        with self.assertRaises(ValidationError):
            parseProblemMarkdown(huge)


class ProblemApiTests(TestCase):
    """录题 API 矩阵：两级归属 + 对象级 403/404 + 学生白名单 + 脏输入"""

    @classmethod
    def setUpTestData(cls):
        cls.teacher = User.objects.create_user(username='T9101', password=PASSWORD)
        cls.teacher2 = User.objects.create_user(username='T9102', password=PASSWORD)
        cls.courseTeam = User.objects.create_user(username='C9101', password=PASSWORD)
        cls.student = User.objects.create_user(username='S9101', password=PASSWORD)
        cls.superUser = User.objects.create_superuser(username='SU9101',
                                                      password=PASSWORD)
        cls.teacher.groups.add(Group.objects.get(name='教师'))
        cls.teacher2.groups.add(Group.objects.get(name='教师'))
        cls.courseTeam.groups.add(Group.objects.get(name='课程组'))
        cls.student.groups.add(Group.objects.get(name='学生'))
        # 列表可见性测试用的三本题：本人个人题 / 他人个人题 / 公共题
        cls.personalProblem = Problem.objects.create(
            title='本人个人题', statement_md='正文', samples=[],
            tests=[{'input': '1', 'output': '1', 'points': 100}],
            languages=['c', 'python'], owner=cls.teacher)
        cls.otherPersonal = Problem.objects.create(
            title='他人个人题', statement_md='正文', samples=[],
            tests=[{'input': '1', 'output': '1', 'points': 100}],
            languages=['c', 'python'], owner=cls.teacher2)
        cls.publicProblem = Problem.objects.create(
            title='公共题', statement_md='正文', samples=[],
            tests=[{'input': '1', 'output': '1', 'points': 100}],
            languages=['c', 'python'], owner=cls.courseTeam, is_public=True)

    def setUp(self):
        self.client = APIClient()

    def _login(self, user):
        self.client.force_login(user)

    def _mdPayload(self, **kwargs):
        payload = {
            'markdown': buildMd(samples=[('1 2', '3')],
                                tests=[('1 2', '3', '')]),
            'languages': ['c', 'python'],
            'tier': 'basic',
            'compareStrategy': 'exact',
        }
        payload.update(kwargs)
        return payload

    # ---------- 列表 ----------

    def test_teacher_list_visible_set(self):
        """教师题库列表 = 公共题 + 本人个人题（不含他人个人题）"""
        self._login(self.teacher)
        resp = self.client.get('/api/teacher/problems/')
        self.assertEqual(resp.status_code, 200)
        ids = {item['id'] for item in resp.data['results']}
        self.assertIn(self.personalProblem.id, ids)
        self.assertIn(self.publicProblem.id, ids)
        self.assertNotIn(self.otherPersonal.id, ids)

    def test_student_cannot_access_teacher_problem_api(self):
        """学生无 homework:bank → 教师题库接口 403"""
        self._login(self.student)
        self.assertEqual(self.client.get('/api/teacher/problems/').status_code,
                         403)
        self.assertEqual(self.client.post(
            '/api/teacher/problems/', self._mdPayload(),
            format='json').status_code, 403)

    # ---------- 建题（端点决定归属，12.3） ----------

    def test_teacher_create_personal_problem(self):
        """教师建个人题：is_public=False、owner=本人、revision=1、解析正确"""
        self._login(self.teacher)
        resp = self.client.post('/api/teacher/problems/', self._mdPayload(),
                                format='json')
        self.assertEqual(resp.status_code, 200)
        problem = Problem.objects.get(id=resp.data['id'])
        self.assertFalse(problem.is_public)
        self.assertEqual(problem.owner_id, self.teacher.id)
        self.assertEqual(problem.revision, 1)
        self.assertEqual(problem.title, '两数之和')
        # 未标注单点 → 100 分
        self.assertEqual(problem.tests[0]['points'], 100)

    def test_course_team_create_public_problem(self):
        """课程组建公共题：is_public=True（请求体 isPublic 无效）"""
        self._login(self.courseTeam)
        resp = self.client.post('/api/teacher/public-problems/',
                                self._mdPayload(isPublic=False), format='json')
        self.assertEqual(resp.status_code, 200)
        problem = Problem.objects.get(id=resp.data['id'])
        self.assertTrue(problem.is_public)

    def test_teacher_cannot_create_public_problem(self):
        """教师无 question_bank:manage → 公共题库录题 403"""
        self._login(self.teacher)
        resp = self.client.post('/api/teacher/public-problems/',
                                self._mdPayload(), format='json')
        self.assertEqual(resp.status_code, 403)

    def test_student_cannot_create_problem(self):
        """学生无题库权限 → 录题 403"""
        self._login(self.student)
        resp = self.client.post('/api/teacher/problems/', self._mdPayload(),
                                format='json')
        self.assertEqual(resp.status_code, 403)

    # ---------- 编辑 / 删除（对象级 403/404，12.2） ----------

    def test_teacher_update_own_problem(self):
        """教师编辑本人题：全文替换成功、revision+1"""
        self._login(self.teacher)
        url = f'/api/teacher/problems/{self.personalProblem.id}/'
        resp = self.client.put(url, self._mdPayload(
            markdown=buildMd(title='改后标题', tests=[('1', '1', '')])),
            format='json')
        self.assertEqual(resp.status_code, 200)
        self.personalProblem.refresh_from_db()
        self.assertEqual(self.personalProblem.title, '改后标题')
        self.assertEqual(self.personalProblem.revision, 2)

    def test_teacher_update_others_problem_404(self):
        """教师编辑他人个人题 → 404（防探测）"""
        self._login(self.teacher)
        resp = self.client.put(
            f'/api/teacher/problems/{self.otherPersonal.id}/',
            self._mdPayload(), format='json')
        self.assertEqual(resp.status_code, 404)

    def test_teacher_update_public_problem_404(self):
        """教师无 question_bank:manage → 编辑公共题 404"""
        self._login(self.teacher)
        resp = self.client.put(
            f'/api/teacher/problems/{self.publicProblem.id}/',
            self._mdPayload(), format='json')
        self.assertEqual(resp.status_code, 404)

    def test_course_team_update_public_problem(self):
        """课程组编辑公共题成功"""
        self._login(self.courseTeam)
        resp = self.client.put(
            f'/api/teacher/problems/{self.publicProblem.id}/',
            self._mdPayload(markdown=buildMd(title='公共改', tests=[('1', '1', '')])),
            format='json')
        self.assertEqual(resp.status_code, 200)
        self.publicProblem.refresh_from_db()
        self.assertEqual(self.publicProblem.title, '公共改')

    def test_course_team_cannot_manage_teacher_personal(self):
        """课程组无权管理教师个人题 → 404"""
        self._login(self.courseTeam)
        resp = self.client.get(
            f'/api/teacher/problems/{self.personalProblem.id}/')
        self.assertEqual(resp.status_code, 404)

    def test_superuser_manage_any(self):
        """超级用户放行：可编辑教师个人题"""
        self._login(self.superUser)
        resp = self.client.put(
            f'/api/teacher/problems/{self.personalProblem.id}/',
            self._mdPayload(markdown=buildMd(title='超管改', tests=[('1', '1', '')])),
            format='json')
        self.assertEqual(resp.status_code, 200)

    def test_teacher_delete_own_problem(self):
        """教师删除本人题：204 且题目消失"""
        self._login(self.teacher)
        resp = self.client.delete(
            f'/api/teacher/problems/{self.personalProblem.id}/')
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(Problem.objects.filter(
            pk=self.personalProblem.id).exists())

    def test_update_invalid_markdown_rejected(self):
        """编辑提交非法 md → 400 且原题不被改动（解析先于落库）"""
        self._login(self.teacher)
        url = f'/api/teacher/problems/{self.personalProblem.id}/'
        resp = self.client.put(url, self._mdPayload(
            markdown='没有标题\n```input\n1\n```\n```output\n1\n```\n'),
            format='json')
        self.assertEqual(resp.status_code, 400)
        self.personalProblem.refresh_from_db()
        self.assertEqual(self.personalProblem.title, '本人个人题')
        self.assertEqual(self.personalProblem.revision, 1)

    # ---------- 学生题面 ----------

    def test_student_view_problem_whitelist(self):
        """学生读题：白名单字段齐全，不含 tests/spjCode/判题配置（6.2）"""
        self._login(self.student)
        resp = self.client.get(f'/api/problems/{self.publicProblem.id}/')
        self.assertEqual(resp.status_code, 200)
        for field in ('id', 'title', 'statementMd', 'samples', 'languages'):
            self.assertIn(field, resp.data)
        for secret in ('tests', 'spjCode', 'spj_code', 'tier',
                       'compareStrategy', 'compare_strategy'):
            self.assertNotIn(secret, resp.data)

    def test_student_view_teacher_personal_problem(self):
        """学生可读作业里引用的教师个人题（不按 is_public 过滤，12.1）"""
        self._login(self.student)
        resp = self.client.get(f'/api/problems/{self.personalProblem.id}/')
        self.assertEqual(resp.status_code, 200)

    def test_student_view_missing_problem_404(self):
        """题不存在 → 404"""
        self._login(self.student)
        self.assertEqual(self.client.get('/api/problems/999999/').status_code,
                         404)

    def test_teacher_detail_never_contains_tests_or_spj(self):
        """保密链：教师详情也不含 tests/spj_code（教师有原始 md 可核对）"""
        spjProblem = Problem.objects.create(
            title='spj 题', statement_md='正文', samples=[],
            tests=[{'input': '1', 'output': '1', 'points': 100}],
            languages=['python'], owner=self.teacher,
            has_spj=True, spj_code='print(100)\n')
        self._login(self.teacher)
        resp = self.client.get(f'/api/teacher/problems/{spjProblem.id}/')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('statementMd', resp.data)
        for secret in ('tests', 'spjCode', 'spj_code'):
            self.assertNotIn(secret, resp.data)

    # ---------- 脏输入 ----------

    def test_invalid_metadata_rejected(self):
        """脏元数据全部 400：语言白名单 / tier / 比对策略 / md 类型"""
        self._login(self.teacher)
        badPayloads = [
            {'languages': ['java']},
            {'languages': []},
            {'languages': ['c', 'c']},
            {'tier': 'extreme'},
            {'compareStrategy': 'fuzzy'},
            {'markdown': 123},
        ]
        for bad in badPayloads:
            resp = self.client.post('/api/teacher/problems/',
                                    self._mdPayload(**bad), format='json')
            self.assertEqual(resp.status_code, 400, msg=f'{bad} 未拒绝')

    def test_marked_points_stored(self):
        """标注点分值落库正确（70+30 恰为 100）"""
        self._login(self.teacher)
        resp = self.client.post('/api/teacher/problems/',
                                self._mdPayload(markdown=buildMd(
                                    tests=[('1', '1', ' points=70'),
                                           ('2', '2', ' points=30')])),
                                format='json')
        self.assertEqual(resp.status_code, 200)
        problem = Problem.objects.get(id=resp.data['id'])
        self.assertEqual([t['points'] for t in problem.tests], [70, 30])

    # ---------- 认证边界 ----------

    def test_unauthenticated_403(self):
        """未登录全部 403"""
        anon = APIClient()
        self.assertEqual(anon.get('/api/teacher/problems/').status_code, 403)
        self.assertEqual(anon.post('/api/teacher/problems/', self._mdPayload(),
                                   format='json').status_code, 403)
        self.assertEqual(anon.get(f'/api/problems/{self.publicProblem.id}/')
                         .status_code, 403)

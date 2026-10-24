"""作业组卷与学生作业查询 API 测试

规格：docs/OJ对接设计.md 12.4（M7 决策）：
- 选题可见集合 = 公共题库 + 本人个人题（他人个人题 404）
- 作业题数上限 20、revision 快照冻结
- 已有提交 → 题目列表锁定；无提交可删、有提交 400
- 截止时间只允许向后延长
- 学生列表三态（未开始/进行中/已截止）、详情开始前 404 防提前看题
- 白名单：学生端题目不含 tests/spj_code
"""
from datetime import timedelta

from django.contrib.auth.models import Group
from django.utils import timezone
from django.test import TestCase
from rest_framework.test import APIClient

from academics.models import (AdminClass, ClassMembership, Course,
                              CourseCollege, Department, Major, TeachingClass)
from teaching.models import Homework, HomeworkProblem, Problem, Submission
from users.models import SchoolIdentity, StudentProfile, User

PASSWORD = 'Test12345'


class HomeworkApiTests(TestCase):
    """组卷 API（教师视角）+ 学生作业查询 API（越权矩阵全覆盖）"""

    @classmethod
    def setUpTestData(cls):
        cls.teacher = User.objects.create_user(username='T9201', password=PASSWORD)
        cls.teacher2 = User.objects.create_user(username='T9202', password=PASSWORD)
        cls.courseTeam = User.objects.create_user(username='C9201', password=PASSWORD)
        cls.student = User.objects.create_user(username='S9201', password=PASSWORD)
        cls.otherStudent = User.objects.create_user(username='S9202', password=PASSWORD)
        cls.superUser = User.objects.create_superuser(username='SU9201',
                                                      password=PASSWORD)
        cls.teacher.groups.add(Group.objects.get(name='教师'))
        cls.teacher2.groups.add(Group.objects.get(name='教师'))
        cls.courseTeam.groups.add(Group.objects.get(name='课程组'))
        cls.student.groups.add(Group.objects.get(name='学生'))
        cls.otherStudent.groups.add(Group.objects.get(name='学生'))
        # 学生身份链（3 跳推导依赖，与 tests_submission.py 同构）
        department = Department.objects.create(code='D96', name='测试学院')
        major = Major.objects.create(department=department, code='96', name='测试专业')
        adminClass = AdminClass.objects.create(
            name='测试2301', major=major, enrollment_year=2023)
        otherAdminClass = AdminClass.objects.create(
            name='测试2302', major=major, enrollment_year=2023)
        course = Course.objects.create(code='TEST02', name='测试课程')
        courseCollege = CourseCollege.objects.create(
            course=course, department=department)
        courseCollege.admin_classes.add(adminClass, otherAdminClass)
        cls.teachingClass = TeachingClass.objects.create(
            name='测试一班', term='2026-2027-1', course_college=courseCollege)
        cls.teachingClass.admin_classes.add(adminClass)
        cls.teachingClass.teachers.add(cls.teacher)
        cls.otherTeachingClass = TeachingClass.objects.create(
            name='测试二班', term='2026-2027-1', course_college=courseCollege)
        cls.otherTeachingClass.admin_classes.add(otherAdminClass)
        cls.otherTeachingClass.teachers.add(cls.teacher2)

        def makeStudent(user, number, targetClass):
            identity = SchoolIdentity.objects.create(
                user=user, school_number=number,
                identity_type=SchoolIdentity.IDENTITY_STUDENT)
            profile = StudentProfile.objects.create(
                identity=identity, major=major, enrollment_year=2023)
            ClassMembership.objects.create(
                student_profile=profile, admin_class=targetClass,
                joined_at=timezone.now().date())

        makeStudent(cls.student, '20239601', adminClass)
        makeStudent(cls.otherStudent, '20239602', otherAdminClass)

        # 三本题：本人个人题 / 他人个人题 / 公共题（组卷选题可见性用）
        cls.personalProblem = Problem.objects.create(
            title='本人题', statement_md='正文', samples=[],
            tests=[{'input': '1', 'output': '1', 'points': 100}],
            languages=['c', 'python'], owner=cls.teacher)
        cls.otherPersonal = Problem.objects.create(
            title='他人题', statement_md='正文', samples=[],
            tests=[{'input': '1', 'output': '1', 'points': 100}],
            languages=['c', 'python'], owner=cls.teacher2)
        cls.publicProblem = Problem.objects.create(
            title='公共题', statement_md='正文', samples=[],
            tests=[{'input': '1', 'output': '1', 'points': 100}],
            languages=['c', 'python'], owner=cls.courseTeam, is_public=True)
        cls.now = timezone.now()

    def setUp(self):
        self.client = APIClient()

    def _login(self, user):
        self.client.force_login(user)

    def _createPayload(self, **kwargs):
        """组卷请求体：默认开放中的时间窗口 + 本人题"""
        payload = {
            'title': '第一次作业',
            'startAt': (self.now - timedelta(hours=1)).isoformat(),
            'deadline': (self.now + timedelta(days=1)).isoformat(),
            'problemIds': [self.personalProblem.id],
        }
        payload.update(kwargs)
        return payload

    def _createHomework(self, **kwargs):
        return self.client.post(
            f'/api/teacher/teaching-classes/{self.teachingClass.id}/homeworks/',
            self._createPayload(**kwargs), format='json')

    def test_homework_tab_page_renders(self):
        """作业 Tab 页面正常渲染，含选题搜索与已选清单容器"""
        self._login(self.teacher)
        resp = self.client.get(
            f'/teacher/teaching-classes/{self.teachingClass.id}/?tab=homework')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'pickerSearch')
        self.assertContains(resp, 'selectedProblems')

    def _makeSubmission(self, homework, user=None, problem=None, score=0):
        """直接建一条已判题提交（锁定/删除语义测试用，绕开判题链路）"""
        return Submission.objects.create(
            user=user or self.student, problem=problem or self.personalProblem,
            homework=homework, language='c', code='int main(){return 0;}',
            fingerprint='f' * 64, submit_type=Submission.SUBMIT_HOMEWORK,
            status=Submission.STATUS_ACCEPTED, score=score, total_score=100,
            problem_revision=1, tests_hash='t' * 64, judge_snapshot={'tests': []})

    # ---------- 组卷：创建 ----------

    def test_create_homework_success(self):
        """创建成功：题序 = 数组序，revision 冻结为选题时修订号"""
        self._login(self.teacher)
        resp = self._createHomework(
            problemIds=[self.personalProblem.id, self.publicProblem.id])
        self.assertEqual(resp.status_code, 200)
        homework = Homework.objects.get(id=resp.data['id'])
        self.assertEqual(homework.teaching_class_id, self.teachingClass.id)
        self.assertEqual(homework.created_by_id, self.teacher.id)
        items = homework.problems.order_by('order')
        self.assertEqual([i.problem_id for i in items],
                         [self.personalProblem.id, self.publicProblem.id])
        self.assertEqual([i.problem_revision for i in items],
                         [self.personalProblem.revision,
                          self.publicProblem.revision])

    def test_create_non_own_class_404(self):
        """非本班教师组卷 404（不暴露教学班存在性）"""
        self._login(self.teacher2)
        self.assertEqual(self._createHomework().status_code, 404)

    def test_student_cannot_create_403(self):
        """学生无 homework:publish → 403"""
        self._login(self.student)
        self.assertEqual(self._createHomework().status_code, 403)

    def test_create_others_personal_problem_404(self):
        """选题他人个人题 404 且作业不落库（可见集合 = 公共 + 本人）"""
        self._login(self.teacher)
        resp = self._createHomework(problemIds=[self.otherPersonal.id])
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(Homework.objects.filter(title='第一次作业').exists())

    def test_create_missing_problem_404(self):
        """选题不存在的题 404"""
        self._login(self.teacher)
        resp = self._createHomework(problemIds=[999999])
        self.assertEqual(resp.status_code, 404)

    def test_create_too_many_problems_400(self):
        """超过 20 题上限 400"""
        self._login(self.teacher)
        resp = self._createHomework(problemIds=list(range(1, 22)))
        self.assertEqual(resp.status_code, 400)

    def test_create_duplicate_problem_ids_400(self):
        """题目数组有重复 400"""
        self._login(self.teacher)
        resp = self._createHomework(
            problemIds=[self.personalProblem.id, self.personalProblem.id])
        self.assertEqual(resp.status_code, 400)

    def test_create_empty_problem_ids_400(self):
        """题目数组为空 400"""
        self._login(self.teacher)
        for bad in ([], 'not-a-list'):
            resp = self._createHomework(problemIds=bad)
            self.assertEqual(resp.status_code, 400, msg=f'{bad} 未拒绝')

    def test_create_bad_time_400(self):
        """开始时间不晚于截止时间 400"""
        self._login(self.teacher)
        resp = self._createHomework(
            startAt=(self.now + timedelta(hours=1)).isoformat(),
            deadline=(self.now + timedelta(hours=1)).isoformat())
        self.assertEqual(resp.status_code, 400)
        resp = self._createHomework(
            startAt='not-a-datetime')
        self.assertEqual(resp.status_code, 400)

    def test_create_bad_title_400(self):
        """标题空/超长 400"""
        self._login(self.teacher)
        for badTitle in ('', '   ', '长' * 101):
            resp = self._createHomework(title=badTitle)
            self.assertEqual(resp.status_code, 400, msg=f'{badTitle!r} 未拒绝')

    # ---------- 组卷：列表 / 详情 ----------

    def test_teacher_list_own_class_homeworks(self):
        """本班教师看本班作业列表（含聚合计数）；他班教师 404"""
        self._login(self.teacher)
        self._createHomework()
        homework = Homework.objects.get(title='第一次作业')
        self._makeSubmission(homework)
        resp = self.client.get(
            f'/api/teacher/teaching-classes/{self.teachingClass.id}/homeworks/')
        self.assertEqual(resp.status_code, 200)
        item = resp.data[0]
        self.assertEqual(item['id'], homework.id)
        self.assertEqual(item['problemCount'], 1)
        self.assertEqual(item['submissionCount'], 1)

        self._login(self.teacher2)
        resp = self.client.get(
            f'/api/teacher/teaching-classes/{self.teachingClass.id}/homeworks/')
        self.assertEqual(resp.status_code, 404)

    def test_detail_contains_problems_without_secrets(self):
        """详情含题序与修订号快照，不含 tests/spj 字段（6.2 保密链）"""
        self._login(self.teacher)
        self._createHomework(problemIds=[self.publicProblem.id])
        homework = Homework.objects.get(title='第一次作业')
        resp = self.client.get(f'/api/teacher/homeworks/{homework.id}/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data['problems']), 1)
        problemItem = resp.data['problems'][0]
        self.assertEqual(problemItem['problemId'], self.publicProblem.id)
        self.assertEqual(problemItem['order'], 0)
        self.assertEqual(problemItem['problemRevision'],
                         self.publicProblem.revision)
        for secret in ('tests', 'spjCode', 'spj_code', 'statementMd'):
            self.assertNotIn(secret, problemItem)
        self.assertNotIn('tests', resp.data)

    # ---------- 组卷：编辑 / 删除（锁定语义） ----------

    def test_update_replace_problems_before_submission(self):
        """无提交：题目列表可整体替换，revision 重新快照"""
        self._login(self.teacher)
        self._createHomework()
        homework = Homework.objects.get(title='第一次作业')
        # 改题前把题目 revision 提高，验证重新快照
        self.publicProblem.revision = 5
        self.publicProblem.save(update_fields=['revision'])
        resp = self.client.put(f'/api/teacher/homeworks/{homework.id}/',
                               self._createPayload(
                                   title='改后标题',
                                   problemIds=[self.publicProblem.id]),
                               format='json')
        self.assertEqual(resp.status_code, 200)
        item = HomeworkProblem.objects.get(homework=homework)
        self.assertEqual(item.problem_id, self.publicProblem.id)
        self.assertEqual(item.problem_revision, 5)

    def test_update_lock_after_submission(self):
        """已有提交：改题 400、改标题 200（题目列表锁定，12.4）"""
        self._login(self.teacher)
        self._createHomework()
        homework = Homework.objects.get(title='第一次作业')
        self._makeSubmission(homework)
        url = f'/api/teacher/homeworks/{homework.id}/'
        resp = self.client.put(url, self._createPayload(
            problemIds=[self.personalProblem.id, self.publicProblem.id]),
            format='json')
        self.assertEqual(resp.status_code, 400)
        resp = self.client.put(url, self._createPayload(title='新标题'),
                               format='json')
        self.assertEqual(resp.status_code, 200)
        homework.refresh_from_db()
        self.assertEqual(homework.title, '新标题')
        # 中间表未被改动
        self.assertEqual(homework.problems.count(), 1)

    def test_update_deadline_only_extend(self):
        """截止时间只允许向后延长（缩短 400）"""
        self._login(self.teacher)
        self._createHomework()
        homework = Homework.objects.get(title='第一次作业')
        url = f'/api/teacher/homeworks/{homework.id}/'
        resp = self.client.put(url, self._createPayload(
            deadline=(self.now + timedelta(hours=1)).isoformat()),
            format='json')
        self.assertEqual(resp.status_code, 400)
        resp = self.client.put(url, self._createPayload(
            deadline=(self.now + timedelta(days=2)).isoformat()),
            format='json')
        self.assertEqual(resp.status_code, 200)

    def test_delete_without_submissions_204(self):
        """无提交可删（连带中间表）"""
        self._login(self.teacher)
        self._createHomework()
        homework = Homework.objects.get(title='第一次作业')
        resp = self.client.delete(f'/api/teacher/homeworks/{homework.id}/')
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(Homework.objects.filter(pk=homework.id).exists())

    def test_delete_with_submissions_400(self):
        """有提交 400（历史数据保护）"""
        self._login(self.teacher)
        self._createHomework()
        homework = Homework.objects.get(title='第一次作业')
        self._makeSubmission(homework)
        resp = self.client.delete(f'/api/teacher/homeworks/{homework.id}/')
        self.assertEqual(resp.status_code, 400)

    def test_superuser_manage_any(self):
        """超管放行：可编辑任意教学班作业"""
        self._login(self.superUser)
        self._createHomework()
        homework = Homework.objects.get(title='第一次作业')
        resp = self.client.put(f'/api/teacher/homeworks/{homework.id}/',
                               self._createPayload(title='超管改'),
                               format='json')
        self.assertEqual(resp.status_code, 200)

    # ---------- 学生侧 ----------

    def test_student_list_statuses(self):
        """三态派生：未开始/进行中/已截止 + 我的提交概况"""
        self._login(self.teacher)
        self._createHomework(title='未开始',
                             startAt=(self.now + timedelta(hours=2)).isoformat(),
                             deadline=(self.now + timedelta(days=2)).isoformat())
        self._createHomework(title='进行中')
        self._createHomework(title='已截止',
                             startAt=(self.now - timedelta(days=2)).isoformat(),
                             deadline=(self.now - timedelta(hours=1)).isoformat())
        ongoing = Homework.objects.get(title='进行中')
        self._makeSubmission(ongoing, score=100)

        self._login(self.student)
        resp = self.client.get('/api/homeworks/')
        self.assertEqual(resp.status_code, 200)
        byTitle = {item['title']: item for item in resp.data}
        self.assertEqual(byTitle['未开始']['status'], 'pending_start')
        self.assertEqual(byTitle['进行中']['status'], 'ongoing')
        self.assertEqual(byTitle['已截止']['status'], 'closed')
        self.assertEqual(byTitle['进行中']['mySubmitCount'], 1)
        self.assertEqual(byTitle['进行中']['myBestScore'], 100)
        self.assertEqual(byTitle['进行中']['problemCount'], 1)

    def test_student_detail_whitelist(self):
        """学生详情：题目走学生白名单，不含 tests/spj/判题配置"""
        self._login(self.teacher)
        self._createHomework(problemIds=[self.personalProblem.id])
        homework = Homework.objects.get(title='第一次作业')
        self._makeSubmission(homework, score=80)

        self._login(self.student)
        resp = self.client.get(f'/api/homeworks/{homework.id}/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['status'], 'ongoing')
        problemItem = resp.data['problems'][0]
        for field in ('id', 'title', 'statementMd', 'samples', 'languages'):
            self.assertIn(field, problemItem)
        for secret in ('tests', 'spjCode', 'spj_code', 'tier',
                       'compareStrategy'):
            self.assertNotIn(secret, problemItem)
        self.assertEqual(len(resp.data['mySubmissions']), 1)
        self.assertEqual(resp.data['mySubmissions'][0]['score'], 80)

    def test_student_detail_wrong_class_404(self):
        """非本班学生详情 404（防探测）"""
        self._login(self.teacher)
        self._createHomework()
        homework = Homework.objects.get(title='第一次作业')

        self._login(self.otherStudent)
        resp = self.client.get(f'/api/homeworks/{homework.id}/')
        self.assertEqual(resp.status_code, 404)

    def test_student_detail_before_start_404(self):
        """开始前详情 404（防提前看题）；列表仍可见「未开始」"""
        self._login(self.teacher)
        self._createHomework(title='未开始',
                             startAt=(self.now + timedelta(hours=2)).isoformat(),
                             deadline=(self.now + timedelta(days=2)).isoformat())
        homework = Homework.objects.get(title='未开始')

        self._login(self.student)
        self.assertEqual(self.client.get(f'/api/homeworks/{homework.id}/')
                         .status_code, 404)
        resp = self.client.get('/api/homeworks/')
        self.assertEqual(resp.data[0]['status'], 'pending_start')

    def test_unauthenticated_403(self):
        """未登录全部 403"""
        anon = APIClient()
        self.assertEqual(anon.get('/api/homeworks/').status_code, 403)
        self.assertEqual(anon.post(
            f'/api/teacher/teaching-classes/{self.teachingClass.id}/homeworks/',
            self._createPayload(), format='json').status_code, 403)

    def test_problem_access_requires_own_started_homework(self):
        self._login(self.teacher)
        self._createHomework(problemIds=[self.personalProblem.id, self.publicProblem.id])
        homework = Homework.objects.get(title='第一次作业')
        self._login(self.student)
        for problem in (self.personalProblem, self.publicProblem):
            url = f'/api/problems/{problem.id}/'
            self.assertEqual(self.client.get(url).status_code, 404)
            response = self.client.get(url, {'homeworkId': homework.id})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(set(response.data),
                             {'id', 'title', 'statementMd', 'samples', 'languages'})
            self.assertEqual(self.client.get(url, {'homeworkId': 'bad'}).status_code, 404)
        url = f'/api/problems/{self.personalProblem.id}/'
        self._login(self.otherStudent)
        self.assertEqual(self.client.get(url, {'homeworkId': homework.id}).status_code, 404)
        self._login(self.student)
        homework.start_at = self.now + timedelta(hours=1)
        homework.save()
        self.assertEqual(self.client.get(url, {'homeworkId': homework.id}).status_code, 404)
        homework.start_at = self.now - timedelta(days=2)
        homework.deadline = self.now - timedelta(days=1)
        homework.save()
        self.assertEqual(self.client.get(url, {'homeworkId': homework.id}).status_code, 200)
        self.assertEqual(self.client.get(f'/api/problems/{self.otherPersonal.id}/',
                                        {'homeworkId': homework.id}).status_code, 404)

    def test_editing_bank_problem_does_not_change_homework(self):
        self._login(self.teacher)
        self._createHomework()
        homework = Homework.objects.get(title='第一次作业')
        item = homework.problems.get()
        original = item.problem_snapshot
        self.personalProblem.title = '新版标题'
        self.personalProblem.statement_md = '新版题面'
        self.personalProblem.samples = [{'input': 'new', 'output': 'new'}]
        self.personalProblem.languages = ['python']
        self.personalProblem.tests = [{'input': 'SECRET_NEW', 'output': 'SECRET_EXPECTED', 'points': 50}]
        self.personalProblem.spj_code = 'SECRET_SPJ'
        self.personalProblem.has_spj = True
        self.personalProblem.tier = 'advanced'
        self.personalProblem.revision = 2
        self.personalProblem.save()
        teacherData = self.client.get(f'/api/teacher/homeworks/{homework.id}/').data['problems'][0]
        self.assertEqual(teacherData['title'], original['title'])
        self.assertEqual(teacherData['tier'], original['tier'])
        self.assertEqual(teacherData['languages'], original['languages'])
        self.assertEqual(teacherData['problemRevision'], 1)
        self._login(self.student)
        detail = self.client.get(f'/api/homeworks/{homework.id}/')
        problem = detail.data['problems'][0]
        self.assertEqual(problem['title'], original['title'])
        self.assertEqual(problem['statementMd'], original['statement_md'])
        self.assertEqual(problem['samples'], original['samples'])
        self.assertEqual(problem['languages'], original['languages'])
        direct = self.client.get(f'/api/problems/{self.personalProblem.id}/',
                                 {'homeworkId': homework.pk})
        self.assertEqual(direct.data, problem)
        for secret in ('problem_snapshot', 'tests', 'spj_code', 'SECRET_NEW', 'SECRET_SPJ'):
            self.assertNotIn(secret, detail.content.decode())

    def test_title_only_update_keeps_existing_snapshot(self):
        self._login(self.teacher)
        self._createHomework()
        homework = Homework.objects.get(title='第一次作业')
        original = homework.problems.get().problem_snapshot
        self.personalProblem.title = '新题目'
        self.personalProblem.save()
        response = self.client.put(f'/api/teacher/homeworks/{homework.id}/',
                                   self._createPayload(title='改作业名'), format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(homework.problems.get().problem_snapshot, original)

"""页面路由、HttpOnly CSRF 和页面改密交互回归。"""
from datetime import timedelta
import json
import re
import shutil
import subprocess
from unittest import mock, skipUnless

from django.contrib.auth.models import Group
from django.template.loader import render_to_string
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from academics.models import (AdminClass, ClassMembership, Course, CourseCollege,
                              Department, Major, TeachingClass)
from teaching.models import Homework, HomeworkProblem, Problem, Submission
from users.models import SchoolIdentity, StudentProfile, User


class PageCsrfTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.teacher = User.objects.create_user(username='page-teacher', password='Test12345')
        cls.teacher.groups.add(Group.objects.get(name='教师'))
        cls.student = User.objects.create_user(username='page-student', password='Test12345')
        cls.student.groups.add(Group.objects.get(name='学生'))
        department = Department.objects.create(code='PAGE', name='学院')
        major = Major.objects.create(department=department, code='P', name='专业')
        adminClass = AdminClass.objects.create(name='页面班', major=major)
        identity = SchoolIdentity.objects.create(user=cls.student, school_number='PAGE1',
                                                identity_type='student')
        profile = StudentProfile.objects.create(identity=identity, major=major, enrollment_year=2026)
        ClassMembership.objects.create(student_profile=profile, admin_class=adminClass,
                                       joined_at=timezone.localdate())
        course = Course.objects.create(code='PAGE', name='课程')
        cc = CourseCollege.objects.create(course=course, department=department)
        cc.admin_classes.add(adminClass)
        cls.tc = TeachingClass.objects.create(name='教学班', term='T1', course_college=cc)
        cls.tc.admin_classes.add(adminClass)
        cls.tc.teachers.add(cls.teacher)
        cls.problem = Problem.objects.create(title='题目', statement_md='正文', owner=cls.teacher,
            languages=['c'], tests=[{'input': '', 'output': '1', 'points': 100}])
        cls.homework = Homework.objects.create(teaching_class=cls.tc, title='作业',
            start_at=timezone.now() - timedelta(hours=1), deadline=timezone.now() + timedelta(days=1))
        HomeworkProblem.objects.create(homework=cls.homework, problem=cls.problem,
                                       problem_revision=1)

    def pageClient(self, user, path):
        client = APIClient(enforce_csrf_checks=True)
        client.force_login(user)
        response = client.get(path)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.cookies['csrftoken']['httponly'])
        token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"',
                          response.content.decode()).group(1)
        return client, token

    def test_teacher_and_course_team_home_link(self):
        self.client.force_login(self.teacher)
        self.assertContains(self.client.get('/home/'), 'href="/teacher/"')
        self.teacher.groups.add(Group.objects.get(name='课程组'))
        self.assertContains(self.client.get('/home/'), 'href="/teacher/"')

    def test_problem_create_edit_delete_with_page_token(self):
        client, token = self.pageClient(self.teacher, '/teacher/question-bank/')
        payload = {'markdown': '# 新题\n```input\n1\n```\n```output\n1\n```'}
        self.assertEqual(client.post('/api/teacher/problems/', payload, format='json').status_code, 403)
        response = client.post('/api/teacher/problems/', payload, format='json', HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 200)
        url = f'/api/teacher/problems/{response.data["id"]}/'
        self.assertEqual(client.put(url, payload, format='json').status_code, 403)
        self.assertEqual(client.put(url, payload, format='json', HTTP_X_CSRFTOKEN=token).status_code, 200)
        self.assertEqual(client.delete(url).status_code, 403)
        self.assertEqual(client.delete(url, HTTP_X_CSRFTOKEN=token).status_code, 204)

    def test_homework_create_delete_with_page_token(self):
        client, token = self.pageClient(self.teacher,
            f'/teacher/teaching-classes/{self.tc.pk}/?tab=homework')
        payload = {'title': '新作业', 'startAt': timezone.now().isoformat(),
                   'deadline': (timezone.now() + timedelta(days=1)).isoformat(),
                   'problemIds': [self.problem.pk]}
        url = f'/api/teacher/teaching-classes/{self.tc.pk}/homeworks/'
        self.assertEqual(client.post(url, payload, format='json').status_code, 403)
        response = client.post(url, payload, format='json', HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 200)
        url = f'/api/teacher/homeworks/{response.data["id"]}/'
        self.assertEqual(client.delete(url).status_code, 403)
        self.assertEqual(client.delete(url, HTTP_X_CSRFTOKEN=token).status_code, 204)

    @mock.patch('teaching.submission_service.async_task')
    def test_student_submit_with_page_token(self, dispatch):
        client, token = self.pageClient(self.student, f'/student/homeworks/{self.homework.pk}/')
        payload = {'problemId': self.problem.pk, 'homeworkId': self.homework.pk,
                   'submitType': 'homework', 'language': 'c', 'code': 'int main(){return 0;}'}
        self.assertEqual(client.post('/api/submissions/', payload, format='json').status_code, 403)
        self.assertFalse(Submission.objects.exists())
        response = client.post('/api/submissions/', payload, format='json', HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 200)

    def test_student_change_password_with_page_token(self):
        client, token = self.pageClient(self.student, '/student/profile/')
        url = '/api/auth/change-password/'
        payload = {'oldPassword': 'Test12345', 'newPassword': 'NewPagePass123!'}
        self.assertEqual(client.post(url, payload, format='json').status_code, 403)
        response = client.post(url, payload, format='json', HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 200)
        self.student.refresh_from_db()
        self.assertTrue(self.student.check_password(payload['newPassword']))
        self.assertEqual(client.get('/api/auth/me/').status_code, 200)


@skipUnless(shutil.which('node'), 'JavaScript 交互测试需要 Node.js')
class PageJavascriptTests(SimpleTestCase):
    def test_csrf_helper_and_password_interaction(self):
        helper = render_to_string('teaching/api_request.html')
        profile = render_to_string('teaching/student_profile.html',
                                   {'user': User(username='page-student', name='学生'),
                                    'csrf_token': 'page-token'})
        scripts = re.findall(r'<script>(.*?)</script>', helper + profile, re.S)
        harness = r'''
const vm = require('node:vm');
const assert = require('node:assert/strict');
const scripts = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const elements = new Map();
const get = id => {
    if (!elements.has(id)) elements.set(id, {value: '', disabled: false, classList: {add() {}}});
    return elements.get(id);
};
const calls = [], alerts = [];
let response = {ok: true, status: 200, json: async () => ({message: '成功'})};
const document = {getElementById: get, querySelector: () => ({value: 'page-token'})};
Object.defineProperty(document, 'cookie', {get() {throw Error('不得读取 HttpOnly Cookie');}});
const ctx = vm.createContext({document, Headers, alert: text => alerts.push(text),
    fetch: async (url, options) => {calls.push({url, options}); return response;}});
scripts.forEach(script => vm.runInContext(script, ctx));
(async () => {
    for (const method of ['POST', 'PUT', 'PATCH', 'DELETE']) {
        await ctx.apiFetch('/api/test/', {method, headers: {'Content-Type': 'application/json'}});
        const call = calls.at(-1);
        assert.equal(call.options.headers.get('X-CSRFToken'), 'page-token');
        assert.equal(call.options.headers.get('Content-Type'), 'application/json');
        assert.equal(call.options.mode, 'same-origin');
    }
    await ctx.apiFetch('/api/test/');
    assert.equal(calls.at(-1).options.headers.has('X-CSRFToken'), false);
    get('oldPassword').value = 'Test12345';
    get('newPassword').value = get('confirmPassword').value = 'NewPagePass123!';
    response = {ok: false, status: 400, json: async () => ({error: '旧密码不正确'})};
    await ctx.changePassword();
    assert.equal(alerts.at(-1), '旧密码不正确');
    assert.equal(get('newPassword').value, 'NewPagePass123!');
    assert.equal(get('changePasswordButton').disabled, false);
    response = {ok: true, status: 200, json: async () => ({message: '成功'})};
    await ctx.changePassword();
    assert.equal(calls.at(-1).url, '/api/auth/change-password/');
    assert.equal(calls.at(-1).options.headers.get('X-CSRFToken'), 'page-token');
    assert.equal(JSON.parse(calls.at(-1).options.body).oldPassword, 'Test12345');
    for (const id of ['oldPassword', 'newPassword', 'confirmPassword']) assert.equal(get(id).value, '');
    assert.equal(get('changePasswordButton').disabled, false);
})().catch(error => {console.error(error); process.exitCode = 1;});
'''
        result = subprocess.run([shutil.which('node'), '-e', harness],
                                input=json.dumps(scripts), text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

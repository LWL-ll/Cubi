"""在独立 SQLite 文件中验证共享资料写入等待，不触碰业务库。"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from pathlib import Path
import sqlite3
import tempfile
from threading import Event
from unittest import skipUnless

from django.conf import settings
from rest_framework.exceptions import ValidationError
from django.db import connection, connections, transaction
from django.db.backends.sqlite3.base import DatabaseWrapper
from django.db.models import F
from django.test import TransactionTestCase

from academics.models import AcademicTerm, AdminClass, Course, CourseCollege, Department, Major, TeachingClass
from users.models import User

from .models import CourseMaterial
from .services import removeResource, saveMaterial


@skipUnless(connection.vendor == 'sqlite', 'SQLite 文件锁回归')
class SharedMaterialSqliteConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(username='shared-concurrency', password=None)
        department = Department.objects.create(code='SHARED-LOCK', name='并发学院')
        major = Major.objects.create(department=department, code='SHARED-LOCK', name='并发专业')
        adminClass = AdminClass.objects.create(name='并发班', major=major, enrollment_year=2026)
        self.course = Course.objects.create(code='SHARED-LOCK', name='并发课程')
        self.cc = CourseCollege.objects.create(course=self.course, department=department)
        self.cc.admin_classes.add(adminClass)
        AcademicTerm.objects.create(code='SHARED-LOCK', name='并发学期')
        self.tc = TeachingClass.objects.create(name='并发教学班', term='SHARED-LOCK', course_college=self.cc)
        self.tc.admin_classes.add(adminClass)
        self.material = CourseMaterial.objects.create(course_college=self.cc, title='原始资料',
            original_name='原始.txt', storage_name='not-an-actual-file.txt', size=1)
        root = Path(settings.PRIVATE_MATERIAL_ROOT)
        root.mkdir(exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(prefix='shared-sqlite-test-', dir=root)
        self.addCleanup(self.directory.cleanup)
        self.privateDb = Path(self.directory.name) / 'concurrency.sqlite3'

    def _runBehindWriter(self, operation, writerChange=None):
        # TransactionTestCase 不持有外层事务；备份的是已提交的临时测试数据库。
        self.assertFalse(connection.in_atomic_block)
        with closing(sqlite3.connect(self.privateDb)) as destination:
            connection.connection.backup(destination)
        config = deepcopy(connection.settings_dict)
        config['NAME'] = str(self.privateDb)
        config['OPTIONS'] = {**config['OPTIONS'], 'timeout': 5}
        writerHeld, updateAttempt, releaseWriter = Event(), Event(), Event()

        def installConnection():
            wrapper = DatabaseWrapper(deepcopy(config), alias='default')
            connections['default'] = wrapper
            return wrapper

        def writer():
            wrapper = installConnection()
            try:
                with transaction.atomic():
                    Course.objects.filter(pk=self.course.pk).update(name=F('name'))
                    if writerChange is not None:
                        writerChange()
                    writerHeld.set()
                    if not releaseWriter.wait(10):
                        raise AssertionError('没有收到释放写锁信号')
            finally:
                wrapper.close()

        def mutate():
            wrapper = installConnection()
            try:
                user = User.objects.get(pk=self.user.pk)
                cc = CourseCollege.objects.get(pk=self.cc.pk)
                obj = CourseMaterial.objects.get(pk=self.material.pk)

                def markUpdate(execute, sql, params, many, context):
                    if wrapper.in_atomic_block and sql.lstrip().upper().startswith('UPDATE'):
                        updateAttempt.set()
                    return execute(sql, params, many, context)

                with wrapper.execute_wrapper(markUpdate):
                    operation(user, cc, obj)
            finally:
                wrapper.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(writer)
            try:
                self.assertTrue(writerHeld.wait(10), '写锁未建立')
                second = pool.submit(mutate)
                self.assertTrue(updateAttempt.wait(10), '共享资料写入未开始')
            finally:
                releaseWriter.set()
            first.result(timeout=10)
            second.result(timeout=10)

        with closing(sqlite3.connect(self.privateDb)) as private:
            return private.execute('SELECT title FROM course_team_coursematerial WHERE id = ?',
                                   [self.material.pk]).fetchone()

    def test_metadata_update_waits_for_other_writer_without_lock_upgrade_error(self):
        result = self._runBehindWriter(lambda user, cc, obj: saveMaterial(
            user, cc, {'title': '并发修改成功'}, obj=obj))
        self.assertEqual(result, ('并发修改成功',))

    def test_selected_material_delete_waits_for_other_writer_without_lock_upgrade_error(self):
        self.material.visibility = 'classes'
        self.material.save()
        self.material.visible_classes.add(self.tc)
        result = self._runBehindWriter(lambda user, cc, obj: removeResource(
            user, obj, 'material:class_visible'))
        self.assertIsNone(result)

    def test_visibility_changed_while_waiting_requires_refresh(self):
        self.material.visibility = 'classes'
        self.material.save()
        self.material.visible_classes.add(self.tc)

        def changeScope():
            CourseMaterial.objects.filter(pk=self.material.pk).update(visibility='course')
            CourseMaterial.objects.get(pk=self.material.pk).visible_classes.clear()

        with self.assertRaises(ValidationError):
            self._runBehindWriter(lambda user, cc, obj: saveMaterial(
                user, cc, {'visibility': 'course', 'title': '不可覆盖'}, obj=obj), changeScope)
        with closing(sqlite3.connect(self.privateDb)) as private:
            self.assertEqual(private.execute(
                'SELECT title, visibility FROM course_team_coursematerial WHERE id = ?',
                [self.material.pk]).fetchone(), ('原始资料', 'course'))

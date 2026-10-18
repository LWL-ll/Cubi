"""AsyncTask 模型测试：状态默认值、索引存在、str 输出

模型是「提交→轮询」模式的地基（docs/异步任务设计.md 第四章），
字段语义与索引约定必须以测试固化，防止后续改动破坏前端轮询协议。
"""
from django.test import TestCase

from tasks.models import AsyncTask
from users.models import User

PASSWORD = 'Test12345'


class AsyncTaskModelTests(TestCase):
    """AsyncTask 模型基础行为（状态默认值 / 索引 / str 输出）"""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username='T9001', password=PASSWORD)

    def test_statusDefaultsToPending(self):
        """新任务状态默认 pending，进度与总量默认 0"""
        task = AsyncTask.objects.create(
            type=AsyncTask.TYPE_IMPORT, created_by=self.user)
        self.assertEqual(task.status, AsyncTask.STATUS_PENDING)
        self.assertEqual(task.progress, 0)
        self.assertEqual(task.total, 0)

    def test_indexesExist(self):
        """高频查询索引存在：某用户近期任务、按状态过滤（限流检查用）"""
        indexFields = [
            list(index.fields) for index in AsyncTask._meta.indexes
        ]
        self.assertIn(['created_by', 'created_at'], indexFields)
        self.assertIn(['status'], indexFields)

    def test_strShowsTypeStatusAndId(self):
        """str 输出包含类型中文名、主键与状态中文名（admin 排障用）"""
        task = AsyncTask.objects.create(
            type=AsyncTask.TYPE_IMPORT, created_by=self.user)
        self.assertEqual(str(task), f'批量导入 #{task.pk}（等待中）')

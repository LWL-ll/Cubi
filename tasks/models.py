from django.conf import settings
from django.db import models


class AsyncTask(models.Model):
    """异步任务统一记录：提交→轮询模式的地基

    一次异步操作一条记录：提交时创建（pending），worker 执行时更新
    （running → done/failed），前端轮询本表获知进度与结果。
    """
    STATUS_PENDING = 'pending'
    STATUS_RUNNING = 'running'
    STATUS_DONE = 'done'
    STATUS_FAILED = 'failed'
    STATUS_CHOICES = [
        (STATUS_PENDING, '等待中'),
        (STATUS_RUNNING, '执行中'),
        (STATUS_DONE, '已完成'),
        (STATUS_FAILED, '失败'),
    ]

    TYPE_IMPORT = 'import'            # 批量导入（student/teacher/course 共用）
    TYPE_AI = 'ai_analyze'            # AI 批改分析（后续轮次）
    TYPE_REMIND = 'remind'            # 作业提醒（后续轮次）
    TYPE_CHOICES = [
        (TYPE_IMPORT, '批量导入'),
        (TYPE_AI, 'AI批改'),
        (TYPE_REMIND, '作业提醒'),
    ]

    type = models.CharField('任务类型', max_length=20, choices=TYPE_CHOICES)
    status = models.CharField('状态', max_length=10, choices=STATUS_CHOICES,
                              default=STATUS_PENDING)
    total = models.PositiveIntegerField('总量', default=0)   # 总行数等（预检时算好，供进度计算）
    progress = models.PositiveIntegerField('进度', default=0)  # 0-100
    result = models.JSONField('结果', null=True, blank=True)  # 成功结果（successCount/errors）
    error = models.TextField('错误信息', blank=True)          # 失败原因（已脱敏，前端可见）
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, models.CASCADE,
                                   verbose_name='提交人',
                                   related_name='async_tasks')
    created_at = models.DateTimeField('提交时间', auto_now_add=True)
    started_at = models.DateTimeField('开始时间', null=True, blank=True)
    finished_at = models.DateTimeField('结束时间', null=True, blank=True)

    class Meta:
        verbose_name = '异步任务'
        verbose_name_plural = '异步任务'
        ordering = ['-created_at']
        indexes = [
            # 高频查询：某用户的近期任务、排队中的任务（限流检查用）
            models.Index(fields=['created_by', 'created_at']),
            models.Index(fields=['status']),
        ]

    def __str__(self):
        return f'{self.get_type_display()} #{self.pk}（{self.get_status_display()}）'

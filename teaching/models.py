"""教学业务数据模型

V2 教学班级结构（docs/教学班级设计.md）：教师/作业/通知/资料挂教学班，
学生经「行政班 → 教学班」推导归属。

OJ 判题五张表（docs/OJ对接设计.md 第九章）：
- Problem 题目（题面 + 测试点 JSON + 可选 spj，两级题库）
- Homework 作业（教学班 × 题目集合 + 起止时间）
- HomeworkProblem 作业-题目中间表（题序 + 选题时修订号）
- Submission 提交（三种类型合一 + 资格快照 + 判题结果）
- JudgeTask 判题执行表（租约状态机，独立于通用 AsyncTask）

核心设计（第九章要点）：
- 题目修改即 revision+1；提交受理时冻结完整判题快照（judge_snapshot），
  教师改题不改变已受理提交的判题标准，结果可复现
- 测试用例 JSON 存 DB 永不下发（隐藏测试数据不经过前端与 API 响应）
"""
from django.conf import settings
from django.db import models

from academics.models import TeachingClass


class Problem(models.Model):
    """题目：题面 markdown + 测试用例 JSON + 可选 spj

    题库两级归属（docs/OJ对接设计.md 12.1）：
    - is_public=True：课程组公共题库（question_bank:manage 管理）
    - is_public=False：教师个人题库（homework:bank 管理，owner 为教师本人）
    """

    TIER_BEGINNER = 'beginner'
    TIER_BASIC = 'basic'
    TIER_ADVANCED = 'advanced'
    TIER_CHOICES = [
        (TIER_BEGINNER, '入门'),
        (TIER_BASIC, '基础'),
        (TIER_ADVANCED, '进阶'),
    ]

    COMPARE_EXACT = 'exact'
    COMPARE_TRAILING = 'trailing'
    COMPARE_LOOSE = 'loose'
    COMPARE_CHOICES = [
        (COMPARE_EXACT, '精确比对'),
        (COMPARE_TRAILING, '忽略行尾空白'),
        (COMPARE_LOOSE, '忽略全部空白'),
    ]

    LANGUAGE_C = 'c'
    LANGUAGE_PYTHON = 'python'
    LANGUAGE_CHOICES = [
        (LANGUAGE_C, 'C'),
        (LANGUAGE_PYTHON, 'Python'),
    ]

    title = models.CharField('标题', max_length=100)
    # 题面必须是剔除受保护代码块后重建的 markdown（保密链，见 OJ对接设计.md 14.7）
    statement_md = models.TextField('题面 markdown')
    samples = models.JSONField('公开样例', default=list)      # [{input, output}]，可下发
    tests = models.JSONField('隐藏测试点', default=list)      # [{input, output, points}]，永不下发
    languages = models.JSONField('允许语言', default=list)    # ['c', 'python']
    tier = models.CharField('资源档位', max_length=10, choices=TIER_CHOICES,
                            default=TIER_BASIC)
    compare_strategy = models.CharField('比对策略', max_length=10,
                                        choices=COMPARE_CHOICES,
                                        default=COMPARE_EXACT)
    has_spj = models.BooleanField('特殊判定', default=False)
    spj_code = models.TextField('spj 源码（Python）', blank=True)
    revision = models.PositiveIntegerField('修订号', default=1)  # 每次修改题目 +1
    # 教师删号题目保留（历史提交引用）：SET_NULL 保关联、题目转无主
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, models.SET_NULL,
                              null=True, verbose_name='归属教师',
                              related_name='owned_problems')
    is_public = models.BooleanField('公共题库', default=False)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        verbose_name = '题目'
        verbose_name_plural = '题目'
        ordering = ['-updated_at']

    def __str__(self):
        return f'{self.title}（v{self.revision}）'


class Homework(models.Model):
    """作业：教学班 × 题目集合 + 起止时间（practice 提交无此上下文）

    teaching_class PROTECT：有作业的教学班不可删除，保护历史数据。
    created_by SET_NULL：发布教师删号后作业保留。
    """

    teaching_class = models.ForeignKey(TeachingClass, models.PROTECT,
                                       verbose_name='教学班',
                                       related_name='homeworks')
    title = models.CharField('作业标题', max_length=100)
    start_at = models.DateTimeField('开始时间')
    deadline = models.DateTimeField('截止时间')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, models.SET_NULL,
                                   null=True, verbose_name='发布教师',
                                   related_name='published_homeworks')
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        verbose_name = '作业'
        verbose_name_plural = '作业'
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.title}（{self.teaching_class.name}）'


class HomeworkProblem(models.Model):
    """作业-题目中间表：承载题目顺序与作业期版本快照

    problem_revision 是「选题时」的题目修订号快照——作业引用的是
    当时的题目版本，之后教师改题不影响作业展示与判题口径。
    problem PROTECT：已被作业引用的题目不可删除。
    """

    homework = models.ForeignKey(Homework, models.CASCADE,
                                 verbose_name='作业', related_name='problems')
    problem = models.ForeignKey(Problem, models.PROTECT,
                                verbose_name='题目', related_name='homeworks')
    order = models.PositiveSmallIntegerField('题序', default=0)
    problem_revision = models.PositiveIntegerField('选题时修订号')

    class Meta:
        verbose_name = '作业题目'
        verbose_name_plural = '作业题目'
        ordering = ['order']
        constraints = [
            # 同一题目在一份作业中只出现一次
            models.UniqueConstraint(fields=['homework', 'problem'],
                                    name='uniq_homework_problem'),
            # 作业内题序不重复（重排时先腾位再写，业务层处理）
            models.UniqueConstraint(fields=['homework', 'order'],
                                    name='uniq_homework_order'),
        ]

    def __str__(self):
        return f'{self.homework.title} #{self.order} {self.problem.title}'


class Submission(models.Model):
    """提交：三种类型合一（practice/homework，exam 留枚举位二期）

    资格快照（受理时冻结，判题只用快照、不重查实时题目，7.3 节）：
    - judge_snapshot：判题数据完整副本 {tests, spj_code, tier,
      compare_strategy}——判题任务零实时查题，教师改题不影响排队提交
    - problem_revision + tests_hash：审计用（不可复现时定位）
    problem SET_NULL：允许删题；判题时题目缺失置 system_error（7.3 节）。
    """

    SUBMIT_PRACTICE = 'practice'
    SUBMIT_HOMEWORK = 'homework'
    SUBMIT_EXAM = 'exam'          # 留位，二期启用
    SUBMIT_CHOICES = [
        (SUBMIT_PRACTICE, '练习'),
        (SUBMIT_HOMEWORK, '作业'),
        (SUBMIT_EXAM, '考试'),
    ]

    STATUS_QUEUED = 'queued'
    STATUS_RUNNING = 'running'
    STATUS_ACCEPTED = 'accepted'
    STATUS_WRONG_ANSWER = 'wrong_answer'
    STATUS_COMPILE_ERROR = 'compile_error'
    STATUS_TLE = 'time_limit_exceeded'
    STATUS_MLE = 'memory_limit_exceeded'
    STATUS_OLE = 'output_limit_exceeded'
    STATUS_RUNTIME_ERROR = 'runtime_error'
    STATUS_REPEAT = 'repeat'
    STATUS_SYSTEM_ERROR = 'system_error'
    STATUS_CHOICES = [
        (STATUS_QUEUED, '排队中'),
        (STATUS_RUNNING, '判题中'),
        (STATUS_ACCEPTED, '通过'),
        (STATUS_WRONG_ANSWER, '答案错误'),
        (STATUS_COMPILE_ERROR, '编译错误'),
        (STATUS_TLE, '超时'),
        (STATUS_MLE, '内存超限'),
        (STATUS_OLE, '输出超限'),
        (STATUS_RUNTIME_ERROR, '运行错误'),
        (STATUS_REPEAT, '重复提交'),
        (STATUS_SYSTEM_ERROR, '系统错误'),
    ]

    # 终态集合：状态机流转的边界（repeat 为同步拦截终态，不进判题）
    TERMINAL_STATUSES = frozenset([
        STATUS_ACCEPTED, STATUS_WRONG_ANSWER, STATUS_COMPILE_ERROR,
        STATUS_TLE, STATUS_MLE, STATUS_OLE, STATUS_RUNTIME_ERROR,
        STATUS_REPEAT, STATUS_SYSTEM_ERROR,
    ])

    user = models.ForeignKey(settings.AUTH_USER_MODEL, models.CASCADE,
                             verbose_name='提交人', related_name='submissions')
    problem = models.ForeignKey(Problem, models.SET_NULL, null=True,
                                verbose_name='题目', related_name='submissions')
    homework = models.ForeignKey(Homework, models.PROTECT, null=True,
                                 verbose_name='作业', related_name='submissions')
    language = models.CharField('语言', max_length=10,
                                choices=Problem.LANGUAGE_CHOICES)
    code = models.TextField('代码原文')  # 重复检测 + 教师批改要看
    fingerprint = models.CharField('统一换行后原文 SHA256', max_length=64)
    submit_type = models.CharField('提交类型', max_length=10,
                                   choices=SUBMIT_CHOICES,
                                   default=SUBMIT_PRACTICE)
    status = models.CharField('状态', max_length=22, choices=STATUS_CHOICES,
                              default=STATUS_QUEUED)
    score = models.PositiveIntegerField('得分', default=0)
    total_score = models.PositiveIntegerField('题目总分', default=0)
    time_used = models.BigIntegerField('最大单点耗时（纳秒）', null=True)  # 学情分析原料
    memory_used = models.BigIntegerField('最大单点内存（byte）', null=True)
    error_detail = models.TextField('错误详情（脱敏）', blank=True)
    repeat_of = models.ForeignKey('self', models.SET_NULL, null=True,
                                  verbose_name='重复于哪次提交',
                                  related_name='repeats')
    # ---- 资格快照（受理时冻结，判题用它而非实时题目）----
    problem_revision = models.PositiveIntegerField('题目修订号快照')
    tests_hash = models.CharField('测试数据哈希', max_length=64)
    judge_snapshot = models.JSONField('判题数据快照')
    created_at = models.DateTimeField('提交时间', auto_now_add=True)
    judged_at = models.DateTimeField('判题完成时间', null=True, blank=True)

    class Meta:
        verbose_name = '提交'
        verbose_name_plural = '提交'
        ordering = ['-created_at']
        indexes = [
            # 频率检测（10s·3次·429，7.1 节）与「我的提交」列表
            models.Index(fields=['user', 'created_at']),
            # 重复检测窗口：同题同人最近 20 条
            models.Index(fields=['problem', 'user']),
        ]
        constraints = [
            # DB 层防非法组合：挂作业的提交必须是作业类型
            models.CheckConstraint(
                condition=(models.Q(homework__isnull=True)
                           | models.Q(submit_type='homework')),
                name='chk_homework_submit_type'),
        ]

    def __str__(self):
        return f'#{self.pk} {self.user.username} → {self.get_status_display()}'


class JudgeTask(models.Model):
    """判题执行表（独立于通用 AsyncTask：租约字段为判题独有）

    租约状态机（docs/OJ对接设计.md 第八章）：
    Q2 long 集群管「至少一次投递」，本表租约管「至多一次生效」——
    并发 worker 领取靠原子条件 UPDATE（attempt_id + heartbeat）。
    前端轮询的唯一来源是 Submission 详情接口（progress 从本表读）。
    """

    STATUS_PENDING = 'pending'      # 待投递/已投递未领取
    STATUS_LEASED = 'leased'        # 已领取（attempt_id 标识当前租约）
    STATUS_DONE = 'done'            # 终态
    STATUS_RETRYABLE = 'retryable'  # 租约回收，可重新投递
    STATUS_FAILED = 'failed'        # 重试耗尽
    STATUS_CHOICES = [
        (STATUS_PENDING, '等待中'),
        (STATUS_LEASED, '执行中'),
        (STATUS_DONE, '已完成'),
        (STATUS_RETRYABLE, '可重试'),
        (STATUS_FAILED, '失败'),
    ]

    submission = models.OneToOneField(Submission, models.CASCADE,
                                      verbose_name='提交',
                                      related_name='judge_task')
    status = models.CharField('状态', max_length=10, choices=STATUS_CHOICES,
                              default=STATUS_PENDING)
    attempt_id = models.PositiveIntegerField('尝试号', default=0)
    q2_task_id = models.CharField('q2 投递标识', max_length=64, blank=True)
    # 编译产物 fileId：判后即删，租约回收时按此字段清理残留（可审计）
    file_id = models.CharField('编译产物 fileId', max_length=32, blank=True)
    progress = models.PositiveSmallIntegerField('进度 0-100', default=0)
    error = models.TextField('执行错误（脱敏）', blank=True)
    claimed_at = models.DateTimeField('领取时间', null=True, blank=True)
    heartbeat_at = models.DateTimeField('心跳时间', null=True, blank=True)
    started_at = models.DateTimeField('开始时间', null=True, blank=True)
    finished_at = models.DateTimeField('结束时间', null=True, blank=True)

    class Meta:
        verbose_name = '判题任务'
        verbose_name_plural = '判题任务'
        ordering = ['id']
        indexes = [
            # 租约回收扫描：pending 超时未投递、leased 心跳过期
            models.Index(fields=['status']),
        ]

    def __str__(self):
        return f'判题任务 #{self.submission_id}（{self.get_status_display()}）'

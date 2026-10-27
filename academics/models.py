from django.conf import settings
from django.db import models


class Department(models.Model):
    """学院：学生导入时按学号格式解析自动生成（get_or_create），无单独导入入口

    V2：原 College 升级——加业务编码 code 与启用开关（学校改名时关系稳定），
    详见 docs/design/数据库表重构设计.md
    """
    code = models.CharField('学院编码', max_length=32, unique=True)
    name = models.CharField('学院名称', max_length=100)
    is_active = models.BooleanField('是否启用', default=True)

    class Meta:
        verbose_name = '学院'
        verbose_name_plural = '学院'

    def __str__(self):
        return self.name


class Major(models.Model):
    """专业：学院下的专业（V2 新增层，行政班挂专业下而非直接挂学院）"""
    department = models.ForeignKey(
        Department, verbose_name='所属学院', on_delete=models.PROTECT,
        related_name='majors')
    code = models.CharField('专业编码', max_length=32)
    name = models.CharField('专业名称', max_length=100)
    is_active = models.BooleanField('是否启用', default=True)

    class Meta:
        unique_together = [('department', 'code')]
        verbose_name = '专业'
        verbose_name_plural = '专业'

    def __str__(self):
        return self.name


class AdminClass(models.Model):
    """行政班级（学籍概念）：学生经 ClassMembership 挂班，教师任课挂教学班

    V2：原 Clazz 重构——挂专业、加年级与编码，
    移除 students/teachers 两个 M2M（学生关系走成员表、教师任课挂教学班）
    """
    name = models.CharField('班级名称', max_length=50)
    major = models.ForeignKey(
        Major, verbose_name='所属专业', blank=True,
        on_delete=models.PROTECT, related_name='admin_classes')
    enrollment_year = models.SmallIntegerField('年级', null=True, blank=True)
    code = models.CharField('班级编码', max_length=32, blank=True)
    is_active = models.BooleanField('是否启用', default=True)

    class Meta:
        # 不同年级可能出现相同班级名，用 专业+年级+名称 联合唯一
        unique_together = [('major', 'enrollment_year', 'name')]
        verbose_name = '行政班级'
        verbose_name_plural = '行政班级'

    def __str__(self):
        return self.name


class ClassMembership(models.Model):
    """学生-行政班成员关系：left_at 为空表示当前在班（转班历史留痕）

    条件唯一约束保证同一学生同时只有一个当前成员关系（SQLite/PG）。
    """
    student_profile = models.ForeignKey(
        'users.StudentProfile', verbose_name='学生', on_delete=models.CASCADE,
        related_name='memberships')
    admin_class = models.ForeignKey(
        AdminClass, verbose_name='行政班级', on_delete=models.CASCADE,
        related_name='memberships')
    joined_at = models.DateField('入班日期')
    left_at = models.DateField('离班日期', null=True, blank=True)

    class Meta:
        verbose_name = '班级成员'
        verbose_name_plural = '班级成员'
        constraints = [
            models.UniqueConstraint(
                fields=['student_profile'], condition=models.Q(left_at__isnull=True),
                name='uniq_current_student_membership'),
            models.CheckConstraint(
                condition=models.Q(left_at__isnull=True) | models.Q(left_at__gte=models.F('joined_at')),
                name='membership_dates_ordered'),
        ]

    def __str__(self):
        return f'{self.student_profile.identity.school_number} → {self.admin_class.name}'


class Course(models.Model):
    """课程：教务导入课程后，按 课程 -> 学院 -> 教学班 三层结构设置"""
    code = models.CharField('课程编号', max_length=20, unique=True)
    name = models.CharField('课程名称', max_length=50)
    is_active = models.BooleanField('是否启用', default=True)
    # 课程开设的学院（与 CourseCollege 关联表配套使用）
    departments = models.ManyToManyField(
        Department, through='CourseCollege', related_name='courses',
        verbose_name='开设学院')

    class Meta:
        verbose_name = '课程'
        verbose_name_plural = '课程'

    def __str__(self):
        return f'{self.name}（{self.code}）'


class CourseCollege(models.Model):
    """课程×学院 关联表：课程组组长、本课程教师池、可组班的行政班候选池都挂在这一层"""
    course = models.ForeignKey(
        Course, on_delete=models.CASCADE, related_name='course_colleges',
        verbose_name='课程')
    department = models.ForeignKey(
        Department, blank=True, on_delete=models.CASCADE,
        related_name='course_colleges', verbose_name='学院')
    # 课程组组长（教务用工号指定；指定后自动加入“课程组”分组）
    leader = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name='课程组组长', null=True,
        blank=True, on_delete=models.SET_NULL, related_name='led_course_colleges')
    # 本课程教师池（导入后自动分配到本课程已关联的行政班）
    teachers = models.ManyToManyField(
        settings.AUTH_USER_MODEL, verbose_name='本课程教师', blank=True,
        related_name='course_college_teach')
    # 本课程×学院下可组教学班的行政班候选池
    admin_classes = models.ManyToManyField(
        AdminClass, verbose_name='本课程行政班', blank=True,
        related_name='course_colleges')

    class Meta:
        # 同一课程在同一学院只能有一条设置记录
        unique_together = [('course', 'department')]
        verbose_name = '课程学院'
        verbose_name_plural = '课程学院'

    def __str__(self):
        return f'{self.course.name} - {self.department.name}'

    def save(self, *args, **kwargs):
        from django.contrib.auth import get_user_model
        from django.db import router, transaction
        from .services import syncCourseTeamMembership

        using = kwargs.get('using') or router.db_for_write(type(self), instance=self)
        with transaction.atomic(using=using):
            # 先串行同课程的更新，再按用户锁定跨课程的任职变更。
            Course.objects.using(using).filter(pk=self.course_id).update(id=models.F('id'))
            oldLeaderId = type(self).objects.using(using).filter(pk=self.pk) \
                .values_list('leader_id', flat=True).first()
            leaderIds = sorted({uid for uid in (oldLeaderId, self.leader_id) if uid})
            for uid in leaderIds:
                get_user_model().objects.using(using).filter(pk=uid).update(id=models.F('id'))
            super().save(*args, **kwargs)
            newLeaderId = type(self).objects.using(using).filter(pk=self.pk) \
                .values_list('leader_id', flat=True).get()
            if oldLeaderId != newLeaderId:
                if newLeaderId:
                    self.teachers.add(newLeaderId)
                for uid in leaderIds:
                    syncCourseTeamMembership(uid, using)


class TeachingClass(models.Model):
    """教学班（授课单位）：由 1~N 个行政班按学期组成（行政班是原子单位，不拆分）

    设计详见 docs/design/教学班级设计.md：学生不直接挂教学班，
    归属由「学生 → 成员关系 → 行政班 → 教学班」推导，
    对象级校验：教师查 teachers、学生经行政班三跳推导
    """
    name = models.CharField('教学班名称', max_length=50)
    term = models.CharField('学期', max_length=20)
    course_college = models.ForeignKey(
        CourseCollege, verbose_name='课程×学院', on_delete=models.PROTECT,
        related_name='teaching_classes')
    admin_classes = models.ManyToManyField(
        AdminClass, verbose_name='组成行政班', blank=True,
        related_name='teaching_classes')
    teachers = models.ManyToManyField(
        settings.AUTH_USER_MODEL, verbose_name='授课教师', blank=True,
        related_name='teaching_classes')

    class Meta:
        # 同课程同学期教学班名唯一
        unique_together = [('course_college', 'term', 'name')]
        verbose_name = '教学班'
        verbose_name_plural = '教学班'

    def __str__(self):
        return f'{self.name}（{self.term}）'

    def save(self, *args, **kwargs):
        from django.db import router, transaction
        from .services import lockTeachingCourse, validateTeachingClasses
        from .term_service import requireTermWrite

        using = kwargs.get('using') or router.db_for_write(type(self), instance=self)
        with transaction.atomic(using=using):
            previous = type(self).objects.using(using).filter(pk=self.pk).values_list('term', flat=True).first()
            for code in sorted({code for code in (previous, self.term) if code}):
                requireTermWrite(code, using=using)
            lockTeachingCourse(self.course_college, using)
            if self.pk:
                validateTeachingClasses(
                    self.course_college, self.term, self.admin_classes.all(),
                    excludeId=self.pk, using=using)
            super().save(*args, **kwargs)


class AcademicTerm(models.Model):
    """学期登记；教学班继续使用 code 字符串以保持原接口兼容。"""
    OPEN = 'open'
    CLOSED = 'closed'
    ARCHIVED = 'archived'
    code = models.CharField('学期编码', max_length=20, unique=True)
    name = models.CharField('学期名称', max_length=50)
    start_date = models.DateField('开始日期', null=True, blank=True)
    end_date = models.DateField('结束日期', null=True, blank=True)
    status = models.CharField('状态', max_length=12, default=OPEN, choices=[
        (OPEN, '开放'), (CLOSED, '已关闭'), (ARCHIVED, '已归档')])
    is_current = models.BooleanField('当前学期', default=False)
    closed_at = models.DateTimeField('关闭时间', null=True, blank=True)
    archived_at = models.DateTimeField('归档时间', null=True, blank=True)

    class Meta:
        ordering = ['-code']
        constraints = [
            models.UniqueConstraint(fields=['is_current'], condition=models.Q(is_current=True),
                                    name='unique_current_academic_term'),
            models.CheckConstraint(condition=(models.Q(start_date__isnull=True,
                                                       end_date__isnull=True)
                | models.Q(start_date__isnull=False, end_date__isnull=False,
                           end_date__gte=models.F('start_date'))),
                                   name='academic_term_dates_ordered'),
            models.CheckConstraint(condition=models.Q(is_current=False) | models.Q(status='open'),
                                   name='current_academic_term_open'),
        ]

    def __str__(self):
        return self.name


class TeachingClassRecord(models.Model):
    """关闭时冻结名单，归档时冻结逐作业成绩；保留原始提交供追溯。"""
    teaching_class = models.OneToOneField(
        TeachingClass, on_delete=models.PROTECT, related_name='term_record')
    roster = models.JSONField(default=list)
    gradebooks = models.JSONField(default=dict)
    closed_at = models.DateTimeField()
    archived_at = models.DateTimeField(null=True, blank=True)


class AuditEvent(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                              null=True, related_name='audit_events')
    actor_username = models.CharField(max_length=150)
    action = models.CharField(max_length=100)
    object_type = models.CharField(max_length=100)
    object_id = models.CharField(max_length=64)
    before = models.JSONField(default=dict)
    after = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-id']
        indexes = [models.Index(fields=['action', 'created_at'], name='audit_action_time_idx'),
                   models.Index(fields=['object_type', 'object_id'], name='audit_object_idx')]

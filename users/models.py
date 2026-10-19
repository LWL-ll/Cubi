from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """自定义用户模型：教务/课程组/教师/学生四类账号共用一张表

    - username：登录账号，存学号或工号原值（数据库唯一约束防撞号）
    - 角色划分用 Django 原生 Group（教务/课程组/教师/学生四个分组），
      不在模型上加 role 字段：分组在 admin 后台可直接管理，
      课程组组长以后由教务把教师加入"课程组"分组实现
    """
    # 真实姓名：学情分析、通知等业务功能需要显示
    name = models.CharField('姓名', max_length=50, blank=True)
    # 首次登录强制改密：教务导入创建时置 True，改密成功后置 False；
    # 全局兜底拦截在 users/middleware.py（不能只靠前端跳转）
    must_change_password = models.BooleanField('首次登录须改密', default=False)

    class Meta:
        verbose_name = '用户'
        verbose_name_plural = '用户'
        # 注册全部细粒度权限点（2026-10-20 公共题库改为系统管理员独占，
        # 删除 question_bank_manage，共 22 个）
        # 注意：教师的所有权限在业务接口里还要做「仅限教学班级」的对象级校验
        permissions = [
            # ---- 教务 ----
            ('class_manage', '全校班级增删改查'),
            ('class_assign_teacher', '分配教师到教学班'),
            ('course_manage', '全校课程增删改查'),
            ('course_assign_class', '分配班级到课程'),
            ('user_assign_role', '指定课程组组长'),
            # ---- 课程组（2026-10-13 按《课题组.xmind》重新设计）----
            ('knowledge_manage', 'AI Agent知识库增删改查'),
            ('material_class_visible', '课程下班级资料增删改查'),
            ('chapter_manage', '课程章节设计'),
            # ---- 教师（仅限教学班级）----
            ('notice_manage', '班级通知增删改查'),
            ('analysis_view', '班级学情分析图表查看'),
            ('material_class_manage', '班级内部资料增删改查'),
            ('homework_publish', '作业发布'),
            ('homework_bank', '作业题库管理'),
            ('homework_remind', '作业提醒'),
            ('homework_comment', '作业评语增删改查'),
            ('homework_answer', '作业答案改查'),
            ('homework_ai', 'Agent分析学生改查'),
            # ---- 学生 ----
            ('homework_view', '作业查看与提交'),
            ('notice_view', '查看班级通知'),
            ('material_view', '查看班级资料'),
            ('analysis_view_self', '查看自己的学生画像'),
            # ---- 通用 ----
            ('profile_edit', '修改个人资料'),
        ]

    def __str__(self):
        return f'{self.name or self.username}（{self.username}）'


class SchoolIdentity(models.Model):
    """学校身份：平台账号与学号/工号的绑定（V2 账号与身份分离）

    - User 只管登录，学号/工号只是绑定的学校身份
    - 封闭式下由教务导入直接创建并置为 verified；
      将来开放注册后走 pending → 审核流程
    - 详见 docs/数据库表重构设计.md
    """
    IDENTITY_STUDENT = 'student'
    IDENTITY_TEACHER = 'teacher'

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name='平台账号',
        on_delete=models.CASCADE, related_name='identities')
    school_code = models.CharField('学校代码', max_length=32, default='NSU')
    school_number = models.CharField('学号/工号', max_length=32)
    identity_type = models.CharField(
        '身份类型', max_length=16,
        choices=[(IDENTITY_STUDENT, '学生'), (IDENTITY_TEACHER, '教师')])
    verification_status = models.CharField('核验状态', max_length=16, default='verified')
    verified_at = models.DateTimeField('核验时间', null=True, blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        verbose_name = '学校身份'
        verbose_name_plural = '学校身份'
        # 同一学工号只能绑定一个账号；一人一校一类型一条身份
        unique_together = [
            ('school_code', 'identity_type', 'school_number'),
            ('user', 'school_code', 'identity_type'),
        ]

    def __str__(self):
        return f'{self.school_number}（{self.get_identity_type_display()}）'


class StudentProfile(models.Model):
    """学生资料：只存学生特有的学籍属性（账号在 User、学号在 SchoolIdentity）"""
    ACADEMIC_ACTIVE = '在读'
    ACADEMIC_SUSPENDED = '休学'
    ACADEMIC_GRADUATED = '毕业'
    ACADEMIC_DROPPED = '退学'

    identity = models.OneToOneField(
        SchoolIdentity, verbose_name='学校身份', on_delete=models.CASCADE,
        related_name='student_profile')
    major = models.ForeignKey(
        'academics.Major', verbose_name='所属专业', blank=True,
        on_delete=models.PROTECT, related_name='student_profiles')
    enrollment_year = models.SmallIntegerField('入学年份')
    academic_status = models.CharField(
        '学籍状态', max_length=20, default=ACADEMIC_ACTIVE,
        choices=[(ACADEMIC_ACTIVE, '在读'), (ACADEMIC_SUSPENDED, '休学'),
                 (ACADEMIC_GRADUATED, '毕业'), (ACADEMIC_DROPPED, '退学')])
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('修改时间', auto_now=True)

    class Meta:
        verbose_name = '学生资料'
        verbose_name_plural = '学生资料'

    def __str__(self):
        return f'{self.identity.school_number} 学生资料'


class TeacherProfile(models.Model):
    """教师资料：教职档案（所属学院只是档案属性，课程权限由任课关系决定）"""
    identity = models.OneToOneField(
        SchoolIdentity, verbose_name='学校身份', on_delete=models.CASCADE,
        related_name='teacher_profile')
    department = models.ForeignKey(
        'academics.Department', verbose_name='所属学院', null=True, blank=True,
        on_delete=models.PROTECT, related_name='teacher_profiles')
    title = models.CharField('职称', max_length=50, blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('修改时间', auto_now=True)

    class Meta:
        verbose_name = '教师资料'
        verbose_name_plural = '教师资料'

    def __str__(self):
        return f'{self.identity.school_number} 教师资料'

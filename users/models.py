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

    class Meta:
        verbose_name = '用户'
        verbose_name_plural = '用户'
        # 注册全部细粒度权限点（按 xmind《用户权限》逐条翻译，共 18 个）
        permissions = [
            # ---- 教务 ----
            ('class_manage', '全校班级增删改查'),
            ('class_assign_teacher', '分配教师到班级'),
            ('course_manage', '全校课程增删改查'),
            ('course_assign_class', '分配班级到课程'),
            ('user_assign_role', '指定课程组组长'),
            # ---- 课程组 ----
            ('material_manage', '资料管理增删改查'),
            ('knowledge_manage', '知识库管理'),
            ('question_bank_manage', '题库管理增删改查'),
            ('chapter_manage', '课程章节设置'),
            # ---- 教师 ----
            ('notice_manage', '班级通知增删改查'),
            ('analysis_view', '学情分析查看'),
            ('homework_publish', '作业发布'),
            ('homework_bank', '作业题库管理'),
            ('homework_remind', '作业提醒'),
            ('homework_collect', '作业回收'),
            ('homework_ai', 'AI分析学生'),
            # ---- 学生 ----
            ('homework_view', '作业查看与提交'),
            # ---- 通用 ----
            ('profile_edit', '修改个人资料'),
        ]

    def __str__(self):
        return f'{self.name or self.username}（{self.username}）'

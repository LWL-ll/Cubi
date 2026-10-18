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
        # 注册全部细粒度权限点（教师权限按《教师.xmind》2026-10-13 重新设计，共 21 个）
        # 注意：教师的所有权限在业务接口里还要做「仅限教学班级」的对象级校验
        permissions = [
            # ---- 教务 ----
            ('class_manage', '全校班级增删改查'),
            ('class_assign_teacher', '分配教师到班级'),
            ('course_manage', '全校课程增删改查'),
            ('course_assign_class', '分配班级到课程'),
            ('user_assign_role', '指定课程组组长'),
            # ---- 课程组（2026-10-13 按《课题组.xmind》重新设计）----
            ('knowledge_manage', 'AI Agent知识库增删改查'),
            ('material_class_visible', '课程下班级资料增删改查'),
            ('question_bank_manage', '作业题库增删改查'),
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

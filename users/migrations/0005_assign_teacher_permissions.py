"""数据迁移：教师权限重新设计（2026-10-13 按《教师.xmind》）

1. 给「教师」分组分配新的 10 项权限（所有权限仅限教学班级，对象级校验在业务接口实现）
2. 删除废弃的 homework_collect 权限点（已拆成 homework_comment + homework_answer，无分组使用）

注意：本迁移执行时 post_migrate 信号尚未触发（Meta.permissions 的新权限点还没入库），
所以新权限点在这里自行 get_or_create（与 0002 同样的原因）。
"""
from django.db import migrations

# 教师分组新权限：codename -> 中文名（与 users/permissions.py、User.Meta.permissions 保持一致）
TEACHER_PERMS = {
    'notice_manage': '班级通知增删改查',
    'analysis_view': '班级学情分析图表查看',
    'material_class_manage': '班级内部资料增删改查',
    'homework_publish': '作业发布',
    'homework_bank': '作业题库管理',
    'homework_remind': '作业提醒',
    'homework_comment': '作业评语增删改查',
    'homework_answer': '作业答案改查',
    'homework_ai': 'Agent分析学生改查',
    'profile_edit': '修改个人资料',
}


def forward(apps, schema_editor):
    """分配教师新权限并删除废弃权限点（幂等）"""
    ContentType = apps.get_model('contenttypes', 'ContentType')
    Permission = apps.get_model('auth', 'Permission')
    Group = apps.get_model('auth', 'Group')
    userModel = apps.get_model('users', 'User')
    contentType = ContentType.objects.get_for_model(userModel)

    # 1. 自建新权限点并分配给教师分组
    teacherGroup = Group.objects.filter(name='教师').first()
    if teacherGroup is not None:
        for codename, name in TEACHER_PERMS.items():
            perm, _ = Permission.objects.get_or_create(
                content_type=contentType, codename=codename,
                defaults={'name': name})
            teacherGroup.permissions.add(perm)

    # 2. 删除废弃的 homework_collect（已拆成评语/答案两个权限点，无分组使用）
    Permission.objects.filter(
        content_type=contentType, codename='homework_collect').delete()


def backward(apps, schema_editor):
    """回滚：清空教师分组权限（历史分配无法精确恢复，保守清空）"""
    Group = apps.get_model('auth', 'Group')
    teacherGroup = Group.objects.filter(name='教师').first()
    if teacherGroup is not None:
        teacherGroup.permissions.clear()


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0004_alter_user_options'),
    ]

    operations = [
        migrations.RunPython(forward, backward),
    ]

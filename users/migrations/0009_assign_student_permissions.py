"""数据迁移：给学生组分配新权限（2026-10-14）

学生权限设计：作业查看与提交（homework_view）、修改个人资料（profile_edit），
本次新增「查看自己的学生画像」（analysis_view_self）。

注意：本迁移执行时 post_migrate 信号尚未触发，新权限点需自行 get_or_create（与 0002/0005/0007 相同）。
"""
from django.db import migrations

# 新权限点：codename -> 中文名
NEW_PERM = {
    'analysis_view_self': '查看自己的学生画像',
}


def forward(apps, schema_editor):
    """get_or_create 新权限点并分配给「学生」分组（幂等）"""
    ContentType = apps.get_model('contenttypes', 'ContentType')
    Permission = apps.get_model('auth', 'Permission')
    Group = apps.get_model('auth', 'Group')
    userModel = apps.get_model('users', 'User')
    contentType = ContentType.objects.get_for_model(userModel)

    studentGroup = Group.objects.filter(name='学生').first()
    if studentGroup is not None:
        for codename, name in NEW_PERM.items():
            perm, _ = Permission.objects.get_or_create(
                content_type=contentType, codename=codename,
                defaults={'name': name})
            studentGroup.permissions.add(perm)


def backward(apps, schema_editor):
    """回滚：从学生组移除新权限点并删除记录"""
    ContentType = apps.get_model('contenttypes', 'ContentType')
    Permission = apps.get_model('auth', 'Permission')
    Group = apps.get_model('auth', 'Group')
    userModel = apps.get_model('users', 'User')
    contentType = ContentType.objects.get_for_model(userModel)

    newPerm = Permission.objects.filter(
        content_type=contentType, codename='analysis_view_self').first()
    if newPerm is not None:
        # 先清分组引用再删记录（与 0007 相同）
        newPerm.group_set.clear()
        newPerm.delete()


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0008_alter_user_options'),
    ]

    operations = [
        migrations.RunPython(forward, backward),
    ]

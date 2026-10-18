"""数据迁移：给学生组分配新权限（2026-10-16）

学生端新增两个只读查看权限（与教师/课程组共用同一批数据）：
- notice_view：查看班级通知（教师 notice:manage 发布）
- material_view：查看班级资料（教师 material:class_manage / 课程组 material:class_visible 下发）

注意：本迁移执行时 post_migrate 信号尚未触发，新权限点需自行 get_or_create（与 0002/0005/0007/0009 相同）。
"""
from django.db import migrations

# 新权限点：codename -> 中文名
NEW_PERM = {
    'notice_view': '查看班级通知',
    'material_view': '查看班级资料',
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
    userModel = apps.get_model('users', 'User')
    contentType = ContentType.objects.get_for_model(userModel)

    for codename in NEW_PERM:
        perm = Permission.objects.filter(
            content_type=contentType, codename=codename).first()
        if perm is not None:
            # 先清分组引用再删记录（与 0007 相同）
            perm.group_set.clear()
            perm.delete()


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0010_alter_user_options'),
    ]

    operations = [
        migrations.RunPython(forward, backward),
    ]

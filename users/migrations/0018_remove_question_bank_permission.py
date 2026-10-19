"""收回课程组的公共题库写权限（2026-10-20）

公共题库改由系统管理员独占维护：
1. 从「课程组」分组移除 question_bank_manage
2. 删除该 Permission 记录（Meta.permissions 已不再注册）

反向迁移恢复权限记录并重新分配给课程组，便于回滚。
"""
from django.db import migrations

CODENAME = 'question_bank_manage'
GROUP_NAME = '课程组'
PERMISSION_NAME = '作业题库增删改查'


def removeQuestionBankPermission(apps, schemaEditor):
    Group = apps.get_model('auth', 'Group')
    Permission = apps.get_model('auth', 'Permission')
    group = Group.objects.filter(name=GROUP_NAME).first()
    permission = Permission.objects.filter(
        content_type__app_label='users', codename=CODENAME).first()
    if group is not None and permission is not None:
        group.permissions.remove(permission)
    if permission is not None:
        permission.delete()


def restoreQuestionBankPermission(apps, schemaEditor):
    ContentType = apps.get_model('contenttypes', 'ContentType')
    Group = apps.get_model('auth', 'Group')
    Permission = apps.get_model('auth', 'Permission')
    contentType, _ = ContentType.objects.get_or_create(
        app_label='users', model='user')
    permission, _ = Permission.objects.get_or_create(
        content_type=contentType, codename=CODENAME,
        defaults={'name': PERMISSION_NAME})
    group = Group.objects.filter(name=GROUP_NAME).first()
    if group is not None:
        group.permissions.add(permission)


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0017_alter_user_options'),
    ]

    operations = [
        migrations.RunPython(removeQuestionBankPermission,
                             restoreQuestionBankPermission),
    ]

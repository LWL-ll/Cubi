"""现有非超级用户账号标记为待改密

导入创建的账号初始密码在 xlsx 中明文流转，有泄露面；
存量账号（含演示数据）统一标记为首次登录须改密，
超级用户保留（管理员手工维护密码，不受此约束）。
"""
from django.db import migrations


def markExistingUsers(apps, schemaEditor):
    """把现存非超级用户全部置 must_change_password=True"""
    userModel = apps.get_model('users', 'User')
    userModel.objects.filter(is_superuser=False) \
        .update(must_change_password=True)


def unmarkAll(apps, schemaEditor):
    """回滚：清空标记（不恢复具体哪些账号原本为 True）"""
    userModel = apps.get_model('users', 'User')
    userModel.objects.update(must_change_password=False)


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0014_user_must_change_password'),
    ]

    operations = [
        migrations.RunPython(markExistingUsers, unmarkAll),
    ]

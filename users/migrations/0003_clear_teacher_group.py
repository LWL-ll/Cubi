"""数据迁移：清空「教师」分组的全部权限（用户要重新设计教师权限，先删除现有分配）

注意：只移除教师分组与权限点的关联，权限点本身保留（等重新设计定稿后调整）；
「课程组」分组共用 material:manage 权限点，不受本迁移影响。
"""
from django.db import migrations


def forward(apps, schema_editor):
    """清空教师分组的权限分配（幂等：重复执行无副作用）"""
    Group = apps.get_model('auth', 'Group')
    teacherGroup = Group.objects.filter(name='教师').first()
    if teacherGroup is not None:
        teacherGroup.permissions.clear()


def backward(apps, schema_editor):
    """回滚：无历史记录可恢复，保持空（重新设计后由新迁移分配）"""
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0002_create_groups'),
    ]

    operations = [
        migrations.RunPython(forward, backward),
    ]

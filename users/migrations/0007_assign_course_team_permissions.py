"""数据迁移：课程组权限重新设计（2026-10-13 按《课题组.xmind》）

xmind 结构：资料（AI Agent知识库 / 课程下班级显示 各自增删改查）、题库（作业）、课程设计（章节设计）
1. 新增 material_class_visible（课程下班级资料增删改查）并分配给「课程组」分组
2. 删除废弃的 material_manage（课程组资料管理，已被两类资料权限替代）
3. 同步三个权限点的中文名（post_migrate 只创建缺失权限，不更新已有权限的名称）

注意：本迁移执行时 post_migrate 信号尚未触发，新权限点需自行 get_or_create（与 0002/0005 相同）。
"""
from django.db import migrations

# 新权限点：codename -> 中文名
NEW_PERM = {
    'material_class_visible': '课程下班级资料增删改查',
}

# 中文名同步：codename -> 新中文名（按《课题组.xmind》）
RENAME_MAP = {
    'knowledge_manage': 'AI Agent知识库增删改查',
    'question_bank_manage': '作业题库增删改查',
    'chapter_manage': '课程章节设计',
}

# 回滚用：中文名还原表
RENAME_MAP_BACK = {
    'knowledge_manage': '知识库管理',
    'question_bank_manage': '题库管理增删改查',
    'chapter_manage': '课程章节设置',
}


def forward(apps, schema_editor):
    """分配课程组新权限、删除废弃权限点、同步中文名（幂等）"""
    ContentType = apps.get_model('contenttypes', 'ContentType')
    Permission = apps.get_model('auth', 'Permission')
    Group = apps.get_model('auth', 'Group')
    userModel = apps.get_model('users', 'User')
    contentType = ContentType.objects.get_for_model(userModel)

    # 1. 新权限点 get_or_create 并分配给课程组
    courseGroup = Group.objects.filter(name='课程组').first()
    if courseGroup is not None:
        for codename, name in NEW_PERM.items():
            perm, _ = Permission.objects.get_or_create(
                content_type=contentType, codename=codename,
                defaults={'name': name})
            courseGroup.permissions.add(perm)

    # 2. 同步三个权限点的中文名
    for codename, newName in RENAME_MAP.items():
        Permission.objects.filter(
            content_type=contentType, codename=codename).update(name=newName)

    # 3. 删除废弃的 material_manage（先移除所有分组引用，再删记录）
    oldPerm = Permission.objects.filter(
        content_type=contentType, codename='material_manage').first()
    if oldPerm is not None:
        oldPerm.group_set.clear()
        oldPerm.delete()


def backward(apps, schema_editor):
    """回滚：恢复 material_manage 并分配给课程组，移除新权限点，还原中文名"""
    ContentType = apps.get_model('contenttypes', 'ContentType')
    Permission = apps.get_model('auth', 'Permission')
    Group = apps.get_model('auth', 'Group')
    userModel = apps.get_model('users', 'User')
    contentType = ContentType.objects.get_for_model(userModel)

    # 1. 恢复废弃的权限点
    oldPerm, _ = Permission.objects.get_or_create(
        content_type=contentType, codename='material_manage',
        defaults={'name': '课程组资料管理增删改查'})
    courseGroup = Group.objects.filter(name='课程组').first()
    if courseGroup is not None:
        courseGroup.permissions.add(oldPerm)

    # 2. 移除新权限点（先清分组引用再删记录）
    newPerm = Permission.objects.filter(
        content_type=contentType, codename='material_class_visible').first()
    if newPerm is not None:
        newPerm.group_set.clear()
        newPerm.delete()

    # 3. 还原中文名
    for codename, oldName in RENAME_MAP_BACK.items():
        Permission.objects.filter(
            content_type=contentType, codename=codename).update(name=oldName)


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0006_alter_user_options'),
    ]

    operations = [
        migrations.RunPython(forward, backward),
    ]

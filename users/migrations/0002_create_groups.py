"""数据迁移：创建教务/课程组/教师/学生四个分组并分配权限

注意（Django 迁移经典坑）：
- Meta.permissions 里声明的权限记录要等 post_migrate 信号才入库，
  而数据迁移在迁移执行期间运行，此时查不到权限记录；
  所以这里必须在迁移内用 get_or_create 自行创建（幂等）
- 分组名与权限分配内联在本迁移中（不 import 业务模块），
  保证历史迁移不随业务代码修改而变化
"""
from django.db import migrations

# 分组 key -> 中文组名
GROUP_NAMES = {
    'admin': '教务',
    'course_team': '课程组',
    'teacher': '教师',
    'student': '学生',
}

# 权限 codename -> 权限中文名（与 User.Meta.permissions 保持一致）
PERMISSION_NAMES = {
    'class_manage': '全校班级增删改查',
    'class_assign_teacher': '分配教师到班级',
    'course_manage': '全校课程增删改查',
    'course_assign_class': '分配班级到课程',
    'user_assign_role': '指定课程组组长',
    'material_manage': '资料管理增删改查',
    'knowledge_manage': '知识库管理',
    'question_bank_manage': '题库管理增删改查',
    'chapter_manage': '课程章节设置',
    'notice_manage': '班级通知增删改查',
    'analysis_view': '学情分析查看',
    'homework_publish': '作业发布',
    'homework_bank': '作业题库管理',
    'homework_remind': '作业提醒',
    'homework_collect': '作业回收',
    'homework_ai': 'AI分析学生',
    'homework_view': '作业查看与提交',
    'profile_edit': '修改个人资料',
}

# 分组 key -> 权限 codename 列表（与 xmind《用户权限》一致）
GROUP_CODENAMES = {
    'admin': ['class_manage', 'class_assign_teacher', 'course_manage',
              'course_assign_class', 'user_assign_role', 'profile_edit'],
    'course_team': ['material_manage', 'knowledge_manage',
                    'question_bank_manage', 'chapter_manage', 'profile_edit'],
    'teacher': ['notice_manage', 'analysis_view', 'material_manage',
                'homework_publish', 'homework_bank', 'homework_remind',
                'homework_collect', 'homework_ai', 'profile_edit'],
    'student': ['homework_view', 'profile_edit'],
}


def createGroups(apps, schema_editor):
    """创建四个分组并分配权限（幂等：重复执行不会产生重复数据）"""
    Group = apps.get_model('auth', 'Group')
    Permission = apps.get_model('auth', 'Permission')
    ContentType = apps.get_model('contenttypes', 'ContentType')
    userModel = apps.get_model('users', 'User')

    # 取 users.User 的 content type（不存在则自动创建）
    contentType = ContentType.objects.get_for_model(userModel)

    # 迁移内自行创建全部权限记录（幂等，post_migrate 会自动跳过已存在的）
    for codename, name in PERMISSION_NAMES.items():
        Permission.objects.get_or_create(
            content_type=contentType, codename=codename, defaults={'name': name})

    # 创建分组并按表分配权限
    for key, groupName in GROUP_NAMES.items():
        group, _ = Group.objects.get_or_create(name=groupName)
        perms = list(Permission.objects.filter(
            content_type=contentType, codename__in=GROUP_CODENAMES[key]))
        group.permissions.set(perms)


def removeGroups(apps, schema_editor):
    """回滚：删除四个分组"""
    Group = apps.get_model('auth', 'Group')
    for name in GROUP_NAMES.values():
        Group.objects.filter(name=name).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(createGroups, removeGroups),
    ]

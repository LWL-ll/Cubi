"""权限体系一致性系统检查（manage.py check 时自动执行）

权限点定义散落在四处，任何一处改漏都会造成权限漂移：
1. User.Meta.permissions（权限点注册）
2. PERM_CODES（权限码 -> codename 映射）
3. GROUP_PERMISSIONS（分组 -> 权限码分配）
4. 数据库各分组实际分配（管理员可在 /admin/ 改动，改错能及时发现）

数据库尚未迁移完成时（全新环境首次 check）自动跳过数据库部分。
"""
from django.apps import apps
from django.core.checks import Error, register
from django.db.utils import DatabaseError

from .permissions import GROUP_NAMES, GROUP_PERMISSIONS, PERM_CODES


def _getDbGroupPerms():
    """读取数据库各分组的实际权限 codename 集合；表不存在时抛 DatabaseError"""
    Group = apps.get_model('auth', 'Group')
    result = {}
    for key, name in GROUP_NAMES.items():
        group = Group.objects.filter(name=name).first()
        result[key] = (
            set(group.permissions.values_list('codename', flat=True))
            if group is not None else None)
    return result


@register()
def check_permission_consistency(app_configs, **kwargs):
    """断言权限点注册、映射、分组定义、数据库分配四处一致"""
    errors = []
    User = apps.get_model('users', 'User')

    # 1. Meta.permissions 与 PERM_CODES 的 codename 双向一致
    modelCodenames = {codename for codename, _ in User._meta.permissions}
    mapCodenames = set(PERM_CODES.values())
    onlyInModel = modelCodenames - mapCodenames
    onlyInMap = mapCodenames - modelCodenames
    if onlyInModel:
        errors.append(Error(
            'Meta.permissions 注册的权限点未出现在 PERM_CODES 映射里',
            hint=f'请补映射或删除注册：{sorted(onlyInModel)}',
            obj='users.User.Meta.permissions'))
    if onlyInMap:
        errors.append(Error(
            'PERM_CODES 中的权限码未注册到 Meta.permissions',
            hint=f'请补注册或删除映射：{sorted(onlyInMap)}',
            obj='users.permissions.PERM_CODES'))

    # 2. 分组定义与映射引用一致
    if set(GROUP_NAMES) != set(GROUP_PERMISSIONS):
        errors.append(Error(
            'GROUP_NAMES 与 GROUP_PERMISSIONS 的分组 key 不一致',
            hint=f'NAMES={sorted(GROUP_NAMES)}，PERMISSIONS={sorted(GROUP_PERMISSIONS)}',
            obj='users.permissions'))
    for key, codes in GROUP_PERMISSIONS.items():
        unknown = set(codes) - set(PERM_CODES)
        if unknown:
            errors.append(Error(
                f'分组 {key} 引用了未定义的权限码',
                hint=f'未知权限码：{sorted(unknown)}',
                obj='users.permissions.GROUP_PERMISSIONS'))

    # 3. 数据库实际分配与定义一致（未迁移时跳过）
    try:
        dbPerms = _getDbGroupPerms()
    except DatabaseError:
        return errors
    for key, definedCodes in GROUP_PERMISSIONS.items():
        definedCodenames = {PERM_CODES[code] for code in definedCodes}
        actual = dbPerms.get(key)
        if actual is None:
            errors.append(Error(
                f'数据库缺少「{GROUP_NAMES[key]}」分组',
                hint='请运行 python manage.py migrate 创建分组',
                obj=f'Group:{key}'))
        elif actual != definedCodenames:
            errors.append(Error(
                f'「{GROUP_NAMES[key]}」分组实际权限与定义不一致',
                hint=f'多出：{sorted(actual - definedCodenames)}；缺少：{sorted(definedCodenames - actual)}',
                obj=f'Group:{key}'))
    return errors

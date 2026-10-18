"""教务业务服务层：跨视图共享的业务规则收敛点

「一人一班」是数据完整性约束（SQLite 无部分唯一索引，只能服务层强制）：
所有写学生-班级关系的代码路径必须收敛到这里，
禁止在视图/导入函数里各自直写 ClassMembership。
"""
from datetime import date

from .models import ClassMembership


def enrollStudent(profile, adminClass, joinedAt=None):
    """学生入班统一入口：强制「一人同时仅一个在班成员关系」

    转班语义：已有当前成员关系时先置 left_at（历史留痕），再建新关系；
    已在目标班时幂等返回现有关系（防重复导入产生脏数据）。
    """
    joinedAt = joinedAt or date.today()
    current = profile.memberships.filter(left_at__isnull=True).first()
    if current is not None:
        if current.admin_class_id == adminClass.id:
            # 已在目标班：幂等返回，不重复建关系
            return current
        # 转班：旧关系置离班日期，留痕（历史作业归属问题的落点之一）
        current.left_at = joinedAt
        current.save(update_fields=['left_at'])
    return ClassMembership.objects.create(
        student_profile=profile, admin_class=adminClass, joined_at=joinedAt)

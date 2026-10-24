"""教务业务服务层：跨视图共享的业务规则收敛点

「一人一班」由数据库条件唯一约束兜底，服务层负责转班事务：
所有写学生-班级关系的代码路径必须收敛到这里，
禁止在视图/导入函数里各自直写 ClassMembership。
"""
from datetime import date

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F

from .models import ClassMembership, Course, CourseCollege, TeachingClass


def syncCourseTeamMembership(userId, using='default'):
    """课程组身份跟随组长任职，保留教师组和其他权限。调用方须在事务内锁定用户。"""
    from django.contrib.auth import get_user_model
    from django.contrib.auth.models import Group

    user = get_user_model().objects.using(using).filter(pk=userId).first()
    group = Group.objects.using(using).filter(name='课程组').first()
    if user is None or group is None:
        return
    if CourseCollege.objects.using(using).filter(leader_id=userId).exists():
        user.groups.add(group)
    else:
        user.groups.remove(group)


@transaction.atomic
def enrollStudent(profile, adminClass, joinedAt=None):
    """学生入班统一入口：强制「一人同时仅一个在班成员关系」

    转班语义：已有当前成员关系时先置 left_at（历史留痕），再建新关系；
    已在目标班时幂等返回现有关系（防重复导入产生脏数据）。
    """
    joinedAt = joinedAt or date.today()
    # UPDATE 先获取写锁：SQLite 的 select_for_update 不生效，PG 同样按行串行。
    type(profile).objects.filter(pk=profile.pk).update(id=F('id'))
    current = profile.memberships.filter(left_at__isnull=True).first()
    if current is not None:
        if current.admin_class_id == adminClass.id:
            # 已在目标班：幂等返回，不重复建关系
            return current
        # 转班：旧关系置离班日期，留痕（历史作业归属问题的落点之一）
        if joinedAt < current.joined_at:
            raise ValidationError('转班日期不能早于原入班日期')
        current.left_at = joinedAt
        current.save(update_fields=['left_at'])
    return ClassMembership.objects.create(
        student_profile=profile, admin_class=adminClass, joined_at=joinedAt)


def lockTeachingCourse(courseCollege, using='default'):
    """组班的所有写入口按课程串行，避免先查后写的并发冲突。"""
    Course.objects.using(using).filter(pk=courseCollege.course_id).update(id=F('id'))


def validateTeachingClasses(courseCollege, term, adminClasses, excludeId=None,
                            using='default'):
    """组成班级来自本学院候选池，同课程同学期不得重复组班。"""
    ids = [item.pk for item in adminClasses]
    if courseCollege.admin_classes.using(using).filter(
            pk__in=ids, major__department_id=courseCollege.department_id).count() != len(ids):
        raise ValidationError('组成班级必须属于本学院及本课程候选池')
    conflicts = TeachingClass.objects.using(using).filter(
        course_college__course_id=courseCollege.course_id,
        term=term, admin_classes__pk__in=ids).exclude(pk=excludeId)
    if conflicts.exists():
        raise ValidationError('同课程同学期，行政班不能重复加入教学班')


@transaction.atomic
def createTeachingClass(courseCollege, name, term, adminClasses):
    lockTeachingCourse(courseCollege)
    adminClasses = list(adminClasses)
    if not adminClasses:
        raise ValidationError('教学班至少包含一个行政班')
    validateTeachingClasses(courseCollege, term, adminClasses)
    if TeachingClass.objects.filter(
            course_college=courseCollege, term=term, name=name).exists():
        raise ValidationError('同学期已存在同名教学班')
    teachingClass = TeachingClass.objects.create(
        course_college=courseCollege, name=name, term=term)
    teachingClass.admin_classes.set(adminClasses)
    teachingClass.teachers.set(courseCollege.teachers.all())
    return teachingClass

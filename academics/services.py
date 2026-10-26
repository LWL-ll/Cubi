"""教务业务服务层：跨视图共享的业务规则收敛点

「一人一班」由数据库条件唯一约束兜底，服务层负责转班事务：
所有写学生-班级关系的代码路径必须收敛到这里，
禁止在视图/导入函数里各自直写 ClassMembership。
"""
from datetime import date

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F
from django.db.models.deletion import ProtectedError
from django.utils import timezone

from .models import AdminClass, ClassMembership, Course, CourseCollege, TeachingClass


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


@transaction.atomic
def deleteTeachingClass(teachingClass):
    lockTeachingCourse(teachingClass.course_college)
    current = TeachingClass.objects.filter(pk=teachingClass.pk).first()
    if current is None:
        raise ValidationError('教学班不存在')
    try:
        current.delete()
    except ProtectedError:
        raise ValidationError('教学班已有作业、资料或通知，不能解散') from None


@transaction.atomic
def deleteCourseCollege(courseCollege):
    lockTeachingCourse(courseCollege)
    current = CourseCollege.objects.filter(pk=courseCollege.pk).first()
    if current is None:
        raise ValidationError('课程×学院不存在')
    try:
        current.teaching_classes.all().delete()
        current.delete()
    except ProtectedError:
        raise ValidationError('课程×学院下已有作业、资料或通知，不能解除') from None


def lockAdminClassCourses(adminClass, using='default'):
    """行政班删除与组班、候选池调整按同一课程顺序加锁。"""
    courseIds = set(adminClass.course_colleges.using(using).values_list('course_id', flat=True))
    courseIds.update(adminClass.teaching_classes.using(using).values_list(
        'course_college__course_id', flat=True))
    for courseId in sorted(courseIds):
        Course.objects.using(using).filter(pk=courseId).update(id=F('id'))


def validateAdminClassDeletion(adminClass, using='default'):
    if ClassMembership.objects.using(using).filter(admin_class_id=adminClass.pk).exists():
        raise ValidationError('行政班已有学生成员历史，不能删除')
    if TeachingClass.objects.using(using).filter(admin_classes__pk=adminClass.pk).exists():
        raise ValidationError('行政班仍属于教学班，请先调整教学班组成')


@transaction.atomic
def deleteAdminClass(adminClass):
    lockAdminClassCourses(adminClass)
    if not AdminClass.objects.filter(pk=adminClass.pk).update(id=F('id')):
        raise ValidationError('行政班不存在')
    current = AdminClass.objects.get(pk=adminClass.pk)
    validateAdminClassDeletion(current)
    current.delete()


@transaction.atomic
def removeCandidateAdminClass(courseCollege, adminClass):
    lockTeachingCourse(courseCollege)
    current = CourseCollege.objects.filter(pk=courseCollege.pk).first()
    if current is None:
        raise ValidationError('课程×学院不存在')
    current.admin_classes.remove(adminClass)


@transaction.atomic
def removeTeachingAdminClass(teachingClass, adminClass):
    lockTeachingCourse(teachingClass.course_college)
    current = TeachingClass.objects.filter(pk=teachingClass.pk).first()
    if current is None:
        raise ValidationError('教学班不存在')
    current.admin_classes.remove(adminClass)


def _lockStudentProfile(profile):
    if not type(profile).objects.filter(pk=profile.pk).update(id=F('id')):
        raise ValidationError('学生档案不存在')
    return type(profile).objects.select_related('major__department').get(pk=profile.pk)


@transaction.atomic
def transferStudentToday(profile, adminClass):
    """普通转班只改变当日成员关系，保留学生账号、学籍和历史提交。"""
    profile = _lockStudentProfile(profile)
    if not AdminClass.objects.filter(pk=adminClass.pk).update(id=F('id')):
        raise ValidationError('目标行政班不存在')
    target = AdminClass.objects.select_related('major__department').get(pk=adminClass.pk)
    if (target.major_id != profile.major_id or target.enrollment_year is None
            or target.enrollment_year != profile.enrollment_year):
        raise ValidationError('普通转班只能选择同专业、同入学年份的行政班')
    if not target.major.is_active or not target.major.department.is_active:
        raise ValidationError('目标行政班所属专业或学院未启用')
    today = timezone.localdate()
    current = profile.memberships.filter(left_at__isnull=True).first()
    if current is not None and current.joined_at > today:
        raise ValidationError('原入班日期晚于今天，不能办理当日转班')
    return enrollStudent(profile, target, today)


@transaction.atomic
def leaveStudentToday(profile):
    """当日退班留痕；重复退班无操作，不改变学生学籍状态。"""
    profile = _lockStudentProfile(profile)
    current = profile.memberships.filter(left_at__isnull=True).first()
    if current is None:
        return None
    today = timezone.localdate()
    if current.joined_at > today:
        raise ValidationError('原入班日期晚于今天，不能办理当日退班')
    current.left_at = today
    current.save(update_fields=['left_at'])
    return current

"""M2M 写入也执行组班约束，覆盖页面、API、后台及反向关联入口。"""
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db.models import F
from django.db.models.signals import m2m_changed, post_delete, pre_delete
from django.dispatch import receiver

from .models import AdminClass, CourseCollege, TeachingClass
from .services import (lockAdminClassCourses, lockTeachingCourse, syncCourseTeamMembership,
                       validateAdminClassDeletion, validateTeachingClasses)


@receiver(pre_delete, sender=CourseCollege)
def lockDeletedLeader(sender, instance, using, **kwargs):
    lockTeachingCourse(instance, using)
    # 删除对象可能是先前加载的实例；加锁后读取实际任职，避免漏回收新组长。
    instance.leader_id = CourseCollege.objects.using(using).filter(pk=instance.pk) \
        .values_list('leader_id', flat=True).first()
    if instance.leader_id:
        get_user_model().objects.using(using).filter(pk=instance.leader_id).update(id=F('id'))


@receiver(post_delete, sender=CourseCollege)
def revokeDeletedLeader(sender, instance, using, **kwargs):
    if instance.leader_id:
        syncCourseTeamMembership(instance.leader_id, using)


@receiver(m2m_changed, sender=TeachingClass.admin_classes.through)
def validateTeachingClassMembers(sender, instance, action, reverse, pk_set,
                                  using, **kwargs):
    if action in ('pre_remove', 'pre_clear'):
        if reverse:
            teachingClasses = instance.teaching_classes.using(using).all()
            if action == 'pre_remove':
                teachingClasses = teachingClasses.filter(pk__in=pk_set)
            for teachingClass in teachingClasses.order_by('course_college__course_id', 'pk'):
                lockTeachingCourse(teachingClass.course_college, using)
                if not teachingClass.admin_classes.using(using).exclude(pk=instance.pk).exists():
                    raise ValidationError('教学班至少保留一个组成班级')
        else:
            lockTeachingCourse(instance.course_college, using)
            currentIds = set(instance.admin_classes.using(using).values_list('pk', flat=True))
            removedIds = currentIds if action == 'pre_clear' else currentIds.intersection(pk_set)
            if removedIds and not currentIds - removedIds:
                raise ValidationError('教学班至少保留一个组成班级')
        return
    if action != 'pre_add' or not pk_set:
        return
    if reverse:
        teachingClasses = TeachingClass.objects.using(using).filter(
            pk__in=pk_set).order_by('course_college__course_id', 'pk')
        seen = set()
        for teachingClass in teachingClasses:
            lockTeachingCourse(teachingClass.course_college, using)
            validateTeachingClasses(teachingClass.course_college,
                                    teachingClass.term, [instance],
                                    excludeId=teachingClass.pk, using=using)
            key = (teachingClass.course_college.course_id, teachingClass.term)
            if key in seen:
                raise ValidationError('同课程同学期，行政班不能重复加入教学班')
            seen.add(key)
    else:
        lockTeachingCourse(instance.course_college, using)
        validateTeachingClasses(instance.course_college, instance.term,
                                AdminClass.objects.using(using).filter(pk__in=pk_set),
                                excludeId=instance.pk, using=using)


@receiver(m2m_changed, sender=CourseCollege.admin_classes.through)
def protectCandidateAdminClasses(sender, instance, action, reverse, pk_set,
                                 using, **kwargs):
    if action not in ('pre_remove', 'pre_clear'):
        return
    if reverse:
        courseColleges = instance.course_colleges.using(using).all()
        if action == 'pre_remove':
            courseColleges = courseColleges.filter(pk__in=pk_set)
        for cc in courseColleges.order_by('course_id', 'pk'):
            lockTeachingCourse(cc, using)
            if cc.teaching_classes.using(using).filter(admin_classes__pk=instance.pk).exists():
                raise ValidationError('行政班已组成教学班，不能移出课程候选池')
    else:
        lockTeachingCourse(instance, using)
        used = instance.teaching_classes.using(using).filter(
            admin_classes__course_colleges=instance)
        if action == 'pre_remove':
            used = used.filter(admin_classes__pk__in=pk_set)
        if used.exists():
            raise ValidationError('行政班已组成教学班，不能移出课程候选池')


@receiver(pre_delete, sender=AdminClass)
def protectAdminClassHistory(sender, instance, using, **kwargs):
    lockAdminClassCourses(instance, using)
    validateAdminClassDeletion(instance, using)

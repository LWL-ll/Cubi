"""M2M 写入也执行组班约束，覆盖页面、API、后台及反向关联入口。"""
from django.contrib.auth import get_user_model
from django.db.models import F
from django.db.models.signals import m2m_changed, post_delete, pre_delete
from django.dispatch import receiver

from .models import AdminClass, CourseCollege, TeachingClass
from .services import (lockTeachingCourse, syncCourseTeamMembership,
                       validateTeachingClasses)


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
                from django.core.exceptions import ValidationError
                raise ValidationError('同课程同学期，行政班不能重复加入教学班')
            seen.add(key)
    else:
        lockTeachingCourse(instance.course_college, using)
        validateTeachingClasses(instance.course_college, instance.term,
                                AdminClass.objects.using(using).filter(pk__in=pk_set),
                                excludeId=instance.pk, using=using)

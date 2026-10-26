"""班级通知与作业站内提醒；所有阅读权限随当前班级关系变化。"""
from collections.abc import Mapping
from datetime import timedelta

from django.db import transaction
from django.db.models import DateTimeField, F, Max, OuterRef, Subquery
from django.http import Http404
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from teaching.helpers import getStudentTeachingClass, getTeacherTeachingClass
from teaching.homework_service import (getManageableHomework, getStudentHomeworkQuery,
                                      lockHomework)
from teaching.models import ClassNotice, HomeworkReminder, NoticeRead
from teaching.teacher_service import classRoster, gradeRows


def validateEmptyPayload(payload):
    if not isinstance(payload, Mapping) or payload:
        raise ValidationError('请求体只能是空对象')


def validateNotice(payload, *, creating):
    if (not isinstance(payload, Mapping) or not payload
            or set(payload) - {'title', 'content'}):
        raise ValidationError('只允许 title、content 字段，且不能为空对象')
    fields = {}
    for key, limit in (('title', 100), ('content', 10000)):
        if key not in payload:
            if creating:
                raise ValidationError(f'{key} 不能为空')
            continue
        value = payload[key]
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise ValidationError(f'{key} 须为 1～{limit} 字的文本')
        try:
            value.encode('utf-8')
        except UnicodeEncodeError:
            raise ValidationError(f'{key} 包含无效字符')
        fields[key] = value.strip()
    return fields


def listClassNotices(teachingClass, user=None):
    notices = ClassNotice.objects.filter(teaching_class=teachingClass).select_related(
        'created_by', 'updated_by')
    if user is not None:
        readAt = NoticeRead.objects.filter(
            notice_id=OuterRef('pk'), user=user).values('read_at')[:1]
        notices = notices.annotate(myReadAt=Subquery(readAt, output_field=DateTimeField()))
    return notices.order_by('-created_at', '-id')


def createNotice(user, teachingClass, payload):
    fields = validateNotice(payload, creating=True)
    return ClassNotice.objects.create(
        teaching_class=teachingClass, created_by=user, updated_by=user, **fields)


def getNotice(user, noticeId, *, student=False):
    notices = ClassNotice.objects.select_related('created_by', 'updated_by')
    if student:
        readAt = NoticeRead.objects.filter(
            notice_id=OuterRef('pk'), user=user).values('read_at')[:1]
        notices = notices.annotate(myReadAt=Subquery(readAt, output_field=DateTimeField()))
    notice = notices.filter(pk=noticeId).first()
    if notice is None:
        raise Http404('通知不存在')
    check = getStudentTeachingClass if student else getTeacherTeachingClass
    try:
        check(user, notice.teaching_class_id)
    except Http404:
        raise Http404('通知不存在') from None
    return notice


def lockNotice(noticeId):
    if not ClassNotice.objects.filter(pk=noticeId).update(id=F('id')):
        raise Http404('通知不存在')
    return ClassNotice.objects.get(pk=noticeId)


def updateNotice(user, notice, payload):
    fields = validateNotice(payload, creating=False)
    with transaction.atomic():
        notice = lockNotice(notice.pk)
        for key, value in fields.items():
            setattr(notice, key, value)
        notice.updated_by = user
        notice.save()
    return notice


def deleteNotice(notice):
    with transaction.atomic():
        notice = lockNotice(notice.pk)
        notice.delete()


def markNoticeRead(user, notice):
    with transaction.atomic():
        notice = lockNotice(notice.pk)
        receipt, _ = NoticeRead.objects.get_or_create(notice=notice, user=user)
    return receipt


def noticeReadership(notice):
    roster = classRoster(notice.teaching_class)
    times = dict(NoticeRead.objects.filter(
        notice=notice, user_id__in=[student['id'] for student in roster]
    ).values_list('user_id', 'read_at'))
    rows = [{**student, 'readAt': times.get(student['id'])} for student in roster]
    count = len(times)
    return {'summary': {'studentCount': len(rows), 'readCount': count,
                        'unreadCount': len(rows) - count}, 'students': rows}


def reminderRecipients(homework, now):
    if not homework.start_at <= now <= homework.deadline:
        raise ValidationError('只有进行中的作业可以发送提醒')
    roster = classRoster(homework.teaching_class)
    rows = gradeRows([homework], roster)[homework.pk]
    eligible = [row for row in rows
                if row['submittedProblemCount'] < row['problemCount']]
    recent = dict(HomeworkReminder.objects.filter(
        homework=homework, recipient_id__in=[row['id'] for row in eligible],
        created_at__gt=now - timedelta(hours=1)
    ).values('recipient_id').annotate(lastSent=Max('created_at')).values_list(
        'recipient_id', 'lastSent'))
    ready = [row['id'] for row in eligible if row['id'] not in recent]
    return len(eligible), ready


def previewReminderRecipients(homework):
    eligibleCount, ready = reminderRecipients(homework, timezone.now())
    return {'homeworkId': homework.pk, 'eligibleCount': eligibleCount, 'readyCount': len(ready),
            'skippedCount': eligibleCount - len(ready)}


def sendHomeworkReminders(user, homework, payload):
    validateEmptyPayload(payload)
    with transaction.atomic():
        # 与交卷及改作业共用锁；重读后计算当前题目、提交和去重窗口。
        homework = lockHomework(homework.pk)
        homework = getManageableHomework(user, homework.pk)
        eligibleCount, ready = reminderRecipients(homework, timezone.now())
        HomeworkReminder.objects.bulk_create([
            HomeworkReminder(homework=homework, recipient_id=studentId, sent_by=user)
            for studentId in ready])
    return {'homeworkId': homework.pk, 'eligibleCount': eligibleCount, 'sentCount': len(ready),
            'skippedCount': eligibleCount - len(ready)}


def listStudentReminders(user):
    return HomeworkReminder.objects.filter(
        recipient=user, homework__in=getStudentHomeworkQuery(user)
    ).select_related('homework', 'sent_by').order_by('-created_at', '-id')


def getStudentReminder(user, reminderId):
    reminder = listStudentReminders(user).filter(pk=reminderId).first()
    if reminder is None:
        raise Http404('提醒不存在')
    return reminder


def markReminderRead(user, reminder):
    # 单条授权条件 UPDATE 先写，避免 SQLite 读事务升级写锁；重复阅读保留首次时间。
    reminders = listStudentReminders(user).filter(pk=reminder.pk)
    reminders.filter(read_at__isnull=True).update(read_at=timezone.now())
    current = reminders.first()
    if current is None:
        raise Http404('提醒不存在')
    return current

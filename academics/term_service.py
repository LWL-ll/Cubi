"""关闭学期冻结名单，归档冻结成绩。所有教学写入先锁学期，再锁业务对象。"""
import json
from collections.abc import Mapping

from django.core.exceptions import ValidationError
from django.contrib.auth import get_user_model
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.db.models import F
from django.http import Http404
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework.exceptions import ValidationError as RequestValidationError

from .audit_service import recordAudit
from .models import AcademicTerm, TeachingClass, TeachingClassRecord


def currentTermCode():
    code = AcademicTerm.objects.filter(is_current=True).values_list('code', flat=True).first()
    if code is None:
        raise ValidationError('请先设置开放的当前学期')
    return code


def requireTermWrite(code, allowClosed=False, using='default'):
    query = AcademicTerm.objects.using(using).filter(code=code)
    # 旧测试/历史工具可创建未登记的字符串学期，公开组班接口单独要求登记。
    # 正式库迁移会为全部既有学期补登记。
    if transaction.get_connection(using).in_atomic_block:
        query.update(id=F('id'))
    term = query.first()
    if term and (term.status == AcademicTerm.ARCHIVED
                 or term.status == AcademicTerm.CLOSED and not allowClosed):
        raise ValidationError('学期已归档，教学数据只读' if term.status == AcademicTerm.ARCHIVED
                              else '学期已关闭，只允许评阅和参考答案收尾')
    return term


def requireClassWrite(teachingClass, allowClosed=False):
    return requireTermWrite(teachingClass.term, allowClosed)


def lockMembershipTerms(profile, adminClass=None):
    """先取得学期写锁，再读取成员，避免 SQLite 读锁升级及名单冻结竞态。

    教务成员异动低频，统一锁学期登记行还覆盖并发转入第三个班的情况。
    已结课的冻结名单不随之后的成员变更而变化。
    """
    AcademicTerm.objects.update(id=F('id'))


def requireTeachingWrite(teachingClass, allowClosed=False):
    try:
        return requireClassWrite(teachingClass, allowClosed)
    except ValidationError as exc:
        raise RequestValidationError(exc.messages) from None


def _payload(payload, allowed):
    if not isinstance(payload, Mapping) or set(payload) - set(allowed):
        raise ValidationError('请求体必须是对象，且只能包含允许的字段')


def _text(payload, key, limit):
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > limit or '\x00' in value:
        raise ValidationError(f'{key} 须为 1～{limit} 字文本')
    try:
        value.encode('utf-8')
    except UnicodeEncodeError:
        raise ValidationError(f'{key} 含无效字符') from None
    return value.strip()


def _dates(payload, *, required):
    if not required and not {'startDate', 'endDate'} & set(payload):
        return {}
    dates = []
    for key in ('startDate', 'endDate'):
        try:
            value = payload.get(key)
            parsed = parse_date(value) if isinstance(value, str) and len(value) == 10 else None
        except ValueError:
            parsed = None
        if parsed is None:
            raise ValidationError('须同时提供 startDate/endDate，格式 YYYY-MM-DD')
        dates.append(parsed)
    if dates[0] > dates[1]:
        raise ValidationError('结束日期不能早于开始日期')
    return {'start_date': dates[0], 'end_date': dates[1]}


def termData(term):
    return {'id': term.pk, 'code': term.code, 'name': term.name,
            'startDate': term.start_date, 'endDate': term.end_date,
            'status': term.status, 'isCurrent': term.is_current,
            'closedAt': term.closed_at, 'archivedAt': term.archived_at}


@transaction.atomic
def createTerm(actor, payload):
    _payload(payload, {'code', 'name', 'startDate', 'endDate'})
    code = _text(payload, 'code', 20)
    # 串行本接口的创建/当前学期切换，避免唯一约束竞态变成 500。
    get_user_model().objects.filter(pk=actor.pk).update(id=F('id'))
    if AcademicTerm.objects.filter(code=code).exists():
        raise ValidationError('学期编码已存在')
    term = AcademicTerm.objects.create(code=code, name=_text(payload, 'name', 50),
                                       **_dates(payload, required=True))
    recordAudit(actor, 'term.create', term, after=termData(term))
    return term


@transaction.atomic
def updateTerm(actor, termId, payload):
    _payload(payload, {'name', 'startDate', 'endDate'})
    term = _lockTerm(termId)
    if term.status != AcademicTerm.OPEN:
        raise ValidationError('已关闭的学期不能修改基本资料')
    before = termData(term)
    if 'name' in payload:
        term.name = _text(payload, 'name', 50)
    for key, value in _dates(payload, required=False).items():
        setattr(term, key, value)
    term.save()
    recordAudit(actor, 'term.update', term, before, termData(term))
    return term


def _lockTerm(termId):
    if not AcademicTerm.objects.filter(pk=termId).update(id=F('id')):
        raise Http404('学期不存在')
    return AcademicTerm.objects.get(pk=termId)


@transaction.atomic
def setCurrentTerm(actor, termId):
    # 事务首条 SQL 取得写锁，包含没有当前学期的情形。
    AcademicTerm.objects.update(id=F('id'))
    term = _lockTerm(termId)
    if term.status != AcademicTerm.OPEN:
        raise ValidationError('只能将开放学期设为当前学期')
    previous = list(AcademicTerm.objects.filter(is_current=True).values_list('code', flat=True))
    AcademicTerm.objects.filter(is_current=True).update(is_current=False)
    term.is_current = True
    term.save(update_fields=['is_current'])
    recordAudit(actor, 'term.current', term, {'currentCodes': previous}, {'currentCode': term.code})
    return term


def _closingRoster(teachingClass):
    from teaching.models import Submission
    from teaching.teacher_service import classRoster
    from users.models import SchoolIdentity
    roster = classRoster(teachingClass)
    ids = {row['id'] for row in roster}
    # 已经转走但留有提交的学生仍保留在结课成绩中。
    users = Submission.objects.filter(homework__teaching_class=teachingClass).exclude(
        user_id__in=ids).values('user_id', 'user__username', 'user__name').distinct()
    for row in users:
        identity = SchoolIdentity.objects.filter(user_id=row['user_id'],
            identity_type=SchoolIdentity.IDENTITY_STUDENT).order_by('pk').first()
        member = None if identity is None or not hasattr(identity, 'student_profile') else (
            identity.student_profile.memberships.filter(admin_class__teaching_classes=teachingClass)
                .select_related('admin_class').order_by('-joined_at', '-pk').first())
        roster.append({'id': row['user_id'], 'username': row['user__username'],
                       'name': row['user__name'], 'schoolNumber': identity.school_number if identity else '',
                       'adminClassId': member.admin_class_id if member else None,
                       'adminClassName': member.admin_class.name if member else ''})
    return roster


@transaction.atomic
def closeTerm(actor, termId):
    from teaching.models import Homework
    term = _lockTerm(termId)
    if term.status in (AcademicTerm.CLOSED, AcademicTerm.ARCHIVED):
        return term
    now = timezone.now()
    if Homework.objects.filter(teaching_class__term=term.code, deadline__gte=now).exists():
        raise ValidationError('仍有未截止作业，不能关闭学期')
    for tc in TeachingClass.objects.filter(term=term.code).order_by('pk'):
        TeachingClassRecord.objects.create(teaching_class=tc, roster=_closingRoster(tc), closed_at=now)
    term.status = AcademicTerm.CLOSED
    term.is_current = False
    term.closed_at = now
    term.save()
    recordAudit(actor, 'term.close', term, after=termData(term))
    return term


@transaction.atomic
def archiveTerm(actor, termId):
    from teaching.models import Homework, Submission
    from teaching.teacher_service import homeworkGradebook
    term = _lockTerm(termId)
    if term.status == AcademicTerm.ARCHIVED:
        return term
    if term.status != AcademicTerm.CLOSED:
        raise ValidationError('请先关闭学期，再完成归档')
    if Submission.objects.filter(homework__teaching_class__term=term.code,
            status__in=[Submission.STATUS_QUEUED, Submission.STATUS_RUNNING]).exists():
        raise ValidationError('仍有排队或判题中的提交，不能归档学期')
    now = timezone.now()
    for record in TeachingClassRecord.objects.filter(teaching_class__term=term.code):
        books = {str(h.pk): homeworkGradebook(h) for h in Homework.objects.filter(
            teaching_class=record.teaching_class).prefetch_related('problems')}
        record.gradebooks = json.loads(json.dumps(books, cls=DjangoJSONEncoder))
        record.archived_at = now
        record.save(update_fields=['gradebooks', 'archived_at'])
    term.status = AcademicTerm.ARCHIVED
    term.archived_at = now
    term.save(update_fields=['status', 'archived_at'])
    recordAudit(actor, 'term.archive', term, after=termData(term))
    return term

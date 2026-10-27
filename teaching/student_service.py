"""学生本人检索、成绩、消息和统计；所有聚合共用同一范围与计分服务。"""
from datetime import timedelta

from django.db.models import Count, F, OuterRef, Q, Subquery, Window, prefetch_related_objects
from django.db.models.functions import RowNumber, TruncDate
from django.http import Http404
from rest_framework.exceptions import ValidationError

from academics.models import AcademicTerm, TeachingClass, TeachingClassRecord
from users.permissions import hasPerm
from .models import ClassNotice, NoticeRead, Submission, TeachingMaterial
from .serializers import SubmissionListSerializer, SubmissionReviewSerializer
from .submission_service import parseObjectId
from .teacher_service import PENDING_STATUSES, gradeRows


def queryFields(params, allowed):
    if set(params) - set(allowed):
        raise ValidationError('包含不支持的查询字段')
    if hasattr(params, 'getlist') and any(len(params.getlist(key)) != 1 for key in params):
        raise ValidationError('查询字段不能重复')


def textFilter(value, maximum=100):
    if not isinstance(value, str) or len(value) > maximum or '\x00' in value:
        raise ValidationError('筛选文本格式错误或过长')
    try:
        value.encode('utf-8')
    except UnicodeEncodeError:
        raise ValidationError('筛选文本含无效字符') from None
    return value.strip()


def booleanFilter(value):
    if value not in ('true', 'false'):
        raise ValidationError('布尔筛选只支持 true/false')
    return value == 'true'


def courseFilters(query, params, prefix):
    for key, field in (('courseId', 'course_college__course_id'), ('teachingClassId', 'pk')):
        if key in params:
            query = query.filter(**{prefix + field: parseObjectId(params[key], key)})
    if 'term' in params:
        term = textFilter(params['term'], 20)
        if not term:
            raise ValidationError('term 不能为空')
        query = query.filter(**{prefix + 'term': term})
    return query


def contextData(scope):
    profile, identity, member = scope.profile, scope.identity, scope.membership
    student = None
    if profile:
        major, department = profile.major, profile.major.department
        student = {'academicStatus': profile.academic_status, 'enrollmentYear': profile.enrollment_year,
                   'major': {'id': major.pk, 'code': major.code, 'name': major.name,
                             'department': {'id': department.pk, 'code': department.code,
                                            'name': department.name}}}
    current = next((term for term in scope.terms.values() if term.is_current), None)
    return {'user': {'id': scope.user.pk, 'username': scope.user.username,
                     'name': scope.user.name, 'email': scope.user.email},
            'identity': None if identity is None else {'schoolCode': identity.school_code,
                'schoolNumber': identity.school_number, 'verificationStatus': identity.verification_status},
            'student': student,
            'adminClass': None if member is None else {'id': member.admin_class_id,
                'name': member.admin_class.name, 'code': member.admin_class.code},
            'currentTerm': current.code if current else None}


def teachingClasses(scope, params):
    queryFields(params, {'page', 'courseId', 'teachingClassId', 'term', 'scope'})
    mode = params.get('scope', 'all')
    if mode not in ('all', 'current', 'history'):
        raise ValidationError('scope 只支持 all/current/history')
    ids = scope.summaryClassIds if mode == 'all' else scope.contentClassIds if mode == 'current' else (
        scope.summaryClassIds - scope.contentClassIds)
    return courseFilters(TeachingClass.objects.filter(pk__in=ids), params, '').select_related(
        'course_college__course', 'course_college__department').prefetch_related('teachers').order_by('-term', '-pk')


def classData(scope, tc):
    course = tc.course_college.course
    return {'id': tc.pk, 'name': tc.name, 'term': tc.term, 'termStatus': scope.termStatus(tc),
            'course': {'id': course.pk, 'code': course.code, 'name': course.name},
            'teachers': [{'id': user.pk, 'name': user.name or user.get_full_name() or user.username}
                         for user in tc.teachers.all()],
            'contentAvailable': tc.pk in scope.contentClassIds,
            'isHistorical': tc.pk not in scope.contentClassIds}


def homeworkQuery(scope, params, now):
    queryFields(params, {'page', 'courseId', 'teachingClassId', 'term', 'scope', 'status',
                         'submissionState', 'q'})
    mode = params.get('scope', 'all')
    if mode not in ('all', 'current', 'history'):
        raise ValidationError('scope 只支持 all/current/history')
    query = scope.homeworks(contentOnly=mode == 'current')
    if mode == 'history':
        query = query.exclude(teaching_class_id__in=scope.contentClassIds)
    query = courseFilters(query, params, 'teaching_class__')
    if 'q' in params:
        query = query.filter(title__icontains=textFilter(params['q']))
    if 'status' in params:
        state = params['status']
        if state == 'pending_start':
            query = query.filter(start_at__gt=now)
        elif state == 'ongoing':
            query = query.filter(start_at__lte=now, deadline__gte=now)
        elif state == 'closed':
            query = query.filter(deadline__lt=now)
        else:
            raise ValidationError('作业状态无效')
    query = query.annotate(problemCount=Count('problems', distinct=True),
        submittedProblemCount=Count('submissions__problem_id', distinct=True, filter=Q(
            submissions__user=scope.user, submissions__submit_type=Submission.SUBMIT_HOMEWORK)))
    if 'submissionState' in params:
        state = params['submissionState']
        if state == 'not_submitted':
            query = query.filter(submittedProblemCount=0)
        elif state == 'partial':
            query = query.filter(submittedProblemCount__gt=0, submittedProblemCount__lt=F('problemCount'))
        elif state == 'submitted':
            query = query.filter(problemCount__gt=0, submittedProblemCount=F('problemCount'))
        else:
            raise ValidationError('提交完成状态无效')
    return query.select_related('teaching_class__course_college__course', 'created_by').order_by('-created_at', '-pk')


def personalRows(scope, homeworks):
    prefetch_related_objects(homeworks, 'problems')
    result = gradeRows(homeworks, scope.roster())
    return {homework.pk: result[homework.pk][0] if result[homework.pk] else None for homework in homeworks}


def applyArchivedOwnScores(homeworks, user):
    records = TeachingClassRecord.objects.filter(teaching_class_id__in={h.teaching_class_id for h in homeworks},
                                                 archived_at__isnull=False)
    books = {int(key): book for record in records for key, book in record.gradebooks.items()}
    for homework in homeworks:
        if homework.pk in books:
            row = next((row for row in books[homework.pk]['students'] if row['id'] == user.pk), None)
            homework.myBestScore = row['score'] if row else None


def homeworkData(scope, homework, row, now):
    tc, creator = homework.teaching_class, homework.created_by
    record = scope.records.get(tc.pk)
    content = tc.pk in scope.contentClassIds
    state = 'pending_start' if now < homework.start_at else 'ongoing' if now <= homework.deadline else 'closed'
    data = {'id': homework.pk, 'title': homework.title, 'teachingClassId': tc.pk,
            'teachingClassName': tc.name, 'courseId': tc.course_college.course_id,
            'courseName': tc.course_college.course.name, 'term': tc.term,
            'termStatus': scope.termStatus(tc), 'startAt': homework.start_at, 'deadline': homework.deadline,
            'status': state, 'teacherName': (creator.name or creator.get_full_name() or creator.username)
                if creator else '（教师已删除）',
            'contentAvailable': content and state != 'pending_start',
            'isHistorical': not content, 'scoreSource': 'archive' if record and record.archived_at else 'live',
            'canSubmit': content and state == 'ongoing' and scope.termStatus(tc) == AcademicTerm.OPEN,
            'answersAvailable': content and homework.answers_released_at is not None and now > homework.deadline,
            'gradeAvailable': row is not None}
    if row is not None:
        for key in ('score', 'totalScore', 'problemCount', 'submittedProblemCount', 'scoredProblemCount',
                    'passedProblemCount', 'submissionCount', 'pendingCount', 'submissionState',
                    'gradingComplete', 'passed', 'unreviewedCount'):
            data[key] = row[key]
        data['myBestScore'], data['mySubmitCount'] = row['score'], row['submissionCount']
    else:
        data.update(score=None, myBestScore=None)
    return data


def ownGrades(scope, homeworkId, now):
    homework = scope.homeworks().select_related('teaching_class__course_college__course',
                                               'created_by').filter(pk=homeworkId).first()
    if homework is None:
        raise Http404('作业不存在')
    if now < homework.start_at:
        raise Http404('作业不存在')
    row = personalRows(scope, [homework])[homework.pk]
    if row is None:
        raise Http404('个人成绩不存在')
    submissions = Submission.objects.filter(user=scope.user, homework=homework,
        submit_type=Submission.SUBMIT_HOMEWORK)
    latest = submissions.annotate(position=Window(RowNumber(), partition_by=[F('problem_id')],
        order_by=[F('created_at').desc(), F('pk').desc()])).filter(position=1)
    latestByProblem = {item.problem_id: item for item in latest}
    reviewed = submissions.filter(teacher_review__isnull=False).select_related('teacher_review__reviewed_by')
    reviewed = reviewed.annotate(position=Window(RowNumber(), partition_by=[F('problem_id')],
        order_by=[F('teacher_review__updated_at').desc(), F('pk').desc()])).filter(position=1)
    reviewByProblem = {item.problem_id: item for item in reviewed}
    points = []
    for original in row['problems']:
        point = {key: original[key] for key in ('problemId', 'title', 'totalScore', 'score', 'passed',
            'submissionCount', 'pendingCount', 'reviewedCount', 'lastSubmittedAt', 'state')}
        item = latestByProblem.get(point['problemId'])
        feedback = reviewByProblem.get(point['problemId'])
        point['latestSubmission'] = None if item is None else {
            'id': item.pk, 'status': item.status, 'score': item.score,
            'createdAt': item.created_at, 'judgedAt': item.judged_at}
        point['teacherReview'] = SubmissionReviewSerializer(feedback.teacher_review).data if feedback else None
        point['feedbackSubmissionId'] = feedback.pk if feedback else None
        points.append(point)
    return {**homeworkData(scope, homework, row, now), 'studentId': scope.user.pk, 'problems': points}


def resourceQuery(scope, params, kind):
    allowed = {'page', 'q', 'courseId', 'teachingClassId', 'term'}
    if kind == 'notices':
        allowed.add('unread')
    queryFields(params, allowed)
    model = ClassNotice if kind == 'notices' else TeachingMaterial
    query = model.objects.filter(teaching_class_id__in=scope.contentClassIds)
    query = courseFilters(query, params, 'teaching_class__')
    if 'q' in params:
        query = query.filter(title__icontains=textFilter(params['q']))
    if kind == 'notices':
        readAt = NoticeRead.objects.filter(notice_id=OuterRef('pk'), user=scope.user).values('read_at')[:1]
        query = query.annotate(myReadAt=Subquery(readAt))
        unreadCount = query.filter(myReadAt__isnull=True).count()
        if 'unread' in params:
            query = query.filter(myReadAt__isnull=booleanFilter(params['unread']))
    else:
        unreadCount = None
    return query.select_related('teaching_class__course_college__course').order_by('-created_at', '-pk'), unreadCount


def dashboard(scope, now):
    from .communication_service import listStudentReminders
    from .serializers import StudentNoticeSerializer
    sections = {name: hasPerm(scope.user, code) for name, code in (
        ('homeworks', 'homework:view'), ('notices', 'notice:view'),
        ('materials', 'material:view'), ('analysis', 'analysis:view_self'))}
    data = {'asOf': now, 'sections': sections, 'homeworks': None, 'notices': None, 'materials': None}
    if sections['homeworks']:
        query = homeworkQuery(scope, {'scope': 'current'}, now)
        ongoing = query.filter(start_at__lte=now, deadline__gte=now)
        pending = ongoing.filter(submittedProblemCount__lt=F('problemCount'))
        soon = pending.filter(deadline__lte=now + timedelta(hours=72)).order_by('deadline', 'pk')
        items = list(soon[:5])
        rows = personalRows(scope, items)
        recent = Submission.objects.filter(user=scope.user).select_related('problem', 'judge_task')
        from .submission_titles import withFrozenTitles
        recent = withFrozenTitles(recent).order_by('-created_at', '-pk')[:5]
        data['homeworks'] = {'upcomingCount': query.filter(start_at__gt=now).count(),
            'ongoingCount': ongoing.count(), 'pendingCount': pending.count(),
            'overdueIncompleteCount': query.filter(deadline__lt=now,
                submittedProblemCount__lt=F('problemCount')).count(),
            'deadlineWindowHours': 72, 'dueSoonCount': soon.count(),
            'dueSoon': [homeworkData(scope, item, rows[item.pk], now) for item in items],
            'recentSubmissions': SubmissionListSerializer(recent, many=True).data}
    if sections['notices']:
        notices, unread = resourceQuery(scope, {}, 'notices')
        reminders = listStudentReminders(scope.user)
        data['notices'] = {'unreadNoticeCount': unread,
            'unreadReminderCount': reminders.filter(read_at__isnull=True).count(),
            'recent': StudentNoticeSerializer(notices[:5], many=True).data}
    if sections['materials']:
        data['materials'] = {'count': TeachingMaterial.objects.filter(
            teaching_class_id__in=scope.contentClassIds).count()}
    return data


def learningAnalysis(scope, params, now):
    queryFields(params, {'days', 'courseId', 'teachingClassId', 'term'})
    raw = params.get('days', '30')
    days = parseObjectId(raw, 'days')
    if days > 366:
        raise ValidationError('days 不能超过 366')
    start = now - timedelta(days=days)
    filters = {key: value for key, value in params.items() if key != 'days'}
    homeworks = list(homeworkQuery(scope, filters, now).filter(start_at__gte=start, start_at__lte=now))
    rows = personalRows(scope, homeworks)

    def stats(items):
        grades = [rows[item.pk] for item in items if rows[item.pk] is not None]
        graded = [grade for grade in grades if grade['gradingComplete'] and grade['totalScore']]
        return {'homeworkCount': len(items),
            'submittedHomeworkCount': sum(row['submittedProblemCount'] > 0 for row in grades),
            'completedHomeworkCount': sum(row['submissionState'] == 'submitted' for row in grades),
            'gradedHomeworkCount': len(graded), 'passedHomeworkCount': sum(row['passed'] for row in grades),
            'completionRate': round(sum(row['submissionState'] == 'submitted' for row in grades) / len(items), 4)
                if items else None,
            'averagePercent': round(sum(row['score'] * 100 / row['totalScore'] for row in graded) / len(graded), 2)
                if graded else None,
            'pendingSubmissionCount': sum(row['pendingCount'] for row in grades)}

    grouped = {}
    for item in homeworks:
        grouped.setdefault(item.teaching_class.course_college.course_id, []).append(item)
    courses = [{'courseId': key, 'courseName': items[0].teaching_class.course_college.course.name,
                **stats(items)} for key, items in sorted(grouped.items())]
    submissions = Submission.objects.filter(user=scope.user, created_at__gte=start, created_at__lte=now,
        homework__in=courseFilters(scope.homeworks(), filters, 'teaching_class__'))
    trend = submissions.annotate(day=TruncDate('created_at')).values('day').annotate(
        submissionCount=Count('pk'), acceptedCount=Count('pk', filter=Q(status=Submission.STATUS_ACCEPTED)),
        pendingCount=Count('pk', filter=Q(status__in=PENDING_STATUSES)),
        systemErrorCount=Count('pk', filter=Q(status=Submission.STATUS_SYSTEM_ERROR)))
    return {'asOf': now, 'windowStart': start, 'days': days, 'summary': stats(homeworks),
            'courses': courses, 'submissionTrend': list(trend.order_by('day'))}

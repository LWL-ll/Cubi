"""教师作业看板、成绩、提交及评阅；统计与导出共用逐题最高有效分。"""
from collections import defaultdict
from io import BytesIO

from django.db import transaction
from django.db.models import Count, F, Max, Q
from django.db.models import prefetch_related_objects
from django.http import Http404
from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from rest_framework.exceptions import ValidationError

from academics.models import ClassMembership, TeachingClassRecord
from academics.term_service import requireTeachingWrite
from teaching.helpers import getTeacherTeachingClass
from teaching.models import Homework, Submission, SubmissionReview

VALID_SCORE_STATUSES = Submission.TERMINAL_STATUSES - {
    Submission.STATUS_SYSTEM_ERROR, Submission.STATUS_REPEAT}
PENDING_STATUSES = (Submission.STATUS_QUEUED, Submission.STATUS_RUNNING)


def classRoster(teachingClass):
    record = TeachingClassRecord.objects.filter(teaching_class=teachingClass).first()
    if record is not None:
        return record.roster
    memberships = ClassMembership.objects.filter(
        admin_class__teaching_classes=teachingClass, left_at__isnull=True
    ).select_related('student_profile__identity__user', 'admin_class').order_by(
        'student_profile__identity__school_number', 'student_profile__identity__user_id')
    seen = set()
    roster = []
    for member in memberships:
        identity = member.student_profile.identity
        user = identity.user
        if user.pk in seen:
            continue
        seen.add(user.pk)
        roster.append({
            'id': user.pk, 'username': user.username, 'name': user.name,
            'schoolNumber': identity.school_number,
            'adminClassId': member.admin_class_id, 'adminClassName': member.admin_class.name,
        })
    return roster


def gradeRows(homeworks, roster):
    """一次聚合多个作业和学生，避免按学生、题目逐条查询。"""
    ids = [student['id'] for student in roster]
    grouped = Submission.objects.filter(
        homework_id__in=[h.pk for h in homeworks], user_id__in=ids,
        submit_type=Submission.SUBMIT_HOMEWORK
    ).values('homework_id', 'user_id', 'problem_id').annotate(
        submissionCount=Count('pk'),
        pendingCount=Count('pk', filter=Q(status__in=PENDING_STATUSES)),
        score=Max('score', filter=Q(status__in=VALID_SCORE_STATUSES)),
        passedCount=Count('pk', filter=Q(status=Submission.STATUS_ACCEPTED)),
        reviewedCount=Count('teacher_review'),
        lastSubmittedAt=Max('created_at'),
    )
    index = {(row['homework_id'], row['user_id'], row['problem_id']): row for row in grouped}
    result = {}
    for homework in homeworks:
        items = list(homework.problems.all())
        rows = []
        for student in roster:
            points = []
            for item in items:
                group = index.get((homework.pk, student['id'], item.problem_id), {})
                count = group.get('submissionCount', 0)
                pending = group.get('pendingCount', 0)
                score = group.get('score')
                passed = bool(group.get('passedCount', 0))
                points.append({
                    'problemId': item.problem_id, 'title': item.problem_snapshot.get('title', ''),
                    'totalScore': sum(int(p.get('points', 0))
                                      for p in item.problem_snapshot.get('tests', [])),
                    'score': score, 'passed': passed, 'submissionCount': count,
                    'pendingCount': pending, 'reviewedCount': group.get('reviewedCount', 0),
                    'lastSubmittedAt': group.get('lastSubmittedAt'),
                    'state': ('not_submitted' if not count else 'judging' if pending
                              else 'system_error' if score is None
                              else 'passed' if passed else 'not_passed'),
                })
            submitted = sum(p['submissionCount'] > 0 for p in points)
            scored = sum(p['score'] is not None for p in points)
            passed = sum(p['passed'] for p in points)
            pending = sum(p['pendingCount'] for p in points)
            submissionCount = sum(p['submissionCount'] for p in points)
            rows.append({
                **student, 'homeworkId': homework.pk, 'problems': points,
                'problemCount': len(points), 'submittedProblemCount': submitted,
                'scoredProblemCount': scored, 'passedProblemCount': passed,
                'submissionCount': submissionCount, 'pendingCount': pending,
                'unreviewedCount': submissionCount - sum(p['reviewedCount'] for p in points),
                'score': sum(p['score'] or 0 for p in points) if scored else None,
                'totalScore': sum(p['totalScore'] for p in points),
                'submissionState': ('not_submitted' if not submitted else
                                    'submitted' if submitted == len(points) else 'partial'),
                'gradingComplete': bool(points) and scored == len(points),
                'passed': bool(points) and passed == len(points),
            })
        result[homework.pk] = rows
    records = TeachingClassRecord.objects.filter(
        teaching_class_id__in={h.teaching_class_id for h in homeworks}, archived_at__isnull=False)
    frozen = {int(key): book for record in records for key, book in record.gradebooks.items()}
    for homework in homeworks:
        if homework.pk in frozen:
            result[homework.pk] = [row for row in frozen[homework.pk]['students'] if row['id'] in ids]
    return result


def gradeSummary(rows):
    graded = [r['score'] for r in rows if r['gradingComplete']]
    return {
        'studentCount': len(rows),
        'submittedStudentCount': sum(r['submittedProblemCount'] > 0 for r in rows),
        'unsubmittedStudentCount': sum(r['submittedProblemCount'] == 0 for r in rows),
        'completedStudentCount': sum(r['submissionState'] == 'submitted' for r in rows),
        'passedStudentCount': sum(r['passed'] for r in rows),
        'pendingSubmissionCount': sum(r['pendingCount'] for r in rows),
        'unreviewedSubmissionCount': sum(r['unreviewedCount'] for r in rows),
        'gradedStudentCount': len(graded),
        'averageScore': round(sum(graded) / len(graded), 2) if graded else None,
    }


def homeworkGradebook(homework):
    record = TeachingClassRecord.objects.filter(teaching_class_id=homework.teaching_class_id,
                                                archived_at__isnull=False).first()
    if record is not None and str(homework.pk) in record.gradebooks:
        return record.gradebooks[str(homework.pk)]
    prefetch_related_objects([homework], 'problems')
    roster = classRoster(homework.teaching_class)
    rows = gradeRows([homework], roster)[homework.pk]
    return {'homeworkId': homework.pk, 'title': homework.title,
            'summary': gradeSummary(rows), 'students': rows,
            'problems': [{'problemId': item.problem_id,
                          'title': item.problem_snapshot.get('title', '')}
                         for item in homework.problems.all()]}


def classDashboard(teachingClass):
    roster = classRoster(teachingClass)
    homeworks = list(Homework.objects.filter(teaching_class=teachingClass).prefetch_related(
        'problems').order_by('-created_at', '-id'))
    results = gradeRows(homeworks, roster)
    homeworkRows = [
        {'id': h.pk, 'title': h.title, 'startAt': h.start_at, 'deadline': h.deadline,
         **gradeSummary(results[h.pk])} for h in homeworks]
    byStudent = defaultdict(list)
    for rows in results.values():
        for row in rows:
            byStudent[row['id']].append(row)
    students = []
    for student in roster:
        grades = byStudent[student['id']]
        scores = [r['score'] for r in grades if r['score'] is not None]
        students.append({
            **student, 'homeworkCount': len(homeworks),
            'submittedHomeworkCount': sum(r['submittedProblemCount'] > 0 for r in grades),
            'completedHomeworkCount': sum(r['submissionState'] == 'submitted' for r in grades),
            'passedHomeworkCount': sum(r['passed'] for r in grades),
            'submissionCount': sum(r['submissionCount'] for r in grades),
            'score': sum(scores) if scores else None,
        })
    return {'teachingClassId': teachingClass.pk, 'studentCount': len(roster),
            'homeworkCount': len(homeworks), 'homeworks': homeworkRows, 'students': students}


def filterStudents(rows, params):
    query = (params.get('q') or '').strip()[:100]
    if query:
        rows = [r for r in rows if any(query.casefold() in r[key].casefold()
                                      for key in ('name', 'username', 'schoolNumber'))]
    state = params.get('submissionState')
    if state:
        if state not in ('not_submitted', 'partial', 'submitted'):
            raise ValidationError({'submissionState': '提交状态筛选无效'})
        rows = [r for r in rows if r.get('submissionState') == state]
    return rows


def teacherSubmissions(teachingClass, params):
    from teaching.submission_service import parseObjectId
    query = Submission.objects.filter(
        homework__teaching_class=teachingClass, submit_type=Submission.SUBMIT_HOMEWORK
    ).select_related('user', 'problem', 'judge_task', 'teacher_review__reviewed_by')
    for key, field in (('homework', 'homework_id'), ('student', 'user_id'), ('problem', 'problem_id')):
        if params.get(key):
            query = query.filter(**{field: parseObjectId(params[key], key)})
    if params.get('status'):
        if params['status'] not in dict(Submission.STATUS_CHOICES):
            raise ValidationError({'status': '判题状态筛选无效'})
        query = query.filter(status=params['status'])
    if params.get('reviewed'):
        if params['reviewed'] not in ('true', 'false'):
            raise ValidationError({'reviewed': 'reviewed 必须是 true/false'})
        query = query.filter(teacher_review__isnull=params['reviewed'] == 'false')
    from .submission_titles import withFrozenTitles
    return withFrozenTitles(query).order_by('-created_at', '-id')


def getTeacherSubmission(user, submissionId):
    from .submission_titles import withFrozenTitles
    submission = withFrozenTitles(Submission.objects.select_related(
        'homework__teaching_class', 'user', 'problem', 'judge_task', 'teacher_review__reviewed_by'
    )).filter(pk=submissionId, submit_type=Submission.SUBMIT_HOMEWORK,
             homework__isnull=False).first()
    if submission is None:
        raise Http404('提交不存在')
    try:
        getTeacherTeachingClass(user, submission.homework.teaching_class_id)
    except Http404:
        raise Http404('提交不存在') from None
    return submission


def saveReview(user, submission, payload):
    from collections.abc import Mapping
    if not isinstance(payload, Mapping) or set(payload) != {'comment'}:
        raise ValidationError('只允许提交 comment 字段')
    comment = payload['comment']
    if not isinstance(comment, str) or not comment.strip() or len(comment) > 5000:
        raise ValidationError('批语须为 1～5000 字的文本')
    try:
        comment.encode('utf-8')
    except UnicodeEncodeError:
        raise ValidationError('批语包含无效字符')
    teachingClass = submission.homework.teaching_class
    with transaction.atomic():
        requireTeachingWrite(teachingClass, allowClosed=True)
        # 同一提交的评阅串行写入，教师身份只取当前会话。
        Submission.objects.filter(pk=submission.pk).update(id=F('id'))
        review, _ = SubmissionReview.objects.update_or_create(
            submission=submission, defaults={'comment': comment.strip(), 'reviewed_by': user})
    return review


def deleteReview(submission):
    teachingClass = submission.homework.teaching_class
    with transaction.atomic():
        requireTeachingWrite(teachingClass, allowClosed=True)
        Submission.objects.filter(pk=submission.pk).update(id=F('id'))
        SubmissionReview.objects.filter(submission=submission).delete()


def exportGradebook(book):
    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet('作业成绩')
    students = book['students']
    problems = book['problems']
    headers = ['账号', '姓名', '学号', '行政班'] + [
        p['title'] for p in problems] + ['总得分', '总分', '提交状态', '有效成绩齐全', '未评阅提交数']

    def append(values):
        cells = []
        for value in values:
            cell = WriteOnlyCell(sheet, value=value)
            if isinstance(value, str):
                cell.data_type = 's'  # Excel 中姓名、题名等始终为文本，不能成为公式。
            cells.append(cell)
        sheet.append(cells)

    append(headers)
    for student in students:
        append([student['username'], student['name'], student['schoolNumber'],
                student['adminClassName']] + [p['score'] for p in student['problems']] + [
                    student['score'], student['totalScore'], student['submissionState'],
                    student['gradingComplete'], student['unreviewedCount']])
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def annotateOwnBestScores(homeworks, user):
    """保持学生列表为可继续查询的 QuerySet，以逐题聚合的子查询计算作业总分。"""
    from django.db.models import IntegerField, OuterRef, Subquery, Sum
    # 分组聚合的二次 SUM 用相关题目子查询，避免 SUM(MAX(...)) 的非法 SQL。
    from teaching.models import HomeworkProblem
    pointBest = Submission.objects.filter(
        homework_id=OuterRef('homework_id'), problem_id=OuterRef('problem_id'),
        user=user, submit_type=Submission.SUBMIT_HOMEWORK,
        status__in=VALID_SCORE_STATUSES
    ).order_by('-score').values('score')[:1]
    scores = HomeworkProblem.objects.filter(homework_id=OuterRef('pk')).order_by().annotate(
        bestScore=Subquery(pointBest, output_field=IntegerField())
    ).values('homework_id').annotate(total=Sum('bestScore')).values('total')[:1]
    return homeworks.annotate(myBestScore=Subquery(scores, output_field=IntegerField()))

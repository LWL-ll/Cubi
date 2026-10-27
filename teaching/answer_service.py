"""教师手工参考答案：提前维护，作业截止后显式公开。"""
from collections.abc import Mapping

from django.db import transaction
from django.http import Http404
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from academics.term_service import requireTeachingWrite

from .homework_service import (getManageableHomework, getStudentHomework,
                               lockHomework)
from .models import HomeworkAnswer


MAX_ANSWER_LENGTH = 50000


def _answerData(answer):
    """只输出教师维护的答案正文，不访问题目或判题快照。"""
    return {
        'problemId': answer.homework_problem.problem_id,
        'content': answer.content,
        'updatedById': answer.updated_by_id,
        'createdAt': answer.created_at,
        'updatedAt': answer.updated_at,
    }


def _answerList(homework):
    answers = HomeworkAnswer.objects.filter(
        homework_problem__homework=homework).select_related(
            'homework_problem').order_by('homework_problem__order', 'pk')
    return {
        'homeworkId': homework.pk,
        'releasedAt': homework.answers_released_at,
        'answers': [_answerData(answer) for answer in answers],
    }


def _lockManageableHomework(user, accessible):
    """与作业编辑、提交和删除共用写锁，并在锁内重查授权。"""
    requireTeachingWrite(accessible.teaching_class, allowClosed=True)
    lockHomework(accessible.pk)
    homework = getManageableHomework(user, accessible.pk)
    requireTeachingWrite(homework.teaching_class, allowClosed=True)
    return homework


def _getHomeworkProblem(homework, problemId):
    item = homework.problems.filter(problem_id=problemId).first()
    if item is None:
        raise Http404('题目不存在')
    return item


def _validateContent(payload):
    if not isinstance(payload, Mapping) or set(payload) != {'content'}:
        raise ValidationError('请求体只能包含 content')
    content = payload['content']
    if not isinstance(content, str) or not content.strip():
        raise ValidationError('参考答案不能为空')
    if len(content) > MAX_ANSWER_LENGTH:
        raise ValidationError(f'参考答案不能超过 {MAX_ANSWER_LENGTH} 字符')
    try:
        content.encode('utf-8')
    except UnicodeEncodeError:
        raise ValidationError('参考答案必须是有效的 UTF-8 文本')
    return content


def listTeacherAnswers(user, homeworkId):
    homework = getManageableHomework(user, homeworkId)
    return _answerList(homework)


def saveAnswer(user, homeworkId, problemId, payload):
    content = _validateContent(payload)
    accessible = getManageableHomework(user, homeworkId)
    with transaction.atomic():
        homework = _lockManageableHomework(user, accessible)
        item = _getHomeworkProblem(homework, problemId)
        answer, _ = HomeworkAnswer.objects.update_or_create(
            homework_problem=item,
            defaults={'content': content, 'updated_by': user})
        return _answerData(answer)


def deleteAnswer(user, homeworkId, problemId):
    accessible = getManageableHomework(user, homeworkId)
    with transaction.atomic():
        homework = _lockManageableHomework(user, accessible)
        item = _getHomeworkProblem(homework, problemId)
        if not HomeworkAnswer.objects.filter(homework_problem=item).delete()[0]:
            raise Http404('参考答案不存在')


def publishAnswers(user, homeworkId, payload):
    if not isinstance(payload, Mapping) or payload:
        raise ValidationError('发布参考答案的请求体必须是空对象')
    accessible = getManageableHomework(user, homeworkId)
    with transaction.atomic():
        homework = _lockManageableHomework(user, accessible)
        if homework.answers_released_at is not None:
            return _answerList(homework)
        now = timezone.now()
        if now <= homework.deadline:
            raise ValidationError('作业截止后才能发布参考答案')
        if not HomeworkAnswer.objects.filter(
                homework_problem__homework=homework).exists():
            raise ValidationError('请先维护至少一道题目的参考答案')
        homework.answers_released_at = now
        homework.save(update_fields=['answers_released_at'])
        return _answerList(homework)


def listStudentAnswers(user, homeworkId):
    try:
        homework = getStudentHomework(user, homeworkId)
    except Http404:
        raise Http404('参考答案不存在')
    if (homework.answers_released_at is None
            or timezone.now() <= homework.deadline):
        raise Http404('参考答案不存在')
    return _answerList(homework)

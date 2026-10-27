"""作业组卷服务层：CRUD + 选题可见性 + 锁定语义（docs/OJ对接设计.md 12.4）

M7 决策（2026-10-18，2026-10-20 题库权限调整）：
- 题库归属：公共题库由系统管理员导入/维护、全平台教师可选题；
  教师个人题库面向教学班级（自建自用）；选题可见集合 =
  公共题 + 本人个人题，他人个人题一律 404（防探测）
- 作业题数上限 20（与测试点上限一致）
- 已有提交 → 题目列表锁定（只允许改标题/时间，改题 400）；
  无提交 → 题目列表可整体替换（revision 重新快照）
- 无提交可删；有提交 400（DB 层 Submission.homework PROTECT 兜底）
- 截止时间只允许向后延长（缩短 400，防误伤已规划学生）
- 参考答案首次公开后永久禁止延期和更换题目（含题序）

权限链：视图层 PermRequired(homework:publish) 管「能不能用功能」，
服务层 getManageableHomework/getTeacherTeachingClass 管
「能不能对这个教学班/这份作业用」，否则 404 不暴露存在性。
"""
import logging
from collections.abc import Mapping

from django.db import transaction
from django.db.models import Count, F, Q
from django.http import Http404
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.exceptions import ValidationError

from academics.models import TeachingClass
from academics.term_service import requireTeachingWrite
from teaching.models import (Homework, HomeworkProblem, Problem, Submission,
                             buildProblemSnapshot)
from teaching.helpers import getStudentTeachingClass
from users.models import SchoolIdentity

logger = logging.getLogger('codemind.homework')

MAX_HOMEWORK_PROBLEMS = 20  # 单份作业题目数上限（M7 决策 a）


def parseDatetimeField(payload, key, label):
    """解析 ISO 时间字段：必填、可解析；naive 时间按本机时区补齐"""
    raw = payload.get(key)
    if not isinstance(raw, str) or not raw:
        raise ValidationError(f'{label}不能为空')
    try:
        parsed = parse_datetime(raw)
    except ValueError:
        parsed = None
    if parsed is None:
        raise ValidationError(f'{label}格式错误（需 ISO 时间，如 2026-10-20T23:59）')
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed)
    return parsed


def validateHomeworkPayload(payload):
    """组卷请求校验（按脏数据）：标题/起止时间/题目数组

    返回 (title, startAt, deadline, problemIds)。
    """
    if not isinstance(payload, Mapping):
        raise ValidationError('请求体必须是对象')
    title = payload.get('title')
    if not isinstance(title, str):
        raise ValidationError('作业标题必须是字符串')
    title = title.strip()
    if not title:
        raise ValidationError('作业标题不能为空')
    if len(title) > 100:
        raise ValidationError('作业标题不能超过 100 字符')
    startAt = parseDatetimeField(payload, 'startAt', '开始时间')
    deadline = parseDatetimeField(payload, 'deadline', '截止时间')
    if startAt >= deadline:
        raise ValidationError('截止时间必须晚于开始时间')
    problemIds = payload.get('problemIds')
    if not isinstance(problemIds, list) or not problemIds:
        raise ValidationError('problemIds 必须是题目 id 数组且不能为空')
    if len(problemIds) > MAX_HOMEWORK_PROBLEMS:
        raise ValidationError(f'单份作业最多 {MAX_HOMEWORK_PROBLEMS} 道题')
    for problemId in problemIds:
        if type(problemId) is not int or not 0 < problemId < 2 ** 63:
            raise ValidationError('problemIds 必须是有效的正整数数组')
    if len(set(problemIds)) != len(problemIds):
        raise ValidationError('problemIds 不能有重复项')
    return title, startAt, deadline, problemIds


def loadSelectableProblems(user, problemIds):
    """选题可见性校验：题目存在 + 公共题或本人个人题（超管放行）

    保持 problemIds 的数组顺序（order = 数组序）；越权与不存在
    统一 404，不暴露他人题库存在性。
    """
    problemsById = {p.id: p for p in Problem.objects.filter(pk__in=problemIds)}
    ordered = []
    for problemId in problemIds:
        problem = problemsById.get(problemId)
        if problem is None or not (
                user.is_superuser or problem.is_public
                or problem.owner_id == user.id):
            raise Http404('题目不存在')
        ordered.append(problem)
    return ordered


def listClassHomeworks(user, teachingClass):
    """该教学班作业列表（教师视角）：题数 + 提交数聚合，created_at 倒序"""
    return Homework.objects.filter(teaching_class=teachingClass) \
        .annotate(problemCount=Count('problems', distinct=True),
                  submissionCount=Count('submissions', distinct=True)) \
        .order_by('-created_at')


def createHomework(user, teachingClass, payload):
    """创建作业：校验 → 选题 → 建 Homework + 中间表（revision 快照冻结）"""
    title, startAt, deadline, problemIds = validateHomeworkPayload(payload)
    problems = loadSelectableProblems(user, problemIds)
    with transaction.atomic():
        requireTeachingWrite(teachingClass)
        homework = Homework.objects.create(
            teaching_class=teachingClass, title=title,
            start_at=startAt, deadline=deadline, created_by=user)
        HomeworkProblem.objects.bulk_create([
            HomeworkProblem(homework=homework, problem=problem,
                            order=index,
                            problem_revision=problem.revision,
                            problem_snapshot=buildProblemSnapshot(problem))
            for index, problem in enumerate(problems)
        ])
    return homework


def getManageableHomework(user, homeworkId):
    """教师对象级取作业：本人教学班的作业（超管放行），否则 404"""
    homework = Homework.objects.select_related(
        'teaching_class').filter(pk=homeworkId).first()
    if homework is None:
        raise Http404('作业不存在')
    if not (user.is_superuser or homework.teaching_class.teachers
            .filter(pk=user.pk).exists()):
        raise Http404('作业不存在')
    return homework


def getCurrentProblemIds(homework):
    """当前作业题目 id 序列（按题序）"""
    return [item.problem_id
            for item in homework.problems.order_by('order')]


def lockHomework(homeworkId):
    """事务内先取得写锁再读取，提交与教师修改共用（SQLite/PG）。"""
    if not Homework.objects.filter(pk=homeworkId).update(id=F('id')):
        raise Http404('作业不存在')
    return Homework.objects.get(pk=homeworkId)


def updateHomework(user, homework, payload):
    """编辑作业（PUT 全量语义）：

    - 标题/开始时间可改；截止时间只允许向后延长（M7 决策 f）
    - 已有提交 → 题目列表锁定，改题 400（M7 决策 c）
    - 无提交 → 题目列表整体替换，revision 重新快照
    - 参考答案已公开 → 截止时间与题目列表锁定，标题仍可修改
    （选题可见性以请求者身份校验，入口已由 getManageableHomework 保证是本班教师）
    """
    title, startAt, deadline, problemIds = validateHomeworkPayload(payload)
    with transaction.atomic():
        requireTeachingWrite(homework.teaching_class)
        homework = lockHomework(homework.pk)
        if deadline < homework.deadline:
            raise ValidationError('截止时间只允许向后延长')
        problemIdsChanged = problemIds != getCurrentProblemIds(homework)
        if homework.answers_released_at is not None:
            if deadline > homework.deadline:
                raise ValidationError('参考答案已公开，不能延长截止时间')
            if problemIdsChanged:
                raise ValidationError('参考答案已公开，题目不可变更')
        if problemIdsChanged and Submission.objects.filter(homework=homework).exists():
            raise ValidationError('已有提交，题目不可变更')
        if problemIdsChanged:
            # 先腾位再写：删旧中间表后按新序重建（避开 (homework, order) 唯一约束）
            homework.problems.all().delete()
            problems = loadSelectableProblems(user, problemIds)
            HomeworkProblem.objects.bulk_create([
                HomeworkProblem(homework=homework, problem=problem,
                                order=index,
                                problem_revision=problem.revision,
                                problem_snapshot=buildProblemSnapshot(problem))
                for index, problem in enumerate(problems)
            ])
        homework.title = title
        homework.start_at = startAt
        homework.deadline = deadline
        homework.save()
    return homework


def deleteHomework(homework):
    """删除作业：有提交 400（历史数据保护），无提交连带中间表删除"""
    with transaction.atomic():
        requireTeachingWrite(homework.teaching_class)
        homework = lockHomework(homework.pk)
        if Submission.objects.filter(homework=homework).exists():
            raise ValidationError('已有提交的作业不可删除')
        homework.delete()


# ================= 学生侧（homework:view，M7） =================

def getStudentHomeworkQuery(user):
    """学生所在教学班的作业集合（3 跳推导，与 getStudentTeachingClass 同链）

    超管返回全部作业；非学生身份返回空集（权限层已挡，双保险）。
    """
    if user.is_superuser:
        return Homework.objects.all()
    identity = user.identities.filter(
        identity_type=SchoolIdentity.IDENTITY_STUDENT).first()
    if identity is None or not hasattr(identity, 'student_profile'):
        return Homework.objects.none()
    teachingClassIds = TeachingClass.objects.filter(
        admin_classes__memberships__student_profile=identity.student_profile,
        admin_classes__memberships__left_at__isnull=True) \
        .values_list('id', flat=True).distinct()
    return Homework.objects.filter(teaching_class_id__in=teachingClassIds)


def annotateStudentStats(homeworkQuery, user):
    """学生与教师使用同一口径：每题最高有效分，再合计为作业成绩。"""
    from .teacher_service import annotateOwnBestScores
    return annotateOwnBestScores(homeworkQuery.annotate(
        problemCount=Count('problems', distinct=True),
        mySubmitCount=Count('submissions',
                            filter=Q(submissions__user=user), distinct=True)), user)


def getStudentHomework(user, homeworkId):
    """学生对象级取作业：属于该作业的教学班（3 跳），否则 404

    开始前 404（防提前看题，M7 决策 e）；截止后仍可查看与我的提交。
    """
    homework = Homework.objects.filter(pk=homeworkId).first()
    if homework is None:
        raise Http404('作业不存在')
    getStudentTeachingClass(user, homework.teaching_class_id)
    if timezone.now() < homework.start_at:
        raise Http404('作业不存在')
    return homework


def listStudentHomeworks(user):
    """我的作业列表：状态由前端按 startAt/deadline 派生（后端给原始时间）"""
    return annotateStudentStats(getStudentHomeworkQuery(user), user) \
        .select_related('teaching_class') \
        .order_by('-created_at')


def getStudentHomeworkProblem(user, homeworkId, problemId):
    """题面只在已开始且学生所属的作业中可见，截止后仍可回看。"""
    if not str(homeworkId or '').isdigit():
        raise Http404('题目不存在')
    homework = getStudentHomework(user, homeworkId)
    item = homework.problems.filter(
        problem_id=problemId).first()
    if item is None:
        raise Http404('题目不存在')
    return item.getFrozenProblem()

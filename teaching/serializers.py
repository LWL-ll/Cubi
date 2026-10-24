"""教师工作台序列化器（列表项轻量数据；详情复用 academics 的序列化器）"""
from django.utils import timezone
from rest_framework import serializers

from academics.models import ClassMembership, TeachingClass
from academics.serializers import (CourseBriefSerializer,
                                   DepartmentBriefSerializer)

from .agent_service import getTutorReport


class TeacherTeachingClassSerializer(serializers.ModelSerializer):
    """我的教学班列表项：名称、学期、课程、学院、学生数

    学生数经「组成行政班 → 当前成员关系」推导。
    """
    course = CourseBriefSerializer(source='course_college.course', read_only=True)
    department = DepartmentBriefSerializer(
        source='course_college.department', read_only=True)
    studentCount = serializers.SerializerMethodField()

    class Meta:
        model = TeachingClass
        fields = ['id', 'name', 'term', 'course', 'department', 'studentCount']

    def get_studentCount(self, obj):
        """当前在班成员数 = 组成行政班的成员关系聚合（多班合班自动去重场景下
        按成员关系计数，一人一班约束保证不重复）"""
        return ClassMembership.objects.filter(
            admin_class__teaching_classes=obj,
            left_at__isnull=True).count()


class SubmissionListSerializer(serializers.Serializer):
    """学生提交列表项：字段白名单（docs/OJ对接设计.md 12.2）

    白名单原则：只含提交人本人可见字段，绝不含测试点数据、预期输出、
    spj 输出、他人提交信息。字段全显式声明（不用 ModelSerializer），
    白名单一目了然；题目被删后 problemId/problemTitle 为 null（提交保留）。
    """
    id = serializers.IntegerField(read_only=True)
    problemId = serializers.IntegerField(source='problem_id', read_only=True)
    problemTitle = serializers.SerializerMethodField()
    homeworkId = serializers.IntegerField(source='homework_id', read_only=True)
    submitType = serializers.CharField(source='submit_type', read_only=True)
    language = serializers.CharField(read_only=True)
    status = serializers.CharField(read_only=True)
    score = serializers.IntegerField(read_only=True)
    totalScore = serializers.IntegerField(source='total_score', read_only=True)
    timeUsed = serializers.IntegerField(source='time_used', read_only=True)
    memoryUsed = serializers.IntegerField(source='memory_used', read_only=True)
    errorDetail = serializers.CharField(source='error_detail', read_only=True)
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    judgedAt = serializers.DateTimeField(source='judged_at', read_only=True)
    progress = serializers.SerializerMethodField()

    def get_problemTitle(self, obj):
        return obj.problem.title if obj.problem else None

    def get_progress(self, obj):
        """判题进度从 JudgeTask 读（前端轮询的唯一来源，第八章）"""
        task = getattr(obj, 'judge_task', None)
        return task.progress if task else 0


class SubmissionDetailSerializer(SubmissionListSerializer):
    """学生提交详情：列表字段 + code 原文 + Agent 讲题报告摘要

    code 仅本人可见（对象级 404 已保证）；教师看学生代码走
    教学班聚合接口（analysis:view），不走本接口。
    agentReport 只含学生可见字段：correctedCode 是 Agent 自测代码，
    绝不下发（docs/OJ系统介绍.md 保密链）。
    """
    code = serializers.CharField(read_only=True)
    repeatOfId = serializers.IntegerField(source='repeat_of_id', read_only=True)
    agentReport = serializers.SerializerMethodField()

    def get_agentReport(self, obj):
        """Agent 讲题报告摘要；无报告（Agent 未启用/已通过）返回 None"""
        report = getTutorReport(obj)
        if report is None:
            return None
        return {
            'status': report.status,
            'diagnosis': report.diagnosis,
            'explanation': report.explanation,
            'iterations': report.iterations,
        }


class ProblemListSerializer(serializers.Serializer):
    """教师题库列表项（白名单：不含 tests/spj_code/statement_md）

    保密链（OJ对接设计.md 14.7）：测试点数据与 spj 源码只存在于服务端
    判题链路，任何列表接口一律不含这些字段。
    """
    id = serializers.IntegerField(read_only=True)
    title = serializers.CharField(read_only=True)
    tier = serializers.CharField(read_only=True)
    compareStrategy = serializers.CharField(source='compare_strategy',
                                            read_only=True)
    languages = serializers.JSONField(read_only=True)
    hasSpj = serializers.BooleanField(source='has_spj', read_only=True)
    isPublic = serializers.BooleanField(source='is_public', read_only=True)
    revision = serializers.IntegerField(read_only=True)
    ownerName = serializers.SerializerMethodField()
    sampleCount = serializers.SerializerMethodField()
    testCount = serializers.SerializerMethodField()
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    updatedAt = serializers.DateTimeField(source='updated_at', read_only=True)

    def get_ownerName(self, obj):
        """归属展示：公共题显示课程组，个人题显示教师姓名（owner 可能已删号）"""
        if obj.is_public:
            return '公共题库'
        return obj.owner.get_full_name() or obj.owner.username \
            if obj.owner else '（教师已删除）'

    def get_sampleCount(self, obj):
        return len(obj.samples or [])

    def get_testCount(self, obj):
        return len(obj.tests or [])


class ProblemDetailSerializer(ProblemListSerializer):
    """教师题目详情：列表字段 + 题面与公开样例（仍不含 tests/spj_code）"""
    statementMd = serializers.CharField(source='statement_md', read_only=True)
    samples = serializers.JSONField(read_only=True)


class ProblemStudentSerializer(serializers.Serializer):
    """学生题面详情（homework:view）：只给读题所需字段

    白名单：title/statement_md/samples/languages——不含 tests、spj_code、
    tier/compare_strategy 等判题内部配置（6.2 不可变边界）。
    """
    id = serializers.IntegerField(read_only=True)
    title = serializers.CharField(read_only=True)
    statementMd = serializers.CharField(source='statement_md', read_only=True)
    samples = serializers.JSONField(read_only=True)
    languages = serializers.JSONField(read_only=True)


class HomeworkListSerializer(serializers.Serializer):
    """教师作业列表项（白名单：不含学生提交明细，统计走聚合字段）"""
    id = serializers.IntegerField(read_only=True)
    title = serializers.CharField(read_only=True)
    teachingClassId = serializers.IntegerField(source='teaching_class_id',
                                               read_only=True)
    startAt = serializers.DateTimeField(source='start_at', read_only=True)
    deadline = serializers.DateTimeField(read_only=True)
    problemCount = serializers.IntegerField(read_only=True)
    submissionCount = serializers.IntegerField(read_only=True)
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)


class HomeworkProblemItemSerializer(serializers.Serializer):
    """教师作业详情内的题目项：题序 + 选题时修订号快照（12.4）

    不含 tests/spj_code（保密链 6.2）；tier/languages 属题目公开配置，
    教师端可见。problemId 是题目主键（非中间表主键）。
    """
    problemId = serializers.IntegerField(source='problem_id', read_only=True)
    title = serializers.CharField(source='problem_snapshot.title', read_only=True)
    order = serializers.IntegerField(read_only=True)
    problemRevision = serializers.IntegerField(source='problem_revision',
                                               read_only=True)
    tier = serializers.CharField(source='problem_snapshot.tier', read_only=True)
    languages = serializers.JSONField(source='problem_snapshot.languages', read_only=True)
    hasSpj = serializers.BooleanField(source='problem_snapshot.has_spj', read_only=True)


class HomeworkDetailSerializer(HomeworkListSerializer):
    """教师作业详情：列表字段 + 题目列表（按题序）"""
    problems = HomeworkProblemItemSerializer(many=True, read_only=True)


class SubmissionBriefSerializer(serializers.Serializer):
    """学生作业详情内我的提交摘要：终态与分数（不含 code 原文）"""
    id = serializers.IntegerField(read_only=True)
    problemId = serializers.IntegerField(source='problem_id', read_only=True)
    status = serializers.CharField(read_only=True)
    score = serializers.IntegerField(read_only=True)
    totalScore = serializers.IntegerField(source='total_score', read_only=True)
    timeUsed = serializers.IntegerField(source='time_used', read_only=True)
    memoryUsed = serializers.IntegerField(source='memory_used', read_only=True)
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)


class HomeworkStudentListSerializer(serializers.Serializer):
    """学生作业列表项：时间 + 题数 + 我的提交概况

    status 由服务端按当前时间派生（未开始/进行中/已截止），
    前端据此渲染徽章；教师姓名取自发布教师（可能已删号）。
    """
    id = serializers.IntegerField(read_only=True)
    title = serializers.CharField(read_only=True)
    startAt = serializers.DateTimeField(source='start_at', read_only=True)
    deadline = serializers.DateTimeField(read_only=True)
    problemCount = serializers.IntegerField(read_only=True)
    mySubmitCount = serializers.IntegerField(read_only=True)
    myBestScore = serializers.IntegerField(read_only=True)
    status = serializers.SerializerMethodField()
    teacherName = serializers.SerializerMethodField()

    def get_status(self, obj):
        """三态派生：未开始 / 进行中 / 已截止"""
        now = timezone.now()
        if now < obj.start_at:
            return 'pending_start'
        if now <= obj.deadline:
            return 'ongoing'
        return 'closed'

    def get_teacherName(self, obj):
        """发布教师姓名；删号后显示占位（Homework.created_by SET_NULL）"""
        creator = getattr(obj, 'created_by', None)
        if creator is None:
            return '（教师已删除）'
        return creator.get_full_name() or creator.username


class HomeworkStudentDetailSerializer(serializers.Serializer):
    """学生作业详情：时间 + 题目列表（学生白名单）+ 我的提交摘要

    题目复用 ProblemStudentSerializer 白名单（不含 tests/spj_code）；
    开始前详情已被服务层 404（防提前看题，12.4）。
    """
    id = serializers.IntegerField(read_only=True)
    title = serializers.CharField(read_only=True)
    startAt = serializers.DateTimeField(source='start_at', read_only=True)
    deadline = serializers.DateTimeField(read_only=True)
    status = serializers.SerializerMethodField()
    problems = serializers.SerializerMethodField()
    mySubmissions = SubmissionBriefSerializer(source='my_submissions_cache',
                                              many=True, read_only=True)

    def get_status(self, obj):
        now = timezone.now()
        if now < obj.start_at:
            return 'pending_start'
        if now <= obj.deadline:
            return 'ongoing'
        return 'closed'

    def get_problems(self, obj):
        """题目列表按题序输出，复用学生题面白名单"""
        problems = [item.getFrozenProblem() for item in obj.problems.all()]
        return ProblemStudentSerializer(problems, many=True).data

"""教师工作台序列化器（列表项轻量数据；详情复用 academics 的序列化器）"""
from rest_framework import serializers

from academics.models import ClassMembership, TeachingClass
from academics.serializers import (CourseBriefSerializer,
                                   DepartmentBriefSerializer)


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
    """学生提交详情：列表字段 + code 原文与重复关联

    code 仅本人可见（对象级 404 已保证）；教师看学生代码走
    教学班聚合接口（analysis:view），不走本接口。
    """
    code = serializers.CharField(read_only=True)
    repeatOfId = serializers.IntegerField(source='repeat_of_id', read_only=True)

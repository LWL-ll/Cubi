"""教务业务序列化器（前后端分离 API 用）

V2 表结构（docs/数据库表重构设计.md）：
学生经 成员关系 → 行政班 → 教学班 推导，教师任课挂教学班。
"""
from rest_framework import serializers

from .models import (AdminClass, ClassMembership, Course, CourseCollege,
                     Department, Major, TeachingClass)


class DepartmentBriefSerializer(serializers.ModelSerializer):
    """学院简要信息（课程/课程×学院里嵌套展示用）"""

    class Meta:
        model = Department
        fields = ['id', 'code', 'name']


class DepartmentSerializer(serializers.ModelSerializer):
    """学院详情：含行政班数量"""
    adminClassCount = serializers.IntegerField(
        source='admin_classes.count', read_only=True)

    class Meta:
        model = Department
        fields = ['id', 'code', 'name', 'is_active', 'adminClassCount']


class MajorSerializer(serializers.ModelSerializer):
    """专业详情：含所属学院（行政班详情里嵌套展示用）"""
    department = DepartmentBriefSerializer(read_only=True)

    class Meta:
        model = Major
        fields = ['id', 'code', 'name', 'department']


class UserBriefSerializer(serializers.Serializer):
    """用户简要信息（学生/教师/组长列表用）：id、账号、姓名"""
    id = serializers.IntegerField()
    username = serializers.CharField()
    name = serializers.CharField()


class CourseBriefSerializer(serializers.ModelSerializer):
    """课程简要信息（行政班/教学班详情里嵌套展示用）"""

    class Meta:
        model = Course
        fields = ['id', 'code', 'name']


class AdminClassBriefSerializer(serializers.ModelSerializer):
    """行政班简要信息（课程×学院/教学班详情里嵌套展示用）"""
    studentCount = serializers.IntegerField(
        source='memberships.count', read_only=True)

    class Meta:
        model = AdminClass
        fields = ['id', 'name', 'studentCount']


class TeachingClassBriefSerializer(serializers.ModelSerializer):
    """教学班简要信息（课程×学院详情里嵌套展示用）"""
    studentCount = serializers.SerializerMethodField()

    class Meta:
        model = TeachingClass
        fields = ['id', 'name', 'term', 'studentCount']

    def get_studentCount(self, obj):
        """学生数 = 组成行政班的当前成员数之和（经成员关系推导）"""
        return ClassMembership.objects.filter(
            admin_class__teaching_classes=obj,
            left_at__isnull=True).count()


class TeachingClassSerializer(serializers.ModelSerializer):
    """教学班详情（教务/教师视角）：课程、学院、组成行政班、授课教师、
    学生名单（经成员关系聚合，多班合班自动去重）、学生数"""
    course = CourseBriefSerializer(source='course_college.course', read_only=True)
    department = DepartmentBriefSerializer(
        source='course_college.department', read_only=True)
    admin_classes = AdminClassBriefSerializer(many=True, read_only=True)
    teachers = UserBriefSerializer(many=True, read_only=True)
    students = serializers.SerializerMethodField()
    studentCount = serializers.SerializerMethodField()

    class Meta:
        model = TeachingClass
        fields = ['id', 'name', 'term', 'course', 'department',
                  'admin_classes', 'teachers', 'students', 'studentCount']

    def get_students(self, obj):
        """学生名单 = 组成行政班的当前成员聚合；一人一班约束下天然唯一，
        这里仍按用户 id 去重兜底（防御脏数据）"""
        memberships = ClassMembership.objects.filter(
            admin_class__teaching_classes=obj,
            left_at__isnull=True).select_related(
            'student_profile__identity__user')
        seenIds = set()
        users = []
        for membership in memberships:
            user = membership.student_profile.identity.user
            if user.id not in seenIds:
                seenIds.add(user.id)
                users.append(user)
        return UserBriefSerializer(users, many=True).data

    def get_studentCount(self, obj):
        """与简要序列化器同一套推导口径"""
        return ClassMembership.objects.filter(
            admin_class__teaching_classes=obj,
            left_at__isnull=True).count()


class CourseCollegeSerializer(serializers.ModelSerializer):
    """课程×学院 详情：学院、组长、教师池、行政班列表、教学班列表"""
    department = DepartmentBriefSerializer(read_only=True)
    leader = UserBriefSerializer(read_only=True)
    teachers = UserBriefSerializer(many=True, read_only=True)
    admin_classes = AdminClassBriefSerializer(many=True, read_only=True)
    teaching_classes = TeachingClassBriefSerializer(many=True, read_only=True)

    class Meta:
        model = CourseCollege
        fields = ['id', 'department', 'leader', 'teachers',
                  'admin_classes', 'teaching_classes']


class CourseSerializer(serializers.ModelSerializer):
    """课程详情：含开设学院列表"""
    departments = DepartmentBriefSerializer(many=True, read_only=True)

    class Meta:
        model = Course
        fields = ['id', 'code', 'name', 'departments']


class AdminClassSerializer(serializers.ModelSerializer):
    """行政班详情：所属专业（含学院）、课程列表、当前学生列表"""
    major = MajorSerializer(read_only=True)
    courses = serializers.SerializerMethodField()
    students = serializers.SerializerMethodField()

    class Meta:
        model = AdminClass
        fields = ['id', 'name', 'code', 'major', 'enrollment_year',
                  'courses', 'students']

    def get_courses(self, obj):
        """班级的课程 = 关联到本班级的所有 课程×学院 里的课程"""
        courses = Course.objects.filter(
            course_colleges__admin_classes=obj).distinct()
        return CourseBriefSerializer(courses, many=True).data

    def get_students(self, obj):
        """学生经成员关系推导（left_at 为空 = 当前在班），只列当前成员"""
        memberships = obj.memberships.select_related(
            'student_profile__identity__user').filter(left_at__isnull=True)
        return UserBriefSerializer(
            [m.student_profile.identity.user for m in memberships],
            many=True).data

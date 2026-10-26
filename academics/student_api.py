"""教务学生档案与当前成员管理；学籍状态不随转班、退班改变。"""
from collections.abc import Mapping

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Exists, OuterRef, Prefetch, Q, Subquery
from django.http import Http404
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from users.models import SchoolIdentity, StudentProfile
from users.permissions import PermRequired
from .models import AdminClass, ClassMembership
from .services import leaveStudentToday, transferStudentToday


class StudentPagination(PageNumberPagination):
    page_size = 20


def positiveId(value, name):
    if (isinstance(value, str) and len(value) <= 19
            and value.isascii() and value.isdecimal()):
        value = int(value)
    if type(value) is not int or not 0 < value <= 9223372036854775807:
        raise ValidationError(f'{name} 必须为正整数')
    return value


def studentProfiles():
    # 与学生权限 helper 一致：只采用该用户最早的学生身份，缺档案不另选身份。
    firstIdentity = SchoolIdentity.objects.filter(
        user_id=OuterRef('identity__user_id'),
        identity_type=SchoolIdentity.IDENTITY_STUDENT).order_by('pk').values('pk')[:1]
    current = ClassMembership.objects.filter(left_at__isnull=True).select_related('admin_class')
    return StudentProfile.objects.filter(
        identity__identity_type=SchoolIdentity.IDENTITY_STUDENT,
        identity_id=Subquery(firstIdentity)).select_related(
            'identity__user', 'major__department').prefetch_related(
                Prefetch('memberships', queryset=current, to_attr='current_memberships'))


def getStudent(studentId):
    studentId = positiveId(studentId, 'studentId')
    profile = studentProfiles().filter(identity__user_id=studentId).first()
    if profile is None:
        raise Http404('学生不存在')
    return profile


def membershipData(member):
    return {
        'id': member.pk, 'adminClassId': member.admin_class_id,
        'adminClassName': member.admin_class.name,
        'joinedAt': member.joined_at.isoformat(),
        'leftAt': member.left_at.isoformat() if member.left_at else None,
    }


def studentData(profile):
    major = profile.major
    department = major.department
    member = profile.current_memberships[0] if profile.current_memberships else None
    current = membershipData(member) if member else None
    if current is not None:
        current.pop('leftAt')
    return {
        'id': profile.identity.user_id,
        'username': profile.identity.user.username,
        'name': profile.identity.user.name,
        'schoolNumber': profile.identity.school_number,
        'major': {'id': major.pk, 'code': major.code, 'name': major.name,
                  'department': {'id': department.pk, 'code': department.code,
                                 'name': department.name}},
        'enrollmentYear': profile.enrollment_year,
        'academicStatus': profile.academic_status,
        'currentMembership': current,
    }


def filterStudents(queryset, params):
    for field, lookup in (('adminClass', 'memberships__admin_class_id'),
                          ('major', 'major_id'), ('department', 'major__department_id')):
        if field not in params:
            continue
        value = positiveId(params.get(field), field)
        if field == 'adminClass':
            queryset = queryset.filter(memberships__admin_class_id=value,
                                       memberships__left_at__isnull=True)
        else:
            queryset = queryset.filter(**{lookup: value})
    if 'academicStatus' in params:
        status = params.get('academicStatus')
        if status not in dict(StudentProfile._meta.get_field('academic_status').choices):
            raise ValidationError('academicStatus 无效')
        queryset = queryset.filter(academic_status=status)
    if 'inClass' in params:
        value = params.get('inClass')
        if value not in ('true', 'false'):
            raise ValidationError('inClass 只能为 true 或 false')
        current = ClassMembership.objects.filter(
            student_profile_id=OuterRef('pk'), left_at__isnull=True)
        queryset = queryset.alias(hasCurrent=Exists(current)).filter(hasCurrent=value == 'true')
    if 'q' in params:
        query = params.get('q')
        if not isinstance(query, str) or len(query) > 100:
            raise ValidationError('q 须为不超过 100 字的文本')
        try:
            query.encode('utf-8')
        except UnicodeEncodeError:
            raise ValidationError('q 包含无效字符')
        if '\x00' in query:
            raise ValidationError('q 包含无效字符')
        query = query.strip()
        if query:
            queryset = queryset.filter(
                Q(identity__user__name__icontains=query)
                | Q(identity__user__username__icontains=query)
                | Q(identity__school_number__icontains=query))
    return queryset.order_by('identity__school_number', 'identity__user_id')


class StudentListView(APIView):
    permission_classes = [PermRequired]
    permCode = 'class:manage'

    def get(self, request):
        queryset = filterStudents(studentProfiles(), request.query_params)
        paginator = StudentPagination()
        page = paginator.paginate_queryset(queryset, request)
        return paginator.get_paginated_response([studentData(item) for item in page])


class StudentDetailView(APIView):
    permission_classes = [PermRequired]
    permCode = 'class:manage'

    def get(self, request, studentId):
        return Response(studentData(getStudent(studentId)))


class StudentMembershipListView(APIView):
    permission_classes = [PermRequired]
    permCode = 'class:manage'

    def get(self, request, studentId):
        profile = getStudent(studentId)
        members = profile.memberships.select_related('admin_class').order_by('-joined_at', '-id')
        paginator = StudentPagination()
        page = paginator.paginate_queryset(members, request)
        return paginator.get_paginated_response([membershipData(item) for item in page])


class StudentTransferView(APIView):
    permission_classes = [PermRequired]
    permCode = 'class:manage'

    def post(self, request, studentId):
        profile = getStudent(studentId)
        if not isinstance(request.data, Mapping) or set(request.data) != {'adminClassId'}:
            raise ValidationError('请求体只能包含 adminClassId')
        targetId = positiveId(request.data['adminClassId'], 'adminClassId')
        target = AdminClass.objects.filter(pk=targetId).select_related('major__department').first()
        if target is None:
            raise Http404('行政班不存在')
        try:
            transferStudentToday(profile, target)
        except DjangoValidationError as error:
            raise ValidationError(error.messages) from None
        return Response(studentData(getStudent(studentId)))


class StudentLeaveView(APIView):
    permission_classes = [PermRequired]
    permCode = 'class:manage'

    def post(self, request, studentId):
        profile = getStudent(studentId)
        if not isinstance(request.data, Mapping) or request.data:
            raise ValidationError('请求体只能是空对象')
        try:
            leaveStudentToday(profile)
        except DjangoValidationError as error:
            raise ValidationError(error.messages) from None
        return Response(studentData(getStudent(studentId)))

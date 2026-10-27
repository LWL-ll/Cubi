"""普通教师、学生账号维护；高权限目标及密码重置由系统管理员处理。"""
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import F, Prefetch, Q
from django.http import Http404
from django.views.decorators.debug import sensitive_variables

from academics.base_service import integerId, objectData, textValue
from academics.models import Department
from .models import SchoolIdentity, TeacherProfile, User
from .permissions import GROUP_NAMES, GROUP_PERMISSIONS, PERM_CODES, hasPerm


def requireAccountManager(actor):
    if not actor.is_authenticated or not actor.is_active or not hasPerm(actor, 'class:manage'):
        raise PermissionDenied('没有账号管理权限')


def ordinaryAccounts():
    permittedCodes = {PERM_CODES[code] for group in ('teacher', 'student')
                      for code in GROUP_PERMISSIONS[group]}
    privilegedPermissions = Permission.objects.exclude(
        content_type__app_label='users', codename__in=permittedCodes)
    privileged = User.objects.filter(
        Q(is_staff=True) | Q(is_superuser=True)
        | Q(groups__name__in=(GROUP_NAMES['admin'], GROUP_NAMES['course_team']))
        | Q(user_permissions__isnull=False)
        | Q(groups__permissions__in=privilegedPermissions)).values('pk')
    return User.objects.filter(pk__in=User.objects.filter(
        Q(groups__name__in=(GROUP_NAMES['teacher'], GROUP_NAMES['student']))
        | Q(identities__identity_type__in=('teacher', 'student'))).values('pk')).exclude(
            pk__in=privileged)


def managedAccounts(actor):
    requireAccountManager(actor)
    queryset = User.objects.all() if actor.is_superuser else ordinaryAccounts()
    identities = SchoolIdentity.objects.select_related(
        'teacher_profile__department', 'student_profile__major').order_by('pk')
    return queryset.prefetch_related('groups', Prefetch('identities', queryset=identities))


def getManagedAccount(actor, accountId):
    account = managedAccounts(actor).filter(pk=integerId(accountId, 'accountId')).first()
    if account is None:
        raise Http404('账号不存在或不可管理')
    return account


def accountData(account):
    identities = []
    for identity in account.identities.all():
        data = {'id': identity.pk, 'schoolCode': identity.school_code,
                'schoolNumber': identity.school_number, 'identityType': identity.identity_type}
        if identity.identity_type == SchoolIdentity.IDENTITY_TEACHER:
            profile = getattr(identity, 'teacher_profile', None)
            data['teacher'] = None if profile is None else {
                'departmentId': profile.department_id, 'title': profile.title}
        if identity.identity_type == SchoolIdentity.IDENTITY_STUDENT:
            profile = getattr(identity, 'student_profile', None)
            data['student'] = None if profile is None else {
                'majorId': profile.major_id, 'enrollmentYear': profile.enrollment_year,
                'academicStatus': profile.academic_status}
        identities.append(data)
    return {
        'id': account.pk, 'username': account.username, 'name': account.name,
        'email': account.email, 'isActive': account.is_active,
        'mustChangePassword': account.must_change_password,
        'groups': [group.name for group in account.groups.all()], 'identities': identities,
    }


@transaction.atomic
def updateAccount(actor, accountId, payload):
    from academics.audit_service import recordAudit

    requireAccountManager(actor)
    objectData(payload, {'name', 'email', 'isActive', 'title', 'departmentId'})
    # 先锁账号再重新检查目标权限，避免使用请求开始时缓存的角色状态。
    accountId = integerId(accountId, 'accountId')
    if not User.objects.filter(pk=accountId).update(id=F('id')):
        raise Http404('账号不存在或不可管理')
    account = getManagedAccount(actor, accountId)
    before = accountData(account)
    for key, maximum in (('name', 50), ('email', 254)):
        if key in payload:
            value = textValue(payload[key], maximum, key, blank=key == 'email')
            if key == 'email' and value:
                validate_email(value)
            setattr(account, key, value)
    if 'isActive' in payload:
        if type(payload['isActive']) is not bool:
            raise ValidationError('isActive 须为布尔值')
        if account.pk == actor.pk and not payload['isActive']:
            raise ValidationError('不能通过管理接口停用自己的账号')
        account.is_active = payload['isActive']
    if 'title' in payload or 'departmentId' in payload:
        firstIdentity = account.identities.filter(identity_type=SchoolIdentity.IDENTITY_TEACHER).order_by('pk').first()
        teacher = TeacherProfile.objects.filter(identity=firstIdentity).first() if firstIdentity else None
        if teacher is None:
            raise ValidationError('账号没有可维护的教师档案')
        TeacherProfile.objects.filter(pk=teacher.pk).update(id=F('id'))
        teacher.refresh_from_db()
        if 'title' in payload:
            teacher.title = textValue(payload['title'], 50, 'title', blank=True)
        if 'departmentId' in payload:
            targetId = payload['departmentId']
            target = None
            if targetId is not None:
                target = Department.objects.filter(pk=integerId(targetId, 'departmentId')).first()
                if target is None or not target.is_active:
                    raise ValidationError('所属学院不存在或未启用')
            teacher.department = target
        teacher.save(update_fields=['title', 'department', 'updated_at'])
    account.save(update_fields=['name', 'email', 'is_active'])
    result = getManagedAccount(actor, accountId)
    recordAudit(actor, 'account.update', account, before=before, after=accountData(result))
    return result


@transaction.atomic
@sensitive_variables('payload', 'password')
def resetAccountPassword(actor, accountId, payload):
    from academics.audit_service import recordAudit

    requireAccountManager(actor)
    if not actor.is_superuser:
        raise PermissionDenied('只有系统管理员可以重置他人密码')
    objectData(payload, {'password'}, required={'password'})
    textValue(payload['password'], 1024, 'password')
    password = payload['password']
    accountId = integerId(accountId, 'accountId')
    if accountId == actor.pk:
        raise ValidationError('请通过个人修改密码接口修改自己的密码')
    if not User.objects.filter(pk=accountId).update(id=F('id')):
        raise Http404('账号不存在或不可管理')
    account = getManagedAccount(actor, accountId)
    validate_password(password, account)
    before = {'mustChangePassword': account.must_change_password}
    account.set_password(password)
    account.must_change_password = True
    account.save(update_fields=['password', 'must_change_password'])
    recordAudit(actor, 'account.reset_password', account,
                before=before, after={'mustChangePassword': True})
    return account

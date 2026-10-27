"""学生当前内容与本人历史摘要的范围；每次请求重算，不缓存到用户会话。"""
from django.db.models import Q
from django.http import Http404

from academics.models import AcademicTerm, ClassMembership, TeachingClass, TeachingClassRecord
from users.models import SchoolIdentity, StudentProfile
from .models import Homework, Submission


def studentProfile(user):
    identity = user.identities.filter(identity_type=SchoolIdentity.IDENTITY_STUDENT).order_by('pk').first()
    if identity is None:
        return None, None
    profile = StudentProfile.objects.select_related('identity__user', 'major__department').filter(
        identity=identity).first()
    return identity, profile


class StudentScope:
    def __init__(self, user):
        self.user = user
        self.identity, self.profile = studentProfile(user)
        self.membership = None
        if self.profile is not None and self.profile.academic_status == StudentProfile.ACADEMIC_ACTIVE:
            self.membership = ClassMembership.objects.select_related('admin_class').filter(
                student_profile=self.profile, left_at__isnull=True).first()
        raw = TeachingClass.objects.none() if self.membership is None else TeachingClass.objects.filter(
            admin_classes=self.membership.admin_class)
        current = list(raw.select_related('course_college__course', 'course_college__department'))
        self.submittedClassIds = set(Submission.objects.filter(
            user=user, homework__isnull=False, submit_type=Submission.SUBMIT_HOMEWORK
        ).values_list('homework__teaching_class_id', flat=True).distinct())
        candidate = Q(teaching_class_id__in=self.submittedClassIds)
        if self.profile is not None:
            candidate |= Q(teaching_class__admin_classes__memberships__student_profile=self.profile)
        records = list(TeachingClassRecord.objects.filter(candidate).distinct().defer('gradebooks'))
        self.records = {record.teaching_class_id: record for record in records
                        if any(row['id'] == user.pk for row in record.roster)}
        recordIds = {record.teaching_class_id for record in records}
        self.terms = {term.code: term for term in AcademicTerm.objects.all()}
        self.currentClasses = [tc for tc in current if (
            tc.pk in self.records or tc.pk not in recordIds and (
                tc.term not in self.terms or self.terms[tc.term].status == AcademicTerm.OPEN))]
        self.contentClassIds = {tc.pk for tc in self.currentClasses}
        self.summaryClassIds = self.contentClassIds | self.submittedClassIds | set(self.records)

    def termStatus(self, tc):
        term = self.terms.get(tc.term)
        return term.status if term else AcademicTerm.OPEN

    def requireContent(self, tcId):
        tc = next((tc for tc in self.currentClasses if tc.pk == tcId), None)
        if tc is None:
            raise Http404('教学班不存在')
        return tc

    def homeworks(self, *, contentOnly=False):
        if contentOnly:
            return Homework.objects.filter(teaching_class_id__in=self.contentClassIds)
        return Homework.objects.filter(
            Q(teaching_class_id__in=self.contentClassIds | set(self.records))
            | Q(submissions__user=self.user, submissions__submit_type=Submission.SUBMIT_HOMEWORK)
        ).distinct()

    def roster(self):
        return [{'id': self.user.pk, 'username': self.user.username, 'name': self.user.name,
                 'schoolNumber': self.identity.school_number if self.identity else '',
                 'adminClassId': self.membership.admin_class_id if self.membership else None,
                 'adminClassName': self.membership.admin_class.name if self.membership else ''}]

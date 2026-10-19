"""教师工作台 API（前后端分离主线，挂在 /api/teacher/ 前缀下）

安全说明：教师的所有权限仅限教学班级。
- 列表接口数据天然只含当前教师教的教学班（teachers 过滤）
- 详情接口先过 getTeacherTeachingClass 对象级校验，非本班教师一律 404
- 后续功能接口（通知/作业等）都必须沿用这套校验
"""
from django.db.models import Q
from django.http import Http404
from rest_framework.pagination import PageNumberPagination
from rest_framework.views import APIView
from rest_framework.response import Response

from academics.models import TeachingClass
from academics.serializers import TeachingClassSerializer
from teaching.models import Problem, Submission
from users.permissions import IsTeacherGroup, PermRequired

from .helpers import getTeacherTeachingClass
from .homework_service import (createHomework, deleteHomework,
                               getManageableHomework, getStudentHomework,
                               listClassHomeworks, listStudentHomeworks,
                               updateHomework)
from .problem_service import (createProblem, getManageableProblem,
                              updateProblem)
from .serializers import (HomeworkDetailSerializer,
                          HomeworkListSerializer,
                          HomeworkStudentDetailSerializer,
                          HomeworkStudentListSerializer,
                          ProblemDetailSerializer, ProblemListSerializer,
                          ProblemStudentSerializer,
                          SubmissionDetailSerializer,
                          SubmissionListSerializer,
                          TeacherTeachingClassSerializer)
from .submission_service import submitSolution


class SubmissionPagination(PageNumberPagination):
    """提交列表分页：固定每页 20，按 created_at 倒序（12.2 约定）"""
    page_size = 20


class SubmissionListCreateView(APIView):
    """提交受理与我的提交列表：POST/GET /api/submissions/

    POST：受理提交（九项同步校验 + 快照冻结 + on_commit 投递，
    逻辑见 teaching/submission_service.py，docs/OJ对接设计.md 第七章）。
    GET：当前用户的提交列表（超管可见全部），分页 20/页，
    支持 problem/homework/status 过滤。
    权限码：homework:view（学生；教师无此码，403）。
    """
    permission_classes = [PermRequired]
    permCode = 'homework:view'

    def post(self, request):
        # 身份一律取自认证上下文（request.user），请求体 userId 被忽略（7.1）
        submissionId = submitSolution(request.user, request.data)
        return Response({'submissionId': submissionId})

    def get(self, request):
        queryset = Submission.objects.select_related(
            'problem', 'judge_task')
        if not request.user.is_superuser:
            queryset = queryset.filter(user=request.user)
        # 可选过滤：按题/作业/状态（12.2「按题/作业过滤分页」）
        problemId = request.query_params.get('problem')
        homeworkId = request.query_params.get('homework')
        status = request.query_params.get('status')
        if problemId:
            queryset = queryset.filter(problem_id=problemId)
        if homeworkId:
            queryset = queryset.filter(homework_id=homeworkId)
        if status:
            queryset = queryset.filter(status=status)
        paginator = SubmissionPagination()
        page = paginator.paginate_queryset(queryset, request)
        serializer = SubmissionListSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)


class SubmissionDetailView(APIView):
    """提交详情：GET /api/submissions/<id>/

    对象级校验：仅本人（超管放行），否则 404（防探测，12.2）。
    返回含 code 原文与判题进度；不含测试点数据/预期输出/spj 输出。
    权限码：homework:view。
    """
    permission_classes = [PermRequired]
    permCode = 'homework:view'

    def get(self, request, submissionId):
        submission = Submission.objects.select_related(
            'problem', 'judge_task', 'agent_report').filter(
            pk=submissionId).first()
        if submission is None or not (
                request.user.is_superuser
                or submission.user_id == request.user.id):
            raise Http404('提交不存在')
        return Response(SubmissionDetailSerializer(submission).data)


class ProblemPagination(PageNumberPagination):
    """题库列表分页：固定每页 20（12.1 约定）"""
    page_size = 20


class TeacherProblemListCreateView(APIView):
    """教师题库列表与录题：GET/POST /api/teacher/problems/

    GET：题库列表 = 公共题库 + 本人个人题（12.1），按更新时间倒序，
    分页 20/页。权限码：homework:bank（教师）。
    POST：新建个人题（is_public=False，归属由端点决定 12.3）。
    """
    permission_classes = [PermRequired]
    permCode = 'homework:bank'

    def get(self, request):
        queryset = Problem.objects.filter(
            Q(is_public=True) | Q(owner=request.user)
        ).order_by('-updated_at')
        paginator = ProblemPagination()
        page = paginator.paginate_queryset(queryset, request)
        serializer = ProblemListSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)

    def post(self, request):
        problem = createProblem(request.user, request.data, isPublic=False)
        return Response(ProblemDetailSerializer(problem).data)


class PublicProblemCreateView(APIView):
    """公共题库录题：POST /api/teacher/public-problems/

    权限码：question_bank:manage（课程组；教师无此码 → 403）。
    is_public=True 由端点决定，请求体 isPublic 一律无效（12.3）。
    """
    permission_classes = [PermRequired]
    permCode = 'question_bank:manage'

    def post(self, request):
        problem = createProblem(request.user, request.data, isPublic=True)
        return Response(ProblemDetailSerializer(problem).data)


class TeacherProblemDetailView(APIView):
    """题库题目详情/编辑/删除：GET/PUT/DELETE /api/teacher/problems/<id>/

    不设 permCode：教师与课程组权限码不同（homework:bank vs
    question_bank:manage），对象级由 getManageableProblem 统一判
    403/404（12.2，防探测）。
    GET 返回含 statement_md/samples 的详情，不含 tests/spj_code（6.2）。
    PUT = 重传完整 md 全文替换，revision+1（决策 d）。
    DELETE 删除题目（已有提交用快照判题，不受影响）。
    """
    permission_classes = [PermRequired]

    def get(self, request, problemId):
        problem = getManageableProblem(request.user, problemId)
        return Response(ProblemDetailSerializer(problem).data)

    def put(self, request, problemId):
        problem = getManageableProblem(request.user, problemId)
        problem = updateProblem(problem, request.data)
        return Response(ProblemDetailSerializer(problem).data)

    def delete(self, request, problemId):
        problem = getManageableProblem(request.user, problemId)
        problem.delete()
        return Response(status=204)


class StudentProblemDetailView(APIView):
    """学生题面详情：GET /api/problems/<id>/

    权限码：homework:view。不按 is_public 过滤——作业里引用的教师
    个人题学生必须能读（12.1）。返回题面 + 公开样例 + 支持语言；
    永不含 tests/spj_code/判题内部配置（6.2）。
    """
    permission_classes = [PermRequired]
    permCode = 'homework:view'

    def get(self, request, problemId):
        problem = Problem.objects.filter(pk=problemId).first()
        if problem is None:
            raise Http404('题目不存在')
        return Response(ProblemStudentSerializer(problem).data)


class TeacherHomeworkListCreateView(APIView):
    """教学班作业列表与组卷：GET/POST /api/teacher/teaching-classes/<id>/homeworks/

    权限码：homework:publish（教师）。
    对象级：getTeacherTeachingClass 校验是本班教师（否则 404）。
    GET：该班作业列表（题数 + 提交数聚合，12.4）。
    POST：创建作业（选题可见集合 = 公共题库 + 本人个人题，
    problem_revision 冻结选题时修订号）。
    """
    permission_classes = [PermRequired]
    permCode = 'homework:publish'

    def get(self, request, teachingClassId):
        teachingClass = getTeacherTeachingClass(request.user, teachingClassId)
        homeworks = listClassHomeworks(request.user, teachingClass)
        return Response(HomeworkListSerializer(homeworks, many=True).data)

    def post(self, request, teachingClassId):
        teachingClass = getTeacherTeachingClass(request.user, teachingClassId)
        homework = createHomework(request.user, teachingClass, request.data)
        return Response(HomeworkDetailSerializer(homework).data)


class TeacherHomeworkDetailView(APIView):
    """作业详情/编辑/删除：GET/PUT/DELETE /api/teacher/homeworks/<id>/

    权限码：homework:publish；对象级：getManageableHomework
    （非本班教师 404，防探测）。
    PUT 全量语义：已有提交锁定题目列表（400），截止时间只允许延长。
    DELETE：有提交 400，无提交连带中间表删除。
    """
    permission_classes = [PermRequired]
    permCode = 'homework:publish'

    def get(self, request, homeworkId):
        homework = getManageableHomework(request.user, homeworkId)
        return Response(HomeworkDetailSerializer(homework).data)

    def put(self, request, homeworkId):
        homework = getManageableHomework(request.user, homeworkId)
        homework = updateHomework(request.user, homework, request.data)
        return Response(HomeworkDetailSerializer(homework).data)

    def delete(self, request, homeworkId):
        homework = getManageableHomework(request.user, homeworkId)
        deleteHomework(homework)
        return Response(status=204)


class StudentHomeworkListView(APIView):
    """我的作业列表：GET /api/homeworks/

    权限码：homework:view。数据 = 我所在教学班的全部作业
    （3 跳推导），状态三态（未开始/进行中/已截止）+ 我的提交概况
    由序列化器派生（12.4）。
    """
    permission_classes = [PermRequired]
    permCode = 'homework:view'

    def get(self, request):
        homeworks = listStudentHomeworks(request.user)
        return Response(HomeworkStudentListSerializer(homeworks, many=True).data)


class StudentHomeworkDetailView(APIView):
    """学生作业详情：GET /api/homeworks/<id>/

    权限码：homework:view。对象级：属于该作业的教学班（3 跳），
    否则 404；开始前 404（防提前看题，12.4）。
    返回题目列表（学生白名单，不含 tests/spj_code）+ 我的提交摘要。
    """
    permission_classes = [PermRequired]
    permCode = 'homework:view'

    def get(self, request, homeworkId):
        homework = getStudentHomework(request.user, homeworkId)
        # 我的提交摘要：只取最近 10 条（页面轮询展示用）
        submissions = homework.submissions.filter(
            user=request.user).order_by('-created_at')[:10]
        homework.my_submissions_cache = list(submissions)
        return Response(HomeworkStudentDetailSerializer(homework).data)


class TeacherTeachingClassListView(APIView):
    """我的教学班级列表：GET /api/teacher/teaching-classes/

    入口校验：仅教师组（或超级用户）；返回当前教师教的全部教学班
    （跨课程跨学院），按课程名、教学班名排序。
    """
    permission_classes = [IsTeacherGroup]

    def get(self, request):
        teachingClasses = TeachingClass.objects.filter(
            teachers=request.user).distinct() \
            .select_related('course_college__course', 'course_college__department') \
            .order_by('course_college__course__name', 'name')
        return Response(TeacherTeachingClassSerializer(teachingClasses, many=True).data)


class TeacherTeachingClassDetailView(APIView):
    """教师视角教学班详情：GET /api/teacher/teaching-classes/<id>/

    入口校验：仅教师组（或超级用户）；
    对象级校验：教学班教师表必须包含当前用户（超级用户放行），否则 404。
    返回教学班信息、课程、学院、组成班级、学生名单、同班教师
    （复用教务的 TeachingClassSerializer）。
    """
    permission_classes = [IsTeacherGroup]

    def get(self, request, teachingClassId):
        teachingClass = getTeacherTeachingClass(request.user, teachingClassId)
        return Response(TeachingClassSerializer(teachingClass).data)

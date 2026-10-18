"""录题服务层：markdown 解析 + 元数据校验 + 题库两级权限（docs/OJ对接设计.md 10/12 章）

题库两级归属（12.1）：
- is_public=True：课程组公共题库（question_bank:manage 管理）
- is_public=False：教师个人题库（homework:bank 管理，owner=本人）

安全边界（6.2 不可变边界）：tests/spj_code 只存在于服务端判题链路，
任何列表/详情接口一律不下发——教师手里有原始 md 文件可自行核对。
"""
import logging

from django.http import Http404
from rest_framework.exceptions import PermissionDenied, ValidationError

from teaching.models import Problem
from users.permissions import hasPerm

from .problem_markdown import parseProblemMarkdown

logger = logging.getLogger('codemind.problem')

VALID_LANGUAGES = frozenset([Problem.LANGUAGE_C, Problem.LANGUAGE_PYTHON])


def validateMetadata(payload):
    """元数据白名单校验（按脏数据）：languages/tier/compareStrategy

    语言白名单 ∩ 模型支持集；默认 languages=['c','python']、
    tier='basic'、compareStrategy='exact'。
    """
    languages = payload.get('languages', [Problem.LANGUAGE_C,
                                          Problem.LANGUAGE_PYTHON])
    if not isinstance(languages, list) or not languages:
        raise ValidationError('languages 必须是语言数组')
    if len(set(languages)) != len(languages):
        raise ValidationError('languages 不能有重复项')
    for language in languages:
        if language not in VALID_LANGUAGES:
            raise ValidationError(f'不支持的语言：{language}')
    tier = payload.get('tier', Problem.TIER_BASIC)
    if tier not in dict(Problem.TIER_CHOICES):
        raise ValidationError('tier 必须是 beginner/basic/advanced')
    strategy = payload.get('compareStrategy', Problem.COMPARE_EXACT)
    if strategy not in dict(Problem.COMPARE_CHOICES):
        raise ValidationError('compareStrategy 必须是 exact/trailing/loose')
    return languages, tier, strategy


def parseAndApply(problem, payload):
    """解析 markdown 并把解析结果写入题目对象（建题/编辑共用，不落库）"""
    markdown = payload.get('markdown', '')
    if not isinstance(markdown, str):
        raise ValidationError('markdown 必须是字符串')
    languages, tier, strategy = validateMetadata(payload)
    parsed = parseProblemMarkdown(markdown)
    problem.title = parsed['title']
    problem.statement_md = parsed['statement_md']
    problem.samples = parsed['samples']
    problem.tests = parsed['tests']
    problem.languages = languages
    problem.tier = tier
    problem.compare_strategy = strategy
    problem.has_spj = parsed['has_spj']
    problem.spj_code = parsed['spj_code']
    return problem


def createProblem(user, payload, isPublic):
    """录题（端点决定归属，请求体 isPublic 无效）：解析校验后建题 revision=1"""
    problem = Problem(owner=user, is_public=isPublic, revision=1)
    parseAndApply(problem, payload)
    problem.save()
    return problem


def updateProblem(problem, payload):
    """编辑 = 重传完整 md 全文替换（决策 d）：解析成功后 revision+1
    （已受理提交用快照判题，不受改题影响，7.3）"""
    parseAndApply(problem, payload)
    problem.revision += 1
    problem.save()
    return problem


def canManageProblem(user, problem):
    """对象级判断：本人个人题（homework:bank）或公共题（question_bank:manage）；
    超级用户放行"""
    if user.is_superuser:
        return True
    if problem.is_public:
        return hasPerm(user, 'question_bank:manage')
    return problem.owner_id == user.id and hasPerm(user, 'homework:bank')


def getManageableProblem(user, problemId):
    """教师/课程组对象级取题：无任何题库权限 → 403；有权但不是本人
    个人题/公共题 → 404（防探测，与全项目对象级语义一致）"""
    if not (user.is_superuser or hasPerm(user, 'homework:bank')
            or hasPerm(user, 'question_bank:manage')):
        raise PermissionDenied('没有题库管理权限')
    problem = Problem.objects.filter(pk=problemId).first()
    if problem is None or not canManageProblem(user, problem):
        raise Http404('题目不存在')
    return problem

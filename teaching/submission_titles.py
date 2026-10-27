"""提交历史题名取作业冻结快照，单个相关子查询覆盖整页。"""
from django.db.models import CharField, OuterRef, Subquery
from .models import HomeworkProblem


def withFrozenTitles(query):
    title = HomeworkProblem.objects.filter(homework_id=OuterRef('homework_id'),
        problem_id=OuterRef('problem_id')).values('problem_snapshot__title')[:1]
    return query.annotate(frozenProblemTitle=Subquery(title, output_field=CharField()))

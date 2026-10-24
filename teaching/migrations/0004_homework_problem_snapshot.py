"""存量作业以迁移时的当前题目回填；已受理提交的判题快照保持原样。"""
from copy import deepcopy

from django.db import migrations, models


def backfillSnapshots(apps, schemaEditor):
    HomeworkProblem = apps.get_model('teaching', 'HomeworkProblem')
    fields = ('title', 'statement_md', 'samples', 'languages', 'tests',
              'tier', 'compare_strategy', 'has_spj', 'spj_code')
    rows = HomeworkProblem.objects.using(schemaEditor.connection.alias)
    for item in rows.select_related('problem').iterator(chunk_size=100):
        item.problem_snapshot = {field: deepcopy(getattr(item.problem, field))
                                 for field in fields}
        # 旧修订号对应的内容未保存，不能把当前内容标作旧版本。
        item.problem_revision = item.problem.revision
        item.save(update_fields=['problem_snapshot', 'problem_revision'])


class Migration(migrations.Migration):
    dependencies = [('teaching', '0003_problem_source_code_problem_source_hash')]
    operations = [
        migrations.AddField(
            model_name='homeworkproblem', name='problem_snapshot',
            field=models.JSONField(default=dict, verbose_name='作业题目完整快照')),
        migrations.RunPython(backfillSnapshots, migrations.RunPython.noop),
    ]

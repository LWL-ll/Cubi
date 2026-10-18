"""创建判题租约回收定时任务（django-q2 Schedule 表）

落地方式说明（docs/OJ对接设计.md 8.1 租约回收）：
django-q2 的定时任务以 DB 记录承载——Q_CLUSTER 配置里的 schedule 键
在 v2 已移除，运行时由 qcluster 主集群的调度器每分钟扫描 next_run
到期的 Schedule 记录并投递。本迁移在 migrate 时幂等创建该记录，
避免依赖 django admin 手工配置。
"""
from django.db import migrations


def createRecycleSchedule(apps, schemaEditor):
    """创建租约回收定时任务：每分钟一次，无限重复（主集群消费）"""
    Schedule = apps.get_model('django_q', 'Schedule')
    # get_or_create 幂等：已存在则不覆盖（手工调整过的配置不被冲掉）
    Schedule.objects.get_or_create(
        name='judge-lease-recycle',
        defaults={
            'func': 'tasks.tasks_judge.recycleStaleLeases',
            'schedule_type': 'I',  # Schedule.MINUTES：每 N 分钟
            'minutes': 1,
            'repeats': -1,      # -1 = 无限重复
            'cluster': '',      # 主集群（调度器只把空 cluster 的排程给默认集群）
        },
    )


def removeRecycleSchedule(apps, schemaEditor):
    """回滚：删除本迁移创建的排程记录"""
    Schedule = apps.get_model('django_q', 'Schedule')
    Schedule.objects.filter(name='judge-lease-recycle').delete()


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0001_initial'),
        ('django_q', '0019_alter_task_options_alter_ormq_key_alter_ormq_lock_and_more'),
    ]

    operations = [
        migrations.RunPython(createRecycleSchedule, removeRecycleSchedule),
    ]

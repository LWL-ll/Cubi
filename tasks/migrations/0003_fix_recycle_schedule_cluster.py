"""修复租约回收排程：重建缺失记录并把 cluster 修正为 long

背景（验收 5.1/5.2 暴露的真实缺陷，两处叠加）：
1) 0002 注册排程时 cluster=''，只有默认集群调度器会消费；生产只跑
   long 集群，排程即使存在也永远不会被执行；
2) 已部署库中该排程记录缺失（django_q_schedule 表为空），租约回收
   扫描从未运行，导致 worker 重启后 LEASED 判题任务永久悬挂。

本迁移幂等：
- 记录不存在 → 创建（cluster='long'）；
- 已存在但 cluster 不是 long（0002 旧版本写入的 ''）→ 原地修正。
全新部署会先跑修正后的 0002（已是 long）再跑本迁移，为无操作。
"""
from django.db import migrations


def fixRecycleSchedule(apps, schemaEditor):
    """重建/修正租约回收排程：cluster 必须是 long"""
    Schedule = apps.get_model('django_q', 'Schedule')
    schedule, created = Schedule.objects.get_or_create(
        name='judge-lease-recycle',
        defaults={
            'func': 'tasks.tasks_judge.recycleStaleLeases',
            'schedule_type': 'I',  # Schedule.MINUTES：每 N 分钟
            'minutes': 1,
            'repeats': -1,      # -1 = 无限重复
            'cluster': 'long',  # 生产只跑 long 集群，排程必须挂 long
        },
    )
    if not created and schedule.cluster != 'long':
        # 记录已存在但 cluster 写错（0002 旧版本的 ''）→ 原地修正
        schedule.cluster = 'long'
        schedule.save(update_fields=['cluster'])


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0002_create_recycle_schedule'),
    ]

    operations = [
        # 回滚不删记录：删排程是 0002 回滚的职责，本迁移只负责修正
        migrations.RunPython(fixRecycleSchedule, migrations.RunPython.noop),
    ]

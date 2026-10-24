"""拒绝存量冲突后增加当前成员唯一性和日期约束，不自动清理数据。"""
from django.db import migrations, models


def checkMemberships(apps, schemaEditor):
    Membership = apps.get_model('academics', 'ClassMembership')
    rows = Membership.objects.using(schemaEditor.connection.alias)
    duplicates = list(rows.filter(left_at__isnull=True).values('student_profile_id')
                      .annotate(total=models.Count('id')).filter(total__gt=1)
                      .values_list('student_profile_id', flat=True))
    invalid = list(rows.filter(left_at__lt=models.F('joined_at'))
                   .values_list('id', flat=True))
    if duplicates or invalid:
        raise RuntimeError(
            f'成员关系存在冲突，请人工处理后重试迁移：重复当前成员的学生资料ID={duplicates}；'
            f'离班早于入班的成员关系ID={invalid}')


class Migration(migrations.Migration):
    dependencies = [('academics', '0009_align_v2_meta')]
    operations = [
        migrations.RunPython(checkMemberships, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name='classmembership',
            constraint=models.UniqueConstraint(
                fields=['student_profile'], condition=models.Q(left_at__isnull=True),
                name='uniq_current_student_membership')),
        migrations.AddConstraint(
            model_name='classmembership',
            constraint=models.CheckConstraint(
                condition=models.Q(left_at__isnull=True) | models.Q(left_at__gte=models.F('joined_at')),
                name='membership_dates_ordered')),
    ]

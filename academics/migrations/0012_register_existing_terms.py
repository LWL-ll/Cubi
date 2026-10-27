from django.db import migrations


def registerTerms(apps, schema_editor):
    Term = apps.get_model('academics', 'AcademicTerm')
    TeachingClass = apps.get_model('academics', 'TeachingClass')
    using = schema_editor.connection.alias
    codes = set(TeachingClass.objects.using(using).values_list('term', flat=True))
    # 延续迁移前的默认配置，不推断学校的正式开学/结课日期。
    codes.add('2026-2027-1')
    for code in sorted(codes):
        Term.objects.using(using).get_or_create(code=code, defaults={
            'name': code, 'status': 'open', 'is_current': code == '2026-2027-1'})


class Migration(migrations.Migration):
    dependencies = [
        ('academics', '0011_adminclass_is_active_course_is_active_academicterm_and_more'),
    ]
    operations = [migrations.RunPython(registerTerms, migrations.RunPython.noop)]

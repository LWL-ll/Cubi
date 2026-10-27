from django.db import migrations


def addSchedule(apps, schema_editor):
    Schedule = apps.get_model('django_q', 'Schedule')
    Schedule.objects.using(schema_editor.connection.alias).update_or_create(
        name='office-import-recovery', defaults={
            'func': 'tasks.tasks_import.recoverOfficeImports', 'schedule_type': 'I',
            'minutes': 1, 'repeats': -1, 'cluster': 'codemind'})


def removeSchedule(apps, schema_editor):
    apps.get_model('django_q', 'Schedule').objects.using(schema_editor.connection.alias).filter(
        name='office-import-recovery').delete()


class Migration(migrations.Migration):
    dependencies = [('tasks', '0004_office_import_job')]
    operations = [migrations.RunPython(addSchedule, removeSchedule)]

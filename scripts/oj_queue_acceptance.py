"""真实队列与沙箱验收，仅使用新建临时数据库。

运行：python scripts/oj_queue_acceptance.py
需要 OJ_AUTH_TOKEN；约 3～5 分钟。不改业务库，不伪造心跳或手动调用回收。
APIClient 验证 Django 请求链路，不代表浏览器或 TCP 验收。
"""
import argparse
from contextlib import closing
import faulthandler
import gc
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid

# 各个 spawn 进程超出验收等待预算时只输出调用栈，便于定位启动阻塞。
faulthandler.dump_traceback_later(360, repeat=False)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def businessFingerprint():
    """只读逐表摘要；含迁移与业务表，不产生备份或写入。"""
    database = ROOT / 'db.sqlite3'
    if not database.exists():
        return None
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as connection:
        tables = connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        digest = hashlib.sha256(repr(tables).encode())
        for name, _ in tables:
            quoted = '"' + name.replace('"', '""') + '"'
            rows = connection.execute('SELECT * FROM ' + quoted).fetchall()
            for row in sorted(repr(row) for row in rows):
                digest.update(row.encode())
        return digest.hexdigest()


def setup():
    os.environ['DJANGO_SETTINGS_MODULE'] = 'codemind.settings_oj_acceptance'
    os.environ['Q_CLUSTER_NAME'] = 'long'
    import django
    django.setup()
    from django.conf import settings
    require(Path(settings.DATABASES['default']['NAME']).resolve()
            == Path(os.environ['OJ_ACCEPTANCE_DB']).resolve(), '数据库隔离核对失败')
    require(not settings.AGENT_ENABLED, '验收不应启动 Agent')


def workerMain():
    setup()
    from django_q.cluster import Cluster
    cluster = Cluster()
    try:
        cluster.start()
        print('WORKER_READY', flush=True)
        for line in sys.stdin:
            if line.strip() == 'stop':
                break
    finally:
        if cluster.sentinel and cluster.sentinel.is_alive():
            cluster.stop()


class Worker:
    def __init__(self, directory):
        import psutil
        self.log = open(directory / ('worker-' + uuid.uuid4().hex + '.log'),
                        'w', encoding='utf-8')
        self.process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), '--worker'],
            cwd=ROOT, env=os.environ.copy(), stdin=subprocess.PIPE,
            stdout=self.log, stderr=subprocess.STDOUT, text=True,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.identity = psutil.Process(self.process.pid).create_time()
        self.interrupted = False

    def alive(self):
        return self.process.poll() is None

    def kill(self):
        """仅终止本次创建且创建时间匹配的进程树，先暂停以防哨兵重生。"""
        import psutil
        if not self.alive():
            return
        root = psutil.Process(self.process.pid)
        require(root.create_time() == self.identity, 'PID 已复用，拒绝终止')
        root.suspend()
        children = root.children()
        for child in children:
            try:
                child.suspend()
            except psutil.NoSuchProcess:
                pass
        descendants = root.children(recursive=True)
        for child in descendants:
            try:
                child.suspend()
            except psutil.NoSuchProcess:
                pass
        for child in reversed(descendants):
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
        root.kill()
        self.interrupted = True
        _, remaining = psutil.wait_procs(descendants + [root], timeout=20)
        require(not remaining, '本次验收进程树未完全退出')
        self.process.wait(timeout=10)

    def close(self):
        try:
            if self.alive():
                self.process.stdin.write('stop\n')
                self.process.stdin.flush()
                try:
                    self.process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    self.kill()
            require(self.interrupted or self.process.returncode == 0, 'worker 退出状态异常')
        finally:
            self.process.stdin.close()
            self.log.close()


def fixtures():
    from datetime import timedelta
    from django.contrib.auth.models import Group
    from django.utils import timezone
    from academics.models import (AdminClass, ClassMembership, Course, CourseCollege,
                                  Department, Major, TeachingClass)
    from users.models import SchoolIdentity, StudentProfile, User
    from teaching.models import Homework, HomeworkProblem, Problem
    teacher = User.objects.create_user('OJ_ACCEPT_TEACHER')
    teacher.groups.add(Group.objects.get(name='教师'))
    student = User.objects.create_user('OJ_ACCEPT_STUDENT')
    student.groups.add(Group.objects.get(name='学生'))
    department = Department.objects.create(code='OJ_ACCEPT', name='验收虚构学院')
    major = Major.objects.create(department=department, code='OJ', name='验收虚构专业')
    adminClass = AdminClass.objects.create(name='验收行政班', major=major, enrollment_year=2026)
    course = Course.objects.create(code='OJ_ACCEPT', name='验收课程')
    college = CourseCollege.objects.create(course=course, department=department)
    college.admin_classes.add(adminClass)
    teachingClass = TeachingClass.objects.create(
        name='验收教学班', term='2026-2027-1', course_college=college)
    teachingClass.admin_classes.add(adminClass)
    teachingClass.teachers.add(teacher)
    identity = SchoolIdentity.objects.create(
        user=student, school_number='OJ_ACCEPT_STUDENT',
        identity_type=SchoolIdentity.IDENTITY_STUDENT)
    profile = StudentProfile.objects.create(identity=identity, major=major, enrollment_year=2026)
    ClassMembership.objects.create(student_profile=profile, admin_class=adminClass,
                                   joined_at=timezone.now().date())
    problem = Problem.objects.create(
        title='验收 A+B', owner=teacher, statement_md='计算两整数的和',
        languages=['c', 'python'], samples=[{'input': '12 30', 'output': '42\n'}],
        tests=[{'input': '12 30', 'output': '42\n', 'points': 100}])
    homework = Homework.objects.create(
        teaching_class=teachingClass, title='验收作业', created_by=teacher,
        start_at=timezone.now() - timedelta(hours=1),
        deadline=timezone.now() + timedelta(hours=1))
    HomeworkProblem.objects.create(
        homework=homework, problem=problem, order=1, problem_revision=problem.revision)
    return student, problem, homework


def submit(client, problem, homework, language, code):
    from teaching.models import Submission
    response = client.post('/api/submissions/', {
        'problemId': problem.pk, 'homeworkId': homework.pk, 'submitType': 'homework',
        'language': language, 'code': code}, format='json')
    require(response.status_code == 200, f'受理失败 HTTP {response.status_code}')
    submission = Submission.objects.get(pk=response.json()['submissionId'])
    require(bool(submission.judge_snapshot.get('tests')), '受理快照缺少测试点')
    require(bool(submission.judge_task.q2_task_id), '真实队列投递未成功')
    return submission


def waitFor(check, seconds, worker, label):
    deadline = time.monotonic() + seconds
    lastReport = time.monotonic()
    while time.monotonic() < deadline:
        require(worker.alive(), f'{label}：worker 提前退出')
        value = check()
        if value:
            return value
        if time.monotonic() - lastReport >= 30:
            print(f'[等待] {label}，继续等待真实排程', flush=True)
            lastReport = time.monotonic()
        time.sleep(0.2)
    raise RuntimeError(label + '：等待超时')


def verifyVerdict(client, submission, worker):
    from teaching.models import JudgeTask, Submission
    artifacts = set()

    def finished():
        task = JudgeTask.objects.get(submission=submission)
        if task.file_id:
            artifacts.add(task.file_id)
        submission.refresh_from_db()
        if submission.status in Submission.TERMINAL_STATUSES:
            require(submission.status == Submission.STATUS_ACCEPTED,
                    '判题未通过，状态=' + submission.status)
            require(task.status == JudgeTask.STATUS_DONE, '判题任务没有完成')
            return not task.file_id
        return False

    waitFor(finished, 360, worker, '结果落库与产物清理')
    require(submission.score == submission.total_score == 100, '分数不是 100')
    response = client.get(f'/api/submissions/{submission.pk}/')
    require(response.status_code == 200, '结果查询失败')
    data = response.json()
    require(data['status'] == 'accepted' and data['score'] == 100
            and data['progress'] == 100, '查询接口与数据库结果不一致')
    require(not {'tests', 'judge_snapshot', 'spj_code'} & data.keys(), '结果响应泄露内部字段')
    from teaching import judge
    for fileId in artifacts:
        try:
            judge._doRequest('GET', '/file/' + fileId)
        except judge.BadRequestError as error:
            require(error.statusCode == 404, '编译产物删除核验异常')
        else:
            raise RuntimeError('沙箱编译产物仍可访问')
    if submission.language == 'c':
        require(bool(artifacts), '未观察到 C 编译产物，不能确认实际清理')
    print(f'[PASS] {submission.language}：AC / 100 分 / 查询一致 / 产物清理', flush=True)
    return artifacts


def acceptance(directory):
    setup()
    from django.conf import settings
    from django.core.management import call_command
    from django.db import connections
    from django_q.models import OrmQ, Schedule, Task
    from rest_framework.test import APIClient
    from teaching.models import JudgeTask
    from teaching import judge
    from tasks.tasks_judge import getHeartbeatTimeoutSeconds
    require(bool(settings.OJ_AUTH_TOKEN), '未设置沙箱认证凭证')
    call_command('migrate', interactive=False, verbosity=0)
    require(not OrmQ.objects.exists(), '新临时库中不应有队列消息')
    schedule = Schedule.objects.get(func='tasks.tasks_judge.recycleStaleLeases')
    require(schedule.cluster == 'long' and schedule.minutes == 1, '回收排程不是 long 每分钟')
    student, problem, homework = fixtures()
    client = APIClient()
    client.force_login(student)
    cCode = ('#include <stdio.h>\n#include <unistd.h>\n'
             'int main(void){int a,b;scanf("%d%d",&a,&b);sleep(1);printf("%d\\n",a+b);return 0;}')
    cSubmission = submit(client, problem, homework, 'c', cCode)
    require(OrmQ.objects.filter(key='long').exists(), '未看到 long 的真实 ORM 队列消息')
    workers = []
    try:
        worker = Worker(directory)
        workers.append(worker)
        verifyVerdict(client, cSubmission, worker)
        pythonCode = 'a,b=map(int,input().split());print(a+b)\n'
        pythonSubmission = submit(client, problem, homework, 'python', pythonCode)
        verifyVerdict(client, pythonSubmission, worker)
        recovery = submit(client, problem, homework, 'python',
                          'import time\ntime.sleep(2)\n' + pythonCode)
        def leased():
            task = JudgeTask.objects.get(submission=recovery)
            return task if task.status == JudgeTask.STATUS_LEASED else None
        task = waitFor(leased, 60, worker, '等待中断用提交领取租约')
        oldAttempt = task.attempt_id
        beforeSchedule = Schedule.objects.get(pk=schedule.pk).task
        heartbeat = task.heartbeat_at
        started = time.monotonic()
        worker.kill()
        print(f'[中断] 已停止本次 worker 进程树，租约阈值 {getHeartbeatTimeoutSeconds()} 秒', flush=True)
        worker = Worker(directory)
        workers.append(worker)
        verifyVerdict(client, recovery, worker)
        task.refresh_from_db()
        require(task.attempt_id == oldAttempt + 2, '未经过回收作废和重新领取令牌')
        require((task.claimed_at - heartbeat).total_seconds() >= getHeartbeatTimeoutSeconds(),
                '恢复发生在真实心跳过期之前')
        schedule.refresh_from_db()
        require(bool(schedule.task) and schedule.task != beforeSchedule, '未观察到真实回收排程推进')
        require(Task.objects.filter(func='tasks.tasks_judge.recycleStaleLeases',
                                    success=True).exists(), '缺少回收排程成功记录')
        print(f'[PASS] 中断恢复：attempt {oldAttempt} → {task.attempt_id}，'
              f'耗时 {time.monotonic() - started:.1f} 秒', flush=True)
    finally:
        for worker in reversed(workers):
            worker.close()
        # 最终兜底只清理本次临时库中登记的产物。
        for fileId in JudgeTask.objects.exclude(file_id='').values_list('file_id', flat=True):
            judge.deleteFile(fileId)
        connections.close_all()
        # Windows 不允许删除仍被循环引用中的 SQLite 连接占用的文件。
        gc.collect()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        workerMain()
        return 0
    before = businessFingerprint()
    try:
        with tempfile.TemporaryDirectory(prefix='codemind-oj-') as name:
            directory = Path(name).resolve()
            runId = uuid.uuid4().hex
            database = directory / 'acceptance.sqlite3'
            (directory / 'acceptance.json').write_text(
                json.dumps({'run': runId, 'database': str(database)}), encoding='utf-8')
            os.environ['OJ_ACCEPTANCE_RUN'] = runId
            os.environ['OJ_ACCEPTANCE_DB'] = str(database)
            print('[隔离] 新建空临时库，业务数据库仅做只读摘要比较', flush=True)
            acceptance(directory)
        print('[PASS] 临时数据库、日志和本次 worker 已清理', flush=True)
    finally:
        require(businessFingerprint() == before, '业务数据库摘要发生变化，请检查外部写入')
        print('[PASS] 业务数据库逐表摘要未改变', flush=True)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        import traceback
        for frame in traceback.extract_tb(error.__traceback__):
            print(f'[定位] {Path(frame.filename).name}:{frame.lineno} {frame.name}', file=sys.stderr)
        if isinstance(error, OSError) and error.filename:
            print(f'[文件] {Path(error.filename).name}；系统错误 {error.errno}', file=sys.stderr)
        # 不输出原始异常、请求响应或凭证；已知校验错误可安全说明。
        message = str(error) if type(error) is RuntimeError else type(error).__name__
        token = os.environ.get('OJ_AUTH_TOKEN', '')
        if token:
            message = message.replace(token, '[已隐藏]')
        print('[FAIL] ' + message, file=sys.stderr)
        sys.exit(1)

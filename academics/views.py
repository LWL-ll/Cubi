import io

from django.contrib import messages
from django.contrib.auth.models import Group
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render

from openpyxl import load_workbook

from users.models import User
from users.permissions import hasPerm, requirePerm

from .models import Clazz, College, Course, CourseCollege
from .student_no import parseStudentNo


# ---------- 工具函数 ----------

def cellToStr(cell):
    """Excel 单元格转字符串：数字型学号/工号防止变成 20231001.0 这种浮点格式"""
    if cell is None:
        return ''
    if isinstance(cell, float) and cell.is_integer():
        return str(int(cell))
    return str(cell).strip()


def parseXlsxRows(fileObj):
    """解析上传的 xlsx 文件，返回数据行列表

    约定：所有导入 xlsx 的第一行必须是表头，解析时直接跳过；
    列约定：A 列编号（学号/工号/课程编号）、B 列姓名/课程名称、C 列初始密码；
    返回的每行对应物理行号从 2 开始（enumerate start=2 的报错行号正好对应）
    """
    workbook = load_workbook(io.BytesIO(fileObj.read()), read_only=True)
    sheet = workbook.active
    rows = []
    for row in sheet.iter_rows(values_only=True):
        cells = [cellToStr(cell) for cell in row]
        if cells and any(cells):
            rows.append(cells)
    return rows[1:]


def getGroup(name):
    """按中文组名取分组对象"""
    return Group.objects.get(name=name)


def isTeacherUser(user):
    """判断用户是否为教师分组的账号"""
    return user.groups.filter(name='教师').exists()


def findTeacherByNo(teacherNo):
    """按工号查教师账号；不存在返回 None，非教师分组返回字符串错误原因"""
    user = User.objects.filter(username=teacherNo).first()
    if user is None:
        return None, f'工号 {teacherNo} 的教师不存在，请先在批量导入中导入教师'
    if not isTeacherUser(user):
        return None, f'工号 {teacherNo} 不是教师账号'
    return user, ''


def importCourses(rows):
    """按行导入课程：每行 [课程编号, 课程名称]，返回 (成功列表, 错误列表)"""
    okList, errList = [], []
    for lineNo, cells in enumerate(rows, start=2):
        # 第 1 行是表头，所以行号从 2 开始
        if len(cells) < 2 or not cells[0] or not cells[1]:
            errList.append((lineNo, '课程编号和名称不能为空'))
            continue
        code, name = cells[0], cells[1]
        if Course.objects.filter(code=code).exists():
            errList.append((lineNo, f'课程编号 {code} 已存在'))
            continue
        Course.objects.create(code=code, name=name)
        okList.append(name)
    return okList, errList


def importTeachers(rows):
    """按行导入教师账号：每行 [工号, 姓名, 初始密码]，并加入教师分组"""
    okList, errList = [], []
    teacherGroup = getGroup('教师')
    for lineNo, cells in enumerate(rows, start=2):
        if len(cells) < 3 or not all(cells[:3]):
            errList.append((lineNo, '工号、姓名、密码均不能为空'))
            continue
        username, name, password = cells[0], cells[1], cells[2]
        if User.objects.filter(username=username).exists():
            errList.append((lineNo, f'工号 {username} 已存在'))
            continue
        user = User.objects.create_user(username=username, password=password)
        user.name = name
        user.groups.add(teacherGroup)
        user.save()
        okList.append(f'{name}（{username}）')
    return okList, errList


def importTeachersToCourse(rows, cc):
    """导入教师并加入课程×学院的教师池，同时自动分配到本课程已关联的所有班级

    每行 [工号, 姓名, 初始密码]，重复工号按行报错跳过。
    """
    okList, errList = [], []
    teacherGroup = getGroup('教师')
    for lineNo, cells in enumerate(rows, start=2):
        if len(cells) < 3 or not all(cells[:3]):
            errList.append((lineNo, '工号、姓名、密码均不能为空'))
            continue
        username, name, password = cells[0], cells[1], cells[2]
        if User.objects.filter(username=username).exists():
            errList.append((lineNo, f'工号 {username} 已存在'))
            continue
        user = User.objects.create_user(username=username, password=password)
        user.name = name
        user.groups.add(teacherGroup)
        user.save()
        # 加入本课程教师池，并自动分配到本课程已关联的每个班级
        cc.teachers.add(user)
        for clazz in cc.clazzes.all():
            clazz.teachers.add(user)
        okList.append(f'{name}（{username}）')
    return okList, errList


def importStudents(rows, clazz):
    """按行导入学生账号到指定班级：每行 [学号, 姓名, 初始密码]，并加入学生分组"""
    okList, errList = [], []
    studentGroup = getGroup('学生')
    for lineNo, cells in enumerate(rows, start=2):
        if len(cells) < 3 or not all(cells[:3]):
            errList.append((lineNo, '学号、姓名、密码均不能为空'))
            continue
        username, name, password = cells[0], cells[1], cells[2]
        if User.objects.filter(username=username).exists():
            errList.append((lineNo, f'学号 {username} 已存在'))
            continue
        user = User.objects.create_user(username=username, password=password)
        user.name = name
        user.groups.add(studentGroup)
        user.save()
        clazz.students.add(user)
        okList.append(f'{name}（{username}）')
    return okList, errList


def importStudentsAuto(rows):
    """按学号格式自动分班导入学生：解析出学院/班级后自动创建并按班入学生"""
    okList, errList = [], []
    studentGroup = getGroup('学生')
    for lineNo, cells in enumerate(rows, start=2):
        if len(cells) < 3 or not all(cells[:3]):
            errList.append((lineNo, '学号、姓名、密码均不能为空'))
            continue
        username, name, password = cells[0], cells[1], cells[2]
        # 按学号格式解析出学院和班级（格式不对直接报错，不落库）
        try:
            collegeName, clazzName = parseStudentNo(username)
        except ValueError as err:
            errList.append((lineNo, str(err)))
            continue
        if User.objects.filter(username=username).exists():
            errList.append((lineNo, f'学号 {username} 已存在'))
            continue
        college, _ = College.objects.get_or_create(name=collegeName)
        clazz, _ = Clazz.objects.get_or_create(name=clazzName, defaults={'college': college})
        # 历史班级可能没有学院，自动补上
        if clazz.college_id is None:
            clazz.college = college
            clazz.save()
        user = User.objects.create_user(username=username, password=password)
        user.name = name
        user.groups.add(studentGroup)
        user.save()
        clazz.students.add(user)
        okList.append(f'{name}（{username} → {clazzName}）')
    return okList, errList


def showImportResult(request, tip, okList, errList):
    """统一的导入结果提示：成功数 + 逐行失败原因"""
    messages.success(request, f'{tip}导入完成：成功 {len(okList)} 条')
    for lineNo, reason in errList:
        messages.warning(request, f'第 {lineNo} 行跳过：{reason}')


def setCourseCollegeLeader(cc, teacher):
    """指定课程组组长：记为该课程×学院的 leader，并加入「课程组」分组和教师池"""
    cc.leader = teacher
    cc.save()
    cc.teachers.add(teacher)
    teacher.groups.add(getGroup('课程组'))


def unsetCourseCollegeLeader(cc):
    """撤销课程组组长：若他不再担任任何课程组组长，则移出「课程组」分组"""
    leader = cc.leader
    cc.leader = None
    cc.save()
    if leader and not CourseCollege.objects.filter(leader=leader).exists():
        leader.groups.remove(getGroup('课程组'))


# ---------- 页面视图 ----------

@requirePerm('class:manage')
def renderImportPage(request):
    """批量导入页：导入课程 / 导入教师 / 导入班级学生（xlsx 上传）

    学生导入按学号格式自动分配学院和班级，不再手动选择目标班级。
    """
    if request.method == 'POST':
        importType = request.POST.get('type', '')
        csvFile = request.FILES.get('file')
        if not csvFile:
            messages.error(request, '请先选择要上传的 xlsx 文件')
            return redirect('office_import')
        # 动作级权限校验：导入课程需要 course:manage，导入教师/学生需要 class:manage
        needCode = 'course:manage' if importType == 'course' else 'class:manage'
        if not hasPerm(request.user, needCode):
            messages.error(request, '没有权限执行该导入')
            return redirect('office_import')
        rows = parseXlsxRows(csvFile)
        if importType == 'course':
            okList, errList = importCourses(rows)
            showImportResult(request, '课程', okList, errList)
        elif importType == 'teacher':
            okList, errList = importTeachers(rows)
            showImportResult(request, '教师', okList, errList)
        elif importType == 'student':
            okList, errList = importStudentsAuto(rows)
            showImportResult(request, '学生', okList, errList)
        else:
            messages.error(request, '未知的导入类型')
        return redirect('office_import')
    return render(request, 'office/import.html')


@requirePerm('course:manage')
def renderCourseList(request):
    """课程列表：展示已导入的全部课程及开设学院数"""
    courses = Course.objects.all().prefetch_related('course_colleges__college')
    return render(request, 'office/course_list.html', {'courses': courses})


@requirePerm('course:manage')
def renderCourseDetail(request, courseId):
    """课程设置页：展示已开设学院列表，下拉添加学院进入 课程×学院 设置"""
    course = get_object_or_404(Course, id=courseId)
    if request.method == 'POST':
        # 添加/移除学院属于课程分配：需要 course:assign_class
        if not hasPerm(request.user, 'course:assign_class'):
            messages.error(request, '没有权限执行该操作')
            return redirect('office_course_detail', courseId=courseId)
        action = request.POST.get('action', '')
        if action == 'addCollege':
            college = get_object_or_404(College, id=request.POST.get('collegeId'))
            cc, created = CourseCollege.objects.get_or_create(course=course, college=college)
            messages.success(request, f'已为课程开设学院 {college.name}')
        elif action == 'removeCollege':
            college = get_object_or_404(College, id=request.POST.get('collegeId'))
            CourseCollege.objects.filter(course=course, college=college).delete()
            messages.success(request, f'已移除学院 {college.name}（其下的组长、教师池、班级分配一并解除）')
        return redirect('office_course_detail', courseId=courseId)

    ccList = CourseCollege.objects.filter(course=course).select_related(
        'college', 'leader').prefetch_related('clazzes', 'teachers')
    # 可添加的学院 = 还没有为这门课开设的学院
    availableColleges = College.objects.exclude(course_colleges__course=course)
    return render(request, 'office/course_detail.html', {
        'course': course, 'ccList': ccList, 'availableColleges': availableColleges})


@requirePerm('course:manage')
def renderCourseCollege(request, courseId, collegeId):
    """课程×学院 设置页：课程组组长、本课程教师池、本课程班级"""
    cc = get_object_or_404(CourseCollege, course_id=courseId, college_id=collegeId)
    if request.method == 'POST':
        action = request.POST.get('action', '')
        # 每个动作细分权限码校验
        if action in ('setLeader', 'unsetLeader'):
            needCode = 'user:assign_role'
        elif action in ('addClazz', 'removeClazz'):
            needCode = 'course:assign_class'
        else:
            needCode = 'class:manage'  # importTeachers / importStudents
        if not hasPerm(request.user, needCode):
            messages.error(request, '没有权限执行该操作')
            return redirect('office_course_college', courseId=courseId, collegeId=collegeId)

        if action == 'setLeader':
            teacherNo = request.POST.get('teacherNo', '').strip()
            teacher, errMsg = findTeacherByNo(teacherNo)
            if errMsg:
                messages.error(request, errMsg)
            else:
                setCourseCollegeLeader(cc, teacher)
                messages.success(request,
                    f'已设置 {teacher.name or teacher.username}（{teacherNo}）为课程组组长，'
                    '并加入「课程组」分组')
        elif action == 'unsetLeader':
            leader = cc.leader
            unsetCourseCollegeLeader(cc)
            messages.success(request, f'已撤销组长 {leader.name or leader.username}')
        elif action == 'importTeachers':
            csvFile = request.FILES.get('file')
            if not csvFile:
                messages.error(request, '请先选择要上传的 xlsx 文件')
            else:
                okList, errList = importTeachersToCourse(parseXlsxRows(csvFile), cc)
                showImportResult(request, '本课程教师', okList, errList)
        elif action == 'addClazz':
            clazz = get_object_or_404(Clazz, id=request.POST.get('clazzId'))
            # 只允许添加本学院的班级（下拉框里本来就是本学院的，这里再兜底校验一次）
            if clazz.college_id != cc.college_id:
                messages.error(request, f'班级 {clazz.name} 不属于 {cc.college.name}，无法添加')
            else:
                cc.clazzes.add(clazz)
                messages.success(request, f'已添加班级 {clazz.name}')
        elif action == 'removeClazz':
            clazz = get_object_or_404(Clazz, id=request.POST.get('clazzId'))
            cc.clazzes.remove(clazz)
            messages.success(request, f'已移除班级 {clazz.name}（学生和教师账号保留）')
        elif action == 'importStudents':
            csvFile = request.FILES.get('file')
            if not csvFile:
                messages.error(request, '请先选择要上传的 xlsx 文件')
            else:
                okList, errList = importStudentsAuto(parseXlsxRows(csvFile))
                showImportResult(request, '学生', okList, errList)
        return redirect('office_course_college', courseId=courseId, collegeId=collegeId)

    clazzes = cc.clazzes.all().prefetch_related('students', 'teachers')
    teachers = cc.teachers.all()
    # 可添加的班级 = 本学院下还没关联到本课程的班级
    availableClazzes = cc.college.clazzes.exclude(course_colleges=cc)
    return render(request, 'office/course_college.html', {
        'cc': cc, 'course': cc.course, 'college': cc.college,
        'clazzes': clazzes, 'teachers': teachers, 'availableClazzes': availableClazzes})


@requirePerm('class:manage')
def renderClassDetail(request, courseId, collegeId, clazzId):
    """班级详情：分配/移除教师（下拉或工号添加）、导入学生到本班、查看学生列表

    必须从 课程×学院 入口进入（路径自带课程上下文）；
    教师下拉框候选 = 该课程×学院教师池中还没分配到本班的教师。
    """
    cc = get_object_or_404(CourseCollege, course_id=courseId, college_id=collegeId)
    clazz = get_object_or_404(Clazz, id=clazzId)
    # 校验班级确实属于该课程×学院，防止跨课程访问（404 避免信息泄露）
    if not cc.clazzes.filter(id=clazz.id).exists():
        raise Http404('班级不属于该课程学院')

    def back():
        """统一跳回本班级详情页（带课程上下文）"""
        return redirect('office_class_detail', courseId=courseId,
                        collegeId=collegeId, clazzId=clazzId)

    if request.method == 'POST':
        action = request.POST.get('action', '')
        if action in ('addTeacher', 'removeTeacher'):
            needCode = 'class:assign_teacher'
        else:
            needCode = 'class:manage'  # importStudents
        if not hasPerm(request.user, needCode):
            messages.error(request, '没有权限执行该操作')
            return back()

        if action == 'addTeacher':
            # 两种方式：下拉框选教师（teacherId）或直接输工号（teacherNo）
            teacherNo = request.POST.get('teacherNo', '').strip()
            if teacherNo:
                teacher, errMsg = findTeacherByNo(teacherNo)
                if errMsg:
                    messages.error(request, errMsg)
                else:
                    # 工号添加的教师同时加入本课程教师池
                    clazz.teachers.add(teacher)
                    cc.teachers.add(teacher)
                    messages.success(request,
                        f'已添加教师 {teacher.name or teacher.username}（{teacherNo}）并加入本课程教师池')
            else:
                teacher = get_object_or_404(User, id=request.POST.get('teacherId'))
                # 下拉框候选来自教师池，这里再兜底校验一次防止伪造请求
                if not cc.teachers.filter(id=teacher.id).exists():
                    messages.error(request, '该教师不在本课程教师池中，无法分配')
                else:
                    clazz.teachers.add(teacher)
                    messages.success(request, f'已添加教师 {teacher.name or teacher.username}')
        elif action == 'removeTeacher':
            teacher = get_object_or_404(User, id=request.POST.get('teacherId'))
            clazz.teachers.remove(teacher)
            messages.success(request, f'已移除教师 {teacher.name or teacher.username}')
        elif action == 'importStudents':
            csvFile = request.FILES.get('file')
            if not csvFile:
                messages.error(request, '请先选择要上传的 xlsx 文件')
            else:
                rows = parseXlsxRows(csvFile)
                okList, errList = importStudents(rows, clazz)
                showImportResult(request, '学生', okList, errList)
        return back()

    students = clazz.students.all().order_by('username')
    teachers = clazz.teachers.all()
    # 可分配的教师 = 本课程×学院教师池里还没分配到这个班的
    availableTeachers = cc.teachers.exclude(id__in=teachers.values('id'))
    return render(request, 'office/class_detail.html', {
        'cc': cc, 'course': cc.course, 'college': cc.college, 'clazz': clazz,
        'students': students, 'teachers': teachers,
        'availableTeachers': availableTeachers})

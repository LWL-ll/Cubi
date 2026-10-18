---
name: "dj-testdata"
description: "Django 测试数据工厂（codemind 教务平台专用）。批量生成教务/课程组/教师/学生账号、学院、班级、课程数据。账号按 Group 入组、学号按 student_no.py 规则生成、统一测试密码 Test12345。生成 Factory 代码、批量生成脚本、fixture 文件。触发：需要测试账号时、性能测试造数据时、测试环境初始化时。"
---

# dj-testdata - 测试数据工厂（codemind 适配版）

## 用法

- **生成 Factory 代码**: 说"用 dj-testdata 给 XX 模型生成 factory"
- **生成测试脚本**: 说"用 dj-testdata 生成 3 个学院+12 个班+500 个学生的脚本"
- **生成 fixture**: 说"用 dj-testdata 生成分组 fixture"

---

## 〇、codemind 模型速查（生成数据前必看）

### 数据模型（与通用示例模型不同，勿照抄 School/Grade 旧模板）

| 模型 | 关键字段 | 说明 |
|------|---------|------|
| users.User | username=学号/工号（唯一）、name=姓名 | **无 role 字段**，角色靠 Group 划分 |
| academics.College | name（唯一） | 学院 |
| academics.Clazz | name（唯一）、college(FK 可空)、students/teachers(M2M User) | 班级挂学生和任课教师 |
| academics.Course | code（唯一）、name | 课程 |
| academics.CourseCollege | course×college（联合唯一）、leader(FK 可空)、teachers、clazzes | 课程×学院关联层 |

teaching 应用暂无模型（Notice/Homework 等按功能轮次再建）。

### 角色分组（users/permissions.py GROUP_NAMES）

| 分组 key | 中文名 | 建议账号前缀 |
|---------|--------|-------------|
| admin | 教务 | A+工号 |
| course_team | 课程组 | C+工号 |
| teacher | 教师 | T+工号 |
| student | 学生 | 学号原值 |

- 四个分组已由数据迁移 0002~0007 创建并分配权限，**生成账号只需 `user.groups.add(group)`**，不要再手动建组配权限。
- 分组判定有优先级（GROUP_PRIORITY）：组长同时也是教师，取课程组身份。

### 学号规则（academics/student_no.py 占位规则）

- 学号 8 位纯数字：`年级(4位) + 学院代码(2位) + 班级序号(2位)`，如 `20231001`
- 学院代码：`10`=计算机学院/计科、`11`=外国语学院/英语、`12`=数学学院/数学
- 班级名 = 专业名 + 年级后两位 + 班级序号：学号 20231001 → 班级「计科2301」
- ⚠️ 8 位规则里**没有班内个人序号位**，同班学生会撞号。真实格式定稿前，生成脚本在学号后补 2 位序号（10 位）防撞；前 8 位必须能被 `parseStudentNo` 解析

### 统一约定

- 测试密码统一 `Test12345`（与 users/tests.py 的 PASSWORD 常量一致）
- 权限测试矩阵已有账号：T9001/T9002（教师）、S9001（学生）、A9001（教务）、C9001（课程组）、SU9001（超级用户），生成新数据时避开这些用户名
- 演示数据应能顺带验证对象级权限：教师只能看到自己 `teach_clazzes` 里的班级（getTeacherClazz）

---

## 一、Factory 模板

### 1.1 用户 Factory（含入组）

```python
# tests/factories/user_factories.py
import factory
from django.contrib.auth.models import Group
from users.models import User

TEST_PASSWORD = 'Test12345'  # 与 users/tests.py 的 PASSWORD 保持一致

# 分组中文名（users/permissions.py GROUP_NAMES），迁移 0002 已建组
GROUP_ADMIN = '教务'
GROUP_COURSE_TEAM = '课程组'
GROUP_TEACHER = '教师'
GROUP_STUDENT = '学生'


class UserFactory(factory.django.DjangoModelFactory):
    """基础用户 Factory：只建账号，不入组（角色由子类决定）"""
    class Meta:
        model = User
        django_get_or_create = ('username',)

    username = factory.Sequence(lambda n: f'user{n:04d}')
    name = factory.Faker('name', locale='zh_CN')
    is_active = True

    @factory.post_generation
    def password(self, create, extracted, **kwargs):
        """统一测试密码，可传 extracted 覆盖"""
        if not create:
            return
        self.set_password(extracted or TEST_PASSWORD)
        self.save()


class AdminFactory(UserFactory):
    """教务账号：A+工号，自动加入「教务」分组"""
    username = factory.Sequence(lambda n: f'A{n:04d}')

    @factory.post_generation
    def join_group(self, create, extracted, **kwargs):
        if not create:
            return
        group, _ = Group.objects.get_or_create(name=GROUP_ADMIN)
        self.groups.add(group)


class TeacherFactory(UserFactory):
    """教师账号：T+工号，自动加入「教师」分组"""
    username = factory.Sequence(lambda n: f'T{n:04d}')

    @factory.post_generation
    def join_group(self, create, extracted, **kwargs):
        if not create:
            return
        group, _ = Group.objects.get_or_create(name=GROUP_TEACHER)
        self.groups.add(group)


class StudentFactory(UserFactory):
    """学生账号：S 前缀占位学号，自动加入「学生」分组

    注意：正式学号应按 student_no.py 的 8 位规则生成（见 1.2 的学号工具），
    这里 S 前缀仅用于与权限测试矩阵的 S9001 保持风格统一。
    """
    username = factory.Sequence(lambda n: f'S{n:04d}')

    @factory.post_generation
    def join_group(self, create, extracted, **kwargs):
        if not create:
            return
        group, _ = Group.objects.get_or_create(name=GROUP_STUDENT)
        self.groups.add(group)
```

### 1.2 组织结构 Factory（学院/班级）

```python
# tests/factories/org_factories.py
import factory
from academics.models import College, Clazz

# 学号规则里的学院代码表（academics/student_no.py COLLEGE_CODES）
COLLEGE_NAMES = ['计算机学院', '外国语学院', '数学学院']


class CollegeFactory(factory.django.DjangoModelFactory):
    """学院 Factory：名称唯一，get_or_create 防重复"""
    class Meta:
        model = College
        django_get_or_create = ('name',)

    name = factory.Iterator(COLLEGE_NAMES)


class ClazzFactory(factory.django.DjangoModelFactory):
    """班级 Factory：班级名按「专业名+年级后两位+序号」规则生成（计科2301）"""
    class Meta:
        model = Clazz
        django_get_or_create = ('name',)

    name = factory.Sequence(lambda n: f'计科23{n:02d}')
    college = factory.SubFactory(CollegeFactory)


def buildStudentNo(clazzName, index):
    """按 student_no.py 规则从班级名反推学号前缀，再补 2 位班内序号防撞

    班级名 计科2301 → 前缀 20231001 → 完整学号 20231001XX（10 位）
    前 8 位可被 parseStudentNo 解析；真实学号格式定稿后只需改本函数。
    """
    from academics.student_no import COLLEGE_CODES
    for code, (_collegeName, majorName) in COLLEGE_CODES.items():
        if clazzName.startswith(majorName):
            rest = clazzName[len(majorName):]  # '2301'
            year, seq = rest[:2], rest[2:]
            return f'20{year}{code}{seq}{index:02d}'
    raise ValueError(f'班级名 {clazzName} 无法反推学号前缀')
```

### 1.3 课程 Factory（课程/课程×学院）

```python
# tests/factories/course_factories.py
import factory
from academics.models import Course, CourseCollege
from .org_factories import CollegeFactory


class CourseFactory(factory.django.DjangoModelFactory):
    """课程 Factory：code 唯一"""
    class Meta:
        model = Course
        django_get_or_create = ('code',)

    code = factory.Sequence(lambda n: f'TEST{n:03d}')
    name = factory.Sequence(lambda n: f'测试课程{n:03d}')


class CourseCollegeFactory(factory.django.DjangoModelFactory):
    """课程×学院关联 Factory：leader 默认留空

    组长由教务通过 user:assign_role 权限指定（自动加入课程组分组），
    数据工厂不代设，避免绕过权限流程。
    """
    class Meta:
        model = CourseCollege

    course = factory.SubFactory(CourseFactory)
    college = factory.SubFactory(CollegeFactory)
    # leader 不设默认值；teachers/clazzes 由教务接口的分配操作维护
```

---

## 二、批量生成脚本

```python
# scripts/generate_test_data.py
"""
批量生成 codemind 演示数据脚本（幂等，可重复执行）

用法:
    cd 项目根目录
    D:/miniconda/envs/codemind/python.exe scripts/generate_test_data.py

环境变量:
    CLASSNO=8                 # 班级总数
    STUDENTS_PER_CLASS=40     # 每班学生数
    TEACHER_COUNT=10          # 教师数
    COURSE_COUNT=3            # 课程数
"""

import os
import django

# 先设置 Django 环境，再导入模型
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'codemind.settings')
django.setup()

from django.contrib.auth.models import Group
from users.models import User
from academics.models import College, Clazz, Course, CourseCollege
from academics.student_no import COLLEGE_CODES

DEFAULT_PASSWORD = 'Test12345'  # 与 users/tests.py 的 PASSWORD 保持一致

# 分组中文名（users/permissions.py GROUP_NAMES；迁移 0002 已建组并分配权限）
GROUP_ADMIN = '教务'
GROUP_TEACHER = '教师'
GROUP_STUDENT = '学生'


def createAccount(username, name, groupName):
    """创建账号并加入指定分组（get_or_create 幂等，可重复跑）"""
    user, created = User.objects.get_or_create(
        username=username, defaults={'name': name})
    if created:
        user.set_password(DEFAULT_PASSWORD)
        user.save()
    user.groups.add(Group.objects.get(name=groupName))
    return user


def generateColleges():
    """生成学院（直接复用学号规则的学院代码表，保证数据一致）"""
    for _code, (collegeName, _majorName) in COLLEGE_CODES.items():
        College.objects.get_or_create(name=collegeName)
    return list(College.objects.all())


def generateClazzes(classesPerCollege=3):
    """每个学院生成班级：班级名 = 专业名+年级后两位+序号（如 计科2301）"""
    clazzes = []
    for _code, (collegeName, majorName) in COLLEGE_CODES.items():
        college = College.objects.get(name=collegeName)
        for seq in range(1, classesPerCollege + 1):
            name = f'{majorName}23{seq:02d}'
            clazz, _ = Clazz.objects.get_or_create(
                name=name, defaults={'college': college})
            if not clazz.college:  # 历史数据学院为空时顺手补齐
                clazz.college = college
                clazz.save()
            clazzes.append(clazz)
    return clazzes


def buildStudentNo(clazzName, index):
    """从班级名反推学号：计科2301 → 2023(年级)+10(学院代码)+01(班级)+班内序号"""
    for code, (_collegeName, majorName) in COLLEGE_CODES.items():
        if clazzName.startswith(majorName):
            rest = clazzName[len(majorName):]  # '2301'
            year, seq = rest[:2], rest[2:]
            # 8 位占位规则无个人序号位，补 2 位防撞（真实格式定稿后改这里）
            return f'20{year}{code}{seq}{index:02d}'
    raise ValueError(f'班级名 {clazzName} 无法反推学号前缀')


def generateStudents(clazzes, perClass=40):
    """每班生成学生：学号按规则生成、加入学生分组、挂进班级 students"""
    created = 0
    for clazz in clazzes:
        for index in range(1, perClass + 1):
            studentNo = buildStudentNo(clazz.name, index)
            createAccount(studentNo, f'学生{index:02d}', GROUP_STUDENT)
            created += 1
        # M2M 批量挂进班级（一次 add，避免逐条写）
        clazz.students.add(*User.objects.filter(
            username__startswith=buildStudentNo(clazz.name, 1)[:8]))
    print(f'学生生成/复用: {created} 人')


def generateTeachers(count=10):
    """生成教师账号（T+工号）并加入教师分组"""
    for i in range(1, count + 1):
        createAccount(f'T{i:04d}', f'教师{i:02d}', GROUP_TEACHER)
    print(f'教师生成/复用: {count} 人')


def assignTeachers(teacherNos, clazzes):
    """把教师批量加入班级 teachers（演示：每个教师执教部分班级）"""
    for i, clazz in enumerate(clazzes):
        teacher = User.objects.get(username=teacherNos[i % len(teacherNos)])
        clazz.teachers.add(teacher)


def generateCourses(count=3):
    """生成课程，并按 课程→学院→班级 三层结构建关联"""
    for i in range(1, count + 1):
        course, _ = Course.objects.get_or_create(
            code=f'TEST{i:03d}', defaults={'name': f'测试课程{i:03d}'})
        # 每个学院都开这门课
        for college in College.objects.all():
            CourseCollege.objects.get_or_create(course=course, college=college)
    print(f'课程生成/复用: {count} 门')


def main():
    perClass = int(os.environ.get('STUDENTS_PER_CLASS', 40))
    classNo = int(os.environ.get('CLASSNO', 3))
    teacherCount = int(os.environ.get('TEACHER_COUNT', 10))
    courseCount = int(os.environ.get('COURSE_COUNT', 3))

    print('=' * 50)
    print('  codemind 演示数据生成（幂等，可重复执行）')
    print('=' * 50)

    colleges = generateColleges()
    clazzes = generateClazzes(classNo)
    generateStudents(clazzes, perClass)
    teacherNos = [f'T{i:04d}' for i in range(1, teacherCount + 1)]
    generateTeachers(teacherCount)
    assignTeachers(teacherNos, clazzes)
    generateCourses(courseCount)

    print(f'\n✅ 完成！统一测试密码: {DEFAULT_PASSWORD}')
    print('   教务账号请通过导入或后台创建（工号 A+4 位）')


if __name__ == '__main__':
    main()
```

### 重置演示数据

```python
# scripts/reset_test_data.py
"""
清空演示数据，保留超级管理员

⚠️ 危险操作：会删除所有业务数据！仅限开发环境使用
"""

import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'codemind.settings')
django.setup()

from django.conf import settings
from django.contrib.auth import get_user_model
from academics.models import CourseCollege, Course, Clazz, College

User = get_user_model()


def resetAll():
    if not settings.DEBUG:
        print('❌ 只允许在 DEBUG 模式下运行！')
        return

    confirm = input('确定要清空所有演示数据吗？输入 YES 确认: ')
    if confirm != 'YES':
        print('已取消')
        return

    # 按依赖顺序删除（先删关联层，再删主体）
    modelsToClear = [CourseCollege, Course, Clazz, College]
    for model in modelsToClear:
        count = model.objects.count()
        model.objects.all().delete()
        print(f'  ✅ 清空 {model.__name__}: {count} 条')

    # 清空普通用户（保留超级管理员）
    count = User.objects.filter(is_superuser=False).count()
    User.objects.filter(is_superuser=False).delete()
    print(f'  ✅ 清空普通用户: {count} 个')


if __name__ == '__main__':
    resetAll()
```

---

## 三、Fixture 模板

```yaml
# fixtures/groups.yaml
# 四个角色分组（仅建空组供演示空库使用）
# ⚠️ 正常项目流程不需要此 fixture：数据迁移 0002~0007 已在 post_migrate
#    建组并分配全部权限，权限分配以迁移为准，fixture 不含权限

- model: auth.group
  pk: 1
  fields:
    name: "教务"

- model: auth.group
  pk: 2
  fields:
    name: "课程组"

- model: auth.group
  pk: 3
  fields:
    name: "教师"

- model: auth.group
  pk: 4
  fields:
    name: "学生"
```

---

## 四、性能测试大数据生成

```python
# scripts/generate_perf_data.py
"""
性能测试大数据量生成（bulk_create 优化版）
生成大量学生账号并统一设置密码、统一入组
"""

import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'codemind.settings')
django.setup()

from django.contrib.auth.models import Group
from django.contrib.auth.hashers import make_password
from users.models import User

TEST_PASSWORD = 'Test12345'


def generateStudentsBulk(count=10000, batchSize=1000):
    """批量生成学生账号（学号前缀 + 序号），统一密码，统一入学生分组"""
    print(f'批量生成 {count} 个学生账号...')

    # 第一步：bulk_create 建账号（不设密码，密码统一第二步批量更新）
    prefix = '20231001'
    users = []
    for i in range(1, count + 1):
        users.append(User(
            username=f'{prefix}{i:04d}',  # 占位规则 8 位 + 4 位序号
            name=f'压测学生{i:05d}',
            is_active=True,
        ))
        if len(users) >= batchSize:
            User.objects.bulk_create(users)
            users = []
    if users:
        User.objects.bulk_create(users)

    # 第二步：统一密码（make_password 一次哈希，update 批量落库，比逐条 set_password 快）
    User.objects.filter(username__startswith=prefix).update(
        password=make_password(TEST_PASSWORD))

    # 第三步：统一加入学生分组
    group, _ = Group.objects.get_or_create(name='学生')
    group.user_set.add(*User.objects.filter(username__startswith=prefix))

    print(f'✅ 完成，共生成 {count} 个')


if __name__ == '__main__':
    generateStudentsBulk()
```

---

## 五、常用测试数据组合

| 场景 | 数据规模 | 用途 |
|------|----------|------|
| 冒烟测试 | 1 学院 × 2 班 × 10 生 × 2 师 + 1 课程 | 快速验证功能 |
| 功能测试 | 3 学院 × 3 班 × 40 生 × 10 师 + 3 课程 | 完整功能测试（含教师多班级边界） |
| 中等规模 | 3 学院 × 8 班 × 40 生 × 20 师 + 6 课程 | 常规性能测试 |
| 大规模 | 6 学院 × 12 班 × 50 生 × 50 师 + 10 课程 | 压力测试 |

## 使用技巧

1. **统一密码** — 所有测试账号密码统一用 `Test12345`（与 users/tests.py 的 PASSWORD 一致）
2. **账号命名** — 角色前缀：A=教务、C=课程组、T=教师、S/学号=学生、SU=超级用户；避开权限测试矩阵已占用的 T9001/T9002/S9001/A9001/C9001/SU9001
3. **角色靠 Group** — 建号后必须 `groups.add(分组)`，否则 getGroupKey 返回 None、所有权限失效
4. **学号走规则** — 学号前 8 位必须能被 `academics/student_no.py` 的 parseStudentNo 解析（年级+学院代码+班级序号）；占位规则无个人序号位，脚本补序号防撞
5. **分层生成** — 先生成学院→班级，再生成学生/教师，最后课程；M2M 挂载用 `add(*users)` 批量写
6. **批量优化** — 数据量大时用 bulk_create + 统一 make_password 批量 update，别逐条 save
7. **幂等设计** — 全部用 get_or_create，脚本可以重复跑不重复建
8. **验证权限边界** — 生成后顺手验证：教师只能访问自己 teach_clazzes 的班级（getTeacherClazz），学生的 study_clazzes 归属正确

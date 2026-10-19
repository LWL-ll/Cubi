# CodeMind 教辅平台 · API 设计文档

> 更新时间：2026-10-18
> 状态：认证/教务/教师工作台 API 已实现并测试通过（V2 表结构：学院/专业/行政班/教学班四层）；教师班级内业务 API（通知/作业等）随功能轮次补充
> 配套文档：《用户权限设计.md》（权限码与校验机制）、《数据库表重构设计.md》（V2 表结构与迁移）、《教学班级设计.md》（教学班模型决策）

## 一、总则

### 1.1 基础约定

| 项 | 约定 |
|---|---|
| 认证方式 | Session 会话（登录接口建立 Cookie 会话），JWT 未启用 |
| 数据格式 | 请求/响应均为 JSON（文件上传用 multipart/form-data） |
| 全局默认 | 所有接口默认要求登录（`IsAuthenticated`），未登录返回 **403**（页面跳转登录页） |
| 权限校验 | 功能级：视图声明 `permCode` 由 `PermRequired` 校验；动作级：写操作按动作细分权限码（见各接口） |
| 权限码体系 | 见《用户权限设计.md》，共 23 个权限码 |

### 1.2 错误码语义（重要）

| 状态码 | 含义 | 场景 |
|---|---|---|
| 400 | 请求错误 | 参数缺失/格式错、未知操作、文件未上传、跨学院班级、同名教学班 |
| 403 | 功能级拒绝 | 未登录、无该功能权限码、非教师组访问教师接口 |
| 404 | 对象级不可见 | 资源不存在**或无权访问该对象**——对象级校验统一返回 404，不暴露对象存在性 |

错误响应统一格式：`{"error": "具体原因"}`。

### 1.3 导入文件约定（xlsx）

- 所有批量导入接口用 multipart 上传 `file` 字段，xlsx 格式
- **第一行为表头（解析时跳过）**；列约定：**A 列编号**（学号/工号/课程编号）、**B 列姓名/课程名称**、**C 列初始密码**
- 学号按 8 位规则解析：前 4 位年级、中 2 位专业码、后 2 位班序（规则见 `academics/student_no.py`，正式格式定稿后只改此文件）；学院/专业/班级不存在时自动创建
- 响应：`{"successCount": n, "errors": [{"line": 行号, "reason": "原因"}]}`（行号为 xlsx 物理行号）

---

## 二、认证接口

### 2.1 登录 `POST /api/auth/login/`

公开接口（AllowAny），登录失败由 django-axes 记录，连续失败 5 次锁定 1 小时。

**请求体**
```json
{"username": "T1002", "password": "Test12345"}
```

**响应 200**
```json
{"username": "T1002", "name": "李老师", "groupKey": "teacher", "groupName": "教师"}
```
`groupKey`：`admin` / `course_team` / `teacher` / `student` / `superuser`

**错误**：400 账号或密码错误 / 请输入账号和密码

### 2.2 登出 `POST /api/auth/logout/`

需登录。响应 200：`{"message": "已退出登录"}`

### 2.3 当前用户与权限 `GET /api/auth/me/` ⭐核心接口

前端登录后调它拿权限码列表，据此渲染界面（前端隐藏只影响体验，后端校验才是防线）。

**响应 200**
```json
{
  "username": "T1002",
  "name": "李老师",
  "groupKey": "teacher",
  "groupName": "教师",
  "mustChangePassword": false,
  "permCodes": ["notice:manage", "analysis:view", "..."]
}
```

### 2.4 修改密码 `POST /api/auth/change-password/`

需登录。请求体：`{"oldPassword": "旧密码", "newPassword": "新密码"}`

- 新密码走 Django 全套强度校验（长度 ≥8、非纯数字、非常见密码、不与用户信息相似），失败 400 + 校验消息
- 成功后自动解除 `must_change_password` 标记，并保持当前会话（不踢下线）
- 响应 200：`{"message": "密码修改成功"}`；失败 400：`{"error": "旧密码不正确"}`

未改密账号的强制拦截（users/middleware.py 全局兜底，不依赖前端跳转）：
`must_change_password=True` 时，除本接口与登录/登出/`me`/admin 外的所有
API 返回 403、所有页面重定向到 `/change-password/`。

### 2.5 找回密码

一期方案：**管理员重置**——超管在 `/admin/` 直接为用户设置新密码（小规模过渡，
重置后需同步置 `must_change_password=True` 让其重新改密）。邮箱自助找回待学校
SMTP 可用后实施（见《后端落地问题清单.md》3.2）。

---

## 三、教务接口（前缀 `/api/`）

> 页面预览在 `/office/`，API 供前端调用。权限码与《用户权限.xmind》逐条对应。
> V2 组织链：学院 Department → 专业 Major → 行政班 AdminClass →（按学期组班）→ 教学班 TeachingClass。

### 3.1 学院

#### `GET /api/departments/` — 学院列表

| 权限 | course:manage |
|---|---|

学院由学号解析自动生成（无导入/新建接口）。

**响应 200**：`[{"id": 1, "code": "D01", "name": "计算机学院", "is_active": true, "adminClassCount": 2}, ...]`

### 3.2 课程

#### `GET /api/courses/` — 课程列表

| 权限 | course:manage |
|---|---|

**响应 200**
```json
[{"id": 1, "code": "C001", "name": "程序设计基础", "departments": [{"id": 1, "code": "D01", "name": "计算机学院"}]}]
```

#### `GET /api/courses/<courseId>/` — 课程详情

| 权限 | course:manage |
|---|---|

响应同上（单对象）。404：课程不存在。

#### `POST /api/courses/<courseId>/` — 开设/移除学院（动作级）

| 权限 | course:assign_class |
|---|---|

**请求体**：`{"action": "addDepartment"|"removeDepartment", "departmentId": 1}`

| action | 说明 |
|---|---|
| addDepartment | 为本课程开设学院（幂等，get_or_create） |
| removeDepartment | 先**解散该课程×学院下的全部教学班**（course_college 为 PROTECT 外键），再删除课程×学院记录（组长、教师池、班级分配一并解除） |

**响应 200**：`{"message": "已为课程开设学院 计算机学院"}`；404：课程/学院不存在；403：无 course:assign_class；400：未知操作。

### 3.3 课程×学院

#### `GET /api/courses/<courseId>/departments/<departmentId>/` — 课程×学院详情

| 权限 | course:manage |
|---|---|

**响应 200**
```json
{
  "id": 1,
  "department": {"id": 1, "code": "D01", "name": "计算机学院"},
  "leader": {"id": 5, "username": "T1002", "name": "李老师"},
  "teachers": [{"id": 5, "username": "T1002", "name": "李老师"}],
  "admin_classes": [{"id": 1, "name": "计科2301", "studentCount": 30}],
  "teaching_classes": [{"id": 1, "name": "高数-计科2301", "term": "2026-2027-1", "studentCount": 30}]
}
```
`teachers` 为教师池（可分配范围）；`leader` 未设置时为 null。404：该课程尚未开设此学院。

#### `POST /api/courses/<courseId>/departments/<departmentId>/` — 组长/教师池/行政班设置（动作级）

权限按动作细分：

| action | 所需权限码 | 参数 |
|---|---|---|
| setLeader | user:assign_role | teacherNo（工号，须已存在） |
| unsetLeader | user:assign_role | — |
| importTeachers | class:manage | file（xlsx，导入教师池并自动加入已组建的教学班） |
| addAdminClass | course:assign_class | adminClassId |
| removeAdminClass | course:assign_class | adminClassId |
| importStudents | class:manage | file（xlsx，按学号自动分班） |

**行为细节**
- setLeader：设置组长，自动加入「课程组」分组、加入教师池
- unsetLeader：撤销后若不再担任任何组长，自动移出「课程组」分组
- addAdminClass：**只允许添加本学院的班级**（经专业层判断学院归属，跨学院返回 400）
- importTeachers：创建教师账号并入「教师」分组、加入教师池，并自动分配到本课程已组建的每个教学班；**工号已存在按行报错跳过**（不修改已有账号）

**响应**：`{"message": ...}` 或导入格式 `{"successCount": n, "errors": [...]}`；400 未知操作/参数错/工号无效/跨学院班级；403 无对应权限码。

### 3.4 行政班

#### `GET /api/admin-classes/` — 全校行政班列表

| 权限 | class:manage |
|---|---|

**响应 200**
```json
[{
  "id": 1, "name": "计科2301", "code": "1001", "enrollment_year": 2023,
  "major": {"id": 1, "code": "10", "name": "计科", "department": {"id": 1, "code": "D01", "name": "计算机学院"}},
  "courses": [{"id": 1, "code": "C001", "name": "程序设计基础"}],
  "students": [{"id": 10, "username": "20231001", "name": "张三"}]
}]
```
学生经成员关系（ClassMembership，left_at 为空 = 当前在班）推导；教师任课挂教学班，行政班无 teachers 字段。

#### `GET /api/admin-classes/<adminClassId>/` — 行政班详情

| 权限 | class:manage |
|---|---|

响应同列表项（单对象）。404：班级不存在。

#### `POST /api/admin-classes/<adminClassId>/` — 导入学生（动作级）

| action | 所需权限码 | 参数 |
|---|---|---|
| importStudents | class:manage | file（xlsx，导入到本班级） |

导入三连：账号 → 学校身份（student）→ 学生资料 → 班级成员关系。400：未知操作/文件未上传。

#### `DELETE /api/admin-classes/<adminClassId>/` — 删除行政班

| 权限 | class:manage |
|---|---|

成员关系级联删除，学生账号保留。响应 200：`{"message": "已删除班级 计科2301"}`

### 3.5 教学班

#### `GET /api/teaching-classes/` — 教学班列表

| 权限 | course:manage |
|---|---|

**查询参数**：`?cc=<courseCollegeId>`（可选，过滤某课程×学院下的教学班）

**响应 200**：教学班详情数组（结构见下）。

#### `POST /api/teaching-classes/` — 组教学班

| 权限 | course:assign_class |
|---|---|

**请求体**
```json
{"name": "高数-计科2301", "courseCollegeId": 1, "adminClassIds": [1, 2], "term": "2026-2027-1"}
```
`term` 省略时用默认当前学期（CURRENT_TERM）。

**规则**（服务端兜底）：
- 组成班级至少一个，且必须都属于该课程×学院候选池（否则 400）
- 同课程同学期同名教学班 → 400
- 新教学班**默认继承课程教师池**

**响应 201**：完整教学班 JSON（含继承的教师列表）。

#### `GET /api/teaching-classes/<teachingClassId>/` — 教学班详情

| 权限 | course:manage |
|---|---|

**响应 200**
```json
{
  "id": 1, "name": "高数-计科2301", "term": "2026-2027-1",
  "course": {"id": 1, "code": "C001", "name": "高等数学"},
  "department": {"id": 1, "code": "D01", "name": "计算机学院"},
  "admin_classes": [{"id": 1, "name": "计科2301", "studentCount": 30}],
  "teachers": [{"id": 5, "username": "T1002", "name": "李老师"}],
  "students": [{"id": 10, "username": "20231001", "name": "张三"}],
  "studentCount": 30
}
```
`students`/`studentCount` 经「组成行政班 → 当前成员关系」聚合推导，多班合班自动去重。

#### `POST /api/teaching-classes/<teachingClassId>/` — 调整组成班级/教师（动作级）

| action | 所需权限码 | 参数 |
|---|---|---|
| addAdminClass | course:assign_class | adminClassId（必须在本课程×学院候选池中，否则 400） |
| removeAdminClass | course:assign_class | adminClassId（**至少保留一个组成班级**，否则 400） |
| addTeacher | class:assign_teacher | teacherNo（工号）**或** teacherId（下拉选） |
| removeTeacher | class:assign_teacher | teacherId |

**行为细节**
- addTeacher 两种方式：
  - **teacherNo 工号**：教师不存在则报错；工号添加的教师**自动加入本课程教师池**
  - **teacherId 下拉**：**校验教师必须在教师池内**（防伪造请求），不在池返回 400
- removeTeacher：仅移除教学班教师关系，账号与池内关系保留

#### `DELETE /api/teaching-classes/<teachingClassId>/` — 解散教学班

| 权限 | class:manage |
|---|---|

组成班级、教师、学生数据都保留（教学班只是授课组织）。响应 200：`{"message": "已解散教学班 ..."}`

### 3.6 批量导入

#### `POST /api/import/` — 通用批量导入

| 权限 | 基础门槛 class:manage；type=course 额外要求 course:manage |
|---|---|

**请求体**（multipart）：`type` = `course` | `teacher` | `student`；`file` = xlsx

| type | 行为 | 实际权限码 |
|---|---|---|
| course | 导入课程（编号+名称，密码列忽略） | course:manage |
| teacher | 导入教师（账号+教师身份+教师资料，入「教师」分组） | class:manage |
| student | 导入学生（**按学号自动生成学院/专业/行政班并分班**，入「学生」分组） | class:manage |

**响应 200**：`{"successCount": n, "errors": [{"line": 2, "reason": "学号不符合 8 位数字格式"}]}`

---

## 四、教师工作台接口（前缀 `/api/teacher/`）

> ⚠️ 入口校验 `IsTeacherGroup`：仅「教师」分组（或超级用户）可访问，其他角色一律 403。
> 对象级校验 `getTeacherTeachingClass`：非当前教师执教的教学班一律 404（不暴露教学班存在性）。
> 学生端接口的对称校验 `getStudentTeachingClass`：学生 → 学校身份 → 学生资料 → 当前成员关系 → 行政班 → 教学班（3 跳推导），离班成员（left_at 非空）404。

### 4.1 `GET /api/teacher/teaching-classes/` — 我的教学班列表

**响应 200**
```json
[{
  "id": 1, "name": "高数-计科2301", "term": "2026-2027-1",
  "course": {"id": 1, "code": "C001", "name": "高等数学"},
  "department": {"id": 1, "code": "D01", "name": "计算机学院"},
  "studentCount": 30
}]
```
按课程名、教学班名排序。教师可执教多个教学班，返回其全部执教教学班。

### 4.2 `GET /api/teacher/teaching-classes/<teachingClassId>/` — 教师视角教学班详情

**响应 200**：完整教学班详情（course、department、组成行政班、学生名单、同班教师、studentCount，同 3.5 字段结构，复用 `TeachingClassSerializer`）。
**404**：教学班不存在，或**不是当前教师的执教教学班**（超级用户放行）。

---

## 五、附录

### 5.1 权限码总表（22 个）

| 角色 | 权限码 | 中文名 |
|---|---|---|
| 教务 | class:manage / class:assign_teacher / course:manage / course:assign_class / user:assign_role | 班级管理 / 分配教师 / 课程管理 / 分配班级 / 指定组长 |
| 课程组 | knowledge:manage / material:class_visible / chapter:manage | AI Agent知识库 / 课程下班级资料 / 课程章节设计 |
| 教师 | notice:manage / analysis:view / material:class_manage / homework:publish / homework:bank / homework:remind / homework:comment / homework:answer / homework:ai | 班级通知 / 学情分析 / 班级内部资料 / 作业发布 / 个人题库 / 作业提醒 / 评语 / 答案 / Agent分析 |
| 学生 | homework:view / notice:view / material:view / analysis:view_self | 作业查看与提交 / 查看班级通知 / 查看班级资料 / 查看自己的学生画像 |
| 通用 | profile:edit | 修改个人资料 |

### 5.2 两级权限模型

- **功能级**：分组权限码（Group → Permission → permCode），回答"能不能用这个功能" → 无权限 **403**
- **对象级**：业务接口内校验，回答"能不能对这个对象用" → 无权访问 **404**（防探测）
  - 教师：`teachingClass.teachers` 含当前用户（1 跳）
  - 学生：身份 → 资料 → 当前成员关系 → 行政班 → 教学班（3 跳推导）

### 5.3 后续规划（随功能轮次补充）

- 教师教学班内业务 API：通知 CRUD（notice:manage）、学情图表（analysis:view）、班级资料 CRUD（material:class_manage）、作业发布/提醒（homework:publish/remind）、批改（homework:comment/answer/ai）
- 教师个人题库 API（homework:bank）
- 课程组业务 API（knowledge:manage 等 4 项）
- 学生作业 API（homework:view）
- 所有教师业务 API 必须套 `getTeacherTeachingClass`、学生业务 API 套 `getStudentTeachingClass` 对象级校验

### 5.4 测试与质量保障

- 权限测试矩阵：`manage.py test users`（11 用例，四角色 × 端点边界）
- 对象级测试矩阵：`manage.py test teaching`（23 用例：教师/学生对象级 + 三个 API 分组权限类 + 教师 API 视图）
- 教务 API 权限矩阵：`manage.py test academics`（48 用例：8 个视图 × 入口权限码/动作级权限码/参数兜底）
- 权限一致性检查：`manage.py check`（定义四处与数据库自动比对）

### 5.5 相关审查技能

项目在 Claude Code 中配置了以下审查技能（`.claude/skills/`），接口设计、评审与联调时可直接调用：

| 技能 | 用途 | 适用场景 |
|---|---|---|
| dj-api-review | RESTful 规范、状态码、响应格式、分页/过滤/排序、版本管理审查 | 写完 API 设计后、前后端联调前 |
| dj-permcheck | 功能权限、数据权限、5 种越权漏洞模式审查 | 接口权限自查、权限评审 |
| dj-serializer | N+1 检测、分层 Serializer、字段级权限、读写分离 | 列表接口慢、Serializer 结构混乱时 |
| dj-orm-opt | 查询性能审查（N+1、索引、批量操作） | 列表页慢、写完复杂查询后 |

用法：在对话中说「用 dj-api-review 审查一下」并附上接口代码/设计即可。

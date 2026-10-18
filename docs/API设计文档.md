# CodeMind 教辅平台 · API 设计文档

> 更新时间：2026-10-14
> 状态：认证/教务/教师工作台 API 已实现并测试通过；教师班级内业务 API（通知/作业等）随功能轮次补充
> 配套文档：《用户权限设计.md》（权限码与校验机制）、《账号与权限设计方案.md》（实施过程）

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
| 400 | 请求错误 | 参数缺失/格式错、未知操作、文件未上传 |
| 403 | 功能级拒绝 | 未登录、无该功能权限码、非教师组访问教师接口 |
| 404 | 对象级不可见 | 资源不存在**或无权访问该对象**——对象级校验统一返回 404，不暴露对象存在性 |

错误响应统一格式：`{"error": "具体原因"}`。

### 1.3 导入文件约定（xlsx）

- 所有批量导入接口用 multipart 上传 `file` 字段，xlsx 格式
- **第一行为表头（解析时跳过）**；列约定：**A 列编号**（学号/工号/课程编号）、**B 列姓名/课程名称**、**C 列初始密码**
- 学号按占位规则 8 位数字解析（前 4 位年级、中 2 位学院代码、后 2 位班级序号，规则见 `academics/student_no.py`，正式格式定稿后只改此文件）
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
  "permCodes": ["notice:manage", "analysis:view", "..."]
}
```

---

## 三、教务接口（前缀 `/api/`）

> 页面预览在 `/office/`，API 供前端调用。权限码与《用户权限.xmind》逐条对应。

### 3.1 学院

#### `GET /api/colleges/` — 学院列表

| 权限 | course:manage |
|---|---|

学院由学号解析自动生成（无导入/新建接口）。

**响应 200**：`[{"id": 1, "name": "计算机学院", "clazzCount": 2}, ...]`

### 3.2 课程

#### `GET /api/courses/` — 课程列表

| 权限 | course:manage |
|---|---|

**响应 200**
```json
[{"id": 1, "code": "C001", "name": "程序设计基础", "colleges": [{"id": 1, "name": "计算机学院"}]}]
```

#### `GET /api/courses/<courseId>/` — 课程详情

| 权限 | course:manage |
|---|---|

响应同上（单对象）。404：课程不存在。

#### `POST /api/courses/<courseId>/` — 开设/移除学院（动作级）

| 权限 | course:assign_class |
|---|---|

**请求体**：`{"action": "addCollege"|"removeCollege", "collegeId": 1}`

| action | 说明 |
|---|---|
| addCollege | 为本课程开设学院（幂等） |
| removeCollege | 移除学院，**连带删除课程×学院记录**（组长、教师池、班级分配一并解除） |

**响应 200**：`{"message": "已为课程开设学院 计算机学院"}`；404：课程/学院不存在；403：无 course:assign_class；400：未知操作。

### 3.3 课程×学院

#### `GET /api/courses/<courseId>/colleges/<collegeId>/` — 课程×学院详情

| 权限 | course:manage |
|---|---|

**响应 200**
```json
{
  "id": 1,
  "college": {"id": 1, "name": "计算机学院"},
  "leader": {"id": 5, "username": "T1002", "name": "李老师"},
  "teachers": [{"id": 5, "username": "T1002", "name": "李老师"}],
  "clazzes": [{"id": 1, "name": "计科2301", "studentCount": 30, "teacherCount": 2}]
}
```
`teachers` 为教师池（可分配范围）；`leader` 未设置时为 null。404：该课程尚未开设此学院。

#### `POST /api/courses/<courseId>/colleges/<collegeId>/` — 组长/教师池/班级设置（动作级）

权限按动作细分：

| action | 所需权限码 | 参数 |
|---|---|---|
| setLeader | user:assign_role | teacherNo（工号，须已存在） |
| unsetLeader | user:assign_role | — |
| importTeachers | class:manage | file（xlsx，导入教师池并自动分配到已关联班级） |
| addClazz | course:assign_class | clazzId |
| removeClazz | course:assign_class | clazzId |
| importStudents | class:manage | file（xlsx，按学号自动分班） |

**行为细节**
- setLeader：设置组长，自动加入「课程组」分组、加入教师池
- unsetLeader：撤销后若不再担任任何组长，自动移出「课程组」分组
- addClazz：**只允许添加本学院的班级**（服务端兜底校验，跨学院返回 400）
- importTeachers：创建教师账号并入「教师」分组、加入教师池，并自动分配到本课程已关联的每个班级；**工号已存在按行报错跳过**（不修改已有账号）

**响应**：`{"message": ...}` 或导入格式 `{"successCount": n, "errors": [...]}`；400 未知操作/参数错/工号无效/跨学院班级；403 无对应权限码。

### 3.4 班级

#### `GET /api/classes/` — 全校班级列表

| 权限 | class:manage |
|---|---|

**响应 200**
```json
[{
  "id": 1, "name": "计科2301",
  "college": {"id": 1, "name": "计算机学院"},
  "courses": [{"id": 1, "code": "C001", "name": "程序设计基础"}],
  "students": [{"id": 10, "username": "20231001", "name": "张三"}],
  "teachers": [{"id": 5, "username": "T1002", "name": "李老师"}]
}]
```

#### `GET /api/classes/<clazzId>/` — 班级详情（含教师池候选）

| 权限 | class:manage |
|---|---|

**查询参数**：`?cc=<courseCollegeId>`（可选，课程×学院上下文）

- 带 `cc`：额外返回 `availableTeachers` = 该课程×学院教师池中**未分配**到本班的教师（班级详情页教师下拉候选）
- 不带 `cc`：`availableTeachers` 为空数组

**响应 200**：班级详情字段 + `"availableTeachers": [...]`

#### `POST /api/classes/<clazzId>/` — 分配/移除教师、导入学生（动作级）

| action | 所需权限码 | 参数 |
|---|---|---|
| addTeacher | class:assign_teacher | teacherNo（工号）**或** teacherId（下拉选） |
| removeTeacher | class:assign_teacher | teacherId |
| importStudents | class:manage | file（xlsx，导入到本班级） |

**行为细节**
- addTeacher 两种方式：
  - **teacherNo 工号**：教师不存在则报错；工号添加的教师**自动加入课程×学院教师池**（需带 ccId 上下文）
  - **teacherId 下拉**：带 `ccId`（body 内）时**校验教师必须在池内**（防伪造请求），不在池返回 400
- removeTeacher：仅移除班级教师关系，账号保留

**请求体示例**：`{"action": "addTeacher", "teacherId": 5, "ccId": 1}`

#### `DELETE /api/classes/<clazzId>/` — 删除班级

| 权限 | class:manage |
|---|---|

学生和教师账号保留，课程分配关系解除。响应 200：`{"message": "已删除班级 计科2301"}`

### 3.5 批量导入

#### `POST /api/import/` — 通用批量导入

| 权限 | 基础门槛 class:manage；type=course 额外要求 course:manage |
|---|---|

**请求体**（multipart）：`type` = `course` | `teacher` | `student`；`file` = xlsx

| type | 行为 | 实际权限码 |
|---|---|---|
| course | 导入课程（编号+名称，密码列忽略） | course:manage |
| teacher | 导入教师（入「教师」分组） | class:manage |
| student | 导入学生（**按学号自动生成学院/班级并分班**，入「学生」分组） | class:manage |

**响应 200**：`{"successCount": n, "errors": [{"line": 2, "reason": "学号不符合 8 位数字格式"}]}`

---

## 四、教师工作台接口（前缀 `/api/teacher/`）

> ⚠️ 入口校验 `IsTeacherGroup`：仅「教师」分组（或超级用户）可访问，其他角色一律 403。
> 对象级校验 `getTeacherClazz`：非当前教师执教的班级一律 404（不暴露班级存在性）。

### 4.1 `GET /api/teacher/clazzes/` — 我的教学班级列表

**响应 200**
```json
[{
  "id": 1, "name": "计科2301",
  "college": {"id": 1, "name": "计算机学院"},
  "courses": [{"id": 1, "code": "C001", "name": "程序设计基础"}],
  "studentCount": 30
}]
```
按学院、班级名排序。教师可执教多个班级，返回其全部执教班级。

### 4.2 `GET /api/teacher/clazzes/<clazzId>/` — 教师视角班级详情

**响应 200**：班级详情（学院、课程列表、学生列表、同班教师列表，同 3.4 的字段结构）。
**404**：班级不存在，或**不是当前教师的执教班级**（超级用户放行）。

---

## 五、附录

### 5.1 权限码总表（23 个）

| 角色 | 权限码 | 中文名 |
|---|---|---|
| 教务 | class:manage / class:assign_teacher / course:manage / course:assign_class / user:assign_role | 班级管理 / 分配教师 / 课程管理 / 分配班级 / 指定组长 |
| 课程组 | knowledge:manage / material:class_visible / question_bank:manage / chapter:manage | AI Agent知识库 / 课程下班级资料 / 作业题库 / 课程章节设计 |
| 教师 | notice:manage / analysis:view / material:class_manage / homework:publish / homework:bank / homework:remind / homework:comment / homework:answer / homework:ai | 班级通知 / 学情分析 / 班级内部资料 / 作业发布 / 个人题库 / 作业提醒 / 评语 / 答案 / Agent分析 |
| 学生 | homework:view / notice:view / material:view | 作业查看与提交 / 查看班级通知 / 查看班级资料 |
| 通用 | profile:edit | 修改个人资料 |

### 5.2 两级权限模型

- **功能级**：分组权限码（Group → Permission → permCode），回答"能不能用这个功能" → 无权限 **403**
- **对象级**：业务接口内校验（教师：`clazz.teachers` 含当前用户），回答"能不能对这个对象用" → 无权访问 **404**（防探测）

### 5.3 后续规划（随功能轮次补充）

- 教师班级内业务 API：通知 CRUD（notice:manage）、学情图表（analysis:view）、班级资料 CRUD（material:class_manage）、作业发布/提醒（homework:publish/remind）、批改（homework:comment/answer/ai）
- 教师个人题库 API（homework:bank）
- 课程组业务 API（knowledge:manage 等 4 项）
- 学生作业 API（homework:view）
- 所有教师业务 API 必须套 `getTeacherClazz` 对象级校验

### 5.4 测试与质量保障

- 权限测试矩阵：`manage.py test users`（11 用例，四角色 × 端点边界）
- 对象级测试矩阵：`manage.py test teaching`（教师/学生对象级校验 + 三个 API 分组权限类，15 用例）
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

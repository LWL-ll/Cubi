# CodeMind 教辅平台 · API 设计文档

> 更新时间：2026-10-27
> 状态：教务、教师及课程组必要业务后端已补齐；本地业务库已于 2026-10-27 备份后应用本轮迁移并校验，生产环境独立验收。新增接口完整契约见 [非学生端后端接口](非学生端后端接口.md)。
> 配套文档：《用户权限设计.md》（权限码与校验机制）、《数据库表重构设计.md》（V2 表结构与迁移）、《教学班级设计.md》（教学班模型决策）

## 一、总则

### 1.1 基础约定

| 项 | 约定 |
|---|---|
| 认证方式 | Session 会话（登录接口建立 Cookie 会话），JWT 未启用 |
| 数据格式 | 请求/响应均为 JSON（文件上传用 multipart/form-data） |
| 全局默认 | 所有接口默认要求登录（`IsAuthenticated`），未登录返回 **403**（页面跳转登录页） |
| 权限校验 | 功能级：视图声明 `permCode` 由 `PermRequired` 校验；动作级：写操作按动作细分权限码（见各接口） |
| 权限码体系 | 见《用户权限设计.md》，共 22 个权限码 |

### 1.2 错误码语义（重要）

| 状态码 | 含义 | 场景 |
|---|---|---|
| 400 | 请求错误 | 参数缺失/格式错、未知操作、文件未上传、跨学院班级、同名教学班 |
| 403 | 功能级拒绝 | 未登录、无该功能权限码、非教师组访问教师接口 |
| 404 | 对象级不可见 | 资源不存在**或无权访问该对象**——对象级校验统一返回 404，不暴露对象存在性 |

认证及部分教务接口返回 `{"error": "具体原因"}`；DRF 权限和对象错误返回 `{"detail": "具体原因"}`，参数校验可能返回字段错误字典或错误列表。客户端须同时处理 HTTP 状态与响应内容。

### 1.3 导入文件约定

**教务批量导入（学生/教师/课程）— xlsx**

- 所有教务批量导入接口用 multipart 上传 `file` 字段，xlsx 格式
- **第一行为表头（解析时跳过）**；列约定：**A 列编号**（学号/工号/课程编号）、**B 列姓名/课程名称**、**C 列初始密码**
- 学号按 8 位规则解析：前 4 位年级、中 2 位专业码、后 2 位班序（规则见 `academics/student_no.py`，正式格式定稿后只改此文件）；学院/专业/班级不存在时自动创建
- 响应：`{"successCount": n, "errors": [{"line": 行号, "reason": "原因"}]}`（行号为 xlsx 物理行号）

**题库批量导入（公共题库）— ZIP**

- 仅系统管理员可调用：`POST /api/admin/problems/import/`，或后台页面 `/admin/teaching/problem/import/`
- multipart 上传 `file`（ZIP）字段；ZIP 内为 `manifest.xlsx`/`manifest.csv` 清单 + 每题一个 `.md`
- 清单列：`file`（必填）、`code`、`tier`、`languages`、`compareStrategy`
- 响应：`{"batchId", "total", "createdCount", "updatedCount", "skippedCount", "errorCount", "errors": [{"file", "reason"}]}`
- 完整包格式、去重/覆盖语义与限制见 `docs/题库批量导入.md`

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
| removeDepartment | 同一事务中解散没有教学数据的教学班，再解除课程×学院；任一教学班已有作业、资料或通知则整次拒绝，保留组长、教师池及全部关联 |

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
| importTeachers | class:manage + class:assign_teacher | file（xlsx，导入教师池并自动加入已组建的教学班） |
| addAdminClass | course:assign_class | adminClassId |
| removeAdminClass | course:assign_class | adminClassId |
| importStudents | class:manage | file（xlsx，按学号自动分班） |

**行为细节**
- setLeader：设置组长，自动加入「课程组」分组、加入教师池
- unsetLeader：撤销后若不再担任任何组长，自动移出「课程组」分组
- addAdminClass：**只允许添加本学院的班级**（经专业层判断学院归属，跨学院返回 400）
- removeAdminClass：已用于组成教学班的行政班不能移出候选池，返回 400；先显式调整教学班组成。
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
学生经成员关系（ClassMembership，left_at 为空 = 当前在班）推导；列表项和详情增加 `studentCount`，仅计当前成员，行政班简表口径相同。学院 `adminClassCount` 经学院→专业→行政班统计。教师任课挂教学班，行政班无 teachers 字段。

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

仅无当前或历史成员、且未组成教学班的闲置行政班允许硬删除；否则 400，保留成员历史和组织关联。闲置行政班的候选池关系随删除清理。响应 200：`{"message": "已删除班级 计科2301"}`。

### 3.5 教学班

#### `GET /api/teaching-classes/` — 教学班列表

| 权限 | course:manage |
|---|---|

**查询参数**：`cc=<courseCollegeId>`、`term=<学期>`，均可选且可组合；无效编号、空学期或超过 20 字的学期返回 400。

**响应 200**：教学班详情数组（结构见下）。

#### `POST /api/teaching-classes/` — 组教学班

| 权限 | course:assign_class |
|---|---|

**请求体**
```json
{"name": "高数-计科2301", "courseCollegeId": 1, "adminClassIds": [1, 2], "term": "2026-2027-1"}
```
`term` 省略时使用 AcademicTerm 中唯一的当前学期；显式提供时也必须已经登记。关闭和归档学期不可新组班。

**规则**（服务端兜底）：
- 组成班级至少一个，且必须都属于该课程×学院候选池（否则 400）
- 请求体必须是对象；名称为非空文本且不超过 50 字，学期为文本且不超过 20 字；组成班编号须为非空、无重复的有效正整数数组。未知字段、非法类型及无效字符均返回 400。
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

没有作业、资料、通知的教学班可以解散，组成行政班、教师和学生账号保留。已有任一上述教学数据则返回 400，保留全部关联。响应 200：`{"message": "已解散教学班 ..."}`。

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

所有教务 XLSX 导入入口共用以下规则：

- 文件不超过 10MiB，ZIP 声明解压总大小不超过 64MiB；不支持加密文件。最多 10000 个物理数据行（含空白行），仅 A～C 列可有内容。
- 第一物理行为表头；空白数据行跳过，但错误行号保持 Excel 原始行号。损坏文件及超限文件返回安全的 400，不输出解析器或数据库详情。
- 学工号按用户名规范化并校验，长度不超过 32；姓名不超过 50；课程编号不超过 20、课程名称不超过 50；初始密码非空且不超过 1024。必填文本须为有效 UTF-8，不接受 NUL 字符。
- 一行一事务：账号、身份、资料、成员关系及本行新建组织或任课关联共同回滚；其他合法行继续导入。重复账号报错跳过，不覆盖原密码或资料；新账号仍须首次登录改密。
- 正式学号格式尚未确定，自动分班继续沿用现有占位规则；指定行政班导入按编号文本校验，不自行推断新的位数或分段。
- 普通教师导入使用 `class:manage`；课程教师池导入额外要求 `class:assign_teacher`，权限不足时整次拒绝，不创建账号。页面后端与 API 使用相同校验和事务规则。

### 3.7 学生档案与成员管理

以下接口均使用 `class:manage`，`studentId` 为用户 ID，与教师名单中的学生 ID 一致。

| 方法与路径 | 作用 |
|---|---|
| `GET /api/students/` | 学生列表，固定分页 20 |
| `GET /api/students/<studentId>/` | 学生档案与当前班级 |
| `GET /api/students/<studentId>/memberships/` | 本人班级成员历史，固定分页 20，按入班日期及记录 ID 倒序 |
| `POST /api/students/<studentId>/transfer/` | 请求体仅 `{"adminClassId": 正整数}`，办理当日转班或重新入班 |
| `POST /api/students/<studentId>/leave/` | 请求体仅 `{}`，办理当日退班 |

- 列表可按 `q`（姓名、账号或学号包含匹配，最多 100 字）、`adminClass`、`major`、`department`、`academicStatus`、`inClass=true/false` 筛选；行政班筛选只认当前关系。错误编号、状态、布尔值或无效文本返回 400。
- 学生字段为 `id/username/name/schoolNumber/major/enrollmentYear/academicStatus/currentMembership`。major 含 `id/code/name/department`，department 含 `id/code/name`。currentMembership 为 `id/adminClassId/adminClassName/joinedAt`，未在班时为 null。历史条目另含 `leftAt`；不返回密码或其他认证字段。
- 每个用户采用最早的学生身份，且该身份须有学生档案，与现有学生权限入口一致；不回退到其他学生身份。不存在的学生或缺少该档案返回 404。
- 转班只允许同专业、同入学年份，目标专业和学院须启用；目标班级年级未知、原入班日期晚于今天时拒绝。服务先锁定并重读学生档案及目标行政班，再关闭旧关系、建立新关系；日期取服务端本地当天。
- 重复转入当前班级、重复退班均幂等。退班只结束成员关系，不删除账号、不改变学籍状态；无当前班时可重新入班。
- 旧作业、提交及评阅保留原归属。成员变化后，学生对原教学班作业、资料和通知的访问随当前成员关系即时变化；本人历史提交仍可回看。
- 普通转班要求学生在读且目标班启用；跨专业/年级或学籍状态变更通过新增 academic 接口办理，见 [非学生端后端接口](非学生端后端接口.md)。正式学号规则仍待确定，接口尚未接入页面。

### 3.8 教务后端验证（2026-10-26）

- 新增 74 项回归：导入 33 项、组织与后台表单 21 项、学生管理 19 项、原权限矩阵新增 1 项；全量 **529 项测试通过**。
- 已验证损坏文件与导入限额、物理错误行号、失败行回滚、密码不覆盖、动作权限、当前人数统计、组织删除与 M2M 正反入口约束，以及转退班后的访问变化和历史保留。
- 后台教学班表单在同一事务中先加入新组成班、再移除旧组成班，合法替换不会违反非空约束。合班权限旧测试相应修正准备数据，保留原有授权断言。
- 验证命令：`python manage.py test`、`python manage.py check`、`python manage.py makemigrations --check --dry-run`、`git diff --check`。未执行业务库迁移，学号占位规则未调整。
- 此为 2026-10-26 教务阶段的历史验收基线。Git 协作规则统一见 [AGENTS.md 的版本管理约定](../AGENTS.md#版本管理)。

### 3.9 非学生端补齐（2026-10-27）

学院/专业、课程/行政班维护、账号资料与超管重置密码、学籍异动、学期关闭/归档、后台导入、审计和课程组资源接口已实现。路径及字段见 [非学生端后端接口](非学生端后端接口.md)。本轮保持 22 个权限码及已有列表契约，增加 academics 0011/0012、tasks 0004/0005、course_team 0001 迁移；已在本地业务库应用，验收见 3.10。

关闭学期冻结名单，归档冻结成绩；原作业、通知、班级资料、评阅、答案、任课分配及提交接口执行对应写限制。教师班级列表/详情、看板、成绩和导出读取冻结口径。全局课程共享库持续维护，指定归档班级的资料范围受保护。

### 3.10 本地业务库迁移验收（2026-10-27）

- 用户单独授权后，先通过 SQLite backup API 创建 `db.sqlite3.backup-20261027-150652-227043`，确认备份完整性、外键及迁移前数据指纹，再执行准确的上述 5 项迁移。
- 原有 44 张表中，40 张业务表按原列计算的逐行 SHA256 多重集指纹与行数完全相同；账号、密码散列、成员、任课及角色权限关联未变。课程/行政班新增 is_active 均为 true。
- 新增 10 张表：学期表登记原编码集合并保留默认当前学期 2026-2027-1，日期留空；其余 9 张新表为空。没有自动关闭学期、冻结历史成绩或创建导入批次。
- 原有元数据行逐行保留，仅新增 5 条迁移、8 个 content type、32 个原生模型权限和 1 条回收排程；原 22 个显式权限码不变。office-import-recovery 为默认 codemind 集群每分钟任务，原判题排程不变。
- SQLite 完整性和外键检查通过；系统检查、无待应用迁移及模型无漂移检查通过。9 个只读接口检查均返回 200，执行时拦截数据库写语句，未创建会话或其他业务数据。
- 数据库、备份与本地核验指纹文件不纳入 Git。生产服务器迁移、默认 worker 的真实消费及进程守护须单独验收。

---

## 四、教学业务接口（题库 / 作业 / 提交）

> 4.1~4.4 使用 `/api/teacher/` 前缀；4.5 学生作业与提交使用 `/api/`；
> 管理员题库导入见第五章。
> 教师接口：入口 `IsTeacherGroup`/权限码 → 对象级 `getTeacherTeachingClass`/`getManageableHomework`，越界 404；
> 学生接口：`getStudentTeachingClass` 经「身份 → 资料 → 成员关系 → 行政班 → 教学班」三跳推导，离班 404。

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

### 4.3 题库接口（教师 + 超级用户）

| 端点 | 方法 | 权限 | 说明 |
|---|---|---|---|
| `/api/teacher/problems/` | GET | homework:bank | 公共题库 + 本人个人题；分页 20；支持 `q`（标题关键词）、`tier`、`scope=all/public/mine` |
| `/api/teacher/problems/` | POST | homework:bank | 新建个人题；`is_public` 由端点决定，请求体无效 |
| `/api/teacher/problems/<id>/` | GET | homework:bank | 题目详情；教师可读公共题与本人个人题，他人个人题 404 |
| `/api/teacher/problems/<id>/` | PUT/DELETE | homework:bank + 对象级 | 编辑/删除本人个人题；公共题仅超级用户可写，越权 404 |
| `/api/teacher/public-problems/` | POST | IsSuperUser | 新建公共题（仅系统管理员） |

**筛选示例**：`GET /api/teacher/problems/?scope=public&tier=basic&q=两数`
**保密链**：题库接口一律不返回 `tests`/`spj_code`；学生题面接口同样走白名单。

### 4.4 作业组卷接口（教师）

| 端点 | 方法 | 权限 | 说明 |
|---|---|---|---|
| `/api/teacher/teaching-classes/<id>/homeworks/` | GET/POST | homework:publish + `getTeacherTeachingClass` | 教学班作业列表 / 创建作业 |
| `/api/teacher/homeworks/<id>/` | GET/PUT/DELETE | homework:publish + `getManageableHomework` | 作业详情 / 编辑 / 删除 |

创建校验：标题非空 ≤100、开始时间 < 截止时间、`problemIds` 非空去重且 ≤20；
题目必须对教师可见（公共题 + 本人个人题）；`problem_revision` 冻结选题时修订号。
编辑：已有提交后题目列表锁定，截止时间只允许向后延长。删除：有提交 400。

### 4.5 学生作业与提交接口（前缀 `/api/`）

| 端点 | 方法 | 权限 | 说明 |
|---|---|---|---|
| `/api/homeworks/` | GET | homework:view | 我的作业列表（三态 + 我的提交概况） |
| `/api/homeworks/<id>/` | GET | homework:view + 对象级 | 作业详情；开始前 404 防提前看题 |
| `/api/problems/<id>/` | GET | homework:view | 学生题面（title/statementMd/samples/languages，无 tests/spj） |
| `/api/submissions/` | POST/GET | homework:view | 受理提交 / 我的提交列表（分页 20，可按题/作业/状态过滤） |
| `/api/submissions/<id>/` | GET | homework:view | 提交详情 + 判题进度（仅本人或超管，否则 404） |

---

## 五、题库导入与教师后端接口

### 5.1 `POST /api/admin/problems/import/` — ZIP 批量导入公共题库

- **权限**：`IsSuperUser`（仅 `is_superuser`；教师/课程组/教务/学生一律 403）
- **请求**：multipart `file`（ZIP），可选 `overwrite`（`1`/`true`/`on`）
- **响应 200**：`{"batchId", "total", "createdCount", "updatedCount", "skippedCount", "errorCount", "errors": [{"file", "reason"}]}`
- **响应 400**：ZIP/清单结构错误（缺 manifest、路径穿越、超过大小/数量限制等）
- **导入语义**：题目进入公共题库；按 `source_code` 去重，内容未变跳过，内容变化且勾选覆盖时更新并 `revision+1`
- 完整包格式、限制与操作步骤见 `docs/题库批量导入.md`

---

### 5.2 教师个人题库批量导入

| 方法与路径 | 权限 | 行为 |
|---|---|---|
| `POST /api/teacher/problems/import/` | homework:bank | ZIP 导入本人个人题库 |
| `GET /api/teacher/problems/imports/` | homework:bank | 本人个人题库导入历史，分页 20 |

- 文件包和校验限制复用公共导入格式；单批 ZIP ≤64MB、题数 ≤2000。
- POST 仅接受 multipart `file`、`overwrite`；后者支持 `true/false/1/0/on/off`，默认 false。归属取当前会话，不接受 ownerId、isPublic 等字段。
- 题目编号在公共题库内唯一；个人题编号按教师分别唯一。公共题及不同教师可使用同一 code，覆盖导入只能修改本库题目；历史作业快照不变。
- 返回原有计数和错误明细，另含 `scope: personal`。历史返回 `id/status/createdAt/finishedAt/result/error`，包含失败批次，不包含他人或公共题库批次。

### 5.3 教师班级看板与成绩

以下接口均要求 `analysis:view`，并校验当前教师任教关系；对象不可见统一 404。

| 方法与路径 | 内容 |
|---|---|
| `GET /api/teacher/teaching-classes/<id>/dashboard/` | 班级人数、作业数、各作业提交/完成/通过人数、待判/未评阅提交数量及平均分 |
| `GET /api/teacher/teaching-classes/<id>/students/` | 当前学生看板，分页 20；支持 q 按姓名、账号、学号搜索 |
| `GET /api/teacher/teaching-classes/<id>/students/<studentId>/` | 当前在班学生的各作业和各题成绩、进度 |
| `GET /api/teacher/homeworks/<id>/grades/` | 作业成绩表，分页 20；支持 q、submissionState |
| `GET /api/teacher/homeworks/<id>/grades/export/` | 相同筛选范围的 xlsx 成绩表，附件下载 |

**统一成绩口径**：

- 每题取该学生、该作业内最高有效终态得分，再合计；`queued/running/system_error/repeat` 不作为有效成绩。编译错误、答案错误等正常判题终态可以是有效 0 分。
- 无有效结果返回 score=null；已有有效成绩不因再次排队而清零。pendingCount 单独展示，gradingComplete 表示各题均已有有效成绩，班级平均分只统计这部分学生。
- totalScore 与题目标题取作业冻结快照；实时改题不改变历史作业。学生作业列表 myBestScore 使用同一逐题合计口径。
- submissionState：`not_submitted` 全部未交，`partial` 部分题已交，`submitted` 每题都有提交；不等同于全部通过，passed 单独返回。系统错误也属于已提交，不能混入未提交名单。
- 看板、成绩与未交名单按当前在班成员计算；转班前的提交仍可通过本班提交列表和详情查看，不删除历史数据。
- grades 响应含 `homeworkId/title/problems/summary/count/next/previous/results`；summary 始终为全班统计，count/results 随筛选变化。未提交筛选使用 `submissionState=not_submitted`。
- 每个学生行包含身份简要信息、score/totalScore、各题 problems、提交/待判/未评阅数量、完成状态。导出保留相同学生和题序；没有学生也保留题目列；姓名、题名等按 Excel 文本写入，不执行公式。

### 5.4 提交查看与评阅

| 方法与路径 | 权限 | 内容 |
|---|---|---|
| `GET /api/teacher/teaching-classes/<id>/submissions/` | analysis:view | 班级提交列表，分页 20；筛选 homework/student/problem/status/reviewed |
| `GET /api/teacher/submissions/<id>/` | analysis:view | 代码、脱敏判题结果、当前教师批语 |
| `GET/PUT/DELETE /api/teacher/submissions/<id>/review/` | homework:comment | 当前批语读取、保存或删除 |

- 筛选 ID 必须为正整数；reviewed 仅接受 true/false。列表不返回代码原文，详情不返回隐藏测试点、判题快照、SPJ 或沙箱内部标识。
- PUT 只接受 `{"comment":"批语"}`，1～5000 字；评阅教师取当前会话。同班授课教师可共同维护，一次提交仅保存一份当前批语，并记录首次/最近评阅时间；不改写原始判题分数。
- GET/PUT 返回 `{"review": {...}}`；未评阅时 GET 返回 `{"review": null}`。DELETE 成功 204，该提交恢复未评阅状态。
- 学生原提交详情新增 teacherReview（批语、教师、时间），只对本人可读；他人提交或非任教教师与资源不存在时均为相同 404。

### 5.5 班级教学资料

| 方法与路径 | 权限 | 内容 |
|---|---|---|
| `GET/POST /api/teacher/teaching-classes/<id>/materials/` | material:class_manage | 列表分页 20、上传（成功 201） |
| `GET/PATCH/DELETE /api/teacher/materials/<id>/` | material:class_manage | 元数据读取、修改或替换文件、删除 |
| `GET /api/teacher/materials/<id>/download/` | material:class_manage | 任教教师附件下载 |
| `GET /api/teaching-classes/<id>/materials/` | material:view | 当前班级学生只读列表，分页 20 |
| `GET /api/materials/<id>/download/` | material:view | 当前班级学生附件下载 |

- 上传须 multipart，title 必填（≤100 字）、description 可选（≤2000 字）、file 必填；PATCH 只接受这三个字段，替换文件时使用 multipart。
- 单文件非空且 ≤50MB；允许扩展名 pdf/doc/docx/xls/xlsx/ppt/pptx/txt/md/csv/zip/c/py，不展开压缩包。文件均以二进制附件下载。
- 50MB 为应用层限制，部署时反向代理上传上限须覆盖文件和 multipart 开销，私有目录须允许应用账号读写。
- 存储在 PRIVATE_MATERIAL_ROOT（默认项目 private_uploads），使用随机文件名；该目录不可配置为公开静态目录。API 只返回原文件名、大小、标题、说明、上传人和时间，不返回物理路径、存储标识或公开 URL。
- 下载前检查班级访问权限，离班立即失去下载权限；教师也只能管理任教班级资料。
- 替换/删除在数据库提交后清理旧文件，保存失败清理新文件；旧文件清理发生磁盘故障时记录随机标识供运维处理，已删除资料不能继续经 API 下载。

### 5.6 个人资料

- `GET/PATCH /api/auth/profile/`，权限 profile:edit，四类角色共用；首次登录仍须先改密。
- GET 返回 username/name/email、只读学校身份列表，以及教师职称和所属学院（无教师档案时 teacher=null）。
- PATCH 仅接受 name（非空、≤50 字）、email（合法邮箱或空字符串）、title（≤50 字，须已有教师档案），部分更新。
- 工号/学号、账号、角色、权限、密码、所属学院及任教关系不可通过本接口修改。未知字段或非法数据整次拒绝，不产生部分更新；教师档案不会由该接口自行创建。

### 5.7 班级通知与阅读情况

| 方法与路径 | 权限 | 内容 |
|---|---|---|
| `GET/POST /api/teacher/teaching-classes/<id>/notices/` | notice:manage | 任教班级通知列表（分页 20）/发布（201） |
| `GET/PATCH/DELETE /api/teacher/notices/<id>/` | notice:manage | 任教班级通知详情、部分修改、删除（204） |
| `GET /api/teacher/notices/<id>/readers/` | notice:manage | 当前学生阅读情况，分页 20 |
| `GET /api/teaching-classes/<id>/notices/` | notice:view | 当前班级学生通知列表，分页 20 |
| `GET /api/notices/<id>/` | notice:view | 当前班级学生通知详情 |
| `POST /api/notices/<id>/read/` | notice:view | 标记本人首次阅读，返回 `{"readAt": ...}` |

- POST 创建只接受 `title`（必填，1～100 字）、`content`（必填，1～10000 字）；PATCH 仅接受这两个字段的非空子集，正文按普通文本保存。不接受班级、作者或接收人覆盖字段。
- 通知返回 `id/teachingClassId/title/content/createdById/updatedById/createdAt/updatedAt`，学生响应另含 `myReadAt`（未读为 null）。GET 不自动标记已读，标记接口请求体为空对象 `{}`，重复请求保留首次时间。
- readers 返回分页 `count/next/previous/results`，另含 `noticeId/summary`；summary 为 `studentCount/readCount/unreadCount`，始终按当前全班计算。results 含当前学生简要身份及 `readAt`，离班历史阅读记录保留但不计入统计。
- 同班教师可共同维护；通知编辑不清空阅读记录，因此已读仅表示曾读过通知，不保证读过最新版本。删除通知同时删除其阅读记录。
- 教师须任教、学生须当前在班；跨班和不存在的通知返回相同 404。所有作者、修改人和阅读人取当前会话。

### 5.8 作业站内提醒

| 方法与路径 | 权限 | 内容 |
|---|---|---|
| `GET /api/teacher/homeworks/<id>/reminders/` | homework:remind | 本班作业待提醒人数预览 |
| `POST /api/teacher/homeworks/<id>/reminders/` | homework:remind | 为满足条件的学生生成站内提醒 |
| `GET /api/reminders/` | notice:view | 本人、当前班级的提醒列表，分页 20 |
| `POST /api/reminders/<id>/read/` | notice:view | 标记本人提醒已读，返回提醒对象 |

- 仅 `startAt <= now <= deadline` 可预览或发送；接收人由服务端当前班级名单计算，为尚未交齐每道题目的学生。提交后判错、系统错误也算该题已交，不以 AC 或成绩判断。
- 同作业、同学生滚动一小时内不重复生成，冷却由同班所有教师共享；满一小时可再次提醒。发送在作业事务锁内重读题目、提交及提醒窗口，避免并发重复生成。
- 预览返回 `homeworkId/eligibleCount/readyCount/skippedCount`；发送返回 `homeworkId/eligibleCount/sentCount/skippedCount`。eligibleCount 为未交齐人数，skippedCount 为仍在冷却的人数。
- 发送与标记已读请求体均为 `{}`；不接受客户端指定发送人、接收人或修改已读时间。重复标记保留首次时间。
- 提醒对象返回 `id/homeworkId/homeworkTitle/teachingClassId/deadline/sentById/createdAt/readAt`。离班后原提醒不可见，他人的提醒与不存在的提醒均为相同 404。
- 本轮仅创建站内数据库记录，不投递邮件、短信或其他外部消息，也不创建异步发送任务；前端尚未接入。

### 5.9 作业参考答案

| 方法与路径 | 权限 | 内容 |
|---|---|---|
| `GET /api/teacher/homeworks/<id>/answers/` | homework:answer | 任教班级作业答案列表，包含草稿 |
| `PUT/DELETE /api/teacher/homeworks/<id>/answers/<problemId>/` | homework:answer | 保存或删除本作业一道题的答案 |
| `POST /api/teacher/homeworks/<id>/answers/publish/` | homework:answer | 作业截止后手动公开，重复请求幂等 |
| `GET /api/homeworks/<id>/answers/` | homework:view | 当前班级学生查看已经公开且已经截止的答案 |

- PUT 只接受 `{"content":"参考答案"}`，正文非空、≤50000 字，允许提前维护。problemId 为本作业题目主键；他班作业或未选入本作业的题目不可写入。同班任教教师共同维护，修改人取会话。
- 列表和发布返回 `{"homeworkId": ..., "releasedAt": ..., "answers": [...]}`；每条为 `problemId/content/updatedById/createdAt/updatedAt`，按作业题序排列。PUT 返回对应答案对象，DELETE 成功 204。
- 发布请求体为 `{}`，首次发布要求 `now > deadline` 且至少有一条答案，允许部分题目先公开。首次公开时记录 releasedAt；以后新增、纠错或删除答案直接反映在已公开列表，删除全部答案也不清空首次公开标志。
- 一旦公开，永久禁止延长该作业截止时间或更换题目。公开、作业编辑和提交共用作业事务锁并重读状态，防止发布与延期并发绕过约束。
- 教师作业列表和详情新增只读 `answersReleasedAt`；学生作业列表和详情新增 `answersAvailable`，仅在已公开且 `now > deadline` 时为 true。这些接口不嵌入答案正文；答案正文只通过独立 answers 接口读取。
- 学生提前、未公开、跨班或离班访问均返回相同“参考答案不存在”404；答案只来自教师手写内容，不读取隐藏测试点、预期输出、SPJ、判题快照或 Agent 自测代码，不改变题面和判题数据。

### 5.10 迁移与验证

- teaching.0006_teacher_backend 包含 SubmissionReview、TeachingMaterial 及题目编号作用域唯一约束；已于 2026-10-26 在本地业务库备份后应用，原有 35 张非元数据表逐行校验未变。生产环境仍须独立确认迁移状态。
- teaching.0007_teacher_communication 包含 ClassNotice、NoticeRead、HomeworkReminder、HomeworkAnswer 及 Homework.answers_released_at；已于 2026-10-26 在本地业务库备份后应用，备份为 `db.sqlite3.backup-20261026-200124-648249`。新增四表初始为空，现有作业公开时间均为 NULL，原有 37 张非元数据业务表逐行指纹不变；SQLite 完整性与外键检查通过，无待应用迁移。元数据仅新增 1 条迁移、4 个 content type 和 16 个原生模型权限，原 22 个显式权限及角色关联不变；生产环境仍须独立确认迁移状态。
- 若已产生跨教师同编号题目，恢复旧版全局唯一约束前须先处理重复编号；不自动删除或合并题目。
- 验证命令：`python manage.py test teaching.tests_notices teaching.tests_reminders teaching.tests_answers`、`python manage.py test`、`python manage.py check`、`python manage.py makemigrations --check --dry-run`。本轮不修改前端或连接真实判题沙箱。
- 本轮验证（2026-10-26）：新增 **49 项**回归（通知 10、提醒 16、答案 23），全量 **455 项测试通过**；`manage.py check` 与 `makemigrations --check --dry-run` 通过。迁移快照测试使用迁移前后历史模型验证旧表结构，避免以当前模型查询旧版字段。

## 六、附录

### 6.1 权限码总表（22 个）

| 角色 | 权限码 | 中文名 |
|---|---|---|
| 教务 | class:manage / class:assign_teacher / course:manage / course:assign_class / user:assign_role | 班级管理 / 分配教师 / 课程管理 / 分配班级 / 指定组长 |
| 课程组 | knowledge:manage / material:class_visible / chapter:manage | AI Agent知识库 / 课程下班级资料 / 课程章节设计 |
| 教师 | notice:manage / analysis:view / material:class_manage / homework:publish / homework:bank / homework:remind / homework:comment / homework:answer / homework:ai | 班级通知 / 学情分析 / 班级内部资料 / 作业发布 / 个人题库 / 作业提醒 / 评语 / 答案 / Agent分析 |
| 学生 | homework:view / notice:view / material:view / analysis:view_self | 作业查看与提交 / 查看班级通知 / 查看班级资料 / 查看自己的学生画像 |
| 通用 | profile:edit | 修改个人资料 |

### 6.2 两级权限模型

- **功能级**：分组权限码（Group → Permission → permCode），回答"能不能用这个功能" → 无权限 **403**
- **对象级**：业务接口内校验，回答"能不能对这个对象用" → 无权访问 **404**（防探测）
  - 教师：`teachingClass.teachers` 含当前用户（1 跳）
  - 学生：身份 → 资料 → 当前成员关系 → 行政班 → 教学班（3 跳推导）

### 6.3 后续规划（随功能轮次补充）

- 教师通知 CRUD、阅读统计、站内作业提醒、参考答案、班级成绩看板、私有资料和提交批语已实现后端，前端待接入；AI 扩展（homework:ai）仍待后续。
- 课程组知识/文档、章节与共享资料管理、教师只读 API 已实现，见新增接口文档。
- 学生画像 API（analysis:view_self）待开发；个人资料 API（profile:edit）已实现。
- 所有教师业务 API 必须套 `getTeacherTeachingClass`、学生业务 API 套 `getStudentTeachingClass` 对象级校验

> 已落地（2026-10-20）：题库 API（公共题库 + 个人题库、筛选/分页）、作业组卷/学生作业/提交 API、管理员 ZIP 批量导入 API。

### 6.4 测试与质量保障

- 权限测试矩阵：`manage.py test users`（17 用例，四角色 × 端点边界）
- 教学业务测试矩阵：`manage.py test teaching`（168 用例：对象级校验、题库/作业/提交/判题/导入）
- 教务 API 权限矩阵：`manage.py test academics`（48 用例：8 个视图 × 入口权限码/动作级权限码/参数兜底）
- 异步任务与判题运维：`manage.py test tasks`（27 用例）
- 上述分模块数量为 2026-10-20 历史基线，当时全量 260 项通过；不代表当前测试规模。
- 2026-10-27 非学生端补齐阶段：全量 **667 项测试通过**，系统检查、迁移漂移检查通过；新增 138 项回归，覆盖基础/账号学籍、学期冻结、后台导入、课程组权限、原生后台审计、旧库升级及独立 SQLite 双连接并发。之后经单独授权完成本地业务库迁移，验收见 3.10。
- 2026-10-26 历史全量基线为 529 项，详见 3.8；教师通知/提醒/参考答案阶段的 455 项基线见 5.10。
- 权限一致性检查：`manage.py check`（定义四处与数据库自动比对）

### 6.5 相关审查技能

项目在 Claude Code 中配置了以下审查技能（`.claude/skills/`），接口设计、评审与联调时可直接调用：

| 技能 | 用途 | 适用场景 |
|---|---|---|
| dj-api-review | RESTful 规范、状态码、响应格式、分页/过滤/排序、版本管理审查 | 写完 API 设计后、前后端联调前 |
| dj-permcheck | 功能权限、数据权限、5 种越权漏洞模式审查 | 接口权限自查、权限评审 |
| dj-serializer | N+1 检测、分层 Serializer、字段级权限、读写分离 | 列表接口慢、Serializer 结构混乱时 |
| dj-orm-opt | 查询性能审查（N+1、索引、批量操作） | 列表页慢、写完复杂查询后 |

用法：在对话中说「用 dj-api-review 审查一下」并附上接口代码/设计即可。

# CodeMind 教辅平台 · OJ 判题对接设计

> 更新时间：2026-10-18（v4：主架构组件映射补充）
> 状态：判题沙箱连通验证完成 ✅；后端总体设计已定稿并经审查修订（见第二十章修订记录），代码随批次落地
> 配套文档：《异步任务设计.md》（django-q2 基础设施）、《API设计文档.md》（接口约定）、《用户权限设计.md》（23 权限码出处）

---

## 一、认知：go-judge 是什么

我们接入的 OJ 底层是 [go-judge](https://docs.goj.ac/cn/)（v1.13.3，Go 编写）——它是一个**判题沙箱服务，不是完整 OJ**：没有题目库、用户系统、提交记录，只负责"把代码放进隔离容器跑一遍，报告运行结果"。

因此分工明确：

| 层 | 谁负责 | 干什么 |
|---|---|---|
| OJ 业务层 | **codemind 自己** | 题目管理、作业提交、答案对错判定、得分聚合、结果归档 |
| 判题沙箱 | go-judge（云上） | 隔离运行代码，返回 status / 耗时 / 内存 / 输出 |

> 关键认知：**go-judge 只回答"程序跑得怎么样"，不回答"答案对不对"**。
> 对错判定在 codemind 业务层做——把沙箱返回的 stdout 与题目预期输出比对（或交给 spj 判定）。

## 二、部署实例与实测记录

- 地址：`https://oj.liuwenli.cn`（nginx 反代 → go-judge，云服务器）
- 历史记忆中的 `127.0.0.1:5050` 正是 go-judge 的默认监听端口（本地开发版），线上走 nginx
- 认证：**`Authorization: Bearer <token>`**（实测确认；`X-Auth-Token` 头无效）

2026-10-18 实测记录（全部通过）：

| 探测 | 请求 | 结果 |
|---|---|---|
| 版本信息 | `GET /version`（公开） | 200，v1.13.3 / go1.27.2 |
| 沙箱配置 | `GET /config`（公开） | 200，cgroup 配置、文件存储路径 |
| 最小判题 | `POST /run`：`echo hello-oj` | `Accepted`，stdout 正确返回 |
| 超时掐断 | `POST /run`：`sleep 10` + clockLimit=1s | `Time Limit Exceeded`，runTime=1.0015s（整秒掐断）✅ |
| 文件上传 | `POST /file`（multipart `file` 字段） | 返回 fileId `"7MEHVO76"` |
| 文件引用判题 | copyIn 用 PreparedFile | `Accepted`，正确读到文件内容 |
| 文件下载/删除 | `GET/DELETE /file/<fileId>` | 200 / 200 |
| gcc 编译链 | `POST /run`：gcc 14.2.0 编译 + `./main` 运行 | ✅ 全绿（镜像原缺 gcc/python3，容器内 apt 安装后复测通过） |
| python3 运行链 | `POST /run`：python3 3.13.5 + 标准库抽样 | ✅ 全绿 |

## 三、端点清单与认证

| 方法 | 路径 | 认证 | 用途 |
|---|---|---|---|
| GET | `/version` | 公开 | 沙箱版本探测（健康检查可用） |
| GET | `/config` | 公开 | 沙箱能力配置（**部署要求限制公网访问，见第十四章**） |
| POST | `/run` | Bearer | **判题**：一次请求同步跑完并返回结果 |
| POST | `/file` | Bearer | 上传文件（multipart，字段名 `file`），返回 fileId |
| GET | `/file/<fileId>` | Bearer | 下载文件内容 |
| DELETE | `/file/<fileId>` | Bearer | 删除文件（释放沙箱磁盘） |
| WS | `/ws` | Bearer | WebSocket 版判题（交互题用，远期） |

认证请求示例：

```
Authorization: Bearer <OJ_AUTH_TOKEN>
```

## 四、判题请求 / 响应格式

### 4.1 请求体 `POST /run`

```json
{
  "cmd": [{
    "args": ["/bin/sh", "-c", "echo hello-oj"],
    "env": ["PATH=/usr/bin:/bin"],
    "files": [
      {"content": ""},
      {"name": "stdout", "max": 1048576},
      {"name": "stderr", "max": 1048576}
    ],
    "cpuLimit": 1000000000,
    "clockLimit": 2000000000,
    "memoryLimit": 268435456,
    "stackLimit": 134217728,
    "procLimit": 32,
    "copyIn": {"main.py": {"content": "print(1)"}},
    "copyOut": ["output.txt"],
    "copyOutCached": ["main"],
    "copyOutMax": 1048576
  }]
}
```

| 字段 | 单位 | 说明 |
|---|---|---|
| `args` | — | 命令行（第 0 项是程序路径，随后是参数） |
| `env` | — | 环境变量数组，**必须带 PATH** |
| `files` | — | fd 0/1/2 的 stdin/stdout/stderr 定义；stdout/stderr 需给 `max` 上限 |
| `copyIn` | — | 注入文件：`{"content"}`（小文件直接内联）/ `{"fileId"}`（引用已上传/缓存文件） |
| `copyOut` | — | 需读取**内容**的输出文件清单（内容返回在 `files`） |
| `copyOutCached` | — | 需**缓存复用**的输出文件清单（返回 fileId，见 4.2）——编译产物必须用这个 |
| `copyOutMax` | byte | 需回传的输出文件总大小上限（**防止输出撑爆内存**） |
| `cpuLimit` | 纳秒 | CPU 时间限制 |
| `clockLimit` | 纳秒 | 真实等待时间限制（**TLE 的主要依据**） |
| `memoryLimit` / `stackLimit` | byte | 内存 / 栈限制 |
| `procLimit` | 个 | 进程/线程数上限（固定 32 即可） |

### 4.2 响应体（Result 数组，与 cmd 一一对应）

```json
[{
  "status": "Accepted",
  "exitStatus": 0,
  "time": 1826422,
  "memory": 2400256,
  "runTime": 27833769,
  "files": {"stderr": "", "stdout": "hello-oj\n"},
  "fileIds": {"main": "7MEHVO76"},
  "error": null,
  "fileError": []
}]
```

| 字段 | 单位 | 说明 |
|---|---|---|
| `status` | — | 运行状态（见第五节映射表，**原始字面量带空格**） |
| `exitStatus` | — | 程序退出码 |
| `time` / `memory` | 纳秒 / byte | CPU 时间 / 峰值内存 |
| `runTime` | 纳秒 | 实际运行时长（判 TLE 用） |
| `files` | — | `copyOut` 清单的内容（文本）；**二进制内容不可靠走这里** |
| `fileIds` | — | `copyOutCached` 清单的缓存 fileId——**编译产物从这里拿，后续用 PreparedFile 引用** |
| `fileError` | — | 文件错误细分类型（见第十五章，按类型决定是否重试） |

## 五、判题状态映射表（核心）

go-judge 的 `status` 为**原始带空格字面量**，共 9 种。**客户端集中一处映射**为 codemind 状态，**未知字面量一律按 system_error 兜底**（防沙箱升级新增状态污染业务数据）：

| go-judge status（原始值） | 含义 | codemind 提交状态 |
|---|---|---|
| `Accepted` | 正常结束（**未判对错**） | 进入 stdout 与预期输出比对（或 spj） |
| `Time Limit Exceeded` | 超 clockLimit | 超时 |
| `Memory Limit Exceeded` | 超 memoryLimit | 内存超限 |
| `Output Limit Exceeded` | 输出超上限 | 输出超限 |
| `Nonzero Exit Status` | 非 0 退出码 | 运行错误 |
| `Signalled` | 进程被信号终止（段错误等） | 运行错误 |
| `Dangerous Syscall` | 被 seccomp 拦截危险系统调用 | 运行错误 |
| `File Error` | 文件错误（细分见 `fileError`） | 按类型分：确定性错误 → 系统错误不重试；瞬时错误 → 重试 1 次 |
| `Internal Error` | 沙箱内部错误 | 系统错误（重试 1 次） |

答案比对策略（codemind 业务层，按题目配置三选一，定义精确化）：

| 策略 | 规则（精确定义） | 适用 |
|---|---|---|
| 精确比对 | stdout 与预期输出**逐字节相等**（不规范化任何内容） | 有固定输出的题 |
| 忽略行尾空白 | 逐行 **rstrip**（仅去行尾的 ASCII 空格与制表符，不动行首），末尾空行两侧都去掉后比对 | 输出末尾多空格容错 |
| 忽略全部空白 | 去掉 ASCII 空白与 Unicode 空白字符（  等空格类）、统一换行（\r\n→\n）后比对 | 格式宽松的题 |

## 六、后端总体设计决策总表（v2 审查修订后）

| # | 主题 | 结论 |
|---|---|---|
| 1 | 提交检测分层 | 同步（权限/输入/频率 10s·3次·429，准入与建表同事务）+ 异步（指纹比对最近 20 条，repeat 终态拦截不进判题，记录关联「重复于哪次提交」） |
| 2 | 任务机制 | django-q2 专用 long 集群 + **独立 JudgeTask 表**（租约状态机）；Q2 管「至少一次投递」，业务状态机管「至多一次生效」 |
| 3 | 判题执行 | 编译 1 次 `copyOutCached` 产物复用；**全测试点执行聚合部分分**（不再提前终止）；三档资源限制显式携带 |
| 4 | 数据模型 | Problem / Homework / Submission / JudgeTask 四张表 + Homework–Problem 中间表；题目快照（revision）随提交冻结；测试用例 DB JSON 永不下发 |
| 5 | 一期范围 | practice + homework（exam 留枚举位）；**SPJ 一期做**（Python + 部分分协议）；交互题/跨用户查重/考试模式二期 |
| 6 | 录题方式 | 教师端单 markdown 文件（题面+样例+测试点+分值+spj）；课程组公共题库 / 教师个人题库分级 |
| 7 | 权限码 | **全部复用现有 23 码**（users/models.py Meta.permissions，零新增） |
| 8 | 查询 API | 学生提交历史；教师教学班聚合 + 成绩导出 xlsx |
| 9 | 安全红线 | 测试点/预期输出/他人代码/SPJ 输出永不下发；`statement_md` 为剔除受保护代码块后重建的正文；日志禁记测试数据 |
| 10 | 资格语义 | **受理时间定资格**（提交时保存题目快照与授权快照）；仅删除/封禁等明确规则在判题时取消 |
| 11 | 部署契约 | go-judge 仅内网/限源可达；nginx 禁公网 /run、/file；/config 关闭或限源；验收强制验证 cgroup |

### 6.1 架构裁定

**裁定 1：异常提交检测在落库之后。** 提交 API 只做同步轻检（权限/输入/频率），通过后**立即创建最小提交记录**（Submission=queued + JudgeTask=pending）再投递；代码重复检测在 worker 内异步执行并更新状态（repeat 为终态但**保留记录**，关联「重复于哪次提交」）。理由：提交记录是审计与限流的依据，必须先于任何检测存在——「检测未过就不落库」会让限流统计失真、异常提交无痕可查。

**裁定 2：调度器的「资源检测/空闲并发查询」以应用侧为准。** go-judge REST API **不存在**「查询空闲并发」的业务接口，不得假设其存在。并发准入由应用侧实现：队列闸（用户未终态 JudgeTask ≤ 3）+ long 集群 workers 数；go-judge 侧固定 `-parallelism` 配置，其监控指标仅作观测与告警，不作为即时准入信号。

**裁定 3：django-q2 是实现候选，不是架构决定。** 判题主状态机（Submission queued→running→terminal + JudgeTask 租约 attempt/心跳/回收）是**架构要求**，独立于任何队列框架：q2 只承担「至少一次投递」，业务状态机承担「至多一次生效」。将来若迁移 Celery（只换投递与运行入口），状态机、租约、快照全部不变。

### 6.2 不可变边界（任何功能轮次不得违反）

1. **身份只来自认证上下文**：所有 OJ API 从会话认证取用户，不接收、忽略、拒绝请求体中的 userId 等身份字段——客户端不可指定「替谁提交」
2. **隐藏测试数据不经过前端与 API 响应**：隐藏测试点（tests）、预期输出、SPJ 源码与输出只存在于服务端判题链路；题目详情、提交详情、任何列表接口一律不含这些字段（statement_md 必须是剔除受保护代码块后重建的正文）
3. **结果详情脱敏**：学生可见的 error_detail 只允许固定白名单文案 + 截断清洗后的实际输出预览；日志/Sentry/任务参数禁止记录测试输入、预期输出、完整沙箱请求与认证头

### 6.3 组件职责边界

| 组件 | 拥有 | 不拥有 |
|---|---|---|
| 任务构建器（tasks_judge.py 构建函数） | 编译（copyOutCached 产物复用）、逐测试点 /run 调用、资源限制拼装、编译产物 fileId 的用后清理 | 对错判定、得分计算 |
| 结果分析器（比对与 spj 解释函数） | go-judge 状态字面量→codemind 状态映射、三策略答案比对、SPJ 退出码与分数协议解释、单点计分 | 任务状态机、队列投递、文件管理 |
| 判题任务主控（runJudgeTask） | 租约领取/释放、阶段编排、部分分聚合、终态收敛 | 以上两组的细节 |

### 6.4 主架构组件 ↔ 实现载体映射（对齐《OJ后端设计》组件链路）

本文是《OJ后端设计》主架构的**细化实现规范，不替代主架构**。
主链路「异常检测 → 调度器 → 任务构建器 → 结果分析器」不变，各组件实现如下：

| 主架构组件 | 实现载体 | 位置 |
|---|---|---|
| Django REST API | 提交 API（第七章）+ 查询 API（12.2） | teaching/api.py |
| 异常提交检测 | 同步频率检测（API 内）+ 异步指纹检测（任务阶段 2） | teaching/api.py + tasks/tasks_judge.py |
| 任务调度器 | django-q2 long 集群（投递队列）+ JudgeTask 租约状态机（并发准入/回收） | Q_CLUSTER + teaching/models.py |
| 任务构建器 | judge.py：编译（copyOutCached）/ 逐点 /run / 资源拼装 / fileId 清理 | teaching/judge.py |
| 结果分析器 | 状态字面量映射 + 三策略比对 + SPJ 解释 + 单点计分 | tasks/tasks_judge.py 分析函数 |
| go-judge + Sandbox | 云上沙箱服务（Bearer 认证，不改动）；沙箱并发 `-parallelism` 固定，仅观测 | oj.liuwenli.cn |
| 数据库 | 五张表（Problem/Homework/HomeworkProblem/Submission/JudgeTask），测试用例 JSON 永不下发 | teaching/models.py |

## 七、提交 API 设计（同步层）

### 7.1 校验清单（毫秒级，400/404/429 快速失败）

| 检查项 | 实现 | 拒绝时 |
|---|---|---|
| 已登录 | IsAuthenticated（**用户一律取自认证上下文，忽略/拒绝请求体 userId**） | 401 |
| 题目存在且对当前用户可见 | 作业题经 `getStudentTeachingClass` 反查教学班归属；练习题登录即可 | 404 |
| 提交权限码 | `homework:view` | 403 |
| 语言 ∈ 题目允许语言集 | 白名单枚举（c / python） | 400 |
| 代码非空且 ≤ 64KB | 防大报文进队列 | 400 |
| 提交类型 ∈ {practice, homework} | 白名单枚举（exam 留位二期） | 400 |
| homework 提交的组合合法性 | homework 非空时：题目必须属于该作业、作业开放中、学生属于该教学班（原子验证） | 400/404 |
| 提交频率 | 同用户近 10 秒 ≥ 3 次（`(user, created_at)` 索引 count 查询） | 429 |
| 队列闸 | 同用户未终态 JudgeTask 数 ≤ 3（超限 400，防积压滥用） | 400 |

**并发防绕过**：频率 count 与 Submission 创建必须在**同一事务**内（SQLite 单写者天然串行；切 PG 后加用户行 `select_for_update` 锁——迁移说明写入代码注释）。当前单校规模 + SQLite 下无需 Redis 滑动窗口。

### 7.2 受理流程（投递原子性）

```
事务内：
  ① 通过 7.1 全部校验
  ② 创建 Submission(status=queued, 含题目快照：problem_revision + tests_hash + 语言/档位/比对策略/分值快照)
  ③ 创建 JudgeTask(status=pending)
事务提交后：
  ④ transaction.on_commit() → async_task 投递 runJudgeTask(submissionId)
  ⑤ 200 {"submissionId": 12}
```

**补投扫描**（防「已建表但未入队」孤儿）：定时任务扫 `JudgeTask.status=pending` 且超过 60s 未投递（`q2_task_id` 为空或投递后超 retry 窗口未 leased）→ 重新投递；`q2_task_id` 记录最近一次投递标识，同一 attempt 只补投一次。

### 7.3 资格语义（受理时间定资格）

提交**受理时**即冻结资格：题目快照（revision/tests_hash）与授权快照随 Submission 落库，判题全程使用快照数据，**不重查实时题目**。排队到截止后才执行不取消——合法提交不被「作业过期」误伤。仅在判题时检查：题目被删除、用户被封禁/移除出教学班（明确规则），此时置 system_error「提交已失效」。

## 八、判题执行规格（租约状态机）

### 8.1 双表状态机（审查修订核心）

```
Submission:  queued → running → terminal（终态不可变）
JudgeTask:   pending → leased(attempt_id, heartbeat) → done | retryable | failed

terminal = accepted | wrong_answer | compile_error | time_limit_exceeded
         | memory_limit_exceeded | output_limit_exceeded | runtime_error
         | repeat | system_error
```

**租约规则**：

- **原子领取**：worker 从 `pending/retryable` 领任务必须原子（SQLite：事务内条件 UPDATE「状态改 leased + attempt_id+1 + claimed_at/heartbeat_at=now」并检查影响行数=1；切 PG 后改 `select_for_update`）——**只有持当前 attempt 的 worker 能写终态、更新进度、清理 fileId**（写操作一律带 attempt_id 条件）
- **心跳**：每跑完一个测试点更新 `heartbeat_at`（与 progress 同一次写库）
- **租约回收**：定时任务扫 `leased` 且心跳超 60s → 置 `retryable` + attempt_id+1 + 清 file_id（回收清理残留产物）→ 重新投递
- **retryable 孤儿兜底**：瞬时错误回退后依赖 q2 retry 重投，但重投链可能断（broker 消息丢失/重试用尽/进程被杀）→ 回收扫描同时兜 `retryable` 且 claimed_at 为空的任务：执行次数有余量 → 置回 `pending` 重新投递（状态迁移防重、claimLease 幂等防并发多投）；余量已尽 → `failed` + system_error 终态（不再悬挂，M8 验收 5.2 实测）
- **Q2 重试防并发重判**：long 集群 `retry > 最长任务执行时间`（见 8.5 预算），且任务入口的幂等闸按 attempt 判定——过期 attempt 的执行结果直接丢弃

### 8.2 runJudgeTask 流程（模块级纯函数，参数只传 submissionId）

```python
def runJudgeTask(submissionId):
    """判题任务：worker 进程内执行，不依赖 request

    执行顺序：租约领取 → 资格检查 → 重复检测 → 编译 → 全测试点判题（聚合部分分）→ 结果落库。
    所有写操作带 attempt_id 条件；任何路径都收敛到终态或 retryable。
    """
    # 阶段 0：租约领取——原子 UPDATE JudgeTask 到 leased(attempt_id)；
    #         影响行数≠1 则直接返回（其他 worker 已领走/已终态）
    # 阶段 1：资格检查——仅查「题目已删除/用户封禁/移除教学班」→ system_error
    #         （用 Submission 上的快照判题，不重查实时题目）
    # 阶段 2：重复检测——fingerprint 比对最近 20 条，命中 → repeat
    #         （error_detail 记录「重复于提交 #xx」）
    # 阶段 3：编译（仅编译型语言，copyOutCached 产物）——失败 → compile_error
    # 阶段 4：全测试点循环——每个点独立判定与计分，跑完所有点聚合 score；
    #         心跳 + progress 每点更新；沙箱级错误（File Error 瞬时/Internal Error）
    #         抛异常 → q2 重试；重试耗尽 → retryable 兜底终态
    # finally：fileId 清理（带 attempt_id 条件）；写终态（带 attempt_id 条件）
```

### 8.3 编译产物复用（copyOutCached）

C 语言**不能每个测试点都编译**：编译 1 次 → 请求带 `copyOutCached: ["main"]` → 响应 `fileIds["main"]` 直接拿缓存 fileId → 后续测试点 `copyIn` 用 `{"fileId"}` 引用 → 判完 `DELETE /file/<fileId>`。

**不使用**「普通 copyOut + 上传 files 内容」路径——二进制经 `files` 字符串返回有编码风险。

Python 无编译阶段，每测试点 `copyIn` 内联代码原文。

### 8.4 单测试点请求形态

```
args  = ["./main"] / ["python3", "main.py"]
copyIn = {"main": {"fileId": 产物}}（C）/ {"main.py": {"content": 代码}}（Python）
files  = [{"content": 该测试点输入},          ← fd0 注入 stdin
          {"name": "stdout", "max": 8MB},
          {"name": "stderr", "max": 8MB}]
资源限制：按题目档位显式携带全部 limit 字段（不依赖沙箱默认值）
HTTP 超时：OJ_RUN_TIMEOUT(30s)，必须 > clockLimit 上限 10s
```

### 8.5 部分分聚合（一期支持）

- **每个测试点独立判定、独立计分**：AC 得该点 `points` 分；WA/TLE/MLE/OLE/RE 该点 0 分；**跑完所有测试点**（不再提前终止）
- 全局终态例外：编译失败 → `compile_error`（score=0）；沙箱级错误重试耗尽 → `system_error`（score=0）
- 聚合规则：`score = Σ 通过测试点分值`；`total_score = Σ 全部测试点分值`
- 主状态取「最差单点结果」：全部 AC → `accepted`；存在非 AC 点 → 按严重度取
  `runtime_error > time_limit_exceeded > memory_limit_exceeded > output_limit_exceeded > wrong_answer`（如某点 TLE、其余 AC → 主状态 `time_limit_exceeded`，score 照记）
- 前端展示：`{status, score, totalScore, progress}`——「超时 · 60/100」语义清晰

### 8.6 Q2 long 集群预算

判题任务投递**专用 long 队列**（独立 qcluster 进程消费），不与导入任务争抢：

| 参数 | 值 | 依据 |
|---|---|---|
| timeout | 900s | 最坏预算：20 点 × (运行 10s + spj 10s) + 编译 10s ≈ 430s，留 2 倍余量 |
| retry | 1000s | **必须 > 最长任务执行时间**（含排队），否则任务未结束即被重投造成并发重判 |
| max_attempts | 2 | 与重试语义对齐；入口 attempt 幂等闸兜底 |
| workers | 2 | 每 worker 同时判 1 个提交；go-judge 侧 -parallelism 固定 |
| queue_limit | 50 | 队列积压上限 |

> 数字按预算推算，部署压测后校准。go-judge 无「查询空闲并发」业务接口——并发准入由应用侧（队列闸 + workers 数）控制，go-judge 监控指标仅作观测。

## 九、数据模型（四张表 + 中间表）

```python
class Problem(models.Model):
    """题目：题面 markdown + 测试用例 JSON + 可选 spj（题库两级归属见第十二章）"""
    title = models.CharField('标题', max_length=100)
    statement_md = models.TextField('题面 markdown（已剔除受保护代码块后重建）')
    samples = models.JSONField('公开样例', default=list)      # [{input, output}]，可下发
    tests = models.JSONField('隐藏测试点', default=list)      # [{input, output, points}]，永不下发
    languages = models.JSONField('允许语言', default=list)    # ['c', 'python']
    tier = models.CharField('资源档位', choices=入门/基础/进阶)
    compare_strategy = models.CharField('比对策略', choices=exact/trailing/loose)
    has_spj = models.BooleanField('特殊判定', default=False)
    spj_code = models.TextField('spj 源码（Python）', blank=True)
    revision = models.PositiveIntegerField('修订号', default=1)  # 每次修改题目 +1
    owner = models.ForeignKey(User, SET_NULL, null=True)       # 归属：教师个人 / 课程组公共；删号题目保留
    is_public = models.BooleanField('公共题库', default=False)
    created_at / updated_at


class Homework(models.Model):
    """作业：教学班 × 题目集合 + 起止时间（practice 提交无此上下文）"""
    teaching_class = models.ForeignKey(TeachingClass, PROTECT)  # 有作业的教学班不可删除
    title = models.CharField('作业标题', max_length=100)
    start_at / deadline = models.DateTimeField('开始/截止时间')
    created_by = models.ForeignKey(User, SET_NULL, null=True)   # 发布教师（删号作业保留）


class HomeworkProblem(models.Model):
    """作业-题目中间表：承载题目顺序与作业期版本快照"""
    homework = models.ForeignKey(Homework, ...)
    problem = models.ForeignKey(Problem, PROTECT)              # 被作业引用的题目不可删除
    order = models.PositiveSmallIntegerField('题序', default=0)
    problem_revision = models.PositiveIntegerField('选题时的题目修订号')
    # 唯一约束 (homework, problem)；作业内题序唯一 (homework, order)


class Submission(models.Model):
    """提交：三种类型合一（practice/homework，exam 留枚举位二期）"""
    user = models.ForeignKey(User, related_name='submissions')
    problem = models.ForeignKey(Problem, SET_NULL, null=True, related_name='submissions')
                              # 允许删题；判题时题目缺失置 system_error（7.3）
    homework = models.ForeignKey(Homework, PROTECT, null=True)  # 练习提交为空；有提交的作业不可删
    language = models.CharField('语言', choices=c/python)
    code = models.TextField('代码原文')                        # 重复检测 + 教师批改要看
    fingerprint = models.CharField('统一换行后原文 SHA256', max_length=64)
    submit_type = models.CharField('提交类型', choices=practice/homework)
    status = models.CharField('状态', choices=queued/running/accepted/wrong_answer/
                               compile_error/time_limit_exceeded/memory_limit_exceeded/
                               output_limit_exceeded/runtime_error/repeat/system_error)
    score = models.PositiveIntegerField('得分', default=0)
    total_score = models.PositiveIntegerField('题目总分', default=0)
    time_used = models.BigIntegerField('最大单点耗时（纳秒）', null=True)   # 学情分析原料
    memory_used = models.BigIntegerField('最大单点内存（byte）', null=True)
    error_detail = models.TextField('错误详情（脱敏）', blank=True)
    repeat_of = models.ForeignKey('self', null=True)           # repeat 关联「重复于哪次提交」
    # ---- 资格快照（受理时冻结，判题用它而非实时题目）----
    problem_revision = models.PositiveIntegerField('题目修订号快照')
    tests_hash = models.CharField('测试数据哈希', max_length=64)  # 不可复现时审计用
    judge_snapshot = models.JSONField('判题数据快照')  # {tests, spj_code, tier, compare_strategy}
                              # 判题任务零实时查题（7.3 必然推论，v5 补列）：
                              # 教师改题不改变已受理提交的判题标准，结果可复现
    created_at / judged_at
    # 索引：(user, created_at) 频率检测；(problem, user) 重复检测窗口查询
    # Check 约束：homework 非空时 submit_type 必须为 homework（DB 层防非法组合）


class JudgeTask(models.Model):
    """判题执行表（独立于通用 AsyncTask：租约字段为判题独有）

    业务名避免与 django-q2 内部 AsyncTask 类同名；前端轮询的唯一来源
    是 Submission 详情接口（progress 从本表读）。
    """
    STATUS_PENDING = 'pending'      # 待投递/已投递未领取
    STATUS_LEASED = 'leased'        # 已领取（attempt_id 标识当前租约）
    STATUS_DONE = 'done'            # 终态
    STATUS_RETRYABLE = 'retryable'  # 租约回收，可重新投递
    STATUS_FAILED = 'failed'        # 重试耗尽

    submission = models.OneToOneField(Submission, related_name='judge_task')
    status = models.CharField('状态', choices=五种)
    attempt_id = models.PositiveIntegerField('尝试号', default=0)
    q2_task_id = models.CharField('q2 投递标识', max_length=64, blank=True)
    file_id = models.CharField('编译产物 fileId', max_length=32, blank=True)  # 回收清理用
    progress = models.PositiveSmallIntegerField('进度 0-100', default=0)
    error = models.TextField('执行错误（脱敏）', blank=True)
    claimed_at / heartbeat_at / started_at / finished_at
```

要点：题目修改即 `revision+1`，提交冻结 `problem_revision` 与 `tests_hash` 快照——**排队提交不会用到新测试数据，教师改题不改变已受理提交的判题标准，结果可复现**。

## 十、Markdown 录题格式（单文件承载整道题）

教师/课程组上传一个 `.md` 文件，后端解析出题面、样例、测试点（含分值）、spj。**约定代码块标签**：

````markdown
# 两数之和

## 题目描述
输入两个整数 a 和 b，输出它们的和。
（题面 markdown 正文，含 ## 小节、列表、行内代码等）

## 样例
```input-sample
1 2
```
```output-sample
3
```

（以下代码块是隐藏测试点，学生不可见；points= 标注分值）
```input points=30
100 200
```
```output
300
```

```input points=70
-5 5
```
```output
0
```

（spj 题加这段，普通题不放）
```spj-python
import sys
def main():
    with open(sys.argv[2]) as f:
        user_out = f.read().strip()
    with open(sys.argv[3]) as f:
        expect = f.read().strip()
    sys.exit(0 if user_out == expect else 1)
main()
```
````

**解析规则**（后端状态机；上传者是可信教师但仍按脏数据校验）：

- `input-sample/output-sample` 成对 → 公开样例（题目详情接口可下发）
- `input/output` 成对 → 隐藏测试点（进 `tests` JSON，永不下发）；input 为空串即「无输入」
- **分值**：`input` 代码块信息字符串可带 `points=N`（1 ≤ N ≤ 100）；未标注的点均分 100 分；`total_score = Σ points`
- 约束：必须成对（不配对 → 400 拒绝整题）、上限 20 组、单组 ≤ 256KB、至少 1 组测试点
- `spj-python` 代码块 → 存 `spj_code` + 置 `has_spj`，入库前 `ast.parse` 语法校验
- **`statement_md` 必须由「剔除全部 input/output/input-sample/output-sample/spj-python 代码块后的正文」重建**——不允许把原始文件原文落库再下发（受保护块会随题面泄漏）

## 十一、SPJ 设计（一期，Python + 部分分协议）

**定位**：三策略比对覆盖不了的题（多解输出、浮点误差、部分分）。spj 本身是一段程序，跑在沙箱里判定。

**判题流程**（spj 题每测试点多一次沙箱调用）：

```
跑用户程序（正常判题链路）→ 拿用户输出
  ▼
POST /run 跑 spj：
  argv  = [checker.py, input文件, user_out文件, expected文件]  ← 经典三参数协议
  copyIn = 三个文件 + spj 源码（content 内联）
```

**部分分协议**：

- `exitStatus = 0` → 该测试点满分（points）
- `exitStatus ≠ 0` → stdout 首行 strip 后若是 `[0, points)` 的整数 n → 该点得 n 分；否则 0 分
- spj 自身沙箱错误（**除 Accepted/Nonzero Exit Status 外的状态**：TLE/MLE/OLE/Signalled/Dangerous Syscall/File Error/Internal Error）→ 重试 1 次 → 仍失败该提交置 `system_error`（**checker 失败 ≠ 用户 WA**，不得把 checker 崩溃判成学生答错）；`Nonzero Exit Status` 是部分分协议的正常信号，不是故障（v9 修订修正 v6 口径）

**安全边界**（checker 是受信任教师代码，但仍按不可信工作负载对待）：

- spj 运行资源用**独立更小档**：cpuLimit 2s / clockLimit 5s / memoryLimit 256MB / stdout 1MB——比用户程序档更紧
- 网络默认隔离（go-judge 提供，不额外放行）
- **spj 的 stdout 默认不回显**：学生可见详情只给固定白名单文案（如「第 3 个测试点未通过」），checker 输出（可能含 expected output / 测试输入）一律不入 error_detail、不入日志
- 一期 spj 限 Python（免编译）；C 版 spj 二期（每次判题先编译）

## 十二、题库权限分级与查询 API

### 12.1 权限分级（全部复用现有 23 码，出处 users/models.py Meta.permissions）

| 角色 | 权限码 | 能力 |
|---|---|---|
| 课程组 | `question_bank:manage` | **公共题库** CRUD（is_public=True），全平台教师可用 |
| 教师 | `homework:bank` | **个人题库** CRUD（is_public=False），仅自己执教的班级可用 |
| 教师 | `homework:publish` | 从公共 + 个人题库选题 → 组 Homework 挂教学班 |
| 教师 | `analysis:view` | 教学班提交聚合与成绩导出 |
| 学生 | `homework:view` | 提交与查看，无任何写权限 |

### 12.2 查询 API 形态

- 学生：`GET /api/submissions/`（自己的提交历史，按题/作业过滤分页）；`GET /api/submissions/<id>/`（详情含 `{status, progress, score, totalScore}`，对象级：非本人 404，超管放行）
- 教师：`GET /api/teacher/teaching-classes/<id>/submissions/`（教学班提交聚合：谁没交、通过率、成绩导出 xlsx——`analysis:view` + `getTeacherTeachingClass` 对象级校验）
- 代码原文访问：学生仅自己的 code；教师仅自己教学班学生的 code（对象级 404 语义）
- 学生可见字段白名单：code 原文、自己的 score/time_used 等本人字段；**不含**测试点数据、预期输出、spj 输出、他人提交

### 12.3 录题端点与归属（M6 落地）

- 建题端点决定归属，请求体 isPublic 一律无效：`POST /api/teacher/problems/`（个人题，homework:bank）、`POST /api/teacher/public-problems/`（公共题，question_bank:manage）
- 教师题库列表 = 公共题库 + 本人个人题（分页 20）；`GET/PUT/DELETE /api/teacher/problems/<id>/` 由服务层对象级判 403/404（教师与课程组权限码不同）
- 学生题面：`GET /api/problems/<id>/`（homework:view），不按 is_public 过滤（作业里引用的个人题学生要能读）
- 编辑 = 重传完整 md 全文替换 + revision+1；tests/spj_code 永不下发（教师手里有原始 md 可核对）

### 12.4 作业组卷 API（M7 落地）

**语义（2026-10-18 定稿）**：题库两级——课程组公共题库面向整个课程（跨教学班复用）、教师题库面向教学班级（自建自用）；选题可见集合 = 公共题 + 本人个人题，他人个人题一律 404（防探测）。

| 端点 | 方法 | 权限 | 说明 |
|---|---|---|---|
| `/api/teacher/teaching-classes/<id>/homeworks/` | GET/POST | homework:publish + getTeacherTeachingClass | 该班作业列表（题数/提交数聚合）；创建作业 |
| `/api/teacher/homeworks/<id>/` | GET/PUT/DELETE | homework:publish + getManageableHomework | 详情（题序 + 修订号快照）；编辑；删除 |
| `/api/homeworks/` | GET | homework:view | 我的作业列表（三态：未开始/进行中/已截止 + 我的提交概况） |
| `/api/homeworks/<id>/` | GET | homework:view + 3 跳对象级 | 详情（题目学生白名单 + 我的提交摘要）；开始前 404 防提前看题 |

**创建校验**：标题非空 ≤100；开始时间 < 截止时间（ISO 时间，naive 按本机时区补齐）；problemIds 非空、去重、≤20（M7 决策 a）；题目存在且对教师可见；order = 数组序；`problem_revision` 冻结选题时修订号（中间表快照，改题不影响已组作业）。

**编辑语义（PUT 全量）**：
- 已有提交 → 题目列表锁定（改题 400「已有提交，题目不可变更」），只允许改标题/时间（M7 决策 c）
- 无提交 → 题目列表整体替换，revision 重新快照（先腾位再写，避开 (homework, order) 唯一约束）
- 截止时间只允许向后延长（缩短 400，防误伤已规划学生，M7 决策 f）

**删除语义**：无提交可删（连带中间表）；有提交 400（DB 层 Submission.homework PROTECT 兜底，M7 决策 d）。

**学生侧**：列表三态由服务端按当前时间派生；详情开始前 404（防提前看题，列表仍显示「未开始」徽章）；题目列表复用学生题面白名单（不含 tests/spj_code/判题配置）；我的提交摘要仅最近 10 条（页面轮询判题进度）。

## 十三、资源限制策略（分档）

按题目难度分档（题目的时间/内存限制取档位上限以内，服务端写死下限兜底）：

| 档位 | cpuLimit | clockLimit | memoryLimit | copyOutMax | 适用 |
|---|---|---|---|---|---|
| 入门 | 2s | 5s | 256 MB | 16 MB | 语法练习、填空 |
| 基础 | 2s | 5s | 256 MB | 16 MB | 常规作业题 |
| 进阶 | 5s | 10s | 512 MB | 32 MB | 算法题 |
| **SPJ 独立档** | 2s | 5s | 256 MB | 1 MB | checker 运行（比用户档更紧） |

固定项：`procLimit=32`、stdout/stderr 各 `max=8MB`、`copyOutMax` 按档位（防输出撑爆 worker 内存）。
**每次请求显式携带全部 limit 字段**——不依赖沙箱默认值，防止服务端默认配置变更影响判题公平。
编译档独立：`clockLimit=10s`、`copyOutMax=32MB`（编译比运行慢）。

**总量压测项**（部署验收）：单请求实际内存 ≈ 程序 memoryLimit + copyOut + 并发缓存文件，需按 go-judge `-parallelism` × q2 workers × 用户上限联合压测后固化部署参数。

## 十四、安全设计

1. **凭证不入仓库**：token 只从环境变量 `OJ_AUTH_TOKEN` 读取（settings.py 已配置），开发/生产各自注入；泄露后立即轮换
2. **沙箱侧**（go-judge 已提供）：cgroup 资源隔离、seccomp 拦截危险系统调用、网络默认隔离
3. **注入方式**：`copyIn` 只用 `content`（代码文本内联）或 `fileId`（`copyOutCached` 产物），**禁用 `LocalFile`（src 绝对路径）**——避免意外暴露宿主机文件
4. **输出防护**：`copyOutMax` + stdout/stderr `max` 双重限流；结果入库前校验结构（status 必须属于 9 种原始字面量枚举、stdout 必须存在）；学生可见的 stdout/stderr 做**截断 + 控制字符清洗**
5. **文件清理**：编译产物 fileId 判后即 `DELETE`；租约回收时按 `file_id` 字段清理残留（可审计）
6. **重试上限**：单提交最多 2 次（max_attempts=2 + attempt 幂等闸），防恶意提交放大判题负载
7. **测试数据保密链**：测试点存 DB 永不下发；`statement_md` 为剔除受保护块后重建；WA 详情只给「第 k 点」+ 实际输出截断预览（不给预期输出）；spj 输出不回显
8. **日志红线**：日志禁止记录测试输入、预期输出、完整沙箱请求与认证头；Sentry/异常采集同样脱敏
9. **代码访问分级**：学生仅本人代码、教师仅本教学班学生代码（对象级 404，与全项目语义一致）
10. **提交限流双层闸**：频率闸（10s·3次·429）+ 队列闸（未终态 JudgeTask ≤ 3），准入与建表同事务防并发绕过
11. **部署契约（go-judge 暴露面）**：
    - go-judge 仅接受应用服务器内网/私网来源：nginx 限源 IP 白名单，**禁止公网访问 /run、/file、/ws、/stream**
    - `/config` 关闭公网访问或仅运维网段可达（减少能力探测面）
    - token 只存在于服务端 secret（环境变量），不落任何客户端
    - 部署验收强制验证 cgroup 隔离可用，**失败即拒绝启动**，不降级运行

## 十五、异常处理（含 FileError 细分）

| 异常 | 处理 |
|---|---|
| 网络错误 / 5xx / 超时 | 重试 1 次 → 仍失败标记「系统错误」，不判为答错 |
| HTTP 401 | token 失效，**不重试**，任务标记失败并告警（提示运维轮换 token） |
| HTTP 400 | 请求体构造错误——代码 bug，记日志排查，不重试 |
| `Internal Error` | 重试 1 次 → 仍失败标记「系统错误」 |
| `File Error`（瞬时类：磁盘/系统繁忙等） | 重试 1 次 → 仍失败标记「系统错误」 |
| `File Error`（确定性：产物缺失/超复制上限/文件名错误等，按 `fileError` 细分类型判定） | **不重试**，直接「系统错误」+ 记日志排查（重试无意义） |
| 响应结构非法 / status 未知字面量 | 按系统错误处理（防沙箱异常响应污染业务数据） |
| spj 崩溃（沙箱级错误） | 重试 1 次 → 仍失败置 system_error（≠ 用户 WA） |

## 十六、配置说明

`codemind/settings.py` 已新增（2026-10-18）：

```python
OJ_BASE_URL = os.environ.get('OJ_BASE_URL', 'https://oj.liuwenli.cn')
OJ_AUTH_TOKEN = os.environ.get('OJ_AUTH_TOKEN', '')   # Bearer 凭证，只走环境变量
OJ_RUN_TIMEOUT = 30   # 单次判题 HTTP 超时（秒）
```

环境变量注入方式：
- **Windows 开发**：`setx OJ_AUTH_TOKEN "..."` 后重启终端
- **生产（Linux）**：systemd 服务文件 `Environment=OJ_AUTH_TOKEN=...`，与《异步任务设计.md》部署章节同文件管理

**编译环境部署契约**（受版本控制的配置项，补齐实测盲区）：目前仅实测 `/bin/sh`——需验证并冻结 gcc / python3 及版本、标准库清单、绝对命令路径、编译参数，写入仓库（如 `deploy/judge-env.md`），沙箱镜像变更必须走该文件评审。

## 十七、测试矩阵

| 层 | 用例 |
|---|---|
| 提交 API | 越权矩阵（未登录/非学生/题目不可见 404/频率 429/队列闸 400）；受理返回 submissionId；忽略请求体 userId |
| 任务函数单测 | 直接同步调用 runJudgeTask（不走队列），mock 判题客户端 HTTP 层 |
| 判定矩阵 | 全 AC；部分点 WA/TLE → **断言全部测试点都执行完**（mock /run 调用次数 = 测试点数+编译）且 score 聚合正确；CE；repeat → **断言零 HTTP 调用** |
| 比对器 | 三策略各一组用例（含行尾空格、全空白差异、Unicode 空白） |
| **并发租约** | 双 worker 并发投递同一 submissionId → **断言只有一个 worker 获得 lease、沙箱只执行一次** |
| **Q2 重试窗口** | retry 小于/大于最长任务时间 → 验证不会并发重判（过期 attempt 结果被丢弃） |
| **快照规则** | 提交后修改题目/作业过期/撤销权限 → 断言采用受理时快照判定；题目删除/用户封禁 → system_error |
| **保密回归** | markdown 含隐藏块/SPJ 块/嵌套代码块 → 断言题目详情与日志均不含秘密数据；SPJ 输出 expected/test input → 断言不回显、归类正确 |
| 幂等 | Submission 已终态再跑 → 直接返回、状态不变、零 HTTP |
| 资源 | 产物 fileId 判后 DELETE 被调用；租约回收清理残留 fileId；progress 单调不减 |
| 异常 | mock 网络错 → 重试 1 次 → system_error；401 → 不重试；FileError 确定性类型 → 不重试 |
| markdown 解析 | 正常题/spj 题/样例题/points 标注；不配对 400；超 20 组 400；spj 语法坏 400 |
| 投递原子性 | 模拟「建表成功但未入队」→ 补投扫描重新投递且同一 attempt 只补投一次 |
| 客户端重试 POST | 一期无幂等键：重复 POST 产生独立提交（写入 API 契约，前端防重） |

## 十八、落地批次规划

| 批次 | 内容 | 依赖 |
|---|---|---|
| ① tasks 地基 | 装 django-q2、AsyncTask 模型、Q_CLUSTER（含判题 long 集群预算） | 无 |
| ② 判题客户端 | `teaching/judge.py`：submitRun/uploadFile/deleteFile + 状态字面量集中映射 | go-judge 云实例（已通） |
| ③ 模型 | Problem / Homework / HomeworkProblem / Submission / JudgeTask 五表 + 迁移 | ① |
| ④ 提交 API | 同步层（校验/限流事务/受理快照/投递 on_commit）+ 提交查询 API | ③ |
| ⑤ 任务函数 | tasks_judge.py（租约状态机 + copyOutCached + 部分分聚合）+ 比对器 + spj | ①②③ |
| ⑥ 收尾 | markdown 解析器 + 录题 API、租约回收与补投定时任务、测试矩阵、部署契约（judge-env.md + nginx 限源） | ⑤ |

## 十九、待办（随批次实现）

- [x] `teaching/judge.py` 判题客户端封装（submitRun / uploadFile / deleteFile / 状态映射）
- [ ] 沙箱镜像语言实测并冻结部署契约：python / gcc 版本、标准库、绝对路径（实测脚本已就绪，实测输出待回填 judge-env.md）
- [x] 答案比对器（三策略精确定义版）
- [x] markdown 解析器（代码块 → 题目数据 + statement_md 重建 + points）
- [x] JudgeTask 租约状态机 + 补投/回收定时任务
- [ ] nginx 限源与 /config 收敛（部署侧；模板已就绪，实例侧待执行）
- [ ] 跨用户抄袭检测（二期）
- [ ] exam 考试模式（二期）：限时锁定、交卷封存
- [ ] 交互题 `/ws` 支持（远期）
- [ ] C 版 spj（二期，每次判题先编译）
- [ ] 语义指纹（二期，按语言归一化，仅提示不阻断）

## 二十、审查修订记录（2026-10-18 v2）

v1 经外部设计审查（P0×5 / P1×14 / P2×8），本版修订：

- **P0 全部修正**：编译产物改 `copyOutCached`（废弃 files 兜底路径）；状态字面量改原始带空格值 + 未知兜底；幂等闸升级为 attempt 租约状态机（防并发重复执行）；`statement_md` 改为剔除受保护块后重建 + SPJ 输出不回显；新增 go-judge 部署契约（限源/关 /config/cgroup 验收）
- **P1 修正**：删除「空闲并发查询」承诺；API 忽略请求体 userId；受理时间定资格 + 题目快照（revision + tests_hash）冻结；指纹改统一换行后原文哈希（防误伤缩进语义）；**部分分一期支持**（全点执行 + score/total_score + SPJ 分数协议，主状态取最差单点）；投递 `transaction.on_commit` + 补投扫描；限流准入与建表同事务；判题独立 JudgeTask 表 + Q2 long 集群预算（retry > 最长任务）；租约回收替代固定 10 分钟扫描；SPJ 独立资源档 + checker 失败 ≠ 用户 WA；Homework–Problem 中间表 + DB 约束；WA 可见输出截断清洗 + 日志红线
- **P2 修正**：比较器三策略精确定义；FileError 按细分类型决定重试；资源总量压测项；API 契约补全（学生可见字段白名单/终态不可变/无幂等键说明）；编译环境部署契约（judge-env.md）
- **未采纳**：「权限码证据不足」——23 权限码已核验于 users/models.py Meta.permissions 与《用户权限设计.md》（审查时未读取该文档）

### v3（2026-10-18，第二轮意见）

- 新增 6.1 架构裁定：检测在落库后（先最小记录后异步检测）；调度器并发准入以应用侧实现（go-judge 无「空闲并发查询」接口）；django-q2 为实现候选、主状态机为架构要求
- 新增 6.2 不可变边界三条：身份只来自认证上下文；隐藏测试数据不经过前端与 API 响应；结果详情脱敏
- 新增 6.3 组件职责边界：Builder（编译/逐点调用/产物复用/清理）与 Analyzer（状态映射/比对/SPJ 解释）显式分工

### v4（2026-10-18，第三轮意见）

- 新增 6.4 主架构组件 ↔ 实现载体映射表：声明本文是《OJ后端设计》的细化实现规范，主链路「异常检测 → 调度器 → 任务构建器 → 结果分析器」不变，逐组件标注实现载体与代码位置（本轮其余 P0/P1/P2 意见已由 v2 修订覆盖，审查基线为 v1）

### v5（2026-10-18，M3 落地修订）

- 第九章补 `judge_snapshot` 字段（{tests, spj_code, tier, compare_strategy} 完整副本）：7.3「判题全程使用快照数据，不重查实时题目」的必然推论，草案漏列——判题任务零实时查题，教师改题不改变已受理提交的判题标准
- 补全草案未写明的外键策略：Problem.owner SET_NULL（删号题目保留）、Homework.teaching_class PROTECT（有作业删不掉教学班）、Homework.created_by SET_NULL、Submission.problem SET_NULL（配合 7.3「题目被删除→system_error」）、Submission.homework PROTECT、HomeworkProblem.problem PROTECT（被作业引用的题不可删）

### v6（2026-10-18，M5 判题主控落地修订）

- **租约回收落地方式**：django-q2 的定时任务以 DB 的 Schedule 记录承载（Q_CLUSTER 配置里的 schedule 键在 v2 已移除），由 qcluster long 集群调度器每分钟扫描 next_run 投递（**排程的 cluster 字段必须为 long，否则永远不会被消费**——M8 验收 5.1 实测：0002 曾注册为 cluster='' 导致回收从未运行、任务悬挂）。落地为 `tasks/migrations/0002_create_recycle_schedule.py` 数据迁移（migrate 幂等创建，name=judge-lease-recycle，每分钟无限重复）+ `0003_fix_recycle_schedule_cluster.py`（修正已部署库）——`recycleStaleLeases` 除 8.1 的「leased 心跳超 60s 回收」外，附带「pending 超 120s 补投」扫描（受理 on_commit 投递失败兜底）与「retryable 孤儿兜底」扫描（q2 重投链断裂救活，余量用尽转 failed）
- **编译命令冻结**：`gcc main.c -o main -O2 -std=c11 -lm`（对应 copyOutCached ["main"] 与 args ["./main"]）；Python 直接 `python3 main.py` 内联。gcc/python3 具体版本与标准库清单仍属部署契约（deploy/judge-env.md 待实测后冻结）
- **沙箱错误重试语义精确化**：用户程序/编译遇 File Error 或 Internal Error（客户端已重试 1 次）→ 任务层转瞬时异常 → 租约回退 retryable（清 file_id + 清理产物）→ 抛给 q2 按 retry=1000s 重投；checker 故障（spj 沙箱级错误）→ 任务内重试 1 次 → 仍失败置 system_error（≠ 用户 WA，不消耗 q2 重试额度）
- **attempt 执行次数公式**：8.1 回收与领取两处均 attempt_id+1（回收 +1 作废令牌、领取 +1 建立新租约），执行次数 = (attempt_id + 1) // 2；claimLease 后该值 > max_attempts(2) → 直接 failed + system_error（重试上限闸集中在任务入口一处）
- **spj 判题形态补全**：argv = ["python3", "checker.py", "input.txt", "user_out.txt", "expected.txt"]（Python 的 sys.argv 视角与 11 章示例 sys.argv[2]=user_out 一致）；checker 非 Accepted 一律按故障处理（含 TLE/MLE/RE，防 checker 自身问题冤枉学生）

### v7（2026-10-18，M6 markdown 录题落地修订）

- **解析器实现口径**（`teaching/problem_markdown.py`）：逐行状态机提取受保护块（input-sample/output-sample/input/output/spj-python），普通代码块原样保留进正文；未闭合围栏 400；points 仅接受 1~100 整数，未标注点平分 100（余数补给前几个未标注点，标注点保持标注值——教师自行保证总和语义）；标题取第一个一级标题行并在重建正文时剔除；md 总上限 6MB、隐藏测试点/样例各上限 20 组、单组数据 256KB；spj 入库前 ast.parse 语法校验
- **题库两级归属 API 落地**（12.1/12.3）：教师列表 = 公共题库 + 本人个人题（分页 20，按 updated_at 倒序）；建题端点决定归属——`POST /api/teacher/problems/` 个人题（homework:bank）、`POST /api/teacher/public-problems/` 公共题（question_bank:manage），请求体 isPublic 一律无效；`GET/PUT/DELETE /api/teacher/problems/<id>/` 不设统一 permCode，由服务层 `getManageableProblem` 对象级判 403（无任何题库权限）/404（无权限或非本人个人题，防探测）
- **学生题面端点**（12.2）：`GET /api/problems/<id>/`（homework:view），不按 is_public 过滤——作业里引用的教师个人题学生要能读；白名单仅 title/statementMd/samples/languages，不含 tests/spj_code/tier/compareStrategy
- **编辑语义冻结**（决策 d）：编辑 = 重传完整 md 全文替换（与建题同一条解析校验链），成功后 revision+1；已受理提交用快照判题不受影响（7.3）；解析失败 400 且原题不动（先解析后落库）

### v8（2026-10-18，M7 作业组卷与页面落地修订）

- **新增 12.4 作业组卷 API 规格**（全章节）：端点表、创建校验（题数上限 20、数组序 = 题序、revision 快照冻结）、编辑锁定语义、删除语义、学生三态与开始前 404
- **题库定位语义定稿**：课程组公共题库面向整个课程（跨教学班复用）、教师题库面向教学班级（自建自用）；选题可见集合 = 公共题 + 本人个人题
- **锁定决策落地**：已有提交 → 题目列表锁定（只允许改标题/时间）；截止时间只允许向后延长；无提交可删、有提交 400（DB 层 PROTECT 兜底）
- **学生详情防提前看题**：开始前详情 404，列表仅显示「未开始」徽章（提交层 7.1 的「作业尚未开始」400 保持不变，学生已知道作业存在）
- **页面实装**（开发预览）：教师题库页（录题/编辑 md 表单 + 课程组公共题入口）、教学班作业 Tab（作业列表 + 组卷选题面板）、学生作业列表（三态徽章）、学生作业详情（题面 markdown 渲染 + 代码提交 + 判题结果 2s 轮询）——全部 fetch 12.x API，服务端校验为唯一防线

### v9（2026-10-18，M8 部署验收修订）

- **部署契约落地**：新建 `deploy/judge-env.md`（实例/认证/实测结论表/资源档/go-judge 侧要求/Django 侧部署步骤/变更流程）与 `deploy/nginx-oj.conf`（/run、/file 仅应用服务器可达、/config 关闭或限源、其余路径 403 的限源模板）
- **实测脚本**：`scripts/sandbox_probe.py` 一次性实测（cgroup 红线/编译命令/语言版本/标准库抽样/绝对路径/内存限制强制），输出结论冻结进部署契约——镜像升级后必须重跑并更新契约
- **验收清单**：`docs/OJ验收清单.md` 端到端六节（环境自检 → 录题组卷 → 真实判题 → SPJ 与锁定 → 租约运维 → 结论），每步含预期结果
- **部署红线重申**：cgroup 验收失败即拒绝启动判题（不降级运行）；nginx 限源验收后 /config 关闭；凭证只走 OJ_AUTH_TOKEN 环境变量
- **验收修复（围栏末尾换行）**：端到端验收实测发现 `extractBlocks` 用 `'\n'.join` 丢失数据围栏内容末尾换行，导致 expected 存成 `"30"` 而程序输出 `"30\n"`，exact 逐字节比对误判 WA。修复口径：4 个数据围栏（input-sample/output-sample/input/output）按**文件语义**保留内容末尾换行（与程序 stdout 行尾一致）；spj-python 是代码块不在此列。此后录题数据一律按文件内容存储，比对层（第五章三策略）无需任何规范化补偿
- **验收修复（checker 状态门槛）**：实测发现 runSpj 仅认 Accepted，而部分分协议要求 checker exit≠0（go-judge 必然报 `Nonzero Exit Status`）→ 部分分被误判为 checker 故障置 system_error。**v6 的「checker 非 Accepted 一律按故障处理」作废**，修正口径：checker 正常执行 = Accepted（exit 0 满分）+ Nonzero Exit Status（exit≠0 部分分协议信号），二者进分数协议；其余状态（TLE/MLE/OLE/Signalled/Dangerous Syscall/File Error/Internal Error）仍按故障重试 1 次。单元测试 mock 教训：`Accepted + exitStatus≠0` 是现实不存在的组合，mock 必须用真实状态字面量

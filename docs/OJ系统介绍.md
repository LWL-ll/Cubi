# OJ 判题系统介绍（详细版）

> 本文是 codemind 教学平台 OJ 判题子系统的**详细文档**：讲清整体架构、
> 每一步流程、数据模型、协议细节与运维方法。协议条款以
> [OJ对接设计.md](OJ对接设计.md) 为权威（本文引用其章节号），
> 验收状态见 [OJ验收清单.md](OJ验收清单.md)。

## 目录

1. [系统定位](#一系统定位)
2. [总体架构](#二总体架构)
3. [核心数据模型](#三核心数据模型)
4. [提交受理流程（同步段）](#四提交受理流程同步段)
5. [判题执行流程（异步段）](#五判题执行流程异步段)
6. [答案比对与 SPJ 部分分](#六答案比对与-spj-部分分)
7. [租约状态机详解](#七租约状态机详解)
8. [沙箱客户端（go-judge 对接）](#八沙箱客户端go-judge-对接)
9. [安全与保密](#九安全与保密)
10. [部署与运维](#十部署与运维)
11. [API 端点速查](#十一api-端点速查)
12. [验收状态](#十二验收状态)

---

## 一、系统定位

codemind 是前后端分离的教辅平台（Django 6.1 + DRF），OJ 判题是作业
闭环的最后一环：学生在作业截止前提交代码，系统在**云沙箱**中编译运行、
按测试点判分，结果（部分分 + 脱敏详情）返回学生与教师端。

三条设计底线：

1. **判题链路完全异步**——提交立即返回受理结果，判题在独立
   qcluster long 集群中进行，前端轮询拿终态；
2. **至少一次投递 + 至多一次生效**——Q2 队列保证任务不丢，
   租约状态机保证任务不重判、不悬挂；
3. **判题只用受理时冻结的快照**——判题任务零实时查题，
   教师改题不影响排队中的提交。

---

## 二、总体架构

### 2.1 架构图

```
浏览器/前端
   │  POST /api/submissions/（受理：九项校验 → 快照冻结 → 建表 → 投递）
   ▼
Django Web 进程（runserver/gunicorn）
   │  transaction.on_commit → async_task(cluster='long')
   ▼
django-q2 持久队列（SQLite ormq，long 集群专用）
   │  qcluster（独立进程：Q_CLUSTER_NAME=long python manage.py qcluster）
   ▼
租约状态机（JudgeTask：pending → leased → done / retryable / failed）
   │  编译 + 逐测试点判题 + spj（HTTP + Bearer OJ_AUTH_TOKEN）
   ▼
go-judge 云沙箱（https，独立部署，nginx 限源）
   │  gcc 编译 / 程序运行 / checker 运行（cpu/clock/memory/proc/copyOut 限额）
   ▼
结果落库（Submission 终态 + 得分 + 脱敏详情；JudgeTask done）
   ▲
回收排程（Schedule 记录，挂 long 集群，每分钟）——兜底三扫
```

### 2.2 组件分工

| 组件 | 职责 | 代码位置 |
|---|---|---|
| Django + DRF | 受理提交、作业锁定、查询接口、权限 | `teaching/api.py`、`teaching/submission_service.py` |
| django-q2 | 「至少一次投递」：持久队列 + 失败重试 | `settings.py` Q_CLUSTER / ALT_CLUSTERS |
| JudgeTask 租约 | 「至多一次生效」：并发准入、心跳、回收、重试上限 | `tasks/tasks_judge.py` |
| go-judge 沙箱 | 编译与运行隔离 | `teaching/judge.py`（HTTP 客户端） |
| 回收排程 | 每分钟兜底扫描（**必须挂 long 集群**） | `tasks/migrations/0002、0003` |

### 2.3 关键配置

**Q_CLUSTER（django-q2，settings.py）**：

| 项 | 主集群 codemind | long 集群（判题专用） |
|---|---|---|
| workers | 2 | 2 |
| timeout（单任务超时） | 600s | 900s |
| retry（失败重投等待） | 700s | 1000s |
| max_attempts | 2 | 2 |
| queue_limit | 50 | 50 |
| broker | Django ORM（django_q_ormq 表） | 同左 |

> 注意：`retry` 必须大于 `timeout`——否则任务超时被杀前就被重投，
> 同一任务会并发执行（django-q2 官方约束，实测警告确认）。

**沙箱环境变量（settings.py）**：

| 变量 | 说明 |
|---|---|
| `OJ_BASE_URL` | 沙箱地址，默认 `https://oj.liuwenli.cn` |
| `OJ_AUTH_TOKEN` | 判题凭证，**只从环境变量读取，严禁硬编码** |
| `OJ_RUN_TIMEOUT` | 单次判题 HTTP 超时 = 30s（> 最大 clockLimit 10s，留网络余量） |

**判题常量（tasks/tasks_judge.py）**：

| 常量 | 值 | 含义 |
|---|---|---|
| MAX_JUDGE_ATTEMPTS | 2 | 单提交最多执行 2 次 |
| HEARTBEAT_TIMEOUT_SECONDS | 60 | 租约心跳超时（回收触发） |
| PENDING_REDISPATCH_SECONDS | 120 | pending 补投扫描阈值 |
| REPEAT_SCAN_COUNT | 20 | 重复检测窗口（同用户同题最近 20 条） |
| MAX_PREVIEW_CHARS | 500 | WA 输出预览截断长度 |

---

## 三、核心数据模型

三张表都在 `teaching/models.py`。

### 3.1 Problem（题目）

| 字段 | 说明 |
|---|---|
| statement_md | 题面 markdown（**剔除受保护代码块后重建**，保密链 14.7） |
| samples | 公开样例 `[{input, output}]`，可下发 |
| tests | 隐藏测试点 `[{input, output, points}]`，**永不下发** |
| languages | 允许语言 `['c', 'python']` |
| tier | 资源档位 beginner / basic / advanced |
| compare_strategy | 比对策略 exact / trailing / loose |
| has_spj / spj_code | 特殊判定开关 + checker 源码（Python） |
| revision | 修订号，每次改题 +1 |
| owner / is_public | 题库归属：公共题库（is_public=True，系统管理员导入/维护，教师只读选题）/ 教师个人题库 |

### 3.2 Submission（提交）

三种类型合一：practice（练习）/ homework（作业）/ exam（考试，二期留位）。

| 字段 | 说明 |
|---|---|
| user / problem / homework | 提交人、题目（SET_NULL 允许删题）、作业（PROTECT） |
| language / code | 语言 + 代码原文（重复检测、教师批改要看） |
| fingerprint | 统一换行后原文 SHA256（重复检测指纹） |
| status | 状态机主字段（见下） |
| score / total_score | 得分 / 题目总分（Σ 测试点分值） |
| time_used / memory_used | 最大单点耗时（纳秒）/ 最大单点内存（byte），学情分析原料 |
| error_detail | 错误详情（**脱敏后**） |
| repeat_of | 重复于哪次提交（自引用） |
| problem_revision / tests_hash | 审计快照：受理时修订号 + 测试数据哈希（不可复现时定位） |
| judge_snapshot | **判题数据完整副本**（见 4.2），判题零实时查题 |
| judged_at | 判题完成时间 |

**状态枚举与状态机**：

```
非终态：queued（排队中）→ running（判题中）
终态（不可变）：accepted / wrong_answer / compile_error
              / time_limit_exceeded / memory_limit_exceeded
              / output_limit_exceeded / runtime_error
              / repeat / system_error
```

DB 约束：挂作业的提交必须是 homework 类型（CheckConstraint）；
索引：`(user, created_at)` 频率检测、「我的提交」列表；`(problem, user)` 重复检测窗口。

### 3.3 JudgeTask（判题执行表）

与 Submission 一对一，独立于通用异步任务表（租约字段为判题独有）。

| 字段 | 说明 |
|---|---|
| status | 租约状态：pending → leased → done / retryable / failed |
| attempt_id | 尝试号 = **租约令牌**（见 7.2） |
| q2_task_id | q2 投递标识（受理投递后回写，补投扫描依据） |
| file_id | 编译产物 fileId（判后即删，回收时按此清理残留） |
| progress | 进度 0-100（前端轮询的进度来源） |
| claimed_at / heartbeat_at | 领取时间 / 心跳时间（回收扫描依据） |
| started_at / finished_at | 开始 / 结束时间 |

---

## 四、提交受理流程（同步段）

入口：`POST /api/submissions/`（`teaching/submission_service.py: submitSolution`）。
全程**毫秒级、零沙箱调用**，任何校验不过立刻 400/404/429 快速失败。

### 4.1 九项同步校验（按执行顺序）

| # | 校验 | 失败结果 |
|---|---|---|
| 1 | 题目存在 | 404「题目不存在」 |
| 2 | 提交类型白名单 + 与 homeworkId 一致性（作业提交必带 homeworkId，非作业提交不能带） | 400 |
| 3 | 语言白名单（c/python）+ 题目允许语言集 | 400 |
| 4 | 代码非空且 ≤ 64KB | 400 |
| 5 | 题目已配置测试点（防 0 分判题） | 400「题目尚未配置测试点」 |
| 6 | 作业组合校验（仅作业提交）：作业存在 404 → 学生经「行政班 → 教学班」三跳推导在班（不在班 404，不暴露存在性）→ 题目属于作业（不属于 404）→ 未开始/已截止 400 | 404 / 400 |
| 7 | 频率闸：同用户近 10 秒已有 ≥3 条提交 → 429「提交过于频繁」 | 429 |
| 8 | 队列闸：同用户未终态 JudgeTask ≥3 个 → 400「排队中的提交过多」 | 400 |
| 9 | 快照冻结 + 原子建表 + on_commit 投递（见 4.2~4.4） | — |

关键点：**频率 count 与建表必须同事务**——并发提交下 count 与插入串行化
（SQLite 单写者天然串行；切 PG 后需按用户行 select_for_update）。

### 4.2 快照冻结（judge_snapshot）

受理时把判题所需数据**一次拷走**：

```python
{
    'tests': problem.tests,                  # 全部隐藏测试点
    'spj_code': problem.spj_code,            # checker 源码
    'tier': problem.tier,                    # 资源档位
    'compare_strategy': problem.compare_strategy,
}
```

判题任务**只读这个快照，零实时查题**——教师改题（修订号 +1）不影响
排队中的提交；题目被删则判题时按快照继续（problem_id 为 NULL 才报
「题目已被删除」）。

同时落库审计字段：`problem_revision`（受理时修订号）、
`tests_hash`（测试数据排序序列化 SHA256）、`fingerprint`（统一换行后
代码 SHA256——**不去空白**，缩进/字符串内空白有语义，去空白会误伤
Python 缩进，语义指纹推迟二期）。

### 4.3 原子建表 + 投递

```python
with transaction.atomic():
    checkFrequency(user)          # 频率闸（与建表同事务）
    checkQueueLimit(user)         # 队列闸
    submission = Submission(status=queued, 完整快照)
    JudgeTask(submission, status=pending)
    transaction.on_commit(投递)
```

**投递语义**：事务提交后才 `async_task(runJudgeTask, submissionId,
cluster='long')` 并回写 `q2_task_id`；投递失败**不影响受理响应**
（记日志、q2_task_id 留空）——pending 补投扫描兜底（见 7.5）。

### 4.4 受理时序

```
学生 → POST /api/submissions/ ── 九项校验（毫秒级）
                              ── 事务：Submission(queued) + JudgeTask(pending)
                              ── 返回 {submissionId}        ← 学生收到
                              ── on_commit: async_task(long)
                              ── qcluster worker 领取 → 判题（异步，学生轮询）
```

**作业锁定规则**（验收 4.3~4.5，`homework_service.py`）：
已有提交的作业 → 题目列表锁定（改题 400「已有提交，题目不可变更」）、
不可删除（400「已有提交的作业不可删除」）；截止时间**只允许向后延长**
（缩短 400，防误伤已规划学生）。

---

## 五、判题执行流程（异步段）

入口：`tasks/tasks_judge.py: runJudgeTask(submissionId)`（Q2 long 集群消费）。
执行顺序：租约领取 → 资格复查 → 重复检测 → 编译 → 全测试点判题 →
聚合落库。

### 5.1 阶段总览

| 阶段 | 做什么 | 沙箱调用 | 失败处理 |
|---|---|---|---|
| 0 领取 | 原子条件 UPDATE 领租约（见 7.3） | 无 | 领不到直接返回 |
| 1 资格复查 | 仅查身份类（题目删除/封禁/移出教学班） | 无 | system_error |
| 2 重复检测 | 同用户同题最近 20 条找相同指纹 | 无 | repeat 终态 |
| 3 编译 | 仅 C：gcc 编译，产物 copyOutCached 复用 | 1 次 | compile_error / 瞬时错误重投 |
| 4 逐点判题 | 每点独立跑独立计分（部分分） | 每点 1 次（+spj） | 见 5.10 |
| 5 聚合落库 | 主状态取最差单点，得分求和，事务写终态 | 无 | — |
| 6 清理 | 删编译产物（幂等） | 1 次 DELETE | 记警告日志 |

### 5.2 阶段 1：资格复查（零 HTTP）

判题只用快照，不重查题目——资格复查**只查身份类**：

- `problem_id` 为 NULL（题被删）→ system_error「题目已被删除」
- 用户被封禁 → system_error「账号已被封禁」
- 作业提交且学生已不在教学班 → system_error「已被移出教学班」

### 5.3 阶段 2：重复检测（零 HTTP）

同用户同题**最近 20 条**提交中找 `fingerprint` 相同的 → 直接
`repeat` 终态（`repeat_of` 指向被重复的提交，score 0）。
防误刷：不消耗沙箱资源。

### 5.4 阶段 3：编译（仅 C）

Python 免编译（判题时内联代码原文直跑）。C 编译请求（go-judge cmd）：

| 项 | 值 |
|---|---|
| args | `['gcc', 'main.c', '-o', 'main', '-O2', '-std=c11', '-lm']` |
| env | `PATH=/usr/bin:/bin` |
| copyIn | `main.c` ← 代码原文 |
| copyOutCached | `['main']`（产物沙箱缓存复用，返回 fileId 落 JudgeTask.file_id） |
| 限额 | 编译档独立：clock 10s、copyOutMax 32MB（编译比运行慢） |

编译失败（gcc 非零退出）→ `compile_error` 终态 + 脱敏 stderr；
沙箱瞬时错误（File Error / Internal Error）→ 瞬时异常交给 Q2 重投。

### 5.5 阶段 4：单测试点判题

每个测试点一条独立 go-judge cmd，**跑完不提前终止**（部分分）：

| 项 | C | Python |
|---|---|---|
| args | `['./main']` | `['python3', 'main.py']` |
| copyIn | `main` ← 编译产物 fileId | `main.py` ← 代码原文 |
| stdin | 测试点输入 | 同左 |
| stdout/stderr | 各 8MB | 同左 |

**资源档**（每次请求显式携带全部 limit，不依赖沙箱默认值）：

| 档位 | cpuLimit | clockLimit | memoryLimit | copyOutMax |
|---|---|---|---|---|
| 入门 beginner | 2s | 5s | 256MB | 16MB |
| 基础 basic | 2s | 5s | 256MB | 16MB |
| 进阶 advanced | 5s | 10s | 512MB | 32MB |

固定项：procLimit 32、stdout/stderr 各 8MB；未知档位按入门档兜底（保守从严）。

**沙箱状态 → 提交状态映射**（`judge.py: JUDGE_STATUS_MAP`）：

| go-judge 原始状态（9 种字面量） | 提交状态 |
|---|---|
| Accepted | 中间态，进比对/spj 再判对错 |
| Time Limit Exceeded | time_limit_exceeded |
| Memory Limit Exceeded | memory_limit_exceeded |
| Output Limit Exceeded | output_limit_exceeded |
| Nonzero Exit Status / Signalled / Dangerous Syscall | runtime_error |
| File Error / Internal Error | 任务层瞬时异常（重投）；客户端已重试 1 次 |
| 未知字面量 | system_error（防沙箱升级新增状态污染业务数据） |

每点跑完**同写 progress + 心跳**（`progress = index * 100 // total`）：
写失败说明租约已被回收 → 本副本抛 JudgeLeaseLost 静默退出（结果作废）。

### 5.6 阶段 5：聚合与落库

```
score  = Σ 各点得分                      （部分分求和）
status = 最差单点（RE > TLE > MLE > OLE > WA）；全 AC → accepted
time_used  = max 单点耗时（纳秒）
memory_used = max 单点内存（byte）
error_detail = 主状态对应点的详情（脱敏）
```

写终态在**事务内**：JudgeTask 条件 UPDATE 带 `attempt_id` 作闸（闸通过
才写 Submission）——过期 attempt 的执行结果直接丢弃（至多一次生效）。

### 5.7 异常路径总表（runJudgeTask）

| 异常 | 来源 | 处理 | 终态 |
|---|---|---|---|
| JudgeLeaseLost | 判题中租约被回收（心跳写失败） | 记日志，静默退出 | 回收路径接管 |
| JudgeTransientError | 沙箱 File/Internal Error、网络错误 | 余量内 → retryable 回退 + 抛给 Q2 重投；余量尽 → markFailed | 重投 / system_error |
| SpjFailedError | checker 连续故障 | 直接终态（**≠ 用户 WA**） | system_error「判题程序（checker）异常」 |
| JudgeClientError | 401 / 4xx / 响应非法（确定性错误） | 直接终态 | system_error「判题服务暂时不可用」 |
| 编译错误 | gcc 非零退出 | 直接终态 + 脱敏 stderr | compile_error |
| 无测试点快照 | 脏数据 | 直接终态 | system_error「题目测试数据异常」 |

---

## 六、答案比对与 SPJ 部分分

### 6.1 比对三策略

| 策略 | 算法 | 适用 |
|---|---|---|
| exact | 逐字节相等 | 默认 |
| trailing | 逐行 rstrip（仅行尾 ASCII 空格/制表符，不动行首）+ 首尾空行两侧都去掉 | 常见格式题 |
| loose | 去掉 ASCII 与 Unicode 空白（含 \xa0 等空格类）+ 统一换行（\r\n→\n） | 空白无关题 |

未知策略按精确比对兜底。

### 6.2 SPJ 特殊判题（部分分协议）

录题时用 `spj-python` 代码块提供 checker（Python）。判题流程：
用户程序 Accepted 后，**checker 单独跑一次沙箱**：

```
args:   python3 checker.py input.txt user_out.txt expected.txt
copyIn: checker.py（源码）+ input.txt（测试输入）
        + user_out.txt（用户输出）+ expected.txt（预期输出）
限额:   spj 独立档（比用户档更紧）：cpu 2s / clock 5s / mem 256MB，
        stdout/stderr 各 1MB
```

**打分协议**：

- checker exit 0 → 该点**满分**；
- checker exit ≠ 0 → stdout **首行** strip 后若是 `[0, 满分)` 的整数 →
  该分（协议信号）；否则 0 分。

**checker 故障 ≠ 用户 WA**：checker 沙箱 TLE/MLE/File Error 等故障
→ 重试 1 次，仍失败 → 提交置 system_error（学生不背锅）。
实测教训：v6 曾写「非 Accepted 一律按故障」，把部分分协议的 exit≠0
误杀成 system_error（验收 4.2 抓到，见 OJ对接设计.md v9 修订记录）。
正常执行状态白名单：`Accepted` + `Nonzero Exit Status`。

**WA 详情脱敏**（14.7）：只给「第 k 个测试点未通过」+ 学生**自己输出**
的截断预览（500 字符、控制字符清洗），**绝不含预期输出**。

---

## 七、租约状态机详解

### 7.1 状态转移图

```
                    ┌─────────── 领取（原子 UPDATE）──────────────┐
                    │                                            │
   pending ──→ leased(attempt_id, heartbeat) ──判题成功──→ done
      ▲            │  │                                          ▲
      │   心跳超 60s │  └── 瞬时错误 fallbackForRetry ──→ retryable ──┐
      │            ▼                              （q2 重投 → 领取）    │
      └── 孤儿兜底置回 pending ◄──────────────────────────────────────┘
                    │
     领取时执行次数 > 2 / 回收余量尽 ──→ failed（system_error，任务终结）
```

### 7.2 attempt_id 语义与执行次数

- `attempt_id` 是**租约令牌**，每经过一次「回收 → 重投 → 领取」+2：
  回收 +1 作废旧令牌，领取 +1 建立新租约；
- **执行次数公式**：`getExecutionCount(attemptId) = (attemptId + 1) // 2`
  ——领取时执行次数 > MAX_JUDGE_ATTEMPTS(2) → 直接 markFailed；
- 所有写操作（进度/终态/清理/回退）都带 `attempt_id` 条件——
  只有持当前租约的 worker 能写，过期 attempt 的结果自然丢弃。

### 7.3 领取（claimLease）

```sql
UPDATE JudgeTask SET status='leased', attempt_id=attempt_id+1,
       claimed_at=now, heartbeat_at=now
WHERE pk=? AND status IN ('pending','retryable')
-- 影响行数 = 1 才算领到：并发 worker 只有一个成功
```

领取成功后写 `started_at`（仅首次）、`Submission.status=running`。

### 7.4 心跳

每判完一个测试点写一次 `heartbeat_at`（与 progress 同一条 UPDATE，
写失败即抛 JudgeLeaseLost）。

### 7.5 回收排程（每分钟三扫，`recycleStaleLeases`）

Schedule 记录承载（name=judge-lease-recycle，cluster=**long**），
每分钟由 long 集群调度器投递。三段扫描：

| 扫 | 目标 | 动作 |
|---|---|---|
| 1 租约回收 | leased 且心跳超 60s（进程被杀/卡死） | 条件 UPDATE → retryable + attempt_id+1 + 清 file_id/claimed_at/heartbeat_at + 删残留编译产物 + 重新投递 |
| 2 补投 | pending 且提交超 120s（受理投递失败兜底） | 重新投递 |
| 3 retryable 孤儿兜底 | retryable 且 claimed_at 为空（无 worker 持有） | 执行次数有余量 → 置回 pending + 重新投递；余量尽 → markFailed |

**历史教训（M8 验收 5.1/5.2 实测抓到）**：
① 迁移 0002 把排程注册为 cluster=''（主集群），而生产只跑 long 集群 →
排程从未被消费，且已部署库中排程记录缺失 → 租约回收从未运行，
LEASED 任务在 worker 重启后永久悬挂（0003 数据迁移修复）；
② q2 retry 重投链会断（broker 消息丢失/重试用尽/进程被杀）→
retryable 孤儿无人救活 → 第三扫兜底（2026-10-18 提交 94259b9）。

**任务永不悬挂的三道防线**：

1. Q2 持久队列重投（至少一次投递）；
2. 回收排程三扫（含 retryable 孤儿兜底）；
3. 重试上限 MAX_JUDGE_ATTEMPTS=2（余量尽 → failed 终态，快速收敛）。

---

## 八、沙箱客户端（go-judge 对接）

`teaching/judge.py`：HTTP 封装 + 状态集中映射，对接 go-judge **v1.13.3**
新版 API（2026-10-18 实测）。

### 8.1 API 形态

| 操作 | 请求 | 说明 |
|---|---|---|
| 判题 | POST /run，体为 `{"cmd": [...]}` | 一次同步跑完，返回 Result 数组（与 cmd 一一对应） |
| 上传 | POST /file（multipart，字段名 file） | 响应为纯 JSON 字符串 fileId（如 "ZSGDNRTD"） |
| 删除 | DELETE /file/\<fileId\> | 404 视为已清理（清理幂等） |

注意：裸数组 cmd 形态实测被 400 拒（cannot unmarshal array into
model.Request）——必须包 `{"cmd": [...]}`。

### 8.2 响应校验（防沙箱异常响应污染业务数据）

- status 必须属于 9 种原始字面量之一，未知值 → SandboxError → system_error；
- files/fileIds 若存在必须是 dict；fileError 若存在必须是 list。

### 8.3 重试策略

| 故障 | 策略 |
|---|---|
| 网络错误/超时/5xx | 透明重试 1 次，仍失败 NetworkError |
| 401 | AuthError 不重试（token 失效，提示运维轮换） |
| 400/其他 4xx | BadRequestError 不重试（请求构造错误，重试同样错） |
| 沙箱 Internal Error | 整体重试 1 次（重跑是全新沙箱进程，可自愈） |
| File Error | 不重试（保守默认：细分类型无实测数据，一律按确定性错误） |

---

## 九、安全与保密

- **凭证管理**：OJ_AUTH_TOKEN 只从环境变量读取（三级查找注入），
  严禁硬编码、入库、打日志；401 提示运维轮换。
- **测试数据保密链**（14.7）：
  - 隐藏测试点（tests）**永不下发**——题面 markdown 入库前剔除受保护
    代码块，学生/教师接口不返回 tests/spj_code（验收 5.4 抓包确认）；
  - 日志与错误详情不含测试输入/预期输出；
  - WA 只给「第 k 点」+ 学生自己输出的截断预览（500 字符、控制字符
    清洗）；RE 附学生自己程序的 stderr（无保密问题）；
  - spj 输出不回显。
- **频率/队列闸**：10 秒 3 次 → 429；同用户未终态 ≥3 → 400；
  代码 ≤64KB；队列积压上限 50。
- **网络防护**：云沙箱 nginx 限源（只放行平台服务器）；会话 Cookie
  HttpOnly + SameSite=Lax；登录失败锁定（django-axes，5 次锁 1 小时）。

---

## 十、部署与运维

### 10.1 进程清单

| 进程 | 启动方式 | 说明 |
|---|---|---|
| Web | runserver / gunicorn | 受理 + 查询 |
| qcluster 主集群 | `python manage.py qcluster` | 导入等通用异步任务 |
| qcluster long | `Q_CLUSTER_NAME=long python manage.py qcluster` | **判题专用**（回收排程也消费于此） |

### 10.2 回收排程

- DB 记录承载（django_q_schedule 表），迁移 0002/0003 幂等创建/修正；
- 每分钟执行 `tasks.tasks_judge.recycleStaleLeases`；
- **cluster 字段必须是 long**——生产只跑 long 集群时，挂错集群的
  排程永远不会被消费（5.1 实测教训）。

### 10.3 故障排查手册

| 症状 | 定位 | 处置 |
|---|---|---|
| 提交卡 queued/running | JudgeTask 状态 + q2_task_id + django_q_ormq | 看是否 pending 补投/回收排程在跑；qcluster 是否活着 |
| 大量任务悬挂 | JudgeTask 按 status 分组统计 | 查回收排程 Schedule 记录存在且 cluster=long；查 qcluster 日志 |
| 全部判题 system_error | judge.py 日志（AuthError/NetworkError/SandboxError） | 查 OJ_BASE_URL 连通性、token 是否轮换、沙箱 /config |
| 401 | AuthError | 轮换 OJ_AUTH_TOKEN 并重启 qcluster |
| 日志刷屏 | codemind.judge / judge_task logger | 按 10.4 红线清理 |

### 10.4 日志红线（验收 5.3）

- 无未处理异常刷屏；
- WA 提交的 spj 输出/预期输出**不落日志**；
- 无测试数据落日志；
- 瞬时错误记 warning（一次），确定性错误记 error（一次）。

### 10.5 迁移路线（架构裁定）

队列框架可换（如 Celery）——「至少一次投递」换实现，租约状态机、
快照冻结、回收排程全部不变。

---

## 十一、API 端点速查

| 端点 | 方法 | 权限 | 说明 |
|---|---|---|---|
| /api/submissions/ | POST | homework:view | 受理提交（九项校验），返回 {submissionId} |
| /api/submissions/ | GET | homework:view | 我的提交列表（分页 20，可过滤 problem/homework/status；超管看全部） |
| /api/submissions/\<id\>/ | GET | homework:view | 提交详情（仅本人或超管，否则 404；含 code 与 progress，不含测试点/预期/spj 输出） |
| /api/teacher/problems/ | POST | homework:bank | 教师录题（个人题库；学生 403） |
| /api/homeworks/ | GET | homework:view | 学生作业列表 |

---

## 十二、验收状态

见 [OJ验收清单.md](OJ验收清单.md)：第二~五节（录题组卷、提交判题、
SPJ 与作业锁定、租约与运维）已全部实测通过（2026-10-19，提交
94259b9）。遗留项：沙箱 nginx 限源部署与 judge-env.md 回填
（等新沙箱上线）；spj 输出红线实测（需 spj 题）；验收签字；
全量测试回归。

---

## 附录：术语表

| 术语 | 含义 |
|---|---|
| 快照（judge_snapshot） | 受理时冻结的判题数据副本，判题零实时查题 |
| 租约（lease） | attempt_id 标识的「判题执行权」，原子领取、心跳保活、超时回收 |
| attempt_id | 租约令牌：回收 +1 作废、领取 +1 新租约；执行次数 = (attempt_id+1)//2 |
| 至少一次投递 | Q2 队列语义：任务可能被重复投递，但不会丢 |
| 至多一次生效 | 租约语义：重复投递只有一个 worker 领到租约并写结果 |
| retryable 孤儿 | 瞬时错误回退后、无 worker 持有（claimed_at 为空）且 Q2 重投链断裂的任务 |
| 部分分 | 每个测试点独立计分，得分求和；主状态取最差单点 |
| spj | 特殊判题（checker 程序），三参数协议 + 退出码/首行打分割 |

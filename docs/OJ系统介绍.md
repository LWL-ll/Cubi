# OJ 判题系统介绍

> 本文是 codemind 教学平台 OJ 判题子系统的**总览文档**：面向新成员与评审，
> 讲清「系统长什么样、判题怎么跑、可靠性靠什么」。逐项协议细节见
> [OJ对接设计.md](OJ对接设计.md)，验收状态见 [OJ验收清单.md](OJ验收清单.md)。

## 一、系统定位

codemind 是前后端分离的教辅平台（Django 6.1 + DRF），OJ 判题是其作业
闭环的最后一环：学生在作业截止前提交代码，系统在**云沙箱**中编译运行、
按测试点判分，并把结果（含部分分与脱敏详情）返回学生与教师端。

判题链路完全异步：学生提交后立即返回受理结果，判题在独立队列集群中
进行，前端轮询拿终态。

## 二、总体架构

```
浏览器/前端
   │  POST /api/submissions/（受理：限流 → 资格 → 快照冻结 → 指纹）
   ▼
Django Web 进程（runserver/gunicorn）
   │  transaction.on_commit → 投递
   ▼
django-q2 持久队列（SQLite ormq，long 集群专用）
   │  qcluster（独立进程，Q_CLUSTER_NAME=long，2 worker）
   ▼
租约状态机（JudgeTask：pending → leased → done/retryable/failed）
   │  编译 + 逐测试点判题（HTTP + OJ_AUTH_TOKEN）
   ▼
go-judge 云沙箱（https，独立部署，nginx 限源）
   │  gcc 编译 / 程序运行 / spj checker 运行
   ▼
结果落库（Submission 终态 + 得分 + 脱敏详情）
```

**组件分工**：

| 组件 | 职责 |
|---|---|
| Django + DRF | 受理提交、作业锁定、查询接口、权限 |
| django-q2 | 「至少一次投递」：持久队列 + 失败重试（long 集群 retry 1000s） |
| JudgeTask 租约 | 「至多一次生效」：并发准入、心跳、回收、重试上限 |
| go-judge 沙箱 | 编译与运行隔离（cpu/clock/memory/proc/copyOut 限额） |
| 回收排程 | 每分钟扫描（Schedule 记录，**必须挂 long 集群**） |

## 三、判题全链路（提交 → 出分）

1. **受理**（Web 进程内，零沙箱调用）：限流（10 秒 4 次）→ 资格检查
   （题目存在/未删除、作业截止校验、用户有效）→ **题目快照冻结**
   （judge_snapshot：题目数据、资源档、比对策略、spj 代码一次拷走，
   判题全程零实时查题）→ 指纹计算（重复检测用）→ 建 Submission(queued)
   + JudgeTask(pending)，事务提交后投递 Q2。
2. **领取**（qcluster worker）：原子条件 UPDATE 领租约
   （pending/retryable → leased，attempt_id+1），并发下只有一个 worker
   成功；超过重试上限直接 failed。
3. **资格复查**：仅查身份类（题目删除/账号封禁/移出教学班）→ 异常直接
   system_error，零 HTTP。
4. **重复检测**：同用户同题最近 20 条提交中找相同指纹 → 命中直接 repeat
   终态（不消耗沙箱资源，防误刷）。
5. **编译**（仅 C，Python 内联运行）：gcc 编译档独立限额，产物
   copyOutCached 复用；编译错误 → compile_error 终态（附脱敏 stderr）。
6. **逐测试点判题**：每个点独立跑独立计分（部分分），跑完聚合：
   主状态取最差单点（RE > TLE > MLE > OLE > WA），score = 通过点分值之和；
   每题判后同写 progress + 心跳。
7. **终态**：写 JudgeTask done + Submission 终态（事务内，attempt_id 作闸，
   过期 attempt 的结果直接丢弃）。

## 四、双表状态机与租约

```
Submission:  queued → running → terminal（终态不可变）
JudgeTask:   pending → leased(attempt_id, heartbeat) → done | retryable | failed

terminal = accepted | wrong_answer | compile_error | time_limit_exceeded
         | memory_limit_exceeded | output_limit_exceeded | runtime_error
         | repeat | system_error
```

**租约规则**（可靠性核心）：

- **原子领取**：条件 UPDATE（状态改 leased + attempt_id+1 + 心跳）检查
  影响行数 = 1；所有写操作（进度/终态/清理）都带 attempt_id 条件——
  只有持当前租约的 worker 能写结果。
- **心跳**：每判完一个测试点写一次 heartbeat_at。
- **租约回收**：回收排程每分钟扫 `leased` 且心跳超 60 秒（进程被杀/
  卡死的判题）→ 置 retryable + attempt_id+1 + 清产物 → 重新投递。
- **pending 补投**：pending 超 120 秒未投递（受理时投递失败兜底）→ 补投。
- **retryable 孤儿兜底**：瞬时错误回退后依赖 Q2 重投，但重投链可能断
  （消息丢失/重试用尽/进程被杀）→ 回收排程同时兜底扫描 retryable 且
  无 worker 持有的任务：余量内置回 pending 重投，余量用尽转 failed 终态
  （任务永不悬挂，M8 验收 5.2 实测）。
- **重试上限**：执行次数 = (attempt_id + 1) // 2，超过 2 次 → failed +
  system_error（沙箱持续故障时快速收敛，不无限重试）。

## 五、判题能力

- **语言**：C（gcc -O2 -std=c11，编译产物复用）、Python 3。
- **资源档**（每请求显式携带，不依赖沙箱默认值）：入门/基础档 cpu 2s、
  clock 5s、内存 256MB；进阶档 cpu 5s、clock 10s、内存 512MB；
  编译档 clock 10s；stdout/stderr 各 8MB；进程数 32。
- **答案比对三策略**：exact 逐字节 / trailing 忽略行尾空白 /
  loose 忽略全部空白。
- **SPJ 特殊判题**：checker 三参数协议（输入、用户输出、预期输出），
  exit 0 = 满分、stdout 首行整数 = 部分分；checker 故障 ≠ 用户 WA
  （重试 1 次仍故障 → system_error，学生不背锅）。
- **部分分**：全测试点独立计分，主状态取最差单点，得分求和。

## 六、安全与保密

- **沙箱鉴权**：OJ_AUTH_TOKEN 仅环境变量注入（三级查找），严禁硬编码、
  入库、打日志。
- **测试数据保密链**：日志与错误详情不含测试输入/预期输出；WA 只给
  「第 k 个测试点」+ 学生自己输出的截断预览（500 字符、控制字符清洗）；
  教师/课程组接口不返回 tests/spj_code（验收 5.4）。
- **网络防护**：云沙箱 nginx 限源（只放行平台服务器）；提交限流
  （10 秒 4 次 → 429）；会话 Cookie HttpOnly + SameSite=Lax；
  登录失败锁定（django-axes，5 次锁 1 小时）。

## 七、部署与运维

- **qcluster**：判题专用 long 集群独立进程（conda 环境 + token 注入），
  与导入等其他异步任务隔离。
- **回收排程**：DB Schedule 记录承载（name=judge-lease-recycle，每分钟），
  由迁移幂等创建；**cluster 字段必须为 long**——历史上曾注册为默认集群
  导致回收从未运行（M8 验收 5.1 抓出并修复，迁移 0002/0003）。
- **可观测**：qcluster 日志红线——无未处理异常刷屏、无测试数据落日志。
- **迁移路线**：队列框架可换（如 Celery）——「至少一次投递」换实现，
  租约状态机、快照、回收全部不变（架构裁定 3）。

## 八、验收状态

见 [OJ验收清单.md](OJ验收清单.md)：第二~五节（基础判题、资源档、SPJ 与
作业锁定、租约与运维）已全部实测通过（2026-10-19，提交 94259b9）。
沙箱 nginx 限源部署与 judge-env.md 回填待新沙箱上线后完成。

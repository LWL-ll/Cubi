# CodeMind 教辅平台 · OJ 判题对接设计

> 更新时间：2026-10-18
> 状态：判题沙箱连通验证完成 ✅（2026-10-18 实测）；业务层对接代码随作业功能轮次实现
> 配套文档：《异步任务设计.md》（AsyncTask 承接批量判题）、《API设计文档.md》（作业接口约定）

---

## 一、认知：go-judge 是什么

我们接入的 OJ 底层是 [go-judge](https://docs.goj.ac/cn/)（v1.13.3，Go 编写）——它是一个**判题沙箱服务，不是完整 OJ**：没有题目库、用户系统、提交记录，只负责"把代码放进隔离容器跑一遍，报告运行结果"。

因此分工明确：

| 层 | 谁负责 | 干什么 |
|---|---|---|
| OJ 业务层 | **codemind 自己** | 题目管理、作业提交、答案对错判定、结果归档 |
| 判题沙箱 | go-judge（云上） | 隔离运行代码，返回 status / 耗时 / 内存 / 输出 |

> 关键认知：**go-judge 只回答"程序跑得怎么样"，不回答"答案对不对"**。
> 对错判定在 codemind 业务层做——把沙箱返回的 stdout 与题目预期输出比对。

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

## 三、端点清单与认证

| 方法 | 路径 | 认证 | 用途 |
|---|---|---|---|
| GET | `/version` | 公开 | 沙箱版本探测（健康检查可用） |
| GET | `/config` | 公开 | 沙箱能力配置 |
| POST | `/run` | Bearer | **判题**：一次请求同步跑完并返回结果 |
| POST | `/file` | Bearer | 上传文件（multipart，字段名 `file`），返回 fileId |
| GET | `/file/<fileId>` | Bearer | 下载文件内容 |
| DELETE | `/file/<fileId>` | Bearer | 删除文件（释放沙箱磁盘） |
| WS | `/ws` | Bearer | WebSocket 版判题（交互题用，暂不接） |

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
    "copyOutMax": 1048576
  }]
}
```

| 字段 | 单位 | 说明 |
|---|---|---|
| `args` | — | 命令行（第 0 项是程序路径，随后是参数） |
| `env` | — | 环境变量数组，**必须带 PATH** |
| `files` | — | fd 0/1/2 的 stdin/stdout/stderr 定义；stdout/stderr 需给 `max` 上限 |
| `copyIn` | — | 注入文件：`{"content"}`（小文件直接内联）/ `{"fileId"}`（引用已上传文件） |
| `copyOut` / `copyOutMax` | byte | 需回传的输出文件清单 + 总大小上限（**防止输出撑爆内存**） |
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
  "fileIds": {},
  "error": null,
  "fileError": []
}]
```

| 字段 | 单位 | 说明 |
|---|---|---|
| `status` | — | 运行状态（见第五节映射表） |
| `exitStatus` | — | 程序退出码 |
| `time` / `memory` | 纳秒 / byte | CPU 时间 / 峰值内存 |
| `runTime` | 纳秒 | 实际运行时长（判 TLE 用） |
| `files` | — | copyOut 与 stdout/stderr 内容 |

## 五、判题状态映射表（核心）

go-judge 共 9 种 status。**除 Accepted 外直接定案，Accepted 进入答案比对环节：**

| go-judge status | 含义 | codemind 提交状态 |
|---|---|---|
| `Accepted` | 正常结束（**未判对错**） | 进入 stdout 与预期输出比对 → 通过 / 答案错误 |
| `TimeLimitExceeded` | 超 clockLimit | 超时 |
| `MemoryLimitExceeded` | 超 memoryLimit | 内存超限 |
| `OutputLimitExceeded` | 输出超上限 | 输出超限 |
| `NonzeroExitStatus` | 非 0 退出码 | 运行错误 |
| `Signalled` | 进程被信号终止（段错误等） | 运行错误 |
| `DangerousSyscall` | 被 seccomp 拦截危险系统调用 | 运行错误 |
| `FileError` | 文件错误 | 系统错误（重试 1 次） |
| `InternalError` | 沙箱内部错误 | 系统错误（重试 1 次） |

答案比对策略（codemind 业务层，按题目配置三选一）：

| 策略 | 规则 | 适用 |
|---|---|---|
| 精确比对 | stdout 与预期输出逐字节相等 | 有固定输出的题 |
| 忽略行尾空白 | 逐行 strip 后比对 | 输出末尾多空格容错 |
| 忽略全部空白 | 去掉所有空白字符后比对 | 格式宽松的题 |

## 六、判题流程设计

### 6.1 单次判题（同步）

go-judge 的 `/run` 本身就是**同步**的：一次 HTTP 请求提交、跑完、带结果返回（实测往返毫秒级），**不需要回调**。clockLimit 是硬超时——到点沙箱杀掉进程并返回 TLE。

### 6.2 批量判题（作业场景，与《异步任务设计.md》衔接）

```
学生提交作业
   │
   ▼
创建 AsyncTask(type='judge', total=测试点数量)   ← django-q2 任务表
   │
   ▼
worker 领取：逐测试点同步调 POST /run
   │    每跑完一个测试点 → task.progress += 1
   ▼
全部跑完 → 汇总提交状态落库 → task.status='done'（result 存汇总）
   │
   ▼
学生端轮询 AsyncTask 进度（提交→轮询模式，见异步任务设计 3.x 节）
```

- **HTTP 超时**：单次请求 `OJ_RUN_TIMEOUT`（30s）必须大于最大 clockLimit（当前档位 10s），留网络余量
- **失败重试**：网络错误 / `InternalError` 重试 1 次（与 AsyncTask max_attempts=2 对齐），仍失败标记「系统错误」
- **幂等**：同一提交重跑不重复计数（AsyncTask 按提交记录去重）

### 6.3 数据流

```
codemind worker ──Bearer──▶ nginx ──▶ go-judge 容器池
      ▲                                  │
      │◄──── status/time/memory/stdout ──┘
      │
      ▼
stdout 比对预期输出 → 提交状态落库
```

## 七、资源限制策略（分档）

按题目难度分档（题目的时间/内存限制取档位上限以内，服务端写死下限兜底）：

| 档位 | cpuLimit | clockLimit | memoryLimit | copyOutMax | 适用 |
|---|---|---|---|---|---|
| 入门 | 2s | 5s | 256 MB | 16 MB | 语法练习、填空 |
| 基础 | 2s | 5s | 256 MB | 16 MB | 常规作业题 |
| 进阶 | 5s | 10s | 512 MB | 32 MB | 算法题 |

固定项：`procLimit=32`、stdout/stderr 各 `max=8MB`、`copyOutMax` 按档位（防输出撑爆 worker 内存）。
**每次请求显式携带全部 limit 字段**——不依赖沙箱默认值，防止服务端默认配置变更影响判题公平。

## 八、安全设计

1. **凭证不入仓库**：token 只从环境变量 `OJ_AUTH_TOKEN` 读取（settings.py 已配置），开发/生产各自注入；泄露后立即轮换
2. **沙箱侧**（go-judge 已提供）：cgroup 资源隔离、seccomp 拦截危险系统调用、网络默认隔离
3. **注入方式**：`copyIn` 只用 `content`（代码文本内联）或 `fileId`（走 /file 上传），**禁用 `LocalFile`（src 绝对路径）**——避免意外暴露宿主机文件
4. **输出防护**：`copyOutMax` + stdout/stderr `max` 双重限流；结果入库前校验结构（status 必须属于 9 种枚举、stdout 必须存在）
5. **文件清理**：`/file` 上传的 fileId 用后即 `DELETE`，防沙箱磁盘堆积（django-q2 定时任务可兜底）
6. **重试上限**：单测试点最多 2 次，防恶意提交放大判题负载

## 九、异常处理

| 异常 | 处理 |
|---|---|
| 网络错误 / 5xx / 超时 | 重试 1 次 → 仍失败标记「系统错误」，不判为答错 |
| HTTP 401 | token 失效，**不重试**，任务标记失败并告警（提示运维轮换 token） |
| HTTP 400 | 请求体构造错误——代码 bug，记日志排查，不重试 |
| `InternalError` / `FileError` | 重试 1 次 → 仍失败标记「系统错误」 |
| 响应结构非法 | 按系统错误处理（防沙箱异常响应污染业务数据） |

## 十、配置说明

`codemind/settings.py` 已新增（2026-10-18）：

```python
OJ_BASE_URL = os.environ.get('OJ_BASE_URL', 'https://oj.liuwenli.cn')
OJ_AUTH_TOKEN = os.environ.get('OJ_AUTH_TOKEN', '')   # Bearer 凭证，只走环境变量
OJ_RUN_TIMEOUT = 30   # 单次判题 HTTP 超时（秒）
```

环境变量注入方式：
- **Windows 开发**：`setx OJ_AUTH_TOKEN "..."` 后重启终端
- **生产（Linux）**：systemd 服务文件 `Environment=OJ_AUTH_TOKEN=...`，与《异步任务设计.md》部署章节同文件管理

## 十一、待办（随作业功能轮次实现）

- [ ] `teaching/judge.py` 判题客户端封装（submitRun / uploadFile / deleteFile，requests 或标准库 urllib）
- [ ] 沙箱镜像语言实测：确认 python / gcc / java 等编译链是否已装（当前仅实测 /bin/sh），缺什么在服务器补镜像
- [ ] 答案比对器（精确 / 忽略行尾空白 / 忽略全部空白三种策略）
- [ ] 判题 AsyncTask 接入 django-q2（批量判题 + 进度上报）
- [ ] Special Judge（自定义校验器）方案——spj 本身也是一段程序，跑在沙箱里
- [ ] 交互题 `/ws` 支持（远期）

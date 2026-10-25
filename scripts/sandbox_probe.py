"""沙箱外部验收：0=全部完成，1=失败，2=功能通过但内部配置待核验。

凭证从环境读取；公网 /config 关闭后继续功能检查，不输出配置或原始响应。
"""
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'codemind.settings')
django.setup()

from django.conf import settings
from teaching import judge

COMPILE_CMD = ['gcc', 'main.c', '-o', 'main', '-O2', '-std=c11', '-lm']
HELLO_C = '#include <stdio.h>\nint main(void) { printf("HELLO_PROBE\\n"); return 0; }\n'
results = []


def record(item, passed, detail):
    verdict = 'PENDING' if passed is None else ('PASS' if passed else 'FAIL')
    detail = str(detail)
    if settings.OJ_AUTH_TOKEN:
        detail = detail.replace(settings.OJ_AUTH_TOKEN, '[已隐藏]')
    results.append((item, verdict, detail))
    print(f'[{verdict}] {item}：{detail}')


def fetchConfig():
    request = urllib.request.Request(
        settings.OJ_BASE_URL.rstrip('/') + '/config', method='GET')
    with urllib.request.urlopen(request, timeout=settings.OJ_RUN_TIMEOUT) as resp:
        return json.loads(resp.read())


def probeConfig():
    try:
        config = fetchConfig()
    except urllib.error.HTTPError as error:
        if error.code in (403, 404):
            record('公网配置接口保护', True, f'HTTP {error.code}')
            record('内部 cgroup 配置', None, '请从服务器本机核验控制器与 seccomp')
        else:
            record('公网配置接口保护', False, f'HTTP {error.code}，不能确认保护有效')
        return
    except (OSError, ValueError):
        record('公网配置接口保护', False, '连接失败或配置响应格式异常')
        return
    record('公网配置接口保护', False, '匿名请求返回 HTTP 200')
    runner = config.get('runnerConfig') if isinstance(config, dict) else None
    if not isinstance(runner, dict):
        record('内部 cgroup 配置', False, '缺少 runnerConfig')
        return
    controllers = runner.get('cgroupControllers')
    if not isinstance(controllers, list):
        controllers = []
    cpu, memory = 'cpu' in controllers, 'memory' in controllers
    record('内部 cgroup 配置', cpu and memory,
           f'cpu={cpu}, memory={memory}, seccomp={runner.get("seccomp") is True}')


def probeVersion():
    request = urllib.request.Request(
        settings.OJ_BASE_URL.rstrip('/') + '/version',
        headers={'Authorization': f'Bearer {settings.OJ_AUTH_TOKEN}'})
    try:
        with urllib.request.urlopen(request, timeout=settings.OJ_RUN_TIMEOUT) as resp:
            version = json.loads(resp.read())
        record('沙箱版本', isinstance(version, (dict, str)) and bool(version),
               '版本接口返回有效 JSON；不输出完整响应')
    except urllib.error.HTTPError as error:
        record('沙箱版本', False, f'HTTP {error.code}')
    except (OSError, ValueError):
        record('沙箱版本', False, '连接失败或版本格式异常')


def command(args, *, copyIn=None, stdin='', memory=268_435_456):
    return {
        'args': args, 'env': ['PATH=/usr/bin:/bin'],
        'files': [{'content': stdin}, {'name': 'stdout', 'max': 8_388_608},
                  {'name': 'stderr', 'max': 8_388_608}],
        'copyIn': copyIn or {}, 'cpuLimit': 2_000_000_000,
        'clockLimit': 10_000_000_000, 'memoryLimit': memory,
        'procLimit': 32, 'copyOutMax': 33_554_432,
    }


def rawRunDiag(runRequest, label):
    try:
        return judge.submitRun(runRequest)
    except judge.SandboxError:
        record(label, False, '沙箱请求失败；响应原文不输出')
        return None


def probeGcc():
    fileIds = []
    try:
        sourceId = judge.uploadFile(HELLO_C.encode('utf-8'))
        fileIds.append(sourceId)
        version = rawRunDiag([command(['gcc', '--version'])], 'gcc 版本')
        if version is not None:
            record('gcc 版本', version[0]['status'] == 'Accepted', '版本命令执行检查')
        compileRun = command(COMPILE_CMD, copyIn={'main.c': {'fileId': sourceId}})
        compileRun['copyOutCached'] = ['main']
        compiled = rawRunDiag([compileRun], 'gcc 编译')
        if compiled is None:
            return
        binaryId = compiled[0].get('fileIds', {}).get('main')
        if binaryId:
            fileIds.append(binaryId)
        accepted = compiled[0]['status'] == 'Accepted' and bool(binaryId)
        record('gcc 编译', accepted, '检查编译状态和缓存产物')
        if not accepted:
            return
        for index in range(2):
            run = rawRunDiag([command(['./main'], copyIn={'main': {'fileId': binaryId}})],
                             f'C 产物复用第 {index + 1} 次')
            if run is None:
                return
            ok = (run[0]['status'] == 'Accepted'
                  and run[0].get('files', {}).get('stdout') == 'HELLO_PROBE\n')
            record(f'C 产物复用第 {index + 1} 次', ok, '执行状态和预期输出检查')
    finally:
        for fileId in dict.fromkeys(fileIds):
            try:
                judge.deleteFile(fileId)
                record('C 临时文件清理', True, '已删除本次源文件或编译产物')
            except judge.SandboxError:
                record('C 临时文件清理', False, f'删除失败，需重试清理 fileId={fileId}')


def probePython():
    version = rawRunDiag([command(['python3', '--version'])], 'Python 版本')
    if version is not None:
        record('Python 版本', version[0]['status'] == 'Accepted', '版本命令执行检查')
    code = 'import json, re, math, sys, os\nprint("STDLIB_OK")\n'
    run = rawRunDiag([command(['python3', 'main.py'],
                             copyIn={'main.py': {'content': code}})], 'Python 标准库')
    if run is not None:
        record('Python 标准库', run[0]['status'] == 'Accepted'
               and run[0].get('files', {}).get('stdout') == 'STDLIB_OK\n',
               '标准库导入和预期输出检查')


def probePaths():
    for tool in ('gcc', 'python3'):
        run = rawRunDiag([command(['which', tool])], f'{tool} 绝对路径')
        if run is not None:
            record(f'{tool} 绝对路径', run[0]['status'] == 'Accepted'
                   and run[0].get('files', {}).get('stdout', '').startswith('/'),
                   '可执行文件绝对路径检查')


def probeMemoryLimit():
    code = 'data = bytearray(300 * 1024 * 1024)\nprint(len(data))\n'
    run = rawRunDiag([command(['python3', 'main.py'], memory=67_108_864,
                             copyIn={'main.py': {'content': code}})], '内存限制')
    if run is not None:
        record('内存限制', run[0]['status'] == 'Memory Limit Exceeded',
               '申请 300MB，限额 64MB，应返回内存超限')


def summarize():
    failed = sum(verdict == 'FAIL' for _, verdict, _ in results)
    pending = sum(verdict == 'PENDING' for _, verdict, _ in results)
    print(f'\n共 {len(results)} 项：失败 {failed}，待核验 {pending}')
    if pending:
        print('公网功能检查不能代替服务器内部配置核验。')
    return 1 if failed else (2 if pending else 0)


def main():
    results.clear()
    if not settings.OJ_AUTH_TOKEN:
        print('FAIL：OJ_AUTH_TOKEN 环境变量未设置')
        return 1
    for probe in (probeConfig, probeVersion, probeGcc, probePython, probePaths,
                  probeMemoryLimit):
        try:
            probe()
        except (judge.SandboxError, OSError, ValueError, KeyError, IndexError, TypeError):
            record(probe.__name__, False, '探针执行异常；不输出远端响应或异常原文')
    return summarize()


if __name__ == '__main__':
    sys.exit(main())

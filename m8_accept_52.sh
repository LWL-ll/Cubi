#!/bin/bash
# M8 验收 5.2：提交后立刻停掉 qcluster 再启动（一条命令）
#   杀净 → 提交新代码（sleep 6，与历史提交全不同防 repeat）→ 确认 queued
#   → 带 token 重启 long 集群 → 轮询新提交出终态
# 通过标准：重启后判题完成（Q2 恢复消费秒级出终态；消息丢失则
#   pending/retryable 兜底扫描 120~180s 内救活），任务不悬挂。
# 用法：bash m8_accept_52.sh
set -u

BASE=http://127.0.0.1:8000
STUDENT_JAR=/tmp/jar_student
LOG_DIR=C:/Users/Lenovo/AppData/Local/Temp/codemind_accept
OUT_LOG=$LOG_DIR/qcluster_m852.out
ERR_LOG=$LOG_DIR/qcluster_m852.err

S_CSRF=$(awk '$6=="csrftoken"{print $7; exit}' "$STUDENT_JAR")

echo "===== 1) 杀净 qcluster（提交时无 worker 在跑） ====="
for round in 1 2 3; do
  powershell.exe -NoProfile -Command 'Get-CimInstance Win32_Process | Where-Object {$_.CommandLine -like "*qcluster*" -and $_.Name -like "python*"} | ForEach-Object { & taskkill /T /F /PID $_.ProcessId }' >/dev/null 2>&1
  sleep 2
  powershell.exe -NoProfile -Command 'Get-CimInstance Win32_Process | Where-Object {$_.Name -like "python*" -and $_.CommandLine -like "*multiprocessing*"} | ForEach-Object { & taskkill /F /PID $_.ProcessId }' >/dev/null 2>&1
  sleep 2
  left=$(powershell.exe -NoProfile -Command '(Get-CimInstance Win32_Process | Where-Object {$_.Name -like "python*" -and ($_.CommandLine -like "*qcluster*" -or $_.CommandLine -like "*multiprocessing*")} | Measure-Object).Count' 2>/dev/null | tr -d '\r ')
  echo "  第 ${round} 轮杀后残留：$left"
  [ "$left" = "0" ] && break
done

echo "===== 2) 提交新代码（worker 不在线，消息落队列） ====="
C8='#include <stdio.h>\n#include <unistd.h>\nint main(){int v8a,v8b;scanf(\"%d %d\",&v8a,&v8b);sleep(6);printf(\"%d\",v8a+v8b);return 0;}'
sresp=$(curl -s -b "$STUDENT_JAR" \
  -H "X-CSRFToken: $S_CSRF" -H "Content-Type: application/json" \
  -X POST "$BASE/api/submissions/" \
  --json "{\"problemId\":3,\"language\":\"c\",\"code\":\"$C8\"}")
sid=$(echo "$sresp" | python -c "import sys,json;print(json.load(sys.stdin).get('submissionId',''))" 2>/dev/null)
echo "  新提交 sid=$sid（期望数字 id）"
[ -n "$sid" ] || { echo "  提交失败：$sresp"; exit 1; }
sleep 3
echo "  重启前状态：$(curl -s -m 5 -b "$STUDENT_JAR" "$BASE/api/submissions/$sid/" | python -c "import sys,json;print(json.load(sys.stdin).get('status','?'))" 2>/dev/null)（期望 queued）"

echo "===== 3) 重启 qcluster（long 集群 + token） ====="
: > "$OUT_LOG"; : > "$ERR_LOG"
powershell.exe -NoProfile -Command '
  $tok = $env:OJ_AUTH_TOKEN
  if (-not $tok) { $tok = [Environment]::GetEnvironmentVariable("OJ_AUTH_TOKEN","User") }
  if (-not $tok) { $tok = [Environment]::GetEnvironmentVariable("OJ_AUTH_TOKEN","Machine") }
  if (-not $tok) { Write-Output "TOKEN_NOT_FOUND" } else { Write-Output "TOKEN_OK" }
  $env:OJ_AUTH_TOKEN = $tok
  $env:Q_CLUSTER_NAME = "long"
  Start-Process -FilePath "D:\miniconda\envs\codemind\python.exe" -ArgumentList "manage.py","qcluster" -WorkingDirectory "C:\Users\Lenovo\OneDrive\Desktop\codemind" -WindowStyle Hidden -RedirectStandardOutput "C:\Users\Lenovo\AppData\Local\Temp\codemind_accept\qcluster_m852.out" -RedirectStandardError "C:\Users\Lenovo\AppData\Local\Temp\codemind_accept\qcluster_m852.err"
' 2>&1
sleep 8

echo "===== 4) 轮询新提交出终态（期望 TLE，sleep 6 超时限） ====="
end=$((SECONDS + 240))
while [ $SECONDS -lt $end ]; do
  resp=$(curl -s -m 10 -b "$STUDENT_JAR" "$BASE/api/submissions/$sid/")
  st=$(echo "$resp" | python -c "import sys,json;print(json.load(sys.stdin).get('status','?'))" 2>/dev/null)
  sc=$(echo "$resp" | python -c "import sys,json;print(json.load(sys.stdin).get('score','?'))" 2>/dev/null)
  echo "  $(date +%H:%M:%S) 提交$sid status=$st score=$sc"
  case "$st" in
    queued|running|'?') ;;   # '?' = 解析失败，继续重试
    *) echo "===== 5.2 通过：提交$sid 终态 $st score=$sc，判题完成 ====="; exit 0 ;;
  esac
  sleep 10
done
echo "===== 超时 240s 仍未终态 ====="
echo "qcluster 日志：$OUT_LOG 与 $ERR_LOG"
exit 1

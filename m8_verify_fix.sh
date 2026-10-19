#!/bin/bash
# M8 验收 5.1/5.2 修复验证（一条命令）：
#   重新登录学生（账号从库查、密码环境变量注入，不落盘不打印）
#   → migrate（补建回收排程，cluster=long）→ 杀净 qcluster
#   → 带 token 重启 long 集群 → 轮询 26/27 出终态
# 前置（一次性，User 级永久环境变量）：
#   powershell -Command "[Environment]::SetEnvironmentVariable('STUDENT_TEST_PASS','<密码>','User')"
# 预期：27 号（retryable）重启后立即被领取，约 10~20s 出终态；
#       26 号（leased 永挂）由回收扫描在心跳超 60s 后救活，约 90~120s 出 TLE。
# 用法：bash m8_verify_fix.sh
set -u

BASE=http://127.0.0.1:8000
STUDENT_JAR=/tmp/jar_student
LOG_DIR=C:/Users/Lenovo/AppData/Local/Temp/codemind_accept
OUT_LOG=$LOG_DIR/qcluster_m8v.out
ERR_LOG=$LOG_DIR/qcluster_m8v.err
PY=D:/miniconda/envs/codemind/python.exe

echo "===== 1) 重新登录学生（26 号提交者，会话 2 小时过期需刷新） ====="
# 1.1 从库里只读查出 26 号提交者的用户名（学号不硬编码进脚本）
STU_USER=$($PY - <<'EOF'
import sqlite3
con = sqlite3.connect('db.sqlite3')
row = con.execute("SELECT user_id FROM teaching_submission WHERE id=26").fetchone()
name = con.execute("SELECT username FROM users_user WHERE id=?", row).fetchone()
print(name[0])
con.close()
EOF
)
echo "  提交者账号：$STU_USER"
[ -n "$STU_USER" ] || { echo "  查不到 26 号提交者，中止"; exit 1; }

# 1.2 密码从环境变量三级查找（bash env → User → Machine），写入临时文件不打印
PASS_FILE=$LOG_DIR/.stu_pass_tmp
powershell.exe -NoProfile -Command '
  $p = $env:STUDENT_TEST_PASS
  if (-not $p) { $p = [Environment]::GetEnvironmentVariable("STUDENT_TEST_PASS","User") }
  if (-not $p) { $p = [Environment]::GetEnvironmentVariable("STUDENT_TEST_PASS","Machine") }
  if (-not $p) { Write-Output "PASS_NOT_FOUND"; exit 1 }
  [IO.File]::WriteAllText("C:\Users\Lenovo\AppData\Local\Temp\codemind_accept\.stu_pass_tmp", $p)
  Write-Output "PASS_OK"
' 2>&1 | tr -d '\r'
STU_PASS=$(cat "$PASS_FILE" 2>/dev/null); rm -f "$PASS_FILE"
[ -n "$STU_PASS" ] || { echo "  未找到 STUDENT_TEST_PASS 环境变量！先执行：powershell -Command \"[Environment]::SetEnvironmentVariable('STUDENT_TEST_PASS','<密码>','User')\" 再重跑本脚本"; exit 1; }

# 1.3 登录：先访问登录页种 csrftoken 入 jar，再 POST 登录刷新会话
rm -f "$STUDENT_JAR"
curl -s -c "$STUDENT_JAR" "$BASE/" -o /dev/null
S_CSRF=$(awk '$6=="csrftoken"{print $7; exit}' "$STUDENT_JAR")
# 用 json.dumps 拼请求体，密码含特殊字符也安全
body=$($PY -c "import json,sys;print(json.dumps({'username':sys.argv[1],'password':sys.argv[2]}))" "$STU_USER" "$STU_PASS")
loginResp=$(curl -s -b "$STUDENT_JAR" -c "$STUDENT_JAR" \
  -H "X-CSRFToken: $S_CSRF" -H "Content-Type: application/json" \
  -X POST "$BASE/api/auth/login/" --data "$body")
echo "$loginResp" | grep -q '"username"' || { echo "  登录失败：$loginResp"; exit 1; }
echo "  登录成功，会话已刷新"

# 1.4 健康检查：带新会话查提交 26
code=$(curl -s -m 5 -b "$STUDENT_JAR" -o /dev/null -w "%{http_code}" "$BASE/api/submissions/26/")
[ "$code" = "200" ] || { echo "  会话仍不通（HTTP $code），中止"; exit 1; }
echo "  Django 正常（/api/submissions/26/ 200）"

echo "===== 2) migrate（补建回收排程记录，cluster=long） ====="
$PY manage.py migrate
$PY - <<'EOF'
import sqlite3
con = sqlite3.connect('db.sqlite3')
rows = con.execute("SELECT name, cluster, minutes FROM django_q_schedule "
                   "WHERE name='judge-lease-recycle'").fetchall()
print('  排程记录:', rows)
con.close()
EOF

echo "===== 3) 杀净 qcluster ====="
for round in 1 2 3; do
  powershell.exe -NoProfile -Command 'Get-CimInstance Win32_Process | Where-Object {$_.CommandLine -like "*qcluster*" -and $_.Name -like "python*"} | ForEach-Object { & taskkill /T /F /PID $_.ProcessId }' >/dev/null 2>&1
  sleep 2
  powershell.exe -NoProfile -Command 'Get-CimInstance Win32_Process | Where-Object {$_.Name -like "python*" -and $_.CommandLine -like "*multiprocessing*"} | ForEach-Object { & taskkill /F /PID $_.ProcessId }' >/dev/null 2>&1
  sleep 2
  left=$(powershell.exe -NoProfile -Command '(Get-CimInstance Win32_Process | Where-Object {$_.Name -like "python*" -and ($_.CommandLine -like "*qcluster*" -or $_.CommandLine -like "*multiprocessing*")} | Measure-Object).Count' 2>/dev/null | tr -d '\r ')
  echo "  第 ${round} 轮杀后残留：$left"
  [ "$left" = "0" ] && break
done

echo "===== 4) 重启 qcluster（long 集群 + token） ====="
: > "$OUT_LOG"; : > "$ERR_LOG"
powershell.exe -NoProfile -Command '
  $tok = $env:OJ_AUTH_TOKEN
  if (-not $tok) { $tok = [Environment]::GetEnvironmentVariable("OJ_AUTH_TOKEN","User") }
  if (-not $tok) { $tok = [Environment]::GetEnvironmentVariable("OJ_AUTH_TOKEN","Machine") }
  if (-not $tok) { Write-Output "TOKEN_NOT_FOUND" } else { Write-Output "TOKEN_OK" }
  $env:OJ_AUTH_TOKEN = $tok
  $env:Q_CLUSTER_NAME = "long"
  Start-Process -FilePath "D:\miniconda\envs\codemind\python.exe" -ArgumentList "manage.py","qcluster" -WorkingDirectory "C:\Users\Lenovo\OneDrive\Desktop\codemind" -WindowStyle Hidden -RedirectStandardOutput "C:\Users\Lenovo\AppData\Local\Temp\codemind_accept\qcluster_m8v.out" -RedirectStandardError "C:\Users\Lenovo\AppData\Local\Temp\codemind_accept\qcluster_m8v.err"
' 2>&1
sleep 8

echo "===== 5) 轮询 26/27（解析失败按重试处理，不再误判终态） ====="
end=$((SECONDS + 240))
while [ $SECONDS -lt $end ]; do
  doneCount=0
  for sid in 26 27; do
    resp=$(curl -s -m 10 -b "$STUDENT_JAR" "$BASE/api/submissions/$sid/")
    st=$(echo "$resp" | python -c "import sys,json;print(json.load(sys.stdin).get('status','?'))" 2>/dev/null)
    sc=$(echo "$resp" | python -c "import sys,json;print(json.load(sys.stdin).get('score','?'))" 2>/dev/null)
    echo "  $(date +%H:%M:%S) 提交$sid status=$st score=$sc"
    case "$st" in
      queued|running|'?') ;;               # '?' = 解析失败，继续重试
      *) doneCount=$((doneCount + 1)) ;;   # 其余均为终态
    esac
  done
  [ "$doneCount" = "2" ] && { echo "===== 26 与 27 均已终态：5.1/5.2 验收通过 ====="; exit 0; }
  sleep 10
done
echo "===== 超时 240s 仍未全部终态 ====="
echo "qcluster 日志：$OUT_LOG 与 $ERR_LOG"
exit 1

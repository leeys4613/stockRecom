#!/bin/bash
# launchd 무인 실행 래퍼: PROGRESS.md를 읽고 "다음 한 단계"를 진행한다.
# - 중복 실행 방지(락), 한도/오류로 종료되면 다음 주기(30분)에 재시도
# - 단계 2 완료 시 스스로 LaunchAgent를 해제하고 종료
set -u

PROJECT="/Users/iyeongseob/dev/stockRecom"
AGENT_LABEL="com.stockrecom.autorun"
LOCKDIR="$PROJECT/.autorun.lock"
LOGDIR="$PROJECT/logs"
MAX_RUN_SECONDS=10800   # 1회 실행 상한 3시간

export PATH="/Users/iyeongseob/.local/bin:/Users/iyeongseob/.nvm/versions/node/v24.21.0/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export HOME="/Users/iyeongseob"

mkdir -p "$LOGDIR"
LOG="$LOGDIR/autorun.log"
ts() { date '+%Y-%m-%d %H:%M:%S'; }

cd "$PROJECT" || exit 1

# 1) 종료 조건: 단계 2 완료 -> cron 제거
if grep -q '^- \[x\] \*\*2\. 점수 엔진' PROGRESS.md; then
  echo "$(ts) 단계 2 완료 확인 -> LaunchAgent 해제 후 종료" >> "$LOG"
  launchctl bootout "gui/$(id -u)/$AGENT_LABEL" >> "$LOG" 2>&1
  exit 0
fi

# 2) 중복 실행 방지 (stale 락은 PID 확인 후 정리)
if ! mkdir "$LOCKDIR" 2>/dev/null; then
  OLDPID=$(cat "$LOCKDIR/pid" 2>/dev/null || echo "")
  if [ -n "$OLDPID" ] && kill -0 "$OLDPID" 2>/dev/null; then
    echo "$(ts) 이미 실행 중(pid $OLDPID) -> 건너뜀" >> "$LOG"
    exit 0
  fi
  echo "$(ts) stale 락 정리" >> "$LOG"
  rm -rf "$LOCKDIR"
  mkdir "$LOCKDIR" || exit 1
fi
echo $$ > "$LOCKDIR/pid"
trap 'rm -rf "$LOCKDIR"' EXIT

PROMPT='이 프로젝트의 PROGRESS.md를 먼저 읽고, "작업 규칙"을 그대로 따라 "다음 할 일"의 단계를 진행해라.
추가 규칙:
- 이번 실행에서는 PROGRESS.md 기준으로 아직 끝나지 않은 가장 앞 단계부터, 단계 2(점수 엔진)까지만 진행한다. 단계 3 이후는 절대 시작하지 않는다.
- 서버 동작 검증은 라이브 서버를 띄우지 말고 FastAPI TestClient와 pytest로 한다.
- 단계를 끝낼 때마다 테스트를 통과시키고 PROGRESS.md(체크박스, 다음 할 일, 결정 사항, 막힌 점)를 갱신한 뒤 git commit 한다. git push는 절대 하지 않는다.
- 커밋 메시지 끝에 빈 줄 후 "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>" 줄을 붙인다.
- 사람 확인이 필요한 항목(가중치 최종 확정 등)은 합리적인 초기값으로 구현하되 PROGRESS.md "사람 확인 필요"에 기록하고 계속 진행한다.
- 막히면 "막힌 점"에 원인과 시도한 것을 기록하고 종료한다. 질문하지 마라.
- 모든 단계 0~2가 끝나면 PROGRESS.md의 해당 체크박스를 [x]로 바꾸고 종료한다.'

echo "$(ts) ===== 실행 시작 =====" >> "$LOG"

# 3) 절전 방지 + 시간 상한 + 헤드리스 실행
caffeinate -i perl -e 'alarm shift; exec @ARGV' "$MAX_RUN_SECONDS" \
  claude -p "$PROMPT" \
    --permission-mode acceptEdits \
    --allowedTools "Read" "Edit" "Write" "Glob" "Grep" \
      "Bash(git add:*)" "Bash(git commit:*)" "Bash(git status:*)" "Bash(git log:*)" "Bash(git diff:*)" \
      "Bash(.venv/bin/python:*)" "Bash(.venv/bin/pytest:*)" "Bash(.venv/bin/python -m pytest:*)" \
      "Bash(/Users/iyeongseob/Library/Python/3.9/bin/uv pip install:*)" \
      "Bash(ls:*)" "Bash(mkdir:*)" "Bash(cat:*)" "Bash(grep:*)" "Bash(sqlite3:*)" \
    --disallowedTools "Bash(git push:*)" "Bash(rm -rf:*)" "Bash(sudo:*)" "Bash(curl:*)" "Bash(crontab:*)" \
  >> "$LOG" 2>&1
RC=$?

echo "$(ts) ===== 실행 종료 (rc=$RC) =====" >> "$LOG"
exit 0

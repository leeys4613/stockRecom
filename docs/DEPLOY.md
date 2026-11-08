# 배포 가이드 (아이맥 상시 실행 + 폰 접속)

원칙: 서버는 `127.0.0.1:8000`에만 바인딩한다. 외부 노출은 **Tailscale serve**(내 tailnet 안에서만 HTTPS)로만 한다.
공유기 포트포워딩과 `tailscale funnel`(인터넷 공개)은 쓰지 않는다.

## 1. 서버 상시 실행 (launchd)
```
cp /Users/iyeongseob/dev/stockRecom/scripts/com.stockrecom.server.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.stockrecom.server.plist
```
- 확인: `curl -s http://127.0.0.1:8000/health` → `{"status":"ok"}`
- 로그: `logs/server.out`, `logs/server.err`
- 재시작: `launchctl kickstart -k gui/$(id -u)/com.stockrecom.server`
- 중지/해제: `launchctl bootout gui/$(id -u)/com.stockrecom.server`
- 서버가 떠 있는 동안 `caffeinate -i`가 유휴 절전을 막는다. 스케줄러(07:30/10:30/13:30/16:30, 거래일만)는 서버 프로세스 안에서 돈다.
- LLM 분석(`claude -p`)은 이 서버 프로세스가 로그인된 사용자 세션에서 실행될 때만 인증된다 → 로그아웃/사용자 전환 금지.

## 2. Tailscale
1. 아이맥에 Tailscale 설치 후 로그인 (https://tailscale.com/download).
2. 폰에도 Tailscale 앱 설치, **같은 계정**으로 로그인.
3. 관리 콘솔(https://login.tailscale.com/admin/dns)에서 MagicDNS와 **HTTPS Certificates**를 켠다 (PWA는 HTTPS 필요).
4. 아이맥 터미널에서 (CLI 경로가 PATH에 없으면 `/Applications/Tailscale.app/Contents/MacOS/Tailscale`):
   ```
   tailscale serve --bg 8000
   tailscale serve status
   ```
   표시되는 `https://<기기이름>.<tailnet>.ts.net` 주소가 접속 URL이다. (옵션 이름은 버전에 따라 다를 수 있으니 `tailscale serve --help` 확인)
5. 되돌리기: `tailscale serve reset`

## 3. 폰에서 PWA 설치
- iPhone(Safari): 위 https 주소 접속 → 공유 → "홈 화면에 추가".
- Android(Chrome): 접속 → 메뉴 → "앱 설치" 또는 "홈 화면에 추가".
- 오프라인/연결 불가 시 마지막으로 본 화면이 보이며, 하단 "화면 생성" 시각으로 오래된 화면인지 확인한다.

## 4. 점검 체크리스트
- [x] 폰에서 대시보드가 열린다 (Tailscale 켠 상태) — 2026-11-08 확인, PWA 설치 완료
- [ ] Tailscale 끈 상태에서는 접속되지 않는다 (외부 비노출 확인)
- [x] "지금 갱신" 후 1~2분 뒤 새로고침하면 점수가 갱신된다
- [ ] 다음 거래일 07:30 이후 `analyses`에 새 행이 생긴다
- [ ] 며칠 뒤 `/performance`에서 1일 후 결과가 채워진다

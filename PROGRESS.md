# PROGRESS.md — 작업 상태 (세션/컨텍스트가 끊겨도 여기서 재개)

> 설계 기준은 `PRD.md`. 이 파일은 "지금 어디까지 했고 다음에 뭘 하는지"만 기록한다.
> 재개 시 순서: 1) 이 파일 읽기 → 2) `git log --oneline -10`, `git status` 확인 → 3) "다음 할 일"의 첫 항목부터 진행.

## 환경
- Python 3.11 venv: `.venv/` (실행은 `.venv/bin/python`, 패키지 설치는 `uv pip install --python .venv/bin/python <pkg>`)
- uv 경로: `/Users/iyeongseob/Library/Python/3.9/bin/uv`
- 의존성: `requirements.txt` (설치 완료)
- 원격: GitHub `leeys4613/stockRecom` (main)

## 작업 규칙 (무인 실행 포함)
1. **한 번에 한 단계만** 진행한다. 단계 완료 기준을 만족하면 멈추고 이 파일을 갱신한다.
2. 단계 완료 = 테스트 통과 + 실제 실행 확인 + 커밋. 테스트가 실패하면 커밋하지 말고 "막힌 점"에 기록한다.
3. 커밋은 단계(또는 의미 있는 단위)마다. 메시지는 한국어 또는 영어 한 줄 요약.
4. 사람 입력이 필요한 작업(API 키, 가중치 결정, 프롬프트 튜닝, KIS 연동)은 **진행하지 말고** "사람 확인 필요"에 적고 멈춘다.
5. 비밀값(`.env`, API 키)은 절대 커밋/출력하지 않는다. `.env.example`에는 키 이름만 둔다.
6. 확률 표현 금지: 결과는 "우호도 점수(0~100)"로 표기한다 (PRD 2장).
7. 수집 실패는 숨기지 말고 `failed` 상태와 시각을 저장/표시한다.
8. 원격 push는 사용자가 요청할 때만 한다.

## 단계 체크리스트

- [x] **준비**: PRD v2, requirements.txt, .gitignore, venv, git 초기 커밋
- [x] **0. 골격**: `config.py`, `db/`(SQLite 스키마), `main.py`(FastAPI, `/health`), `.env.example`
  - 완료 기준: 서버 기동 + `/health` 200 + DB 파일 생성
- [ ] **1. 매크로 대시보드**: `collectors/macro.py`(7개 지표, 실패 처리), DB 저장, `templates/index.html`(지표 표, 신선도 배지, 다크모드)
  - 완료 기준: 브라우저에서 지표 표시, 일부 실패 시에도 페이지 정상
- [ ] **2. 점수 엔진**: `scoring/`(변동률→가중 합산→0~100, 점수 구간별 비중 규칙), `tests/`
  - 완료 기준: 단위 테스트 통과
  - ⚠ 가중치/임계값 초기안은 기록하되 **사람 확인 필요**
- [ ] **3. 뉴스 + LLM + 스케줄러**: `news.py`, `analyzer/llm.py`, APScheduler(거래일 캘린더)
  - ⚠ Anthropic API 키 필요 (사람이 `.env`에 입력)
- [ ] **4. 예측 기록 + PWA**: 예측 저장, 사후 수익률 채우기, 적중률/Brier, manifest/sw, tailscale serve
- [ ] **5. 레포트 크롤링**: `reports.py`, Top5 후보군
- [ ] **6. KIS 연동**: ⚠ 사람 확인 필요 (앱 키, 모의투자, 제공 항목 사전 조사)

## 다음 할 일
1. 단계 1 시작 (`collectors/macro.py` → DB 저장 → `templates/index.html`)

## 사람 확인 필요 (무인 실행 시 여기서 멈춤)
- (없음)

## 결정 사항 로그
- 2026-11-01: 확률(%) 대신 우호도 점수 사용, 켈리 보류, 고정 비중 규칙 먼저
- 2026-11-01: KIS API는 최후순위(6단계)
- 2026-11-01: Python 3.11은 uv로 venv 구성 (시스템 Python 3.9)
- 2026-11-01: 전원 스케줄 대신 상시 가동 + caffeinate

## 막힌 점 / 메모
- (없음)

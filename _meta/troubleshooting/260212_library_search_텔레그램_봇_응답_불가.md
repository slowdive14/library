---
type: troubleshooting
status: done
description: "텔레그램 봇이 모니터링 알림은 보내지만 사용자의 직접적인 메시지(책 이름)에는 응답하지 않는 문제"
created: 2026-02-12
modified: 2026-02-12
phase: bug-fix
---

# 문제
> 텔레그램 봇에게 책 이름을 직접 입력하거나 말을 걸어도 아무런 반응이 없음. (GitHub Actions의 모니터링 알림은 정상 동작함)

---

# 시도
- `bot.py` 코드 분석: 명령어 핸들러(`/s`, `/a` 등)는 등록되어 있으나 일반 텍스트(plain text) 핸들러가 없음을 확인.
- 배포 환경 확인: GitHub Actions 워크플로우 분석 결과 `monitor.py`만 주기적으로 실행될 뿐, 실시간 응답을 담당하는 `bot.py`는 어디서도 실행되고 있지 않음을 확인.

---

# 원인
- **구조적 원인**: GitHub Actions는 15분마다 실행되는 '배치' 방식이라 실시간 응답 봇(`bot.py`)을 상시 가동할 수 없음.
- **코드적 원인**: `bot.py`에 `/`로 시작하지 않는 일반 메시지를 처리할 `MessageHandler`가 누락됨.

---

# 해결
- **코드 수정**: `bot.py`에 일반 텍스트를 받으면 `/s (검색)` 명령어로 연결해주는 핸들러 추가.
- **배포 방식 변경**: 실시간 응답을 위해 Render(무료 서버) 배포용 설정(`Procfile`, Webhook 모드) 추가 및 가이드 작성.

---

# 관련 파일
- `c:\Users\user\Downloads\library_search\bot.py`
- `c:\Users\user\Downloads\library_search\Procfile`
- `c:\Users\user\Downloads\library_search\.github\workflows\monitor.yml`

---

# 재발 방지
- 상시 응답이 필요한 봇 기능은 GitHub Actions가 아닌 별도 서버(Render 등)에서 운용하도록 설계.
- 사용자 편의를 위해 명시적 명령어 없이도 텍스트를 처리할 수 있게 기본 핸들러를 항상 포함할 것.

---

# 📝 쉬운 설명

### 문제
도서관 봇에게 "코스모스"라고 책 이름을 보내도 묵묵부답인 상태였습니다.

### 원인  
봇이 두 가지 이유로 잠들어 있었습니다. 
1. 봇이 실시간으로 대기하고 있는 '집(서버)'이 없었고 (GitHub Actions는 15분마다 한 번씩만 깨어남), 
2. 봇에게 "슬래시(/) 없이 말하는 건 무시해"라고 설정되어 있었기 때문입니다.

### 해결
어떤 말이든 들어오면 책 검색으로 연결해주도록 봇의 귀를 열어주었고, 24시간 깨어 있을 수 있는 Render라는 무료 집(서버)으로 이사갈 준비를 마쳤습니다.

### 관련 파일
- `c:\Users\user\Downloads\library_search\bot.py`

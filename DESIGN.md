# setup-ctf 설계 문서 — CTF 병렬 풀이 자동화 관리 시스템

> 구현 상태: 이 문서대로 구현됨 (`setup-ctf/`). 구현 중 바뀐 점과 스파이크 결과는 8절과 11절 참고.

## 1. Context

CTF에서 문제마다 파일을 받고, 설명을 프롬프트에 붙이고, tmux 윈도우를 열어 `claude` + `/solve-challenge`를 띄우는 과정을 지금은 손으로 반복한다. 이 시간을 없애고 평소 세팅(분야별 tmux 세션, 문제당 윈도우 1개, 로컬 solver → 원격 플래그)을 그대로 자동 재현하는 것이 목표다.

`/setup-ctf`를 호출한 Claude Code 세션이 **관리 세션**이 되어, 로그인 확인 → 시작 시간 대기 → 30초 폴링 → 다운로드/압축 해제 → 분야별 tmux 세션에 풀이 에이전트 spawn → 사용자 지시(종료, 우선순위) 처리까지 맡는다.

### 확정된 결정 (질문 답변)

| 항목 | 결정 |
|---|---|
| 격리 | 디렉토리 분리 + AGENTS.md 규칙만. OS 수준 격리(bwrap, sandbox)는 하지 않음 |
| 막힘 판정 | 에이전트 자기 선언 (AGENTS.md 규칙 + `ctfm report`) |
| 포기 금지 | 플래그 없이 끝나는 문제는 없음. 막힘 알림은 사용자가 insight를 줄 기회일 뿐이고 에이전트는 계속 푼다. 플래그 없이 멈추려는 시도는 Stop hook이 차단 |
| 플랫폼 | CTFd 내장 어댑터 + 그 외 사이트는 관리 세션이 즉석 어댑터 작성 |
| 사이트에서 이미 풀린 문제 | 실행 중 세션은 알림만, 최초 동기화 때 이미 풀린 문제는 spawn 안 함 |
| 권한 모드 | `bypassPermissions` (`--dangerously-skip-permissions`) |
| 기본 모델 | Opus 5 (`--model` 인자로 변경 가능) |
| 슬롯 반환 | 풀림 + kill 때만. 최종 막힘/질문 대기 세션은 슬롯 유지 |
| 대기 순서 | 인자로 지정한 문제 → solve 수 많은 순 → 배점 낮은 순 |

### 조사로 확인된 사실

- 재사용할 기존 도구는 없음. CTFd 다운로더, Discord 송신 스크립트 모두 신규 작성. 재사용 가능한 것은 `ctf_management_system/.env`의 `DISCORD_WEBHOOK_URL` 키와 기존 스킬 `~/.agents/skills/solve-challenge/SKILL.md`.
- `solve-challenge`는 `$ARGUMENTS`를 자유 형식으로 받고(`[category] [challenge-file-or-url]`), Step 0에서 CTFd 토큰을 물어 플랫폼에 접근하려 한다 → AGENTS.md에서 이 단계를 금지해야 함.
- 과거 CTF 디렉토리는 분야 디렉토리 없이 평평한 구조. 분야별 구조는 이번에 새로 도입. 문제 이름에 공백이 섞인 사례가 있어 slug 정규화가 필요.
- 배포 파일에 가짜 `flag.txt`가 들어 있는 경우가 많음 → 플래그 보고를 파일 존재로 판정하면 안 됨.
- Claude Code 2.1.331: `AGENTS.md`는 cwd나 상위 디렉토리에 `CLAUDE.md`가 하나도 없을 때만 자동 로드됨 → `CLAUDE.md`(`@AGENTS.md` import)를 함께 생성해 확실히 로드.
- 신뢰 대화상자: git 저장소가 아닌 디렉토리에서 한 번 신뢰하면 하위 디렉토리에 상속됨(중첩된 `.git` 제외). 관리 세션이 CTF 루트에서 실행되므로 문제 디렉토리는 상속 대상.
- `/loop`, Cron은 최소 1분 단위 → 30초 폴링은 LLM 루프가 아니라 **독립 데몬 프로세스**가 해야 함. 관리 세션은 `Monitor` 도구로 이벤트 로그를 구독(최대 30분, 만료 시 재시작).
- 전역 설정에 `skipDangerousModePermissionPrompt: true`가 있어 bypass 확인 창은 뜨지 않음.

## 2. 아키텍처

핵심 원칙: **속도가 필요한 경로(폴링 → 다운로드 → spawn)는 LLM을 거치지 않는 결정적 스크립트 `ctfm`이 처리**하고, 관리 세션(LLM)은 판단이 필요한 일만 한다.

```
사용자 ──/setup-ctf──▶ 관리 세션 (Claude Code, cwd = <CTF>/)
                         │  ① ctfm init / check   ② (비-CTFd) adapter.py 작성
                         │  ③ ctfm daemon 기동     ④ Monitor로 events.log 구독
                         │  ⑤ 사용자 지시 → ctfm kill / spawn / prioritize / send
                         ▼
              ctfm daemon (tmux `ctl_<ctf>` 세션, LLM 없음)
                 30초마다: adapter.list → diff → fetch/압축 해제 → 큐 → spawn
                 5초마다 : 슬롯/종료 감지 → 다음 문제 spawn
                         │ tmux new-window
                         ▼
   tmux `pwn_<ctf>` │ `web_<ctf>` │ `rev_<ctf>` │ `crypto_<ctf>` │ `ai_<ctf>` │ `misc_<ctf>`
      윈도우 1개 = 문제 1개 = claude (Opus 5, bypass) + /solve-challenge
                         │ ctfm report solved | retry | blocked | ask
                         ▼
              state.json 갱신 + Discord webhook + events.log
```

| 구성 요소 | 역할 | LLM |
|---|---|---|
| `setup-ctf` 스킬 (SKILL.md) | 관리 세션의 절차서: 셋업, 이벤트 대응, 사용자 지시 해석 | O |
| `ctfm` CLI | init, check, up, daemon, status, spawn, kill, prioritize, send, peek, set, events, notes-sync, notify, report, hook | X |
| 플랫폼 어댑터 | `check` / `list` / `fetch` / `download` (읽기 전용, GET만) | X |
| 풀이 에이전트 | 문제 디렉토리에서 `/solve-challenge` 수행, `ctfm report`로 상태 선언 | O |

## 3. 디렉토리 구조

### 구현 저장소 (이 저장소)

```
.env                          # DISCORD_WEBHOOK_URL (기존)
setup-ctf/                    # ← ~/.claude/skills/setup-ctf 가 가리키는 심볼릭 링크 대상
  SKILL.md
  bin/ctfm                    # python3 진입점 (stdlib만 사용)
  ctfm/cli.py state.py daemon.py tmux.py intake.py notify.py
  ctfm/adapters/base.py ctfd.py
  templates/AGENTS.md CHALLENGE.md adapter_skeleton.py
tests/                        # 단위 테스트 + mock_ctfd.py
```

### CTF 작업 디렉토리 (관리 세션 cwd, 디렉토리 이름 = CTF 이름)

```
<CTF>/
  .env                        # CTFD_TOKEN 등 인증 정보 (chmod 600). 문제 디렉토리에는 절대 복사 안 함
  CTF_NOTES.md                # 관리 세션이 기록하는 이 CTF의 특징
  .ctfm/
    config.json               # url, platform, start, max_agents, priority, model, category_map
    state.json                # 문제별 상태 (flock으로 직렬화)
    events.log                # 한 줄 = 이벤트 1개
    adapter.py                # 비-CTFd일 때만
    downloads/<id>/           # 원본 첨부 파일 보존
    launch/<slug>.sh .prompt  # 윈도우 실행 스크립트와 초기 프롬프트
  pwn/<slug>/                 # ← 풀이 에이전트 cwd
    AGENTS.md  CLAUDE.md      # CLAUDE.md 내용은 `@AGENTS.md` 한 줄
    .claude/settings.json     # 포기 차단 Stop hook (5.6절)
    CHALLENGE.md              # 스크래핑한 문제 설명
    STATE.md                  # 에이전트가 작성 (풀이 논리, 검증 여부)
    BLOCKED.md                # 가장 최근의 막힘 설명 (에이전트가 덮어씀)
    <압축 해제된 문제 파일>
  web/ rev/ crypto/ ai/ misc/
```

- **slug**: 문제 이름에서 영숫자, `-`, `_`만 남기고 공백은 `_`. 충돌하면 `-<id>` 덧붙임.
- **압축 해제**: 첨부가 아카이브 1개면 문제 디렉토리에 바로 풀고(최상위 디렉토리가 하나뿐이면 벗겨냄), 2개 이상이면 `<아카이브 이름>/`별로 푼다. 아카이브가 아닌 파일은 그대로 복사. zip/tar는 stdlib(경로 탈출 차단), 그 외는 `7z`.
- **배포본에 섞인 설정 파일**: 아카이브 안의 `CLAUDE.md`, `AGENTS.md`, `.claude/`, `.mcp.json`은 `*.dist`로 이름을 바꾼다. 우리 AGENTS.md를 덮거나 프로젝트 hook이 실행되는 것을 막기 위함(bypass 모드라 필수).
- **분야 매핑**: 사이트 카테고리를 소문자로 바꿔 별칭 표로 6개 중 하나에 대응(`binary exploitation`→pwn, `reversing`→rev, `ml`/`llm`→ai 등). 대응이 없으면(forensics, osint, blockchain 등) `misc`. 관리 세션이 `config.json`의 `category_map`으로 재정의 가능. 원래 카테고리는 CHALLENGE.md에 남김.

## 4. 스킬 인자

```
/setup-ctf <문제 사이트 URL> --token <ctfd 토큰>
           [--start "2026-11-28 10:00"] [--max-agents N] [--priority "문제A,문제B"] [--model <id>]
```

- `--start` 생략 시 즉시 시작. 타임존 표기가 없으면 로컬(KST).
- `--max-agents` 생략 시 기본 8 `[?]` (가정. 원하는 값 알려주면 변경).
- `--priority`는 이름/slug/ID를 대소문자 무시로 매칭. 아직 공개되지 않은 문제 이름도 기억해 두었다가 나타나면 맨 앞에 배치.
- 비-CTFd 사이트는 `--token` 대신 쿠키/세션 값을 주거나, 관리 세션이 필요한 인증 정보를 질문.

## 5. 동작 흐름

### 5.1 셋업 (스킬 호출 즉시)

1. 사전 점검: `tmux`, `python3`, `7z` 존재, cwd가 CTF 디렉토리인지, `DISCORD_WEBHOOK_URL` 조회(`<CTF>/.env` → 저장소 `.env` 순). 웹훅으로 "셋업 시작" 테스트 메시지 전송.
2. `ctfm init`: `config.json` 생성, 토큰을 `<CTF>/.env`에 저장.
3. `ctfm check`: 플랫폼 판별(CTFd면 `/api/v1/` 응답 형태) 후 로그인 확인(CTFd: `GET /api/v1/users/me`). 실패하면 그 자리에서 사용자에게 보고.
4. CTFd가 아니면: 관리 세션이 사이트(HTML, JS, API 호출)를 분석해 `templates/adapter_skeleton.py`를 채운 `.ctfm/adapter.py`를 작성하고 `ctfm check`로 검증.
5. 규칙 페이지가 보이면 읽어서 `CTF_NOTES.md`에 기록: 플래그 포맷, 금지 행위(브루트포스 등), 인스턴스 방식, rate limit.
6. `ctfm up`으로 데몬을 tmux `ctl_<ctf>` 세션에 기동하고 `Monitor`로 `events.log` 구독 시작.

### 5.2 데몬 루프

- 시작 시간 전: 대기. 시작 10초 전부터 문제 목록이 처음 보일 때까지는 5초 간격으로 조회, 이후 30초 간격.
- 매 폴링: `adapter.list()` → 새 문제마다 `fetch` → 다운로드 → 압축 해제 → `CHALLENGE.md`/`AGENTS.md`/`CLAUDE.md` 생성 → 큐에 추가.
- 5초마다 스케줄링: `점유 슬롯 < max_agents`이면 큐 맨 앞 문제 spawn. 정렬 키는 `(우선순위 지정 순서, -solves, points, id)`.
- 사이트에서 풀림으로 바뀐 문제: 큐에 있으면 제거, 실행 중이면 `SITE_SOLVED` 이벤트만.
- 폴링 실패(401/403, 네트워크)는 `POLL_ERROR` 이벤트. 데몬은 죽지 않고 계속 재시도.

### 5.3 spawn

```
tmux new-session|new-window -d -P -F '#{window_id}' -n <slug> -c <문제 디렉토리> \
     "zsh -ic '<CTF>/.ctfm/launch/<slug>.sh; exec zsh'"
```

`launch/<slug>.sh` 내용:

```
claude --disallowedTools AskUserQuestion,EnterPlanMode --dangerously-skip-permissions \
       --model <model> -n "<ctf>/<cat>/<slug>" "$(cat <slug>.prompt)"
echo $? > <CTF>/.ctfm/launch/<slug>.exit      # 데몬이 claude 종료를 감지하는 표식
```

- 초기 프롬프트: `/solve-challenge <분야> 이 디렉토리의 CTF 문제를 풀어라. 먼저 AGENTS.md와 CHALLENGE.md를 읽어라.`
- 프롬프트를 파일로 빼서 tmux 인용 문제를 피하고, 윈도우는 번호가 아니라 `window_id`(`@N`)로 추적(`renumber-windows on` 설정 대응).
- claude가 끝나도 셸이 남아 `claude --continue`로 이어갈 수 있음.
- `AskUserQuestion`, `EnterPlanMode` 도구는 끈다. 질문 대화상자나 plan 승인 대기는 Stop hook에 걸리지 않고 알림 없이 멈추는 통로다(E2E에서 실제로 발생). 질문은 `ctfm report ask`로만 한다.
- 관리 세션에서 물려받는 `CLAUDECODE`, `CLAUDE_CODE_SESSION_ID` 등은 launch 스크립트와 tmux 호출에서 지운다(안 지우면 풀이 세션이 자식 세션처럼 동작).
- **사용자가 직접 보기**: 세션은 사용자의 기본 tmux 서버에 뜬다. `tmux attach -t pwn_<ctf>`으로 붙으면 대화형 claude 화면이 그대로 보이고 직접 입력할 수 있다. 윈도우 이름 앞에 상태가 붙는다: `✔` 플래그 획득, `!` 막힘 알림 보냄(계속 풀이 중), `?` 사용자 답 대기.

### 5.4 문제 상태

| 상태 | 진입 | 슬롯 |
|---|---|---|
| `queued` | 다운로드 완료 | - |
| `running` | spawn | 점유 |
| `waiting_user` | `ctfm report ask` (토큰/시도 제한 관련 질문) | 점유 |
| `blocked` | `ctfm report blocked` (2회 재검토 후, 3번째 막힘). **종료 상태가 아님**: 알림을 보낸 뒤에도 계속 풀이 중 | 점유 |
| `solved` | `ctfm report solved` | 반환 |
| `killed` | 사용자 지시로 `ctfm kill` | 반환 |
| `exited` | claude 프로세스가 스스로 종료 | 반환, 이벤트로 알림 |
| `site_solved` | 최초 동기화 때 이미 풀려 있음 | spawn 안 함 |

### 5.5 `ctfm report` (풀이 에이전트가 호출, cwd로 문제 식별)

| 명령 | 효과 |
|---|---|
| `report solved --flag '<flag>'` | 상태 `solved`, Discord: 분야/문제명/플래그, 슬롯 반환 |
| `report retry` | 재검토 횟수 +1, 재검토 체크리스트 출력(2회째의 claude는 effort 상향 안내, 11절). 2회째 재검토 후에도 진전이 없어 다시 호출하면 "`report blocked`로 알리고 계속 풀어라" 출력 |
| `report blocked` | 상태 `blocked`, `BLOCKED.md` 전문을 Discord로 전송(2000자 초과 시 분할), 재검토 횟수를 0으로 초기화. 출력: "알림 전송됨. 멈추지 말고 다음 재검토 사이클을 시작하라" |
| `report ask --question '<질문>'` | 상태 `waiting_user`, Discord: 질문 내용 |

`blocked` 이후에도 사이클은 반복된다: 재검토 2회 → 알림 → 재검토 2회 → 알림. 알림마다 갱신된 `BLOCKED.md`가 전송되므로 사용자는 가장 최근 알림 하나만 보면 된다.

### 5.6 포기 차단 Stop hook

각 문제 디렉토리의 `.claude/settings.json`에 Stop hook(`ctfm hook stop`)을 건다. 에이전트가 턴을 끝내려 할 때마다 실행되어 cwd로 문제를 식별하고 다음과 같이 판정한다.

| 문제 상태 | 판정 |
|---|---|
| `solved` | 멈춤 허용 |
| `waiting_user` | 답이 올 때까지 멈춤 허용. 답이 들어오면(`ctfm send` 또는 윈도우 직접 입력 → UserPromptSubmit hook) `running`으로 되돌리고 다시 차단 (11절) |
| 그 외 (`running`, `blocked`) | `{"decision":"block","reason":"플래그 미획득. 포기는 없다. 막혔다면 AGENTS.md 8번 절차를 수행하라."}` 로 차단 |

- 사용자의 Esc 중단에는 Stop hook이 실행되지 않으므로 사용자가 직접 세션에 개입하는 것은 막지 않는다. `ctfm kill`도 그대로 동작.
- Claude Code는 Stop hook 연속 차단을 기본 8회로 제한한다 → `launch/<slug>.sh`에서 `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP`을 큰 값으로 설정 `[?]` (S7).
- `ask`가 멈춤의 우회로가 되지 않도록 AGENTS.md에서 용도를 토큰/시도 제한/rate limit 질문으로 한정.

### 5.7 관리 세션의 런타임 역할

- **이벤트 대응**: `NEW`, `SPAWNED`, `SOLVED`, `BLOCKED`, `ASK`, `SITE_SOLVED`, `EXITED`, `POLL_ERROR`, `FETCH_ERROR`. 사용자에게 한 줄로 전달하고, 오류는 직접 고친다(어댑터 수정, 재로그인 요청).
- **사용자 지시 → 명령 매핑**:
  - "X 끝내라" → `ctfm kill X` (윈도우 종료, 대화는 `claude --resume`으로 복구 가능)
  - "X 먼저 풀어라" → `ctfm prioritize X`. 슬롯이 가득 차 있으면 `ctfm spawn X --now`로 한도를 넘겨 즉시 시작하고 초과 사실을 알림 `[?]` (가정: 명시 지시가 한도보다 우선)
  - "X한테 ~라고 전해" → `ctfm send X "<메시지>"` (tmux `load-buffer` + `paste-buffer`). 막힘 알림을 보고 사용자가 주는 insight도 이 경로로 전달하거나 해당 윈도우에 직접 입력
  - "현황" → `ctfm status` (분야별 표: 상태, 재검토 횟수, 경과 시간, 접속 명령), "X 지금 뭐 해" → `ctfm peek X`
  - "최대 N개로" → `ctfm set max-agents N`
- **CTF 특징 기억**: 알게 된 사실(플래그 포맷, 인프라 특이점, 공통 함정)을 `CTF_NOTES.md`에 누적. `ctfm notes-sync`가 모든 문제 디렉토리의 AGENTS.md 노트 구간을 갱신하고, 실행 중 세션에는 `ctfm send`로 변경 사실을 알림.
- `Monitor`가 30분 만료되면 다시 건다. 그 사이 놓친 이벤트는 `events.log`의 마지막 읽은 줄 번호부터 따라잡는다.

## 6. 문제 디렉토리의 AGENTS.md (템플릿 요지)

1. **작업 범위**: 이 디렉토리 밖의 파일은 읽지도 쓰지도 않는다. 예외는 `ctfm report` 명령과 `/tmp`.
2. **풀이 순서**: 로컬에서 동작하는 solver/exploit을 먼저 만들고, 그다음 원격에서 실제 플래그를 얻는다. 배포본의 가짜 플래그는 보고하지 않는다.
3. **플랫폼 접근 금지**: 문제 사이트에 플래그 제출, 힌트 해제, API 호출을 하지 않는다. `solve-challenge`의 Step 0(CTFd 토큰 요청)은 건너뛴다.
4. **먼저 물어볼 것**: 익스에 ctfd 토큰/계정이 필요하거나, 시도 횟수 제한, rate limit이 있거나, 원격이 죽어 있어 사용자가 인스턴스를 띄워야 하면(로컬 solver를 완성한 뒤에) 시도 전에 `ctfm report ask` 실행 → 채팅에 질문 → 멈춤.
5. **플래그 획득 시**: `ctfm report solved --flag '<flag>'` 실행 후 획득 경로를 채팅에 요약.
6. **STATE.md 유지**: 풀이 논리를 단계별로 적고 각 전제를 `[검증됨]`/`[추정]`으로 표시.
7. **포기는 없다**: 이 문제는 풀리게 만들어져 있고, 플래그를 얻기 전에는 작업이 끝나지 않는다. "풀 수 없다", "여기까지가 한계다" 같은 결론으로 턴을 끝내지 않는다. 플래그 없이 멈추면 Stop hook이 되돌려 보낸다.
8. **막혔을 때**: 새 가설이 없거나 같은 접근이 반복 실패하면
   1. `ctfm report retry` 실행
   2. CHALLENGE.md와 배포 파일을 처음부터 다시 읽는다 (기억에 의존하지 않음)
   3. STATE.md의 풀이 논리를 단계별로 재검증하고 `[추정]` 전제를 실제로 확인해 틀린 곳을 찾는다
   4. novel 표면 점검: 아직 안 본 파일/함수/엔드포인트, 제목·설명·태그의 힌트, 버전과 설정 특이점, 로컬과 원격의 차이
   5. 새 가설로 재개
9. **재검토를 2회 했는데도 진전이 없으면 (insight 요청)**: 사용자에게 도움을 청하는 단계이지 끝이 아니다.
   1. 아래 형식의 막힘 설명을 `BLOCKED.md`에 쓰고 같은 내용을 채팅에도 출력한다. 독자는 이 문제를 전혀 모른다고 가정한다.
      - 문제 설명: 무엇이 주어지고 무엇을 해야 플래그가 나오는지
      - 플래그까지의 전체 경로와 그중 이미 확보한 단계 (근거 포함)
      - 막힌 지점: 어느 단계가 왜 안 되는지, 무엇이 있으면 넘어갈 수 있는지
      - 시도했다가 버린 접근과 버린 이유
      - 사용자에게 묻고 싶은 것, 남은 아이디어
   2. `ctfm report blocked` 실행 (Discord로 전송됨)
   3. **멈추지 않고** 남은 아이디어부터 다음 재검토 사이클(8번)을 이어간다. 사용자의 insight가 채팅으로 들어오면 그것을 최우선으로 검증한다.
10. **이 CTF의 노트**: `CTF_NOTES.md` 내용이 여기에 삽입됨.

### CHALLENGE.md 내용

문제 이름, 원래 카테고리, 배점, solve 수, 설명 본문, 접속 정보(`nc host port`, URL), 태그, 공개된 힌트, 제출 시도 제한(있으면), 첨부 파일 목록. 토큰이 든 다운로드 URL은 적지 않는다.

## 7. 어댑터 인터페이스

```python
check(cfg)        -> {ok, user, team, detail}
list(cfg)         -> [{id, name, category, points, solves, solved_by_me}]
fetch(cfg, id)    -> {description, connection_info, files:[{name, url}], tags, hints, max_attempts}
download(cfg, file, dest)
```

- 공통 HTTP 헬퍼는 **GET만 제공**한다. 제출(`POST /api/v1/challenges/attempt`)과 힌트 해제(`/api/v1/unlocks`)를 구조적으로 호출할 수 없게 함.
- CTFd: `Authorization: Token <t>`, `GET /api/v1/challenges`, `GET /api/v1/challenges/<id>`. 응답 필드명(`solved_by_me`, `connection_info`, `max_attempts`)은 실제 인스턴스로 확인 필요 `[?]`.
- 동적 인스턴스 생성처럼 POST가 필요한 동작은 자동화하지 않고, 풀이 에이전트가 사용자에게 묻는다.

## 8. 불확실한 부분과 검증 방법 (구현 첫 단계에서 스파이크)

스파이크 결과 (2026-11-25, Claude Code 2.1.331): S1, S2, S3, S4, S5, S7 통과. S2는 `claude-opus-5`와 `claude-opus-4-8` 모두 오류 없이 호출됨. S6은 로컬 docker `ctfd/ctfd:latest`(개인전 모드)로 통과: 목록/상세 필드명, 토큰 헤더, 시작 전 403("has not started yet")과 그때도 `users/me`는 200, `files`가 `?token=` 붙은 상대 경로 문자열, 다운로드, 시작 감지 후 수집까지 확인. 차이 1건: 비용 0 힌트도 상세 응답에 내용이 없고 unlock(POST)이 필요하다. 어댑터는 GET 전용이므로 CHALLENGE.md에 "잠긴 힌트(비용 N)"로 존재만 적고, 에이전트는 필요하면 `report ask`로 사용자에게 요청한다. 팀 모드(`teams/me`, 팀 단위 `solved_by_me`)는 미검증 `[?]`.

| # | 불확실한 점 | 검증 방법 | 실패 시 대안 |
|---|---|---|---|
| S1 `[?]` | `claude "/solve-challenge ..."` 위치 인자로 스킬이 실행되는지 (문서 미확인) | 임시 디렉토리에서 tmux로 실행해 확인 | 세션 기동 후 `tmux send-keys`로 슬래시 명령 입력 |
| S2 `[?]` | Opus 5의 정확한 모델 ID (`claude-opus-5` 추정, 문서의 `opus` 별칭은 Opus 5.5) | `claude --model <id> -p "hi"` | 사용자가 평소 쓰는 ID를 `config.json` 기본값으로 |
| S3 `[?]` | 문제 디렉토리에서 신뢰 대화상자가 뜨지 않는지 (배포본에 `.git`이 있으면 뜰 수 있음) | CTF 루트 신뢰 후 하위 디렉토리에서 기동 | spawn 5초 뒤 `capture-pane`으로 감지해 `TRUST_PROMPT` 이벤트, 관리 세션이 Enter 전송 |
| S4 `[?]` | `CLAUDE.md`의 `@AGENTS.md` import로 규칙이 로드되는지 | 세션에 "AGENTS.md 3번 규칙이 뭐냐" 질문 | AGENTS.md 내용을 CLAUDE.md에 직접 기록 |
| S5 `[?]` | `Monitor`로 `tail -F events.log` 구독과 만료 후 재시작 | mock 이벤트를 써 넣고 관리 세션 반응 확인 | 관리 세션이 `/loop 1m ctfm events --new` 로 폴링 |
| S6 `[?]` | CTFd API 응답 형태 | 실제 CTFd 인스턴스(대회 사이트 또는 로컬 docker)로 `ctfm check`/`list` | 어댑터 필드 매핑 수정 |
| S7 `[?]` | 문제 디렉토리 `.claude/settings.json`의 Stop hook이 신뢰 재확인 없이 로드되는지, `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP`으로 8회 제한이 풀리는지 | 더미 디렉토리에서 플래그 없이 멈추게 유도해 9회 이상 차단되는지 확인 | hook을 `--settings <파일>`로 전달, 제한이 안 풀리면 데몬이 유휴 세션을 감지해 `ctfm send`로 재개 프롬프트 주입 |

## 9. 구현 순서와 검증

1. **스파이크 S1~S4, S7** → 결과에 따라 5.3절 spawn 방식과 5.6절 hook 전달 방식 확정.
2. **`ctfm` 코어** (state, 정렬, slug, 분야 매핑, 압축 해제) → 검증: 단위 테스트(정렬 순서, slug 충돌, 경로 탈출 아카이브 거부, `*.dist` 개명, 상태 전이).
3. **CTFd 어댑터 + intake** → 검증: `tests/mock_ctfd.py`(고정 JSON과 zip을 내주는 로컬 HTTP 서버)로 `ctfm check`/`list`/다운로드, 생성된 디렉토리 트리와 CHALLENGE.md 확인.
4. **tmux + 데몬 + 스케줄러** → 검증: 에이전트 명령을 `sleep` 더미로 바꾼 설정으로 mock 서버에 문제 5개, `max_agents=2` → 2개만 spawn, 우선순위 순서, 폴링 중 문제 추가 시 30초 내 큐 진입, `ctfm kill` 후 다음 문제 spawn 확인.
5. **`ctfm report` + Stop hook + Discord** → 검증: 더미 문제 디렉토리에서 `solved`/`retry`×4/`blocked`/`ask` 호출, 실제 웹훅 수신과 `state.json`, 슬롯 반환 여부 확인. `ctfm hook stop`에 상태별 입력을 넣어 `running`/`blocked`는 차단, `solved`는 허용, `waiting_user`는 1회만 허용되는지 확인. `blocked` 후 재검토 횟수가 0으로 돌아가고 상태가 종료로 처리되지 않는지 확인.
6. **SKILL.md + 템플릿** → 검증: 과거 문제 하나를 mock 서버에 올리고 `/setup-ctf`를 처음부터 실행. 관리 세션이 로그인 확인 → 데몬 기동 → 실제 claude 에이전트 spawn → AGENTS.md 규칙 준수(플랫폼 미접근, `ctfm report` 호출)까지 확인. 원격 주소를 일부러 죽여 둔 문제로 에이전트가 포기 선언 없이 재검토 → `blocked` 알림 → 계속 진행하는지도 확인.
7. **관리 지시** → 검증: 관리 세션에 "X 끝내", "Y 먼저", "현황" 지시 후 tmux 상태 대조.
8. `~/.claude/skills/setup-ctf` 심볼릭 링크 생성.

## 10. 범위 밖 (요청에 없어 넣지 않음)

- 플래그 자동 제출, 힌트 해제, 인스턴스 자동 생성
- OS 수준 격리, 시간 기반 막힘 감시 (Stop hook은 포기 차단 용도로만 사용하고, 막힘 판정 자체는 에이전트 자기 선언)
- Codex 등 다른 에이전트 CLI 지원, writeup 자동 생성

## 11. 구현 중 바뀐 점

- **`ctfm up` 추가**: 데몬을 tmux `ctl_<ctf>:daemon`에 띄우는 명령. 관리 세션의 환경 변수가 tmux 서버로 새지 않게 `ctfm`이 직접 띄운다.
- **질문 도구 차단**: 5.3절 참고. Stop hook만으로는 질문 대화상자에서 조용히 멈추는 것을 막지 못했다.
- **원격 접속 불가도 `ask` 사유로 추가**: 원격이 죽어 있고 로컬에서 할 일이 없으면 재검토를 반복해도 진전이 없으므로 바로 사용자에게 알린다. 답을 받은 뒤에는 다시 포기 차단이 걸린다.
- **윈도우 이름 상태 표시**(`✔`, `!`, `?`)와 `ctfm status`의 접속 명령: 사용자가 tmux에서 진행 상황을 바로 보기 위함.
- **기존 디렉토리 보호**: `<분야>/<문제이름>/`이 이미 있으면 덮어쓰지 않고 `FETCH_ERROR`로 알린다.
- **zip 실행 권한 복원**: Python `zipfile`은 권한을 복원하지 않아 바이너리의 실행 비트가 사라지므로 직접 복원한다.
- **`ctfm init` 재실행**: 준 값만 바꾼다(토큰 갱신용).
- **잠긴 힌트 표기** (실제 CTFd 검증 후): 비용 0 힌트도 unlock 전에는 내용이 없어서, CHALLENGE.md에 존재와 비용만 적고 필요하면 `report ask`로 사용자에게 요청하게 했다.
- **오프라인 문제 규칙** (`/setup-ctf` E2E 후): "원격에서 얻은 플래그만 보고"를 원격이 있는 문제로 한정했다. 접속 정보가 없는 문제에서 에이전트가 정답을 찾고도 보고하지 않고 질문한 사례가 있었다.
- **codex 풀이 에이전트** (2026-11-25): 설정 `agent`(`ctfm init --agent`, `ctfm set agent`, 문제별 `ctfm spawn --agent`)로 claude와 `insane-codex`를 고른다. resume은 그 문제를 풀던 에이전트로만 이어 간다. codex 프롬프트는 `$solve-challenge`로 시작하고 규칙은 codex가 원래 읽는 `AGENTS.md`로 전달된다. 모델은 `codex_model`(비우면 codex 설정 기본값).
  - **codex에는 Stop hook이 없다.** codex의 Stop hook 자체는 동작한다(`-c`로 inline 등록 시 11회 연속 차단 확인, 출력 형식은 Claude와 같음). 그러나 `-c`, `--enable/--disable`, `--dangerously-bypass-hook-trust` 중 하나라도 주면 공유 app-server 대신 내장 모드로 뜬다. 이 insane-codex 0.170 빌드에는 내장 모드용 `codex-code-mode-host`가 없어서 셸 명령이 전부 실패한다. 파일(`.codex/hooks.json`)로 넣어도 hook마다 신뢰가 필요하고, 신뢰 우회 옵션은 역시 내장 모드를 강제한다. 사용자 결정으로 hook 없이 `insane-codex [resume --last] [-m 모델]`만 쓴다. 대신 관리 세션이 입력 대기 중인 codex를 `ctfm send`로 다시 진행시킨다.
  - 질문 도구: `request_user_input`은 기본 모드에서 호출이 실패한다. 하지만 모델 카탈로그가 붙여 주는 `request_user_input_async`는 동작하고 끌 수 없다(PreToolUse hook으로는 막을 수 있었지만 위 이유로 못 씀). AGENTS.md 4번 규칙으로만 막는다.
  - `ctfm send`는 `waiting_user`를 `running`으로 되돌린다(Claude는 Stop hook이 하던 일).
  - codex 기본 모델은 `gpt-5.6-sol`(ctfm 설정 `codex_model`. `~/.codex/config.toml`의 기본값과 별개). 2026-11-27에 `gpt-6-sol`로 바뀜(아래).
  - **유휴 재개**: Stop hook 대신 데몬이 15초마다 화면을 확인한다. 하단에 작업 중 표시(`esc to interrupt`)가 없고 화면이 `idle_nudge`초(기본 120) 동안 그대로면 `ctfm send`와 같은 방식으로 계속 진행 메시지를 넣고 `NUDGED` 이벤트를 남긴다. claude에도 적용되고, `waiting_user`와 사용자가 보고 있는 윈도우는 건너뛴다. `ctfm set idle-nudge 0`이면 끈다.
  - codex의 셸 명령은 공유 app-server 환경에서 실행돼 실행 스크립트의 `PATH`가 전달되지 않는다. AGENTS.md는 `ctfm` 전체 경로를 쓰게 한다.
  - `[?]` 공유 app-server 모드에서는 명령을 공식 codex 0.172 app-server가 실행하므로, insane 패치가 실제 풀이에 적용되는지 불확실하다.
- **King of the Hill** (2026-11-25): 사이트 카테고리나 문제 이름이 `koth`/`king of the hill`이면 `koth` 유형(7번째 분야, `koth/`, tmux `koth_<ctf>`). 대기 순서 최우선(슬롯 공유).
  - 모델 체인 `koth.chain`(config): `codex gpt-6.2-astra`(fast_rounds 3) → `claude claude-mythos-5-2` → `claude claude-opus-5-5`. 데몬이 15초마다 화면을 보고, 작업 중이 아닐 때 마지막 8줄에 사용량 한도나 안전 차단 문구가 있으면 다음 항목으로 다시 띄운다(같은 에이전트면 resume). `[?]` 실제 한도/차단 화면 문구는 일부만 확인했고 정규식은 추정이다.
  - fast: codex TUI의 `/fast`는 토글이고 작업 중에도 즉시 적용된다(`Service tier set to priority/default`, 하단 표시에 `fast`). 데몬이 하단 표시를 읽어 원하는 상태(보고된 라운드 ≤ fast_rounds)와 다르면 키 입력으로 `/fast`를 넣는다. `-c service_tier=...`는 내장 모드를 강제해서 쓸 수 없다.
  - 사이트 접근 위임: 사용자 결정으로 KotH 에이전트는 사이트에 직접 접근해 라운드를 확인한다(이후 제출까지 자동화 예정). 문제 디렉토리에 `CTF_ACCESS.env`(CTF .env에서 Discord webhook을 뺀 값 + CTF_URL, CHALLENGE_ID, 권한 600). 제출 절차는 `templates/KOTH_STRATEGY.md`가 비어 있는 동안 ask로 묻는다.
  - `ctfm report round --round N`, `ctfm spawn --model`(KotH에 쓰면 자동 전환과 fast 조정을 끔), KotH용 Stop hook/유휴 재개 문구.
  - 모델 전환 때 이전 셸이 늦게 쓰는 종료 표식과 섞이지 않게 종료 표식 파일을 실행마다 새로 만든다.
- **이벤트 구독은 `command grep`** (`/setup-ctf` E2E 후): Claude Code 셸의 `grep`은 내장 ugrep으로 바뀌는데, `--line-buffered`를 줘도 마지막 한 줄을 다음 줄이 올 때까지 내보내지 않는다. 그래서 뒤따르는 이벤트가 없는 ASK가 관리 세션에 4분 넘게 전달되지 않았다.
- **동시 풀이와 effort 상향** (2026-11-27, 사용자 결정: 퍼블 점수 때문에 속도가 중요):
  - **모델 기본값**: claude `claude-opus-5` effort `xhigh`(`claude --effort`), codex `gpt-6-sol` effort `medium` + fast. 설정 `effort`, `codex_effort`, `codex_fast`. KotH는 모델 체인을 따르고 effort를 지정하지 않는다.
  - **동시 풀이**: `DUAL_CATEGORIES = (pwn, rev)` 문제는 윈도우 하나를 좌우로 나눠 왼쪽에 codex, 오른쪽에 claude를 띄운다(`split-window -h -b`). 문제별로 `ctfm spawn --agent dual|claude|codex`로 바꿀 수 있다. 슬롯은 문제당 하나.
    - 상태 구조: 문제 기록이 곧 주 에이전트(claude)의 pane 기록이고, codex는 `peer`에 같은 키(`agent`, `pane`, `exit_file`, `retries`, 유휴 판정 값)로 들어간다. tmux 입력과 화면 캡처는 `window_id`가 아니라 `pane` id로 한다(윈도우 대상은 활성 pane으로 가서 사용자가 pane을 옮기면 엉뚱한 쪽에 들어간다).
    - 같은 디렉토리를 쓴다(로컬 재현 환경과 배포 파일을 공유). 기록 파일은 `STATE.<에이전트>.md`처럼 이름을 나눈다(AGENTS.md 1번의 동시 풀이 문단, pwn·rev에만 렌더링).
    - `ctfm report`를 부른 쪽 구분: 실행 스크립트가 `CTFM_AGENT`를 export한다. claude의 셸에는 전달되고, codex의 셸은 공유 app-server에서 돌아 전달되지 않으므로 변수가 없으면 codex로 본다. `CLAUDECODE` 유무로는 구분할 수 없다(app-server 환경에 claude 변수가 남아 있는 것을 확인).
    - 한쪽이 `solved`를 보고하면 다른 쪽에 종료 메시지를 넣는다. 종료 감지는 pane별이고 둘 다 끝나야 `exited`. codex가 `ask`한 경우 claude의 Stop hook은 멈춤을 허용하지 않는다(`asker`).
  - **codex effort/fast**: `-p <프로필>`도 `-c`처럼 내장 모드를 강제해 셸 도구가 실패한다(확인). 그래서 데몬이 하단 표시(`GPT-6-Sol high fast ·`)를 읽어 설정과 다르면 맞춘다. effort는 `/model` → Enter(커서가 현재 모델) → effort 메뉴에서 커서 이동 → `s`(session). 메뉴에서 Enter나 숫자 키는 `~/.codex/config.toml`의 기본값을 바꾸므로 쓰지 않는다. 작업 중에도 적용된다(확인). `/fast`는 config의 `service_tier`도 바꾼다(확인, 피할 방법 없음). `[?]` effort 변경이 진행 중인 턴에 바로 반영되는지는 미확인. `codex_effort`는 low/medium/high/xhigh/max를 받는다(2026-11-27 추가). 하단 표시는 `xhigh`, `max` 그대로 나오고, max는 메뉴의 "More reasoning…"에서 Enter로 하위 메뉴(Advanced Reasoning: Max, Ultra)를 연 뒤 `s`로 고른다. max로 바뀐 직후 2초쯤은 배너가 하단 표시를 가린다. 실제 codex에서 low/medium/high/xhigh/max 사이 전환과 config 불변을 확인했다. ultra는 넣지 않았다.
  - **effort 상향**: 재검토 한도를 3 → 2로 줄였다. 2회째 `report retry`에서 claude(KotH 제외, 아직 max가 아닐 때)는 "STATE.md를 갱신하고 턴을 끝내라"는 안내를 받고 `escalate=pending`이 된다. Stop hook은 `escalate`가 있는 동안 멈춤을 허용한다. 데몬은 3초 간격으로 화면을 보고, 작업 중 표시가 두 번 연속 없으면 `/effort max`와 `/compact`를 키 입력으로 넣고, compact가 끝나면(compact 중에는 `esc to interrupt`가 보인다, 확인) 엄밀한 재검토 지시를 붙여 넣는다(`ESCALATED`). `effort=max`가 문제에 기록되어 resume 때도 유지된다. 3번째 막힘은 기존 `blocked` 알림이다. codex는 상향 대상이 아니다(2026-11-27에 codex도 대상으로 바꿨다, 아래 항목).
- **Discord 포럼 채널, KotH 라운드 요약, 경쟁형 KotH** (2026-11-27, 사용자 결정: 알림이 한 채널에 섞이면 따라가기 어려움):
  - webhook은 포럼 채널의 것을 쓴다. 문제마다 `[분야] 문제이름` 포스트를 만들어 그 문제의 알림을 모으고, `ctfm notify`는 대회 이름의 공통 포스트로 보낸다. 포스트 id는 `state.json`의 `threads`에 둔다.
  - 확인한 API 동작: `?wait=true` + 본문 `thread_name` → 새 포스트, 응답의 `channel_id`가 포스트 id. `?thread_id=<id>` → 이어 붙임(204). 둘 다 없으면 400(220001). 없는 포스트는 400 Unknown Channel(10003)이라 그때는 새로 만든다. 일반 텍스트 채널 webhook은 스레드를 만들 수 없어 지원하지 않는다.
  - 동시에 온 첫 알림 둘이 포스트를 두 개 만들지 않게 전송을 `notify.lock`으로 직렬화한다(상태 잠금과 별개라 데몬을 막지 않는다).
  - `ctfm report round`에 `--summary`(필수)를 추가해 라운드마다 진행 요약을 그 문제의 포스트로 보낸다.
  - `KOTH_STRATEGY.md` 7.5: 다른 팀의 제출물에 따라 점수가 달라지는 문제면 메인이 claude로 전환된 뒤부터(초반 codex 세션은 점수 확보에만 집중, 사용자 결정) 메인은 기존 전략을 계속하고, 상대 전략 분석과 대응 방안 수립을 맡는 subagent 하나를 병렬로 돌린다(산출물 `OPPONENTS.md`, 제출과 후보 수정은 하지 않음). `[?]` codex(Astra)가 하위 에이전트를 실제로 띄우는지는 확인하지 못했다. 못 쓰면 메인이 직접 분석하도록 적어 두었다.
- **KotH 메인 전환** (2026-11-27, 사용자 결정: codex 사용량이 claude보다 적어 Astra를 오래 쓸 수 없음):
  - Astra(fast)가 초반 메인, 라운드 4부터 Mythos 5.2(effort high, 막히면 Opus 5.5)가 맡는 것이 목표. 체인 첫 항목의 `successor_round`(3)가 후임을 띄우는 라운드, `fast_rounds`(4)는 전환이 늦어져도 Astra가 fast를 유지하는 마지막 라운드다. 체인 항목에 `effort`를 둘 수 있다.
  - 보고된 라운드가 `successor_round` 이상이 되면(라운드 3 동안 미리 준비시킨다) 데몬이 체인의 다음 항목을 **후임**으로 메인의 오른쪽 pane에 띄운다(`SUCCESSOR`). 동시 풀이의 `peer` 구조를 그대로 쓰고 `role=successor`로 구분한다. 후임은 AGENTS_KOTH.md 8번에 따라 제출 없이 Astra의 작업을 확인하고 `HANDOVER.md`를 쓴 뒤 `ctfm report ready`로 알린다(`READY`, Discord).
  - 전환은 관리 세션이 `ctfm handover X`로 한다: Astra pane 종료, 후임 기록을 메인으로 올림, 후임에게 전환 메시지(현재 라운드 확인과 보고, 미제출이면 제출, 경쟁형이면 상대 분석 subagent 시작). 언제 전환할지(목표는 Astra의 라운드 3 제출 뒤 라운드 4 시작 시점, 준비가 늦으면 그 뒤 라운드)는 화면을 봐야 알 수 있어 데몬이 아니라 관리 세션이 판단한다.
  - Stop hook: 후임은 `ready` 전에는 준비를 계속하라는 문구로 차단하고, `ready` 뒤에는 전환을 기다리며 멈추게 둔다. 유휴 재개 메시지는 후임에게 넣지 않는다.
  - 모델 사용 불가 감지는 pane별이다. 메인(Astra)이 막혔는데 후임이 있으면 후임에게 바로 넘기고, 후임이 막히면 후임 pane만 체인의 다음 모델로 다시 띄운다.
  - `ctfm report`를 부른 쪽은 `CTFM_AGENT`가 가리키는 에이전트의 pane, 없으면 codex pane으로 찾는다(메인이 codex이고 후임이 claude인 배치에도 맞게 일반화).
- **플래그 자동 제출 허용** (2026-11-27, 사용자 결정: 속도 때문에 자동 제출이 필요할 때가 있음):
  - "절대 제출하지 않는다"를 "사용자가 명시적으로 허용하면 관리 세션에 한해 제출한다"로 바꿨다. 풀이 에이전트 규칙(사이트에 접근하지 않음)은 그대로다.
  - `ctfm submit <문제> [--flag F] [--approved]`: 설정 `auto_submit`이 켜져 있거나(`ctfm set auto-submit 1`, 대회 전체 허용) `--approved`(이 제출만 허용)일 때만 동작한다. 문제 디렉토리 안에서 부르면 거부한다(풀이 에이전트 차단). 같은 플래그는 다시 제출하지 않고, 결과를 문제 기록의 `submissions`, `SUBMITTED` 이벤트, Discord 포스트에 남긴다.
  - 어댑터에 `submit(http, cid, flag)`(선택)과 `Http.post_json`(제출 전용)을 추가했다. CTFd는 `POST /api/v1/challenges/attempt`. `[?]` 실제 CTFd에 제출해 보지는 않았다. 응답의 `status` 값(`correct`, `incorrect`, `already_solved`, `ratelimited`, `paused`)은 기억에 의존한 것이라 첫 대회에서 확인이 필요하다.
  - 데몬이 자동으로 제출하지 않고 관리 세션이 `SOLVED`마다 명령을 실행하게 한 이유: 오답 뒤 재제출 여부, 제출 횟수 제한, 플래그 형식 확인처럼 판단이 필요한 경우가 있어서다.
- **질문 대기는 답이 올 때까지 유지** (2026-11-27): 전에는 Stop hook이 멈춤을 1회 허용하면서 상태를 바로 `running`으로 되돌렸다. 그러면 데몬이 유휴로 판정해 120초 뒤 "계속 진행하라"를 넣어, 질문하고 기다리던 에이전트가 혼자 풀기 시작했다(CLI로 풀기 어려운 문제를 먼저 묻게 한 규칙과 충돌). 이제 Stop hook은 `waiting_user`인 동안 멈춤을 계속 허용하고, 문제 디렉토리의 UserPromptSubmit hook(`ctfm hook prompt`)이 답이 들어온 시점에 `running`으로 되돌린다. hook이 없는 codex는 전과 같이 `ctfm send`가 되돌린다.
- **에이전트 정상 종료** (2026-11-27, 사용자 결정: tmux로 그냥 죽이지 않는다): `ctfm kill`, KotH 전환(`handover`), 모델 체인 전환은 pane이나 윈도우를 지우기 전에 `stop_agent`로 에이전트를 끝낸다. Escape로 턴을 끊고 → (codex) `/stop` → `/exit` → 실행 스크립트가 종료 표식 파일을 쓸 때까지 대기(설정 `exit_wait`, 기본 20초). 끝나지 않으면 그대로 지우고 이벤트에 "강제 종료"를 남긴다.
  - 확인한 동작: codex는 `/exit`만 보내면 "Disconnected from this task. Any running work continues."라며 화면만 끊기고 턴과 백그라운드 터미널이 app-server에서 계속 돈다. Escape로 턴을 끊고 `/stop`("Stopping all background terminals")을 한 뒤 나가야 남는 것이 없다. claude는 백그라운드 작업이 있으면 `/exit`에 확인 창("Background work is running")이 뜨고 기본 선택 "Exit and stop tasks"에서 Enter를 누르면 끝난다. 작업 중이던 실제 codex와 claude 모두 약 3초에 종료 코드 0으로 끝나고 남은 프로세스가 없었다.
- **풀린 문제 세션 정리** (2026-11-27, 사용자 결정): `solved` 보고 뒤 데몬이 10초 이상 지나고 에이전트가 작업 중이 아닐 때(최대 90초 대기) `stop_agent`로 끝내고 윈도우를 지운다(`CLOSED`). 채팅 요약은 윈도우와 함께 사라지므로 에이전트는 풀이 경로를 `WRITEUP.md`에 적는다. 대화는 `ctfm spawn X --now --resume`으로 다시 열 수 있다.
- **멈춤 방지를 `/goal`로 교체** (2026-11-27, 사용자 결정: 계속 시도시키는 장치를 단순하게): 초기 프롬프트를 `/goal <목표>`로 바꾸고, 5.6절의 포기 차단 Stop hook과 `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP` 설정을 없앴다. claude와 codex 모두 같은 방식이라 codex도 처음으로 스스로 이어 가게 됐다. 데몬의 유휴 재개 메시지(120초)는 목표가 일시 정지되거나 해제된 경우의 보조로 남긴다.
  - 목표 문구(`daemon.py`의 `GOAL*`): jeopardy는 "플래그를 얻어 `ctfm report solved`를 실행해 기록됐다는 출력을 확인했다", KotH는 "대회가 끝날 때까지 매 라운드 제출", 후임은 "`ctfm report ready` 실행". 판정 모델은 대화에 드러난 것만 보므로 조건을 명령 실행으로 잡았다. 스킬은 프롬프트 맨 앞의 `/solve-challenge` 호출 대신 목표 문장 안에서 이름으로 가리킨다(claude는 Skill 도구로 스스로 부른다).
  - 확인한 동작 (Claude Code 2.1.332, insane-codex 0.170): CLI 인자로 준 `/goal ...`이 바로 실행된다(둘 다). claude는 Stop hook으로 구현돼 있어, 목표 미달성인데 도구 없는 턴이 9번 이어지면 "Goal paused · send a message to continue"로 멈춘다(haiku로 약 20초). 메시지가 들어오면 이어 간다. `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP`을 크게 두면 이 일시 정지가 일어나지 않고 계속 돌기 때문에 설정을 없앴다. 목표는 `/compact`와 Esc 뒤에도 남고, 새 `/goal`을 입력하면 교체된다(379자 한 줄 키 입력 확인). codex는 사용자 답이 필요하면 질문 뒤 몇 턴 지나 목표를 blocked로 두고 멈추며, 메시지가 들어오면 이어 간다. codex 명령은 `/goal [<objective>|clear|edit|pause|resume]`.
  - 3단계 막힘 규칙과의 관계: 1번째(재검토)와 3번째(BLOCKED 알림)는 에이전트가 실행하는 명령이라 영향이 없다. 2번째(effort 상향)는 에이전트가 멈춰야 데몬이 `/effort max`, `/compact`를 넣을 수 있는데, 목표가 남아 있어 바로 멈추지 못하고 위의 일시 정지까지 짧은 턴이 이어진 뒤 진행된다. 목표는 compact 뒤에도 남아 재검토 지시 메시지로 다시 이어진다. 질문(`ask`) 대기와 후임의 전환 대기도 같은 방식으로 멈춘다.
  - `ctfm hook stop`은 아무것도 하지 않는 명령으로 남겼다(예전 문제 디렉토리의 Stop hook 설정이 오류로 턴을 막지 않게). UserPromptSubmit hook(`hook prompt`)은 그대로 쓴다.
  - `[?]` 판정 모델이 "Impossible"로 판정하면 목표가 해제된다(문서). 그 뒤에는 유휴 재개 메시지만 남는다. `[?]` codex의 목표가 resume(`codex resume --last`) 뒤에도 남는지는 확인하지 못해, resume 때도 `/goal`을 다시 준다. `[?]` codex 목표 문장 안의 `$solve-challenge`가 스킬을 실제로 불러오는지 미확인.
- **포럼 포스트 태그** (2026-11-27, 사용자 결정: 분야 태그로 관리, 봇 토큰을 계속 사용): 포스트를 만들 때 포럼의 태그 중 이름이 그 문제 분야와 같은 것(대소문자 무시, 없으면 `misc`)을 `applied_tags`에 넣는다. 공통 포스트는 `misc` 태그를 쓴다.
  - 태그 목록은 `.env`의 `DISCORD_BOT_TOKEN`으로 읽는다: webhook 조회(`GET` webhook URL)로 `channel_id`를 얻고 `GET /channels/<channel_id>`(`Authorization: Bot <토큰>`)의 `available_tags`에서 이름 → ID를 얻는다. 새 포스트를 만들 때만 조회하고 저장해 두지 않는다(문제당 한 번이라 드물고, 태그를 바꿔도 바로 반영된다). 봇 권한은 채널 보기면 된다. 글은 계속 webhook으로 쓴다.
  - 토큰이 없으면 태그 없이 만든다. 조회에 실패하면 태그 없이 만들고 `NOTIFY_ERROR`를 남긴다(알림은 잃지 않는다).
  - 확인한 것 (실제 포럼): `applied_tags`는 태그 이름이 아니라 ID만 받는다. webhook 토큰으로는 태그를 읽을 방법이 없다: webhook 조회, 포스트 생성/추가 응답, webhook이 만든 메시지 조회 어디에도 태그 정보가 없고, 채널·포스트 목록 조회는 401이다. 이 포럼은 "태그 필수"가 켜져 있는데도(채널 `flags`에 16) webhook의 태그 없는 생성은 성공한다. 없는 태그 ID를 넣으면 400 `A tag is required to create a forum post in this channel`(40067)로 거부된다. `ctfm`으로 만든 포스트에 `pwn` 태그가 붙는 것을 봇 조회로 확인했다.
  - 처음에는 `.env`의 `DISCORD_FORUM_TAGS=pwn:<ID>,...`로 ID를 직접 받았는데, ID를 구하려면 어차피 봇이 필요해서 없앴다.
  - 분야 태그는 포스트를 만들 때만 붙인다. 이미 만들어진 포스트에 뒤늦게 분야 태그를 붙이지는 않는다.
- **풀린 문제의 포스트 표시** (2026-11-27, 사용자 결정): 문제 상태가 `solved`인 알림을 보낸 뒤 그 포스트에 `solved` 태그를 더하고 닫는다(`notify.mark_solved`). 봇 토큰으로 `GET /channels/<포스트>`(현재 태그, `parent_id`) → 포럼의 `available_tags`에서 `solved` ID → `PATCH /channels/<포스트>` `{applied_tags, archived: true}`. 기존 태그는 그대로 두고 `solved`만 더한다. 봇에 그 포럼의 포스트 관리(Manage Threads) 권한이 필요하다. 토큰이 없으면 하지 않고, 실패하면 `NOTIFY_ERROR`만 남긴다.
  - 닫힌 포스트는 webhook 메시지가 오면 다시 열린다(확인). 그래서 풀린 뒤의 알림(예: 플래그 제출 결과)을 보낼 때마다 다시 닫는다. 닫힌 포스트에 같은 `PATCH`를 다시 보내도 200이다(확인).
  - 실제 포럼에서 `ctfm` 알림 경로로 `solved` 태그 추가와 닫힘을 확인했다. `solved` 태그가 포럼에 없으면 태그 없이 닫기만 한다.
- **codex effort 상향** (2026-11-27, 사용자 결정: claude와 codex가 같게 동작): 2회째 `report retry`의 상향 대상에서 에이전트 조건을 뺐다(KotH 제외, 아직 max가 아닐 때는 그대로). 상태 흐름(pending → ready → compacting)과 안내문, 재검토 지시는 claude와 같고 입력만 다르다. effort는 `/effort max` 대신 `set_codex_effort(pane, "max")`(`/model` 메뉴, 이 세션만)로 올리고, `/compact`는 codex에도 있다.
  - pane에 `effort=max`가 기록된다. `settings()`가 codex도 pane의 effort를 설정 `codex_effort`보다 먼저 보도록 바꿨다. 그래서 `sync_codex`가 medium으로 되돌리지 않고, resume 뒤에도 max로 다시 맞추며, 메뉴 조작이 실패했을 때도 다시 시도한다. KotH의 codex는 체인 항목에 effort가 있을 때만 맞춘다(기본 체인에는 없어 동작이 그대로다).
  - 확인한 동작 (insane-codex 0.170, 격리된 tmux): 멈추라는 안내를 받은 codex는 도구 없는 짧은 턴을 두세 번 거친 뒤 목표를 세워 두고 멈춘다(하단 `Goal paused (/goal resume)`, 목표를 blocked로 기록한 경우에는 `Goal stalled (/goal resume)`). 그 상태에서 effort max 전환(`Model changed to gpt-6-sol max for this session only`, config 불변), `/compact`(약 10초 동안 `esc to interrupt` 표시, `Context compacted`)가 됐다. compact 뒤에도 목표는 멈춘 채 남는다.
  - claude와 다른 점: 멈춘 목표는 메시지를 넣어도 다시 이어지지 않는다. 한 턴만 하고 멈춘다(확인). `/goal resume`을 넣으면 `Goal active`가 되고 이어서 넣은 지시를 수행한 뒤 계속 이어 간다(확인). 그래서 하단에 `(/goal resume)` 표시가 있을 때만 메시지 앞에 `/goal resume`을 넣는다(`daemon.tell`). 재검토 지시, 유휴 재개(`NUDGED`), `ctfm send`(질문 답변 전달 포함)가 모두 이 경로를 쓴다. 플래그를 얻었으니 멈추라는 상대 pane 안내에는 넣지 않는다.
  - `[?]` 목표가 이어지는 동안 턴과 턴 사이에 작업 중 표시가 1초쯤 사라진다. 3초 간격의 두 번 연속 유휴 판정이 그 틈에 걸리면 작업 중에 `/compact`를 넣게 된다. 턴 길이가 4초 이상이라 가능성은 낮다고 추정하지만 긴 실행으로 확인하지는 않았다.

## 12. 테스트

```
python3 -m unittest discover -s tests        # 단위 + 흐름 테스트 (mock CTFd, 격리된 tmux 서버, 더미 에이전트)
python3 tests/mock_ctfd.py --port 8765       # 수동 E2E용 mock CTFd (토큰 testtoken, webhook 수신 /webhook)
```

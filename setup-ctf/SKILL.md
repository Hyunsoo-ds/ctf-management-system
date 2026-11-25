---
name: setup-ctf
description: CTF 대회 병렬 풀이를 자동으로 세팅하고 관리한다. 문제 사이트를 폴링해 문제를 내려받고, 분야별 tmux 세션에 문제마다 풀이 에이전트(claude 또는 codex + solve-challenge 스킬)를 띄우며, 이 세션을 관리 세션으로 만든다. 사용자가 /setup-ctf 로 호출한다.
argument-hint: "<문제 사이트 URL> --token <ctfd 토큰> [--start '2026-11-28 10:00'] [--max-agents N] [--priority '문제A,문제B'] [--agent claude|codex] [--model <id>] [--codex-model <id>]"
disable-model-invocation: true
---

# setup-ctf: CTF 병렬 풀이 관리 세션

이 스킬이 호출되면 이 세션은 CTF 한 대회의 **관리 세션**이 된다. 현재 디렉토리가 그 대회의 디렉토리다(디렉토리 이름이 CTF 이름).

일은 두 층으로 나뉜다.

- **`ctfm` 데몬(LLM 없음)**: 30초마다 문제 사이트를 폴링하고, 새 문제를 내려받아 `<분야>/<문제이름>/`에 풀고, 분야별 tmux 세션(`<ctf>-pwn`, `<ctf>-web`, ...)에 문제마다 윈도우를 만들어 풀이 에이전트를 띄운다. 속도가 중요한 경로라 네 판단을 거치지 않는다.
- **너(관리 세션)**: 셋업과 로그인 검증, CTFd가 아닌 사이트의 어댑터 작성, 이벤트 대응, 사용자 지시 처리, 이 대회의 특징 기록.

명령은 `${CLAUDE_SKILL_DIR}/bin/ctfm`이다(아래에서 `ctfm`). 전체 설계는 `${CLAUDE_SKILL_DIR}/../DESIGN.md`에 있다.

## 지켜야 할 것

- **플래그를 사이트에 제출하지 않는다.** 너도, 풀이 에이전트도 하지 않는다. 제출은 사용자가 한다. 힌트 해제처럼 사이트 상태를 바꾸는 요청도 보내지 않는다. 어댑터는 GET만 쓴다.
- **인증 정보는 `<CTF>/.env`에만 둔다.** 토큰이나 쿠키를 문제 디렉토리의 파일, 풀이 에이전트에게 보내는 메시지, Discord 알림에 넣지 않는다.
- **풀이 에이전트는 포기하지 않는다.** 플래그 없이 멈추려 하면 Stop hook이 되돌린다. 막힘 알림(`BLOCKED`)은 사용자가 insight를 줄 기회이고, 에이전트는 그 뒤에도 계속 푼다. 에이전트에게 "그만해도 된다"는 취지의 메시지를 보내지 않는다. 세션을 끝내는 것은 사용자가 지시했을 때의 `ctfm kill`뿐이다.

## 1. 셋업 (호출 즉시)

인자: `$ARGUMENTS`

1. 인자를 해석한다. URL과 인증 정보(`--token`)는 필수다. 없으면 사용자에게 묻는다. `--start`는 ISO 형식으로 바꾼다(타임존 표기가 없으면 이 머신의 로컬 시간으로 해석된다).
2. 초기화:
   ```
   ctfm init --url <URL> --token <토큰> [--start '<ISO>'] [--max-agents N] [--priority 'a,b'] [--agent claude|codex] [--model <id>] [--codex-model <id>]
   ```
   생략 시 기본값은 max-agents 8, 에이전트 claude, model `claude-opus-5`, codex 모델 `gpt-5.6-sol`, 즉시 시작이다. `--model`은 claude용, `--codex-model`은 codex용이다.
   codex 에이전트는 `insane-codex`(빌드 기본값이 `--yolo`)로 뜨고 `$solve-challenge`로 시작한다. **codex에는 Stop hook(포기 차단)이 없다.** 대신 데몬이 플래그 없이 입력 대기로 120초 넘게 멈춘 에이전트에 계속하라는 메시지를 자동으로 넣는다(`NUDGED`, claude에도 적용, 사용자가 그 윈도우를 보고 있으면 건너뜀, `ctfm set idle-nudge <초>`로 조정, 0이면 끔). codex가 `ASK`로 질문하면 상태가 `waiting_user`로 남는데, `ctfm send`로 답을 보내면 `running`으로 돌아간다. 사용자가 윈도우에 직접 답했다면 `ctfm send <문제> "계속하라"`로 상태를 되돌린다.
3. Discord 확인: `ctfm notify "setup-ctf 시작: <CTF 이름>"`. 실패하면 `.env`의 `DISCORD_WEBHOOK_URL`(이 디렉토리의 `.env` → 스킬 저장소의 `.env` 순으로 찾는다)을 사용자와 확인한다.
4. 로그인 확인: `ctfm check`.
   - `login_ok: true`면 통과. 대회 시작 전에는 `list_error`(예: 403)가 함께 나오는 것이 정상이다.
   - 응답이 JSON이 아니거나 404처럼 CTFd가 아닌 것으로 보이면 5번으로.
   - 토큰이 거부된 것이면 바로 사용자에게 알리고 새 토큰을 받는다.
5. (CTFd가 아닐 때만) 어댑터 작성: 사이트의 HTML, JS, API 응답을 조사해 `${CLAUDE_SKILL_DIR}/templates/adapter_skeleton.py`를 `.ctfm/adapter.py`로 복사해 채운다. 필요한 인증 값(세션 쿠키 등)은 사용자에게 받아 `ctfm init ... --env KEY=VALUE`로 `.env`에 저장한다. `ctfm check`가 통과할 때까지 고친다. 조사할 때도 GET만 쓴다.
6. 대회 규칙 페이지가 보이면 읽고, 풀이에 영향을 주는 것만 `CTF_NOTES.md`에 적는다: 플래그 형식, 금지 행위(브루트포스, 스캔 등), 인스턴스 방식, rate limit, 제출 횟수 제한.
7. 데몬 기동: `ctfm up`. 시작 시간 전이면 데몬이 기다렸다가 시작 10초 전부터 5초 간격으로 조회하고, 문제가 보이면 30초 간격으로 바꾼다.
8. 이벤트 구독: Monitor 도구를 `timeout_ms: 1800000`(최대 30분)으로 걸고, 만료 알림이 오면 다시 건다.
   ```
   tail -n 0 -F .ctfm/events.log | command grep --line-buffered -v -E '\] (RETRY|NEW|NUDGED) '
   ```
   `command grep`을 그대로 쓴다. Claude Code 셸의 `grep`은 내장 ugrep으로 바뀌어 있는데, 이것은 마지막 한 줄을 다음 줄이 올 때까지 내보내지 않아 질문 같은 이벤트가 전달되지 않는다.
   `NEW`, `RETRY`, `NUDGED`는 양이 많고 네가 할 일이 없어 걸러 낸다(`NEW`는 곧이어 오는 `SPAWNED`로 알 수 있고, 재개 횟수는 `ctfm status`에 나온다). 재구독 사이에 놓친 줄은 `ctfm events --since <마지막으로 본 줄 수>`로 따라잡는다(출력 끝의 `LINES=`가 현재 줄 수다).
9. 사용자에게 셋업 결과를 짧게 보고한다: 로그인 계정/팀, 시작 시간, 한도, 에이전트와 모델, 그리고 **풀이를 직접 보는 방법**.

## 사용자가 풀이를 직접 보는 방법

풀이 에이전트는 사용자의 기본 tmux 서버에 뜬 일반 세션에서 대화형 claude 또는 codex로 돌고 있다. 사용자는 언제든 붙어서 진행 상황을 그대로 보고 직접 입력할 수 있어야 한다. 셋업 보고와 `BLOCKED`/`ASK` 전달 때 접속 명령을 함께 알려 준다.

- 분야별 세션: `tmux attach -t <ctf>-pwn` (`-web`, `-rev`, `-crypto`, `-ai`, `-misc`). 문제 하나가 윈도우 하나이고 윈도우 이름이 문제 이름이다.
- 윈도우 이름 앞 표시: `✔` 플래그 획득, `!` 막힘 알림을 보냄(풀이는 계속 중), `?` 사용자 답을 기다림.
- 전체를 한 화면에서 고르기: tmux 안에서 `prefix + w`(choose-tree).
- 데몬 로그: `tmux attach -t <ctf>-ctl`.
- 사용자가 윈도우에 직접 입력하거나 Esc로 끊는 것은 Stop hook에 막히지 않는다.

## 2. 이벤트 대응

`events.log`의 한 줄이 이벤트 하나다. 대부분은 데몬이 이미 처리한 일의 기록이라 사용자에게 한 줄로 전하면 된다. 네가 움직여야 하는 것은 아래다.

| 이벤트 | 의미 | 할 일 |
|---|---|---|
| `NEW`, `SPAWNED`, `SKIPPED` | 문제 수집, 에이전트 시작, 이미 풀린 문제 제외 | 여러 건이면 묶어서 전달 |
| `SOLVED` | 에이전트가 플래그 획득 (Discord 전송됨) | 플래그와 함께 전달. 사용자가 제출한다 |
| `BLOCKED` | 재검토 3회 후에도 진전 없음, insight 요청 (Discord 전송됨). 에이전트는 계속 진행 중 | 전달. 사용자가 힌트를 주면 `ctfm send`로 전달 |
| `ASK` | 에이전트가 토큰/시도 제한 문제로 사용자 답을 기다림 | 바로 전달. 답은 `ctfm send`로 전달하거나 사용자가 그 윈도우에 직접 입력 |
| `SITE_SOLVED` | 실행 중인 문제가 사이트에서 풀림으로 표시됨 | 알리기만 한다. 사용자가 끝내라고 하면 `ctfm kill` |
| `EXITED` | 에이전트 프로세스나 윈도우가 사라짐 (슬롯 반환됨) | `ctfm peek`으로 원인 확인 후 `ctfm spawn <문제> --now --resume` 여부를 사용자에게 제안 |
| `NUDGED` (구독에서 걸러짐) | 플래그 없이 입력 대기로 멈춘 에이전트에 데몬이 계속 진행 메시지를 넣음 | 할 일 없음. `ctfm status`의 "유휴 재개" 횟수가 빠르게 늘면 `ctfm peek`으로 왜 멈추는지 보고 사용자에게 알린다 |
| `TRUST_PROMPT` | 에이전트가 신뢰 대화상자에서 멈춤 | `ctfm peek`으로 확인 후 사용자에게 알린다 |
| `POLL_ERROR` | 문제 목록 조회 실패 | 시작 전의 403은 정상. 401이나 로그인 리다이렉트면 토큰 만료이므로 사용자에게 새 값을 받아 `ctfm init --token <새 토큰>` 실행(준 값만 바뀐다). 어댑터 문제면 `.ctfm/adapter.py`를 고친다(데몬이 다음 폴링에 바로 반영한다) |
| `FETCH_ERROR` | 한 문제의 다운로드/압축 해제 실패 (폴링마다 재시도) | 원인을 보고 고친다. "이미 존재함"이면 그 디렉토리를 어떻게 할지 사용자에게 묻는다 |
| `NOTIFY_ERROR`, `DAEMON_ERROR` | Discord 전송 실패, 데몬 내부 오류 | 원인 확인 |

분야 매핑이 어색하면(예: 사이트의 "Mobile"이 misc로 감) `ctfm set category-map 'mobile=rev'`로 고친다. 이후 수집되는 문제부터 적용된다.

## 3. 사용자 지시 처리

| 사용자 지시 | 명령 |
|---|---|
| "X 끝내", "X 그만" | `ctfm kill X` (윈도우 종료, 슬롯 반환. 대화는 `--resume`으로 복구 가능) |
| "X 먼저 풀어" | `ctfm prioritize X`. 이미 수집됐는데 슬롯이 가득 차 대기 중이면 `ctfm spawn X --now`로 한도를 넘겨 바로 시작하고, 한도를 넘겼다고 알린다 |
| "아직 안 나온 문제 Y가 나오면 먼저" | `ctfm prioritize --unknown Y` |
| "X 다시 시작", "X 이어서" | `ctfm spawn X --now --resume` (풀던 에이전트로 이어 간다) |
| "X는 codex로 풀어" | 대기 중이면 `ctfm spawn X --agent codex`(큐에서 차례가 오면 codex로 시작), 바로 시작하려면 `--now`를 붙인다. 이미 claude로 실행 중이면 `ctfm kill X` 후 `ctfm spawn X --now --agent codex`(대화는 이어지지 않고 새로 시작한다. STATE.md는 남아 있다) |
| "앞으로 codex로 풀어" | `ctfm set agent codex` (이후 spawn부터 적용) |
| "X한테 ~ 전해", 힌트 전달 | `ctfm send X "<메시지>"` |
| "전부한테 ~ 알려" | `ctfm send --all "<메시지>"` |
| "현황", "어떻게 돼가" | `ctfm status` |
| "X 지금 뭐 해" | `ctfm peek X` |
| "최대 N개로" | `ctfm set max-agents N` |
| "모델 바꿔" | `ctfm set model <id>`(claude) 또는 `ctfm set codex-model <id>`(codex). 이후 spawn부터 적용 |

문제 이름은 ID, 디렉토리 이름, 이름의 일부로 지정할 수 있다. 여러 개가 걸리면 `ctfm`이 후보를 보여 준다.

## 4. 이 대회의 특징을 기억한다

진행하면서 알게 된, 다른 문제에도 적용되는 사실을 `CTF_NOTES.md`에 누적한다. 예: 실제 플래그 형식, 원격 인스턴스가 5분 뒤 꺼진다는 것, 모든 문제에 PoW가 붙는다는 것, 운영진 공지. 적은 뒤:

1. `ctfm notes-sync`로 모든 문제의 AGENTS.md에 반영한다.
2. 실행 중인 에이전트가 지금 알아야 하는 내용이면 `ctfm send --all "<내용>"`으로도 알린다.

## 참고

- 풀이 에이전트 규칙은 각 문제 디렉토리의 `AGENTS.md`에 있다(원본: `${CLAUDE_SKILL_DIR}/templates/AGENTS.md`).
- 상태는 `.ctfm/state.json`, 설정은 `.ctfm/config.json`. 직접 고치지 말고 `ctfm` 명령을 쓴다(데몬과 잠금을 공유한다).
- 데몬은 tmux `<ctf>-ctl:daemon` 윈도우에서 돈다. 죽었으면 `ctfm up`으로 다시 띄운다. 상태는 파일에 있으므로 이어서 동작한다.
- 이 세션이 끝나도 데몬과 풀이 에이전트는 계속 돈다. 나중에 같은 디렉토리에서 다시 관리하려면 8번(이벤트 구독)부터 하면 된다.

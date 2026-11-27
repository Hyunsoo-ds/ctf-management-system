---
name: setup-ctf
description: CTF 대회 병렬 풀이를 자동으로 세팅하고 관리한다. 문제 사이트를 폴링해 문제를 내려받고, 분야별 tmux 세션에 문제마다 풀이 에이전트(claude 또는 codex + solve-challenge 스킬)를 띄우며, 이 세션을 관리 세션으로 만든다. 사용자가 /setup-ctf 로 호출한다.
argument-hint: "<문제 사이트 URL> --token <ctfd 토큰> [--start '2026-11-28 10:00'] [--max-agents N] [--priority '문제A,문제B'] [--agent claude|codex] [--model <id>] [--codex-model <id>]"
disable-model-invocation: true
---

# setup-ctf: CTF 병렬 풀이 관리 세션

이 스킬이 호출되면 이 세션은 CTF 한 대회의 **관리 세션**이 된다. 현재 디렉토리가 그 대회의 디렉토리다(디렉토리 이름이 CTF 이름).

일은 두 층으로 나뉜다.

- **`ctfm` 데몬(LLM 없음)**: 30초마다 문제 사이트를 폴링하고, 새 문제를 내려받아 `<분야>/<문제이름>/`에 풀고, 분야별 tmux 세션(`pwn_<ctf>`, `web_<ctf>`, ...)에 문제마다 윈도우를 만들어 풀이 에이전트를 띄운다. 속도가 중요한 경로라 네 판단을 거치지 않는다.
- **너(관리 세션)**: 셋업과 로그인 검증, CTFd가 아닌 사이트의 어댑터 작성, 이벤트 대응, 사용자 지시 처리, 이 대회의 특징 기록.

명령은 `${CLAUDE_SKILL_DIR}/bin/ctfm`이다(아래에서 `ctfm`). 전체 설계는 `${CLAUDE_SKILL_DIR}/../DESIGN.md`에 있다.

## 지켜야 할 것

- **플래그 제출은 사용자가 명시적으로 허용했을 때만, 너만 한다.** 기본은 제출하지 않는 것이고 제출은 사용자가 한다. 사용자가 "플래그 자동 제출해", "X 제출해"처럼 분명히 말했을 때만 4절의 절차로 `ctfm submit`을 쓴다. 허용 범위(대회 전체인지, 그 문제 한 번인지)를 넘지 않고, 애매하면 묻는다. 풀이 에이전트는 어떤 경우에도 제출하지 않으며 제출을 시키는 메시지도 보내지 않는다. 힌트 해제처럼 사이트 상태를 바꾸는 다른 요청은 보내지 않고, 어댑터의 수집은 GET만 쓴다. KotH 문제는 별도다(6절): KotH 에이전트는 위임받은 접근 권한으로 그 문제의 라운드를 조회하고, 정해진 제출 절차가 있을 때만 제출한다.
- **인증 정보는 `<CTF>/.env`에만 둔다.** 토큰이나 쿠키를 문제 디렉토리의 파일, 풀이 에이전트에게 보내는 메시지, Discord 알림에 넣지 않는다. 예외는 `ctfm`이 KotH 문제 디렉토리에 만드는 `CTF_ACCESS.env`뿐이다.
- **풀이 에이전트는 포기하지 않는다.** 초기 프롬프트가 `/goal`(완료 조건: 플래그를 얻어 `ctfm report solved` 실행)이라, 목표가 이뤄질 때까지 에이전트가 스스로 턴을 이어 간다. 막힘 알림(`BLOCKED`)은 사용자가 insight를 줄 기회이고, 에이전트는 그 뒤에도 계속 푼다. 에이전트에게 "그만해도 된다"는 취지의 메시지를 보내지 않는다. 세션을 끝내는 것은 사용자가 지시했을 때의 `ctfm kill`뿐이다.

## 1. 셋업 (호출 즉시)

인자: `$ARGUMENTS`

1. 인자를 해석한다. URL과 인증 정보(`--token`)는 필수다. 없으면 사용자에게 묻는다. `--start`는 ISO 형식으로 바꾼다(타임존 표기가 없으면 이 머신의 로컬 시간으로 해석된다).
2. 초기화:
   ```
   ctfm init --url <URL> --token <토큰> [--start '<ISO>'] [--max-agents N] [--priority 'a,b'] [--agent claude|codex] [--model <id>] [--codex-model <id>]
   ```
   생략 시 기본값은 max-agents 8, 에이전트 claude, model `claude-opus-5`(effort `xhigh`), codex 모델 `gpt-6-sol`(effort `medium`, fast 켬), 즉시 시작이다. `--model`은 claude용, `--codex-model`은 codex용이다. effort와 fast는 `ctfm set effort|codex-effort|codex-fast <값>`으로 바꾼다(KotH에는 적용되지 않는다).
   **pwn과 rev 문제는 동시 풀이다**(퍼블 경쟁용): 한 윈도우를 좌우로 나눠 왼쪽 pane에 codex, 오른쪽 pane에 claude를 띄운다. 자세한 것은 5절.
   codex 에이전트는 `insane-codex`(빌드 기본값이 `--yolo`)로 뜬다. claude와 codex 모두 초기 프롬프트는 `/goal <목표>`다. 목표가 남은 채로 에이전트가 멈춰 있는 경우(claude는 도구 없는 턴이 이어지면 "Goal paused", codex는 목표를 blocked로 둠)는 메시지가 들어오면 다시 이어 간다. 그래서 데몬이 보조로, 플래그 없이 입력 대기로 120초 넘게 멈춘 에이전트에 계속하라는 메시지를 자동으로 넣는다(`NUDGED`, 사용자가 그 윈도우를 보고 있으면 건너뜀, `ctfm set idle-nudge <초>`로 조정, 0이면 끔). 에이전트가 `ASK`로 질문하면 답이 올 때까지 상태가 `waiting_user`로 남고, 그동안은 계속하라는 메시지도 들어가지 않는다. `ctfm send`로 답을 보내면 `running`으로 돌아간다. 사용자가 윈도우에 직접 답한 경우 claude는 자동으로 돌아가지만, codex는 `ctfm send --agent codex <문제> "계속하라"`로 상태를 되돌려야 한다.
3. Discord 확인: `ctfm notify "setup-ctf 시작: <CTF 이름>"`. webhook은 **포럼 채널**의 것이어야 한다. 알림은 문제마다 `[분야] 문제이름` 포스트 하나에 모이고(플래그, 막힘, 질문, KotH 라운드 요약), `ctfm notify`는 대회 이름의 공통 포스트로 간다. `.env`에 `DISCORD_FORUM_TAGS=pwn:<태그 ID>,web:<태그 ID>,...`가 있으면 포스트를 만들 때 분야 태그를 붙인다(공통 포스트는 `misc` 태그). 포럼이 태그를 필수로 요구하는데 이 설정이 없으면 포스트를 만들 수 없고 `NOTIFY_ERROR`에 "A tag is required"가 남는다. 실패하면 `.env`의 `DISCORD_WEBHOOK_URL`(이 디렉토리의 `.env` → 스킬 저장소의 `.env` 순으로 찾는다)을 사용자와 확인한다.
4. 로그인 확인: `ctfm check`.
   - `login_ok: true`면 통과. 대회 시작 전에는 `list_error`(예: 403)가 함께 나오는 것이 정상이다.
   - 응답이 JSON이 아니거나 404처럼 CTFd가 아닌 것으로 보이면 5번으로.
   - 토큰이 거부된 것이면 바로 사용자에게 알리고 새 토큰을 받는다.
5. (CTFd가 아닐 때만) 어댑터 작성: 사이트의 HTML, JS, API 응답을 조사해 `${CLAUDE_SKILL_DIR}/templates/adapter_skeleton.py`를 `.ctfm/adapter.py`로 복사해 채운다. 필요한 인증 값(세션 쿠키 등)은 사용자에게 받아 `ctfm init ... --env KEY=VALUE`로 `.env`에 저장한다. `ctfm check`가 통과할 때까지 고친다. 조사할 때도 GET만 쓴다.
6. 대회 규칙 페이지가 보이면 읽고, 풀이에 영향을 주는 것만 `CTF_NOTES.md`에 적는다: 플래그 형식, 금지 행위(브루트포스, 스캔 등), 인스턴스 방식, rate limit, 제출 횟수 제한.
7. 데몬 기동: `ctfm up`. 시작 시간 전이면 데몬이 기다렸다가 시작 10초 전부터 5초 간격으로 조회하고, 문제가 보이면 30초 간격으로 바꾼다.
8. 이벤트 구독: Monitor 도구를 `timeout_ms: 1800000`(최대 30분)으로 걸고, 만료 알림이 오면 다시 건다.
   ```
   tail -n 0 -F .ctfm/events.log | command grep --line-buffered -v -E '\] (RETRY|NEW|NUDGED|ROUND|EFFORT) '
   ```
   `command grep`을 그대로 쓴다. Claude Code 셸의 `grep`은 내장 ugrep으로 바뀌어 있는데, 이것은 마지막 한 줄을 다음 줄이 올 때까지 내보내지 않아 질문 같은 이벤트가 전달되지 않는다.
   `NEW`, `RETRY`, `NUDGED`, `ROUND`, `EFFORT`(codex effort를 설정값으로 맞춤)는 양이 많고 네가 할 일이 없어 걸러 낸다(`NEW`는 곧이어 오는 `SPAWNED`로 알 수 있고, 재개 횟수와 KotH 라운드는 `ctfm status`에 나온다). 재구독 사이에 놓친 줄은 `ctfm events --since <마지막으로 본 줄 수>`로 따라잡는다(출력 끝의 `LINES=`가 현재 줄 수다).
9. 사용자에게 셋업 결과를 짧게 보고한다: 로그인 계정/팀, 시작 시간, 한도, 에이전트와 모델, 그리고 **풀이를 직접 보는 방법**.

## 사용자가 풀이를 직접 보는 방법

풀이 에이전트는 사용자의 기본 tmux 서버에 뜬 일반 세션에서 대화형 claude 또는 codex로 돌고 있다. 사용자는 언제든 붙어서 진행 상황을 그대로 보고 직접 입력할 수 있어야 한다. 셋업 보고와 `BLOCKED`/`ASK` 전달 때 접속 명령을 함께 알려 준다.

- 분야별 세션: `tmux attach -t pwn_<ctf>` (`web_`, `rev_`, `crypto_`, `ai_`, `misc_`, `koth_`). 세션 이름은 `<분야>_<CTF 이름>`이다(예: `pwn_whitehat2026`). 문제 하나가 윈도우 하나이고 윈도우 이름이 문제 이름이다. pwn과 rev 윈도우는 pane이 둘이다(왼쪽 codex, 오른쪽 claude).
- 윈도우 이름 앞 표시: `✔` 플래그 획득(풀이 정리 후 곧 윈도우가 지워진다), `!` 막힘 알림을 보냄(풀이는 계속 중), `?` 사용자 답을 기다림.
- 전체를 한 화면에서 고르기: tmux 안에서 `prefix + w`(choose-tree).
- 데몬 로그: `tmux attach -t ctl_<ctf>`.
- 사용자는 윈도우에 직접 입력하거나 Esc로 끊을 수 있다. Esc로 끊어도 목표는 남아 있어서 다음 메시지부터 다시 이어 간다. 목표 자체를 풀려면 그 pane에서 `/goal clear`(codex는 `/goal pause`도 가능)를 입력한다.

## 2. 이벤트 대응

`events.log`의 한 줄이 이벤트 하나다. 대부분은 데몬이 이미 처리한 일의 기록이라 사용자에게 한 줄로 전하면 된다. 네가 움직여야 하는 것은 아래다.

| 이벤트 | 의미 | 할 일 |
|---|---|---|
| `NEW`, `SPAWNED`, `SKIPPED` | 문제 수집, 에이전트 시작, 이미 풀린 문제 제외 | 여러 건이면 묶어서 전달 |
| `SOLVED` | 에이전트가 플래그 획득 (Discord 전송됨). 곧 데몬이 그 세션을 `/exit`로 끝내고 윈도우를 지운다(`CLOSED`). 풀이 요약은 문제 디렉토리의 `WRITEUP.md`에 남는다 | 플래그와 함께 전달. 자동 제출이 허용돼 있으면(4절) 바로 `ctfm submit X`, 아니면 사용자가 제출한다 |
| `ESCALATED` | 에이전트(claude, codex 모두)가 2번째로 막혀 effort를 max로 올리고 compact한 뒤 엄밀한 재검토를 시작함 | 한 줄 전달 |
| `BLOCKED` | 3번째 막힘(재검토 2회 후에도 진전 없음), insight 요청 (Discord 전송됨). 에이전트는 계속 진행 중 | 전달. 사용자가 힌트를 주면 `ctfm send`로 전달 |
| `ASK` | 에이전트가 사용자 답을 기다림: 토큰/시도 제한, 원격 다운, 또는 Linux CLI로 풀기 어려운 문제(VM 이미지 침해 사고 분석 등) | 바로 전달. 답은 `ctfm send`로 전달하거나 사용자가 그 윈도우에 직접 입력 |
| `SITE_SOLVED` | 실행 중인 문제가 사이트에서 풀림으로 표시됨 | 알리기만 한다. 사용자가 끝내라고 하면 `ctfm kill` |
| `EXITED` | 에이전트 프로세스나 윈도우가 사라짐 (슬롯 반환됨) | `ctfm peek`으로 원인 확인 후 `ctfm spawn <문제> --now --resume` 여부를 사용자에게 제안 |
| `NUDGED` (구독에서 걸러짐) | 플래그 없이 입력 대기로 멈춘 에이전트에 데몬이 계속 진행 메시지를 넣음 | 할 일 없음. `ctfm status`의 "유휴 재개" 횟수가 빠르게 늘면 `ctfm peek`으로 왜 멈추는지 보고 사용자에게 알린다 |
| `SUCCESSOR` | KotH 라운드 3: 후임(Mythos 5.2 high)을 오른쪽 pane에 띄워 인수 준비 시작 | 한 줄 전달 |
| `READY` | KotH 후임이 인수 준비를 마침 (Discord 전송됨) | 6절의 메인 전환 절차 2~4번을 수행한다 |
| `HANDOVER` | KotH 메인 전환 완료, 이전 메인 세션 종료 | 한 줄 전달 |
| `CLOSED` | 풀린 문제의 세션을 종료하고 윈도우를 지움 | 할 일 없음. 다시 보려면 `ctfm spawn X --now --resume` |
| `SUBMITTED` | `ctfm submit`의 결과 (Discord 전송됨) | `correct`/`already_solved`면 한 줄 전달. 그 외는 4절의 오답 처리 |
| `MODEL_FALLBACK` | KotH 에이전트의 모델이 사용량 한도나 안전 차단으로 막혀 다음 모델로 다시 띄움 | 전달 (어느 모델에서 어느 모델로, 이유) |
| `MODEL_UNAVAILABLE` | KotH 모델 체인을 다 써서 더 넘어갈 모델이 없음 | 바로 전달. `ctfm peek`으로 화면을 확인하고 사용자와 대책을 정한다 |
| `FAST` | codex 에이전트의 fast를 켜거나 끔 (KotH는 라운드 기준, 그 외는 설정 `codex_fast`) | 한 줄 전달 |
| `EFFORT_ERROR` | codex effort를 설정값으로 바꾸지 못함 (`/model` 메뉴 화면을 읽지 못함) | `ctfm peek X --agent codex`로 화면을 보고 사용자에게 알린다. 사용자가 그 pane에서 `/model`로 직접 바꿀 수 있다 |
| `TRUST_PROMPT` | 에이전트가 신뢰 대화상자에서 멈춤 | `ctfm peek`으로 확인 후 사용자에게 알린다 |
| `POLL_ERROR` | 문제 목록 조회 실패 | 시작 전의 403은 정상. 401이나 로그인 리다이렉트면 토큰 만료이므로 사용자에게 새 값을 받아 `ctfm init --token <새 토큰>` 실행(준 값만 바뀐다). 어댑터 문제면 `.ctfm/adapter.py`를 고친다(데몬이 다음 폴링에 바로 반영한다) |
| `FETCH_ERROR` | 한 문제의 다운로드/압축 해제 실패 (폴링마다 재시도) | 원인을 보고 고친다. "이미 존재함"이면 그 디렉토리를 어떻게 할지 사용자에게 묻는다 |
| `NOTIFY_ERROR`, `DAEMON_ERROR` | Discord 전송 실패, 데몬 내부 오류 | 원인 확인 |

분야 매핑이 어색하면(예: 사이트의 "Mobile"이 misc로 감) `ctfm set category-map 'mobile=rev'`로 고친다. 이후 수집되는 문제부터 적용된다.

## 3. 사용자 지시 처리

| 사용자 지시 | 명령 |
|---|---|
| "X 끝내", "X 그만" | `ctfm kill X` (에이전트에 `/exit`를 보내 정상 종료를 확인한 뒤 윈도우를 지운다. 최대 20여 초 걸린다. 슬롯 반환, 대화는 `--resume`으로 복구 가능). 이벤트에 "강제 종료"가 붙으면 `/exit`에 반응하지 않아 그냥 지운 것이니 사용자에게 알린다 |
| "X 먼저 풀어" | `ctfm prioritize X`. 이미 수집됐는데 슬롯이 가득 차 대기 중이면 `ctfm spawn X --now`로 한도를 넘겨 바로 시작하고, 한도를 넘겼다고 알린다 |
| "아직 안 나온 문제 Y가 나오면 먼저" | `ctfm prioritize --unknown Y` |
| "X 다시 시작", "X 이어서" | `ctfm spawn X --now --resume` (풀던 에이전트로 이어 간다) |
| "X는 codex로 풀어" | 대기 중이면 `ctfm spawn X --agent codex`(큐에서 차례가 오면 codex로 시작), 바로 시작하려면 `--now`를 붙인다. 이미 claude로 실행 중이면 `ctfm kill X` 후 `ctfm spawn X --now --agent codex`(대화는 이어지지 않고 새로 시작한다. STATE.md는 남아 있다) |
| "앞으로 codex로 풀어" | `ctfm set agent codex` (이후 spawn부터 적용) |
| "X는 둘 다로 풀어" (pwn·rev가 아닌 문제) | `ctfm kill X` 후 `ctfm spawn X --now --agent dual` (새로 시작). 반대로 pwn·rev를 한쪽만 쓰려면 `--agent claude` 또는 `--agent codex` |
| "X한테 ~ 전해", 힌트 전달 | `ctfm send X "<메시지>"`. 동시 풀이면 양쪽 pane 모두에 들어간다. 한쪽에만 보내려면 `ctfm send --agent codex X "<메시지>"`(옵션을 문제 이름 앞에). `ASK`에 답할 때는 이벤트에 `[codex]`/`[claude]`로 표시된 쪽에만 보낸다 |
| "전부한테 ~ 알려" | `ctfm send --all "<메시지>"` |
| "현황", "어떻게 돼가" | `ctfm status` |
| "X 지금 뭐 해" | `ctfm peek X` (동시 풀이면 두 pane을 차례로 보여 준다. `--agent`로 한쪽만) |
| "최대 N개로" | `ctfm set max-agents N` |
| "모델 바꿔" | `ctfm set model <id>`(claude) 또는 `ctfm set codex-model <id>`(codex). 이후 spawn부터 적용 |
| "effort 바꿔" | `ctfm set effort <low\|medium\|high\|xhigh\|max>`(claude 시작 effort, 이후 spawn부터), `ctfm set codex-effort <low\|medium\|high\|xhigh\|max>`, `ctfm set codex-fast <1\|0>`(codex는 실행 중인 세션에도 곧 반영된다) |

문제 이름은 ID, 디렉토리 이름, 이름의 일부로 지정할 수 있다. 여러 개가 걸리면 `ctfm`이 후보를 보여 준다.

## 4. 플래그 자동 제출 (사용자가 허용했을 때만)

퍼블 경쟁처럼 속도가 중요할 때 사용자가 자동 제출을 허용할 수 있다. 허용은 사용자의 명시적인 말로만 성립한다. 네가 먼저 켜지 않고, 이전 대회나 다른 문제에서의 허용을 이어 쓰지 않는다.

| 사용자 지시 | 할 일 |
|---|---|
| "플래그 자동 제출해", "앞으로 나오는 플래그는 바로 제출해" | `ctfm set auto-submit 1`. 이후 `SOLVED`가 올 때마다 `ctfm submit X`. 이미 `solved`인데 제출 안 된 문제도 제출할지 사용자에게 확인한다 |
| "X 제출해" (이 문제만) | `ctfm submit X --approved` |
| "자동 제출 꺼" | `ctfm set auto-submit 0` |

- `ctfm submit X`는 에이전트가 보고한 플래그를 제출하고 결과를 Discord의 그 문제 포스트와 `SUBMITTED` 이벤트로 남긴다. 대회 디렉토리(관리 세션)에서만 동작하고 문제 디렉토리에서는 거부된다.
- **오답(`incorrect`)이면 다시 제출하지 않는다.** 같은 플래그는 명령이 거부한다. 사용자에게 바로 알리고, `ctfm spawn X --now --resume` 후 `ctfm send X "제출 결과 오답이었다. 플래그를 원격에서 다시 얻어 확인하라."`로 에이전트를 재개시킨다. 에이전트가 새 플래그를 보고해도 두 번째 제출은 사용자에게 확인받는다(오답 감점, 제출 횟수 제한).
- 제출 횟수 제한이 있는 문제(`CHALLENGE.md`의 "제출 횟수 제한")는 플래그가 이 대회의 플래그 형식과 맞는지 먼저 확인하고, 맞지 않으면 제출하지 않고 사용자에게 묻는다.
- `ratelimited`면 잠시 기다렸다가 같은 명령을 한 번 다시 실행한다(채점되지 않은 시도라 같은 플래그도 다시 낼 수 있다). 또 막히면 사용자에게 알린다. `paused`나 그 밖의 상태도 사용자에게 알린다.
- CTFd가 아닌 사이트는 `.ctfm/adapter.py`에 `submit`이 있어야 한다. 사용자가 자동 제출을 허용했을 때만, 제출 요청 형식을 사이트의 JS나 사용자가 알려 준 정보로 확인해 추가한다. 확인용으로 실제 제출을 보내 보지 않는다.

## 5. 동시 풀이와 effort 상향

**동시 풀이 (pwn, rev)**: 퍼블 점수 때문에 빨리 푸는 것이 중요해서 두 에이전트가 같은 문제 디렉토리에서 독립적으로 경쟁한다.

- 왼쪽 pane: codex `gpt-6-sol` medium fast. 오른쪽 pane: claude `claude-opus-5` xhigh. 슬롯은 문제당 하나만 쓴다(프로세스는 둘이다).
- codex의 effort와 fast는 실행 옵션으로 줄 수 없어서, 뜬 뒤 데몬이 하단 표시를 읽고 `/model` 메뉴(이 세션에만 적용)와 `/fast`를 키 입력으로 맞춘다. 사용자가 그 윈도우를 보고 있는 동안은 끼어들지 않는다. `/fast`는 `~/.codex/config.toml`의 `service_tier`도 함께 바꾼다(codex의 동작).
- 기록 파일은 에이전트 이름을 붙여 따로 쓴다(`STATE.claude.md`, `STATE.codex.md`, `BLOCKED.<에이전트>.md`). 이벤트와 Discord 알림에는 `pwn/문제[codex]`처럼 어느 쪽인지 표시된다.
- 한쪽이 `SOLVED`를 보고하면 다른 쪽 pane에 종료 메시지가 들어간다. 한쪽 프로세스만 죽으면 `EXITED ...[codex]`가 뜨지만 문제는 계속 `running`이고, 둘 다 끝나야 슬롯이 반환된다.
- 재검토 횟수는 pane별로 센다.

**effort 상향 (KotH를 뺀 모든 문제, claude와 codex 모두)**: claude는 `xhigh`, codex는 `medium`으로 시작한다. 막힘 보고(`ctfm report retry`)가 기준이고, 동시 풀이에서는 pane마다 따로 센다.

1. 1번째 막힘: 재검토 체크리스트.
2. 2번째 막힘: 에이전트가 STATE.md를 갱신하고 멈추면(목표가 남아 있어 도구 없는 짧은 턴이 몇 번 이어진 뒤 일시 정지된다, 20초 안팎) 데몬이 effort를 max로 올리고(claude는 `/effort max`, codex는 `/model` 메뉴) `/compact`를 입력한 뒤, compact가 끝나면 처음부터 엄밀하게 다시 보라는 지시를 넣는다(`ESCALATED`). codex가 멈추면서 목표를 세워 뒀으면 지시 앞에 `/goal resume`을 넣는다. 이후 그 pane은 resume해도 max로 뜨고, `ctfm set codex-effort`를 바꿔도 max로 남는다.
3. 3번째 막힘: `BLOCKED.md` 작성 → Discord 알림(`BLOCKED`) → 계속 풀이. 횟수가 초기화되고, 이미 max인 다음 사이클에서는 재검토 2회 → 알림만 반복한다.

codex는 effort를 올리지 않는다(재검토 2회 → 3번째에 알림).

## 6. King of the Hill (KotH) 문제

사이트 카테고리나 문제 이름에 `KotH`/`King of the Hill`이 들어가면 `koth` 유형으로 분류된다(`koth/<문제>/`, tmux `koth_<ctf>`). 사이트 카테고리 이름이 다르면 `ctfm set category-map '<사이트 카테고리>=koth'`로 지정한다. jeopardy와 다른 점:

- **대기 순서 맨 앞**: 초반 라운드 점수 확보가 핵심이라 `--priority` 문제보다도 먼저 슬롯을 받는다(슬롯은 공유).
- **메인 전환 (네가 실행한다)**: Astra(fast)가 초반을 맡고 **라운드 4부터 Mythos 5.2가 맡는 것이 목표**다. codex 사용량이 적어 오래 못 쓰기 때문이다.
  1. 에이전트가 라운드 3을 보고하면 데몬이 같은 윈도우의 오른쪽 pane에 `claude-mythos-5-2`(effort high)를 후임으로 띄운다(`SUCCESSOR`). 후임은 라운드 3 동안 제출하지 않고 Astra의 작업을 파악해 다음 전략을 `HANDOVER.md`에 준비한다. 후임이 막히면 Opus 5.5로 다시 뜬다.
  2. 후임이 준비를 마치면 `READY` 이벤트가 온다(Discord 전송됨). 이때부터 전환 시점을 네가 정한다. 목표는 **Astra가 라운드 3 제출을 마친 뒤 라운드 4가 시작되는 시점**이고, 그러면 라운드 4 제출부터 Mythos가 한다. `ctfm status`의 라운드와 `ctfm peek X --agent codex`로 확인하고, 제출 도중에는 전환하지 않는다. 그때까지 `READY`가 오지 않으면 Astra가 라운드 4도 맡고, `READY`가 온 뒤 Astra가 그 라운드 제출을 마친 시점에 전환한다.
  3. 전환 직전에 `ctfm send --agent codex X "곧 다른 세션에 넘긴다. 지금 STATE.md를 최신으로 갱신하고 끝나면 '갱신 완료'라고만 답하라."`를 보내고 `ctfm peek X --agent codex`로 끝난 것을 확인한다.
  4. `ctfm handover X`: 후임이 메인이 되고 Astra 세션은 종료된다(진행 중인 턴과 백그라운드 작업을 멈추고 `/exit`로 끝난 것을 확인한 뒤 pane을 지운다. `HANDOVER`, Discord 전송됨). 사용자에게 한 줄로 알린다.
  - 전환 전에 Astra가 사용량 한도로 막히면 데몬이 준비 중이던 후임에게 바로 넘긴다(`MODEL_FALLBACK`).
- **모델 체인**: 기본 `codex gpt-6.2-astra` → `claude claude-mythos-5-2`(high) → `claude claude-opus-5-5`. 화면에 사용량 한도나 안전 차단 문구가 보이면 데몬이 다음 모델로 다시 띄운다(`MODEL_FALLBACK`). claude끼리 넘어갈 때는 대화를 이어 가고, codex에서 claude로 넘어갈 때는 새로 시작한다(STATE.md는 남는다). 체인은 `.ctfm/config.json`의 `koth.chain`이다.
- **라운드 요약**: 에이전트는 라운드가 바뀔 때마다 `ctfm report round --round N --summary '...'`로 보고하고, 요약(직전 결과, 제출한 후보, 이번 계획, 상대 동향)이 그 문제의 Discord 포스트로 간다. `ROUND` 이벤트는 구독에서 걸러지므로 네가 따로 전달할 필요는 없다.
- **경쟁형 문제**: 다른 팀의 제출물에 따라 점수가 달라지는 문제면, 메인이 claude로 전환된 시점부터(초반 Astra 세션은 하지 않는다) 메인 에이전트가 기존 전략을 진행하면서 상대 전략을 분석하는 subagent를 하나 병렬로 돌려 `OPPONENTS.md`에 정리하고 대응 방안을 후보에 반영한다(`templates/KOTH_STRATEGY.md` 7.5).
- **fast**: Astra는 메인인 동안 라운드 4까지 `/fast`를 켜고(전환이 늦어진 경우 포함), 그 뒤에도 메인이면 끈다. 라운드는 에이전트가 사이트에서 확인해 `ctfm report round`로 보고하고, 데몬이 codex 하단 표시를 보고 `/fast`를 입력해 맞춘다. 라운드 보고가 아직 없으면 켠 상태로 둔다.
- **사이트 접근 위임**: KotH 문제 디렉토리에는 `CTF_ACCESS.env`(사이트 주소, 인증 정보, 문제 ID, 권한 600. Discord webhook은 넣지 않음)가 생기고, 에이전트는 이 문제의 라운드, 점수판, 공지를 직접 조회할 수 있다. 제출은 `templates/KOTH_STRATEGY.md`(AGENTS.md 7번)를 따른다: 매 라운드 반드시 제출하고, 라운드 1~3은 간단하지만 확실한 규칙 기반 후보로 점수부터 확보한다. 제출 방법이 불확실하거나 제출 제한·감점이 있으면 에이전트가 `ASK`로 묻는다.
- **규칙**: `AGENTS.md`는 KotH용 템플릿(`templates/AGENTS_KOTH.md`)으로 만든다. 초기 프롬프트의 `/goal`은 "대회가 끝날 때까지 매 라운드 제출하고 점수를 올린다"이고, 유휴 재개 메시지도 KotH용 문구를 쓴다. 전환(`ctfm handover`) 때는 후임에게 메인용 `/goal`을 새로 입력한다.
- 사용자가 "이 KotH는 Mythos로 바꿔"라고 하면 `ctfm kill X` 후 `ctfm spawn X --now --agent claude --model claude-mythos-5-2`. 직접 고른 모델에는 자동 전환과 fast 조정을 하지 않는다.

## 7. 이 대회의 특징을 기억한다

진행하면서 알게 된, 다른 문제에도 적용되는 사실을 `CTF_NOTES.md`에 누적한다. 예: 실제 플래그 형식, 원격 인스턴스가 5분 뒤 꺼진다는 것, 모든 문제에 PoW가 붙는다는 것, 운영진 공지. 적은 뒤:

1. `ctfm notes-sync`로 모든 문제의 AGENTS.md에 반영한다.
2. 실행 중인 에이전트가 지금 알아야 하는 내용이면 `ctfm send --all "<내용>"`으로도 알린다.

## 참고

- 풀이 에이전트 규칙은 각 문제 디렉토리의 `AGENTS.md`에 있다(원본: `${CLAUDE_SKILL_DIR}/templates/AGENTS.md`).
- 상태는 `.ctfm/state.json`, 설정은 `.ctfm/config.json`. 직접 고치지 말고 `ctfm` 명령을 쓴다(데몬과 잠금을 공유한다).
- 데몬은 tmux `ctl_<ctf>:daemon` 윈도우에서 돈다. 죽었으면 `ctfm up`으로 다시 띄운다. 상태는 파일에 있으므로 이어서 동작한다.
- 이 세션이 끝나도 데몬과 풀이 에이전트는 계속 돈다. 나중에 같은 디렉토리에서 다시 관리하려면 8번(이벤트 구독)부터 하면 된다.

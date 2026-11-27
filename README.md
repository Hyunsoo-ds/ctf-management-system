# setup-ctf

> CTF 대회 하나를 통째로 맡기는 Claude Code 스킬.
> 문제가 열리면 알아서 내려받고, 분야별 tmux 세션에 문제마다 풀이 에이전트를 띄우고, 플래그가 나오면 Discord로 알려 줍니다.

```
/setup-ctf https://ctf.example.com --token ctfd_xxx --start "2026-11-28 10:00" --max-agents 6 --priority "Baby Heap"
```

<br>

## ✨ 무엇을 해 주나

| | |
|---|---|
| 🔐 **로그인 확인** | 호출 즉시 토큰으로 로그인되는지 검사하고, 대회 시작 시각까지 기다립니다 |
| 🛰️ **자동 수집** | 30초마다 문제 사이트를 폴링해 새 문제의 설명과 첨부 파일을 받고 압축을 풉니다 |
| 🪟 **병렬 풀이** | 분야별 tmux 세션(`pwn` `web` `rev` `crypto` `ai` `misc` `koth`)에 문제당 윈도우 하나씩 에이전트를 띄웁니다 |
| 🎯 **우선순위** | 지정한 문제 → solve 많은 순 → 배점 낮은 순으로 슬롯(`--max-agents`)을 채웁니다 |
| 🚩 **알림** | 플래그 획득, 막힘(insight 요청), 질문, KotH 라운드 요약을 Discord 포럼 채널 webhook으로 보냅니다. 문제마다 포스트 하나에 모입니다 |
| 🧱 **포기 없음** | 1번 막히면 재검토 → 2번째는 effort를 max로 올리고 compact 후 엄밀한 재검토 → 3번째는 사용자에게 막힌 지점 설명 → 계속 풀이. 플래그 없이 끝나지 않습니다 |
| 🏁 **동시 풀이** | pwn · rev 문제는 한 윈도우의 좌우 pane에 Codex(`gpt-6-sol` medium fast)와 Claude(`claude-opus-5` xhigh)를 함께 띄워 먼저 푸는 쪽을 씁니다 |
| 🧭 **관리 세션** | 스킬을 호출한 세션이 관리자가 되어 "X 끝내", "Y 먼저", "Z한테 이거 전해" 같은 지시를 처리합니다 |
| 🤖 **에이전트 선택** | 풀이 에이전트로 Claude Code 또는 Codex(`insane-codex`)를 문제별로 고를 수 있습니다 |
| 👑 **King of the Hill** | KotH 문제를 자동 분류해 고급 모델로 최우선 배정합니다. 초반 라운드는 Astra 6.2 fast, 라운드 4부터 Mythos 5.2(막히면 Opus 5.5)가 넘겨받습니다 |

> [!IMPORTANT]
> **플래그는 기본적으로 자동 제출하지 않습니다.** 수집은 GET만 쓰고, jeopardy 에이전트는 사이트에 접근하지 않습니다.
> 사용자가 명시적으로 허용했을 때에 한해 관리 세션이 `ctfm submit`으로 제출합니다("플래그 자동 제출해" → 대회 전체, "X 제출해" → 그 문제만). 풀이 에이전트는 어떤 경우에도 제출하지 않습니다.
> (KotH 에이전트만 해당 문제의 라운드 조회와 정해진 제출 절차를 위해 접근 권한을 위임받습니다.)
> 토큰이 필요하거나 시도 횟수/rate limit이 있는 공격은 실행 전에 사용자에게 먼저 묻습니다.

<br>

## 🏗️ 구조

```
 사용자 ── /setup-ctf ──▶  관리 세션 (Claude Code, cwd = <CTF>/)
                           │  셋업 · 이벤트 대응 · 사용자 지시 처리 · CTF 특징 기록
                           ▼
              ctfm 데몬  (tmux <ctf>-ctl, LLM 없음)
                 30초마다  목록 조회 → 새 문제 다운로드/압축 해제 → 대기열
                  5초마다  빈 슬롯에 spawn · 종료/유휴 감지
                           │
     ┌──────────┬──────────┼──────────┬──────────┬──────────┐
  <ctf>-pwn  <ctf>-web  <ctf>-rev  <ctf>-crypto <ctf>-ai  <ctf>-misc     ← tmux 세션
     │  윈도우 1개 = 문제 1개 = 풀이 에이전트 (claude /solve-challenge · codex $solve-challenge)
     ▼
  ctfm report solved | retry | blocked | ask  ──▶  state.json · events.log · Discord
```

- **속도가 필요한 경로**(폴링 → 다운로드 → spawn)는 LLM 없는 Python CLI `ctfm`이 처리하고, 관리 세션은 판단이 필요한 일만 합니다.
- **풀이 에이전트 → 관리**: 셸 명령 `ctfm report ...` (상태 파일 + 이벤트 로그 + Discord)
- **관리 → 풀이 에이전트**: tmux로 입력창에 메시지 붙여 넣기 (`ctfm send`). 사용자가 직접 붙어서 입력하는 것과 같은 경로입니다.

<details>
<summary><b>저장소 구성</b></summary>

```
setup-ctf/                  ← ~/.claude/skills/setup-ctf 가 이 폴더를 가리킴
├── SKILL.md                  관리 세션 절차서 (셋업 · 이벤트 대응 · 지시 매핑)
├── bin/ctfm                  CLI 진입점 (Python 표준 라이브러리만 사용)
├── ctfm/
│   ├── cli.py                명령: init check up status spawn kill send report hook …
│   ├── daemon.py             폴링 · 스케줄링 · spawn · 종료/유휴 감지
│   ├── intake.py             다운로드 · 안전한 압축 해제 · 문제 디렉토리 생성
│   ├── adapters/             ctfd.py (GET 전용), base.py
│   ├── state.py  tmux.py  notify.py
└── templates/
    ├── AGENTS.md             풀이 에이전트 규칙 (jeopardy)
    ├── AGENTS_KOTH.md        KotH 에이전트 규칙
    ├── KOTH_STRATEGY.md      KotH 전략·제출 절차 (AGENTS_KOTH.md 7번에 삽입)
    ├── CHALLENGE.md          문제 설명 템플릿
    └── adapter_skeleton.py   CTFd가 아닌 사이트용 어댑터 뼈대
tests/                      mock CTFd + 격리된 tmux 서버 기반 테스트
install.sh                  설치 스크립트
DESIGN.md                   전체 설계 문서와 검증 기록
```
</details>

<details>
<summary><b>대회 디렉토리 구성</b> (관리 세션의 cwd)</summary>

```
<CTF이름>/
├── .env                    CTFD_TOKEN 등 인증 정보 (권한 600, 문제 디렉토리에는 복사하지 않음)
├── CTF_NOTES.md            관리 세션이 쌓는 이 대회의 특징 (플래그 형식, 인프라 특이점 …)
├── .ctfm/                  config.json · state.json · events.log · launch/
├── pwn/
│   └── <문제이름>/          ← 풀이 에이전트의 작업 디렉토리
│       ├── AGENTS.md         규칙 (+ CLAUDE.md 는 @AGENTS.md 한 줄)
│       ├── CHALLENGE.md      스크래핑한 문제 설명 · 접속 정보 · 힌트
│       ├── STATE.md          에이전트가 쓰는 풀이 논리 ([검증됨]/[추정])
│       ├── BLOCKED.md        최근 막힘 설명
│       └── <압축 해제된 문제 파일>
└── web/  rev/  crypto/  ai/  misc/
```
</details>

<br>

## 📦 설치

### 0. 준비물

| 필수 | 선택 |
|---|---|
| Linux / WSL2 / macOS | `7z` — 7z·rar 첨부 해제 (`sudo apt install p7zip-full`) |
| Python **3.12+** (또는 3.11.4+) | `insane-codex` — Codex 풀이 에이전트 |
| `tmux` | `npx` — 풀이용 스킬 설치 |
| [Claude Code](https://docs.claude.com/claude-code) (`claude`) 로그인 완료 | Discord webhook URL |

### 1. 저장소 받기

```bash
git clone https://github.com/Hyunsoo-ds/ctf-management-system.git ~/ctf-management-system
cd ~/ctf-management-system
```

### 2. 설치 스크립트 실행

```bash
./install.sh
```

스크립트가 하는 일:

| 단계 | 내용 |
|---|---|
| 1/6 의존성 확인 | python3(안전한 tar 해제 지원 여부까지), tmux, claude, 7z, insane-codex |
| 2/6 풀이용 스킬 | `solve-challenge`, `ctf-*` 스킬이 없으면 `npx skills add ljagiello/ctf-skills -g` 실행을 제안 |
| 3/6 스킬 등록 | `~/.claude/skills/setup-ctf` → 저장소의 `setup-ctf/` 심볼릭 링크 |
| 4/6 Discord | **포럼 채널**의 webhook URL을 받아 저장소 `.env`에 저장 (권한 600). 일반 텍스트 채널 webhook은 포스트를 만들 수 없어 쓸 수 없습니다 |
| 5/6 권한 확인 창 | `~/.claude/settings.json`에 `skipDangerousModePermissionPrompt: true` 추가 제안 |
| 6/6 테스트 | mock CTFd와 격리된 tmux 서버로 전체 흐름 테스트 (약 15초) |

> [!NOTE]
> 5단계가 필요한 이유: 풀이 에이전트는 `--dangerously-skip-permissions`로 뜹니다.
> 확인 창이 켜져 있으면 에이전트가 그 창에서 멈춥니다.

### 3. 대회 폴더 신뢰하기 (최초 1회)

대회 디렉토리들을 모아 둘 상위 폴더를 만들고, 그곳에서 Claude Code를 한 번 실행해 **"Yes, I trust this folder"** 를 선택합니다.
하위 폴더(대회 · 문제 디렉토리)는 신뢰가 상속되므로 풀이 에이전트가 신뢰 대화상자에서 멈추지 않습니다.

```bash
mkdir -p ~/ctf && cd ~/ctf && claude      # 신뢰 선택 후 /exit
```

> [!WARNING]
> 상위 폴더는 git 저장소 **밖**에 두세요. git 저장소 안에서는 신뢰가 하위로 상속되지 않습니다.

### 4. 확인

```bash
ls -l ~/.claude/skills/setup-ctf          # 저장소의 setup-ctf/ 를 가리키면 OK
~/.claude/skills/setup-ctf/bin/ctfm -h    # 명령 목록 출력
```

Claude Code에서 `/setup-ctf`가 자동완성되면 준비 완료입니다.

<br>

## 🚀 사용법

### 대회 시작

```bash
mkdir ~/ctf/MyCTF2026 && cd ~/ctf/MyCTF2026 && claude
```

```
/setup-ctf https://ctf.example.com --token ctfd_xxx --start "2026-11-28 10:00" --max-agents 6 --priority "Baby Heap,Note App"
```

| 인자 | 설명 | 기본값 |
|---|---|---|
| `<URL>` | 문제 사이트 주소 (`/challenges`까지 붙여도 됨) | 필수 |
| `--token` | CTFd 액세스 토큰 (설정 → Access Tokens) | 필수 |
| `--start` | 대회 시작 시각. 타임존이 없으면 로컬 시간 | 즉시 |
| `--max-agents` | 동시에 도는 풀이 에이전트 수 | `8` |
| `--priority` | 먼저 풀 문제 이름 (쉼표 구분, 아직 안 열린 문제도 가능) | — |
| `--agent` | 기본 풀이 에이전트 `claude` \| `codex` | `claude` |
| `--model` / `--codex-model` | 각 에이전트의 모델 (effort는 `ctfm set effort` / `codex-effort` / `codex-fast`) | `claude-opus-5` xhigh / `gpt-6-sol` medium fast |

CTFd가 아닌 사이트라면 관리 세션이 사이트를 분석해 즉석에서 어댑터를 작성합니다.

### 풀이 지켜보기

```bash
tmux attach -t MyCTF2026-pwn       # -web -rev -crypto -ai -misc -koth
```

- 플래그를 얻은 문제는 `WRITEUP.md`에 풀이를 남긴 뒤 세션이 `/exit`로 끝나고 윈도우가 지워집니다.
- 윈도우 하나가 문제 하나입니다. pwn · rev 윈도우는 pane이 둘입니다(왼쪽 Codex, 오른쪽 Claude). 이름 앞 표시: `✔` 플래그 획득 · `!` 막힘 알림 보냄(계속 풀이 중) · `?` 답을 기다림
- tmux 안에서 `prefix + w`로 전체 세션/윈도우를 한 화면에서 고를 수 있습니다.
- 윈도우에 직접 입력해 힌트를 줘도 됩니다.

### 관리 세션에 지시하기

| 이렇게 말하면 | 관리 세션이 하는 일 |
|---|---|
| "현황" | `ctfm status` — 슬롯, 대기 순서, 문제별 상태와 경과 시간 |
| "Baby Heap 먼저 풀어" | 대기열 맨 앞으로. 슬롯이 가득 차 있으면 한도를 넘겨 바로 시작 |
| "Note App 끝내" | 윈도우 종료, 슬롯 반환 (나중에 이어서 재개 가능) |
| "XOR한테 키 길이는 4라고 전해" | 해당 에이전트 입력창에 메시지 전달 |
| "Baby Heap은 codex로 풀어" | 해당 문제만 Codex 에이전트로 시작 |
| "최대 4개로 줄여" | 동시 실행 한도 변경 |
| "플래그 자동 제출해" / "Baby Heap 제출해" | 관리 세션이 플래그를 사이트에 제출 (명시적으로 허용했을 때만). 오답이면 재제출하지 않고 알림 |
| "플래그 형식은 FLAG{...}야" | `CTF_NOTES.md`에 기록하고 모든 에이전트에 전파 |

<br>

## 🧠 풀이 에이전트 규칙 요약

각 문제 디렉토리의 [`AGENTS.md`](setup-ctf/templates/AGENTS.md)에 들어갑니다.

1. **문제 디렉토리 안에서만** 작업합니다.
2. **로컬 solver를 먼저** 완성한 뒤 원격에서 진짜 플래그를 얻습니다. 배포본의 가짜 플래그는 보고하지 않습니다.
3. **문제 사이트에 접근하지 않습니다.** 제출 · 힌트 해제 · API 호출 모두 금지.
4. **먼저 물어볼 것**: 토큰/계정 필요, 시도 횟수 제한, rate limit · 대량 요청, 잠긴 힌트, 원격 서버 다운, Linux CLI로 풀기 어려운 문제(VM 이미지 침해 사고 분석 등).
5. **막히면** `ctfm report retry` → 문제를 처음부터 다시 읽기 → 풀이 논리 재검증 → 놓친 공격 표면 점검. 2번째 막힘에서 Claude는 effort를 `max`로 올리고 컨텍스트를 compact한 뒤 처음부터 엄밀하게 다시 봅니다. 3번째 막힘에서는 배경지식 없는 사람도 이해할 수 있게 `BLOCKED.md`를 쓰고 알림. **그리고 계속 풉니다.**

### 👑 King of the Hill

사이트 카테고리나 문제 이름에 `KotH` / `King of the Hill`이 있으면 `koth` 유형으로 분류되어 다르게 다룹니다.

| | |
|---|---|
| 대기 순서 | 모든 문제보다 먼저 (초반 라운드 점수 확보가 핵심) |
| 모델 | 초반 `gpt-6.2-astra`(codex, fast) → 라운드 4부터 `claude-mythos-5-2`(막히면 `claude-opus-5-5`). 사용량 한도나 안전 차단이 보이면 자동으로 다음 모델로 전환 |
| 메인 전환 | 라운드 3부터 Mythos 5.2(high)가 옆 pane에서 Astra의 작업을 파악하며 다음 전략을 준비 → 라운드 4를 목표로 관리 세션이 전환하고 Astra 세션 종료 (준비가 덜 됐으면 그 뒤 라운드) |
| fast | Astra는 메인인 동안 라운드 4까지 `/fast` 켬 (에이전트가 보고한 라운드 기준) |
| 사이트 접근 | 문제 디렉토리의 `CTF_ACCESS.env`(권한 600)로 그 문제의 라운드·점수판 조회 위임 |
| 라운드 요약 | 라운드가 바뀔 때마다 직전 결과, 제출한 후보, 이번 계획을 Discord의 그 문제 포스트로 전송 |
| 경쟁형 문제 | 다른 팀과 겨루는 문제면 메인이 Claude로 전환된 뒤부터 상대 전략을 분석하는 subagent를 병렬로 돌려 대응 방안을 후보에 반영 |
| 제출 | 매 라운드 반드시 제출. 라운드 1~3은 간단하지만 확실한 규칙 기반 후보로 점수부터 확보 ([`KOTH_STRATEGY.md`](setup-ctf/templates/KOTH_STRATEGY.md)) |

### Claude vs Codex

| | Claude Code | Codex (`insane-codex`) |
|---|---|---|
| 시작 | `/solve-challenge <분야>` | `$solve-challenge <분야>` |
| 포기 차단 | 초기 프롬프트의 `/goal`: 플래그를 얻어 보고할 때까지 스스로 턴을 이어 감 | 같음 (`/goal`) |
| 멈춤 대응 | 데몬 유휴 감지 (보조) | 데몬 유휴 감지: 120초 넘게 입력 대기면 "계속하라" 자동 입력 |
| 질문 대화상자 | 도구 비활성화 | `AGENTS.md` 규칙으로 금지 |

<br>

## 🧪 테스트

```bash
python3 -m unittest discover -s tests       # 단위 + 흐름 테스트
python3 tests/mock_ctfd.py --port 8765      # 수동 실험용 mock CTFd (토큰: testtoken)
```

실제 에이전트로 전체 흐름을 리허설하려면 가상 CTF를 띄웁니다. 분야별 jeopardy 문제 6개(pwn · rev · web · crypto · ai · misc)와 pwn/web 원격 서비스가 함께 뜹니다.

```bash
python3 rehearsal/serve.py                  # 문제 사이트 :8000, pwn 원격 :31337, web 원격 :8081
# 다른 터미널에서: mkdir ~/ctf/REHEARSAL && cd ~/ctf/REHEARSAL && claude
#   /setup-ctf http://127.0.0.1:8000 --token testtoken
```

<br>

## ⚠️ 알려진 한계

- **격리 수준**: 문제 디렉토리 밖 접근 금지는 `AGENTS.md` 규칙일 뿐, OS 수준 격리(샌드박스)는 하지 않습니다.
- **토큰 소비**: 포기 차단 때문에 막힌 에이전트도 계속 토큰을 씁니다. `--max-agents`를 사용량에 맞추세요.
- **CTFd 팀 모드**는 아직 실제 인스턴스로 검증하지 않았습니다 (개인전 모드는 로컬 CTFd로 검증).
- 자세한 설계와 검증 기록은 [`DESIGN.md`](DESIGN.md)를 참고하세요.

#!/usr/bin/env bash
# setup-ctf 설치 스크립트
#   - 의존성 확인 (python3, tmux, claude / 선택: 7z, insane-codex)
#   - 풀이용 스킬(solve-challenge, ctf-*) 확인 및 설치 제안
#   - ~/.claude/skills/setup-ctf 심볼릭 링크 생성
#   - Discord webhook(.env) 설정
#   - Claude Code bypass 확인 창 끄기 (풀이 에이전트가 확인 창에서 멈추지 않도록)
#   - 테스트 실행
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILLS_DIR="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}"
CLAUDE_SETTINGS="$HOME/.claude/settings.json"

if [ -t 1 ]; then
  B=$'\e[1m'; G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; D=$'\e[2m'; N=$'\e[0m'
else
  B=''; G=''; Y=''; R=''; D=''; N=''
fi
step() { printf '\n%s==> %s%s\n' "$B" "$1" "$N"; }
ok()   { printf '  %s✔%s %s\n' "$G" "$N" "$1"; }
warn() { printf '  %s!%s %s\n' "$Y" "$N" "$1"; }
fail() { printf '  %s✘%s %s\n' "$R" "$N" "$1"; exit 1; }
ask()  { local a=""; read -r -p "  $1 [y/N] " a || true; [[ "$a" =~ ^[Yy]$ ]]; }

printf '%ssetup-ctf 설치%s  %s(%s)%s\n' "$B" "$N" "$D" "$REPO_DIR" "$N"

# ── 1. 의존성 ────────────────────────────────────────────────────────────────
step "1/6 의존성 확인"
command -v python3 >/dev/null || fail "python3 가 필요합니다"
python3 -c 'import tarfile, sys; sys.exit(0 if hasattr(tarfile, "data_filter") else 1)' \
  || fail "python3 $(python3 -V 2>&1 | cut -d' ' -f2): tarfile 안전 필터가 없습니다. Python 3.12 이상(또는 3.11.4+)이 필요합니다"
ok "python3 $(python3 -V 2>&1 | cut -d' ' -f2)"
command -v tmux >/dev/null || fail "tmux 가 필요합니다 (예: sudo apt install tmux)"
ok "tmux $(tmux -V | cut -d' ' -f2)"
command -v claude >/dev/null || fail "Claude Code CLI(claude)가 필요합니다: https://docs.claude.com/claude-code"
ok "claude $(claude --version 2>/dev/null | cut -d' ' -f1)"
if command -v 7z >/dev/null; then ok "7z (7z/rar 첨부 해제용)"; else warn "7z 없음: 7z/rar 첨부는 압축을 풀지 못하고 원본만 둡니다 (sudo apt install p7zip-full)"; fi
if command -v insane-codex >/dev/null; then ok "insane-codex (codex 풀이 에이전트용)"; else warn "insane-codex 없음: claude 에이전트만 쓸 수 있습니다"; fi

# ── 2. 풀이용 스킬 ───────────────────────────────────────────────────────────
step "2/6 풀이용 스킬 (solve-challenge, ctf-*)"
if [ -e "$SKILLS_DIR/solve-challenge/SKILL.md" ]; then
  ok "solve-challenge 설치됨"
else
  warn "solve-challenge 스킬이 없습니다. 풀이 에이전트가 이 스킬로 시작합니다"
  if command -v npx >/dev/null && ask "npx skills add ljagiello/ctf-skills -g 로 설치할까요? (에이전트 선택에서 claude-code, codex 선택)"; then
    npx skills add ljagiello/ctf-skills -g
  else
    warn "나중에 직접 설치하세요: npx skills add ljagiello/ctf-skills -g"
  fi
fi

# ── 3. setup-ctf 스킬 링크 ───────────────────────────────────────────────────
step "3/6 setup-ctf 스킬 등록"
mkdir -p "$SKILLS_DIR"
LINK="$SKILLS_DIR/setup-ctf"
if [ -e "$LINK" ] && [ ! -L "$LINK" ]; then
  fail "$LINK 가 이미 있고 심볼릭 링크가 아닙니다. 옮기거나 지운 뒤 다시 실행하세요"
fi
ln -sfn "$REPO_DIR/setup-ctf" "$LINK"
chmod +x "$REPO_DIR/setup-ctf/bin/ctfm"
ok "$LINK → $REPO_DIR/setup-ctf"

# ── 4. Discord webhook ───────────────────────────────────────────────────────
step "4/6 Discord 알림 (.env)"
ENV_FILE="$REPO_DIR/.env"
if [ -f "$ENV_FILE" ] && grep -q '^DISCORD_WEBHOOK_URL=.' "$ENV_FILE"; then
  ok "DISCORD_WEBHOOK_URL 설정됨 ($ENV_FILE)"
else
  url=""
  read -r -p "  Discord webhook URL (건너뛰려면 Enter): " url || true
  if [ -n "$url" ]; then
    printf 'DISCORD_WEBHOOK_URL=%s\n' "$url" >> "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    ok "저장됨 ($ENV_FILE, 권한 600)"
  else
    warn "건너뜀: 플래그/막힘/질문 알림이 전송되지 않습니다. 나중에 $ENV_FILE 에 DISCORD_WEBHOOK_URL=... 추가"
  fi
fi

# ── 5. bypass 확인 창 ────────────────────────────────────────────────────────
step "5/6 Claude Code 권한 확인 창"
skip=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('skipDangerousModePermissionPrompt', False))" \
  "$CLAUDE_SETTINGS" 2>/dev/null || echo False)
if [ "$skip" = "True" ]; then
  ok "skipDangerousModePermissionPrompt 이미 켜짐"
else
  warn "풀이 에이전트는 --dangerously-skip-permissions 로 뜹니다. 확인 창이 켜져 있으면 에이전트가 그 창에서 멈춥니다"
  if ask "$CLAUDE_SETTINGS 에 skipDangerousModePermissionPrompt: true 를 추가할까요?"; then
    python3 - "$CLAUDE_SETTINGS" <<'EOF'
import json, sys
from pathlib import Path
p = Path(sys.argv[1])
d = json.loads(p.read_text()) if p.is_file() else {}
d["skipDangerousModePermissionPrompt"] = True
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
EOF
    ok "추가함"
  else
    warn "건너뜀: claude 를 한 번 --dangerously-skip-permissions 로 실행해 확인 창을 직접 승인하세요"
  fi
fi

# ── 6. 테스트 ────────────────────────────────────────────────────────────────
step "6/6 테스트 (mock CTFd + 격리된 tmux 서버, 약 15초)"
if (cd "$REPO_DIR" && python3 -m unittest discover -s tests >/tmp/setup-ctf-test.log 2>&1); then
  ok "$(grep -o 'Ran [0-9]* tests' /tmp/setup-ctf-test.log) 통과"
else
  tail -n 20 /tmp/setup-ctf-test.log
  fail "테스트 실패 (전체 로그: /tmp/setup-ctf-test.log)"
fi
find "$REPO_DIR" -name __pycache__ -prune -exec rm -rf {} +

cat <<EOF

${G}${B}설치 완료${N}

  다음 단계
  1. 대회 디렉토리들을 둘 상위 폴더를 만들고, 그곳에서 claude 를 한 번 실행해 폴더를 신뢰합니다.
       ${D}mkdir -p ~/ctf && cd ~/ctf && claude${N}     ${D}# "Yes, I trust this folder" 선택 후 종료${N}
  2. 대회마다 디렉토리를 만들고 관리 세션을 시작합니다.
       ${D}mkdir ~/ctf/<CTF이름> && cd ~/ctf/<CTF이름> && claude${N}
       ${D}/setup-ctf https://ctf.example.com --token <ctfd 토큰> --start "2026-11-28 10:00"${N}
EOF

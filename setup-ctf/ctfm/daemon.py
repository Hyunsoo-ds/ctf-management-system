"""폴링 → 수집 → 큐 → spawn 을 LLM 없이 처리하는 데몬과 스케줄러."""
import fcntl
import hashlib
import re
import shlex
import time
from pathlib import Path

from . import adapters, intake, tmux
from .state import (ACTIVE, CTFM_BIN, DUAL_CATEGORIES, label, map_category, pane_label, panes, queue_key, screen_order,
                    unique_slug)

AGENTS = ("claude", "codex")
FIRST_PROMPT = (
    "{skill} {category} 이 디렉토리의 CTF 문제를 풀어라. "
    "먼저 AGENTS.md(규칙)와 CHALLENGE.md(문제 설명, 접속 정보)를 읽어라. 문제 파일은 이 디렉토리에 풀려 있다."
)
FIRST_PROMPT_KOTH = (
    "{skill} 이 디렉토리의 King of the Hill 문제를 맡아라. 먼저 AGENTS.md(규칙)와 CHALLENGE.md(문제 설명)를 읽어라. "
    "초반 라운드 점수 확보가 최우선이다. 사이트에서 현재 라운드를 확인해 보고하고(AGENTS.md 4번), 동작하는 최선의 설계를 빠르게 만들어라."
)
SUCCESSOR_PROMPT = (
    "{skill} 이 디렉토리의 King of the Hill 문제를 곧 넘겨받는다. 지금은 인수 준비 단계이고, 메인 에이전트(codex)가 왼쪽 pane에서 "
    "계속 진행 중이다. 먼저 AGENTS.md를 읽고 8번(인수 준비) 규칙을 따라라: 제출하지 않고, 메인의 파일을 고치지 않는다. "
    "CHALLENGE.md와 STATE.md로 지금까지의 작업을 파악해 넘겨받을 준비를 하고, 끝나면 `ctfm report ready`로 보고하라."
)
HANDOVER_PROMPT = (
    "전환 완료: 이제 네가 이 문제의 메인 에이전트다. 이전 메인(codex) 세션은 종료됐다. AGENTS.md 8번의 제한은 풀렸고 1~7번 전체가 적용된다.\n"
    "1. 사이트와 STATE.md에서 현재 라운드와 이번 라운드 제출 여부를 확인하고 `ctfm report round`로 보고한다(4번).\n"
    "2. 이번 라운드에 아직 제출하지 않았다면 검증된 최선의 후보를 먼저 제출한다(7.1).\n"
    "3. STATE.md를 이어서 쓰고, HANDOVER.md에 세운 개선 계획을 진행한다.\n"
    "4. 다른 팀과 경쟁하는 문제면 7.5대로 상대 전략 분석 subagent를 지금 띄운다."
)
SKILL_CALL = {"claude": "/solve-challenge", "codex": "$solve-challenge"}
RESUME_PROMPT = "중단됐던 풀이를 이어서 계속하라. 먼저 AGENTS.md와 STATE.md를 다시 읽어라."
TRUST_PATTERN = re.compile(r"do you trust|trust this folder|trust the files", re.I)
BUSY_PATTERN = re.compile(r"esc to interrupt", re.I)  # claude와 codex 모두 작업 중일 때 하단에 표시
# 모델을 더 쓸 수 없다는 화면 문구(사용량 한도, 안전 차단). [?] 실제 문구는 일부만 확인됨. 오탐을 줄이려고 KotH에서,
# 에이전트가 작업 중이 아닐 때, 화면 마지막 몇 줄에서만 찾는다.
UNAVAILABLE_PATTERN = re.compile(
    r"usage limit|hit your (usage )?limit|limit reached|out of (credits|usage)|purchase more credits|upgrade your plan"
    r"|violate our usage policy|unable to respond to this request|flagged as potentially|safety (block|check failed)"
    r"|high.risk cyber", re.I)
CODEX_FOOTER = re.compile(r"GPT-[\w.\-]+ (\w+)( fast)? ·")  # 예: "GPT-6.2-Astra high fast · /path ·" → (effort, fast)
EFFORT_MENU = {"low": "Low", "medium": "Medium", "high": "High"}  # codex /model 메뉴의 effort 항목 이름
EXIT_DIALOG = re.compile(r"Background work is running|Exit and stop tasks")  # claude: 백그라운드 작업이 있을 때의 종료 확인 창
CHECK_EVERY = 15
NUDGE_MESSAGE = (
    "플래그를 아직 얻지 못했다. 이 작업은 플래그를 얻어야 끝나며 포기는 없다. 지금 이어서 진행하라. "
    "다음 가설이 있으면 바로 실행하고, 없으면 AGENTS.md 8번(막혔을 때) 절차를, 재검토를 2회 했다면 9번(insight 요청)을 수행한 뒤 계속하라. "
    "토큰/시도 제한/rate limit 때문에 사용자 확인이 필요하면 AGENTS.md 4번대로 `ctfm report ask`를 쓴다."
)
NUDGE_MESSAGE_KOTH = (
    "KotH는 대회가 끝날 때까지 점수를 올리는 문제다. 멈추지 말고 이어서 진행하라. 현재 라운드를 확인해 바뀌었으면 "
    "`ctfm report round`로 진행 요약과 함께 보고하고(AGENTS.md 4번), 이번 라운드에 아직 제출하지 않았다면 마감 전에 검증된 최선의 후보부터 제출하라(AGENTS.md 7.1). 그다음 다음 라운드를 위한 개선을 계속하라. 개선 아이디어가 없으면 AGENTS.md 6번 절차를 따른다."
)
DUAL_PROMPT = (
    " 이 문제는 {other} 에이전트가 같은 디렉토리에서 동시에 풀고 있다. AGENTS.md 1번의 동시 풀이 규칙을 따르고, "
    "네 기록 파일 이름에는 `{agent}`를 붙여라(STATE.{agent}.md)."
)
RIGOROUS_PROMPT = (
    "effort를 max로 올렸고 컨텍스트를 compact했다. 지금까지의 결론을 믿지 말고 처음부터 엄밀하게 다시 본다.\n"
    "1. AGENTS.md, CHALLENGE.md, STATE.md를 다시 읽는다.\n"
    "2. 배포 파일을 처음부터 다시 분석한다. 기억과 요약에 의존하지 않는다.\n"
    "3. STATE.md의 전제를 `[검증됨]`으로 적힌 것까지 하나씩 실제 근거(코드, 주소, 실행 출력)로 다시 확인한다. 근거를 다시 만들지 못한 전제는 `[추정]`으로 내린다.\n"
    "4. 버린 접근도 버린 이유가 맞았는지 다시 확인한다.\n"
    "5. 틀린 전제나 놓친 표면을 찾았으면 새 가설로 풀이를 재개한다. 다시 막히면 `ctfm report retry`를 실행한다."
)


def koth_chain(cfg):
    return cfg.get("koth", {}).get("chain") or []


def settings(cfg, ch, p):
    """pane p의 (모델, effort, fast). pane에 기록된 모델과 effort가 설정 기본값보다 먼저다.
    KotH는 설정의 기본 effort를 쓰지 않고(체인 항목에 있을 때만) fast는 라운드 기준으로 따로 정한다(sync_codex)."""
    koth = ch["category"] == "koth"
    if p["agent"] == "codex":
        return p.get("model") or cfg.get("codex_model"), None if koth else cfg.get("codex_effort"), not koth and bool(cfg.get("codex_fast"))
    return p.get("model") or cfg["model"], p.get("effort") or (None if koth else cfg.get("effort")), False


def describe(cfg, ch, p):
    model, effort, fast = settings(cfg, ch, p)
    return " ".join(filter(None, (p["agent"], model, effort, "fast" if fast else None)))


def agent_command(store, cfg, ch, p, resume):
    if cfg.get("agent_cmd"):  # 테스트용
        return cfg["agent_cmd"]
    agent = p["agent"]
    model, effort, _ = settings(cfg, ch, p)
    if agent == "codex":
        # insane-codex는 빌드 기본값이 --yolo다. -c, -p, --enable/--disable, --dangerously-bypass-hook-trust를 주면
        # 공유 app-server 대신 내장 모드로 뜨는데, 이 빌드에는 codex-code-mode-host가 없어 셸 명령이 전부 실패한다.
        # 그래서 Stop hook(포기 차단)과 질문 도구 차단 hook 없이 띄운다 (DESIGN.md 11절). -m은 괜찮다.
        # effort와 fast도 옵션으로 줄 수 없어 뜬 뒤에 데몬이 화면에서 맞춘다(sync_codex).
        parts = ["insane-codex", *(["resume", "--last"] if resume else []), *(["-m", model] if model else [])]
        return " ".join(shlex.quote(p) for p in parts)
    # 질문 대화상자와 plan 승인 대기는 Stop hook에 걸리지 않고 조용히 멈추는 통로라 도구 자체를 끈다.
    return ("claude --disallowedTools AskUserQuestion,EnterPlanMode "
            f"--dangerously-skip-permissions --model {shlex.quote(model)} "
            + (f"--effort {shlex.quote(effort)} " if effort else "")
            + f"-n {shlex.quote(f'{store.root.name}/{label(ch)}')}" + (" --continue" if resume else ""))


def launch_script(store, cfg, ch, p, resume):
    """pane 하나의 실행 스크립트를 만들어 그 경로를 셸 인자로 돌려준다."""
    agent = p["agent"]
    launch = store.meta / "launch"
    launch.mkdir(parents=True, exist_ok=True)
    key = f"{ch['category']}-{ch['slug']}.{agent}"
    prompt_file, script = launch / f"{key}.prompt", launch / f"{key}.sh"
    # 모델 전환으로 윈도우를 바꿔 띄울 때 이전 셸이 늦게 쓰는 종료 표식과 섞이지 않게 실행마다 다른 파일을 쓴다
    exit_file = launch / f"{key}.{int(time.time() * 1000)}.exit"
    koth = ch["category"] == "koth"
    first_prompt = SUCCESSOR_PROMPT if p.get("role") == "successor" else FIRST_PROMPT_KOTH if koth else FIRST_PROMPT
    prompt = RESUME_PROMPT if resume else first_prompt.format(skill=SKILL_CALL[agent], category=ch["category"])
    if ch.get("peer") and not koth:
        prompt += DUAL_PROMPT.format(agent=agent, other="claude" if agent == "codex" else "codex")
    prompt_file.write_text(prompt)
    script.write_text(
        "#!/bin/sh\n"
        f"unset {' '.join(tmux.SESSION_VARS)}\n"
        "export CLAUDE_CODE_STOP_HOOK_BLOCK_CAP=1000000\n"  # 포기 차단 hook이 8회 제한에 걸리지 않게
        f"export CTFM_AGENT={agent}\n"  # `ctfm report`가 동시 풀이에서 보고한 쪽을 구분한다 (codex 셸에는 전달되지 않는다)
        f"export PATH={shlex.quote(str(CTFM_BIN.parent))}:\"$PATH\"\n"  # 에이전트가 `ctfm report`로 부를 수 있게
        f"cd {shlex.quote(str(store.root / ch['dir']))} || exit 1\n"
        f'{agent_command(store, cfg, ch, p, resume)} "$(cat {shlex.quote(str(prompt_file))})"\n'
        f"echo $? > {shlex.quote(str(exit_file))}\n"
    )
    script.chmod(0o755)
    p["exit_file"] = str(exit_file)
    p.pop("exited", None)
    return shlex.quote(str(script))


def apply_entry(p, chain, idx):
    entry = chain[idx]
    p.update(chain_idx=idx, agent=entry["agent"], model=entry["model"], effort=entry.get("effort"))


def spawn(store, cfg, ch, resume=False, agent=None):
    """문제 하나를 tmux 윈도우에 띄운다. 호출자는 store.locked() 안에 있어야 한다.
    에이전트는 인자 → 문제에 기록된 값 → 설정 기본값 순으로 정한다(resume은 같은 에이전트로 이어야 하므로).
    KotH는 처음 띄울 때 모델 체인의 첫 항목(기본 codex Astra)을 쓴다.
    동시 풀이(기본 pwn, rev)는 윈도우를 좌우로 나눠 왼쪽에 codex, 오른쪽에 claude를 띄운다. 슬롯은 하나만 쓴다."""
    if ch["category"] == "koth" and agent is None and "chain_idx" not in ch and koth_chain(cfg):
        apply_entry(ch, koth_chain(cfg), 0)
    if ch.get("dual", ch["category"] in DUAL_CATEGORIES):
        agent = "claude"  # 포기 차단 hook과 effort 상향이 있는 쪽이 주 에이전트
        ch.setdefault("peer", {"agent": "codex", "retries": 0})
    else:
        ch.pop("peer", None)
    ch["agent"] = agent or ch.get("agent") or cfg.get("agent", "claude")
    chal_dir = store.root / ch["dir"]
    session = tmux.session_name(store.root, ch["category"])
    ch["window_id"], ch["pane"] = tmux.spawn_window(session, ch["slug"], chal_dir, launch_script(store, cfg, ch, ch, resume))
    if ch.get("peer"):
        ch["peer"]["pane"] = tmux.split(ch["pane"], chal_dir, launch_script(store, cfg, ch, ch["peer"], resume), left=True)
    ch.update(status="running", tmux_session=session, spawned_at=time.time())
    who = " | ".join(describe(cfg, ch, p) for p in screen_order(ch))
    store.emit("SPAWNED", f"{label(ch)} → tmux {session}:{ch['slug']} ({who})" + (" (resume)" if resume else ""))


def schedule(store):
    """슬롯이 비어 있으면 대기 순서대로 spawn 한다."""
    with store.locked() as (cfg, st):
        chals = st["challenges"]
        while sum(c["status"] in ACTIVE for c in chals.values()) < int(cfg["max_agents"]):
            queued = [c for c in chals.values() if c["status"] == "queued"]
            if not queued:
                break
            spawn(store, cfg, min(queued, key=lambda c: queue_key(c, cfg["priority"])))


def stop_agent(cfg, p):
    """pane의 에이전트를 정상 종료시킨다. 종료가 확인되면 True. 이 뒤에 호출자가 pane이나 윈도우를 지운다.
    Escape로 진행 중인 턴을 끊고 /exit를 보낸 뒤, 실행 스크립트가 종료 표식 파일을 쓸 때까지 기다린다.
    codex는 /exit만 보내면 화면만 끊기고 작업은 app-server에서 계속 돌기 때문에, 턴을 끊고 /stop으로 백그라운드
    터미널도 정리한 뒤에 나간다. claude는 백그라운드 작업이 있으면 확인 창이 뜨고 기본 선택이 "Exit and stop tasks"다."""
    wait = float(cfg.get("exit_wait", 20))
    if not p.get("exit_file") or p.get("exited") or Path(p["exit_file"]).is_file():
        return True
    if not wait:
        return False
    pane = p["pane"]
    tmux.keys(pane, "Escape", wait=0.5)
    deadline = time.time() + 5
    while BUSY_PATTERN.search(tmux.capture(pane, 15)) and time.time() < deadline:  # 중단이 반영될 때까지
        time.sleep(0.5)
    if p["agent"] == "codex":
        tmux.type_command(pane, "/stop")
        time.sleep(1)
    tmux.type_command(pane, "/exit")
    deadline = time.time() + wait
    while time.time() < deadline and not Path(p["exit_file"]).is_file():
        if EXIT_DIALOG.search(tmux.capture(pane, 15)):
            tmux.keys(pane, "Enter", wait=1)
        time.sleep(0.5)
    return Path(p["exit_file"]).is_file()


FORCED = " (/exit로 끝나지 않아 강제 종료)"


def spawn_successor(store, cfg, ch, idx, resume=False):
    """KotH: 체인의 idx번 모델을 후임으로 메인의 오른쪽 pane에 띄워 인수 준비를 시킨다(AGENTS.md 8번)."""
    peer = ch.setdefault("peer", {"retries": 0, "role": "successor"})
    apply_entry(peer, koth_chain(cfg), idx)
    peer.pop("ready", None)
    peer["pane"] = tmux.split(ch["pane"], store.root / ch["dir"], launch_script(store, cfg, ch, peer, resume), left=False)


def promote(store, cfg, ch):
    """KotH 전환: 후임을 메인으로 올리고 이전 메인 세션을 종료한다. 후임은 같은 대화를 이어 간다.
    이전 메인이 정상 종료됐는지를 돌려준다."""
    peer = ch.pop("peer")
    clean = stop_agent(cfg, ch)
    tmux.kill_pane(ch["pane"])
    for key in ("role", "ready"):
        peer.pop(key, None)
    for key in ("idle_checked", "idle_screen", "idle_since", "nudges", "fast_sent", "exited", "trust_checked", "fallback_exhausted"):
        ch.pop(key, None)
    ch.update(peer)
    tmux.send_text(ch["pane"], HANDOVER_PROMPT)
    return clean


def koth_fallback(store, cfg, ch, p, reason):
    """모델을 더 쓸 수 없는 KotH pane을 체인의 다음 항목으로 넘긴다. 같은 에이전트(claude→claude)면 대화를 이어 간다.
    메인이 막혔는데 준비 중인 후임이 있으면 후임에게 바로 넘긴다."""
    chain, idx = koth_chain(cfg), p.get("chain_idx", -1)
    if p is ch and ch.get("peer"):
        prev = ch.get("model")
        promote(store, cfg, ch)
        store.emit("MODEL_FALLBACK", f"{label(ch)} {prev} 사용 불가({reason}) → 준비 중이던 후임 {ch['model']}에게 넘김")
        return
    if idx < 0 or idx + 1 >= len(chain):
        if not p.get("fallback_exhausted"):
            p["fallback_exhausted"] = True
            store.emit("MODEL_UNAVAILABLE", f"{pane_label(ch, p)} {p.get('model')} 사용 불가({reason}). 넘어갈 다음 모델이 없음, 확인 필요")
        return
    prev, nxt = chain[idx], chain[idx + 1]
    resume = prev["agent"] == nxt["agent"]
    stop_agent(cfg, p)
    if p is ch:
        tmux.kill_window(ch["window_id"])
        apply_entry(ch, chain, idx + 1)
        spawn(store, cfg, ch, resume=resume)
    else:
        tmux.kill_pane(p["pane"])
        spawn_successor(store, cfg, ch, idx + 1, resume=resume)
    store.emit("MODEL_FALLBACK", f"{pane_label(ch, p)} {prev['model']} 사용 불가({reason}) → {nxt['model']}로 다시 띄움")


def menu_moves(screen, effort):
    """codex effort 선택 메뉴 화면에서 커서(›)를 원하는 항목까지 옮길 칸 수(아래가 +). 메뉴가 아니면 None."""
    if "Select Reasoning Level" not in screen:
        return None
    rows = {name: (int(n), bool(cur)) for cur, n, name in re.findall(r"^\s*(›)?\s*(\d)\. (Low|Medium|High)\b", screen, re.M)}
    cursor = [n for n, cur in rows.values() if cur]
    if EFFORT_MENU.get(effort) not in rows or not cursor:
        return None
    return rows[EFFORT_MENU[effort]][0] - cursor[0]


def set_codex_effort(pane, effort):
    """codex TUI의 /model 메뉴를 키 입력으로 조작해 이 세션의 effort만 바꾼다(작업 중에도 된다).
    메뉴에서 Enter나 숫자 키는 ~/.codex/config.toml의 기본값까지 바꾸므로, 커서를 옮긴 뒤 `s`(session)로 고른다."""
    tmux.type_command(pane, "/model")
    time.sleep(1)
    tmux.keys(pane, "Enter", wait=1)  # 모델 목록: 커서가 현재 모델에 있다
    moves = menu_moves(tmux.capture(pane), effort)
    if moves is None:
        tmux.keys(pane, "Escape", "Escape")
        return False
    tmux.keys(pane, *(["Down" if moves > 0 else "Up"] * abs(moves)), "s")
    return True


def sync_codex(store, cfg, ch, p, screen, now):
    """codex의 fast와 effort를 하단 표시로 읽어 설정과 다르면 맞춘다. 실행 옵션으로는 줄 수 없어서다(agent_command 참고).
    KotH는 체인 항목의 fast_rounds 라운드까지만 fast를 켜고 effort는 건드리지 않는다."""
    if p["agent"] != "codex" or now - p.get("fast_sent", 0) < 10:
        return
    match = CODEX_FOOTER.search(screen)
    if not match or tmux.being_viewed(ch["window_id"]):  # 사용자가 입력 중일 수 있는 창에는 끼어들지 않는다
        return
    _, effort, fast = settings(cfg, ch, p)
    detail = ""
    if ch["category"] == "koth":
        idx = ch.get("chain_idx", -1)
        if idx < 0:
            return
        fast_rounds = int(koth_chain(cfg)[idx].get("fast_rounds") or 0)
        fast = (ch.get("round") or 0) <= fast_rounds and fast_rounds > 0
        detail = f" (라운드 {ch.get('round') or '?'}, 기준 {fast_rounds}라운드까지)"
    if fast != bool(match.group(2)):
        tmux.type_command(p["pane"], "/fast")  # 토글. ~/.codex/config.toml의 service_tier도 함께 바뀐다
        p["fast_sent"] = now
        store.emit("FAST", f"{pane_label(ch, p)} fast {'켬' if fast else '끔'}{detail}")
    elif effort and effort != match.group(1) and p.get("effort_tries", 0) < 3:
        p["fast_sent"] = now
        if set_codex_effort(p["pane"], effort):
            store.emit("EFFORT", f"{pane_label(ch, p)} codex effort {match.group(1)} → {effort}")
        else:
            p["effort_tries"] = p.get("effort_tries", 0) + 1
            if p["effort_tries"] == 3:
                store.emit("EFFORT_ERROR", f"{pane_label(ch, p)} codex effort를 {effort}로 바꾸지 못함 (/model 메뉴를 읽지 못함). 현재 {match.group(1)}")


def escalate(store, ch, p, busy):
    """재검토 2회째의 claude: 턴이 끝나면 effort를 max로 올리고 compact한 뒤 엄밀한 재검토를 지시한다.
    pending → (유휴) ready → (계속 유휴면 /effort max, /compact 입력) compacting → (compact 끝) 지시 전송."""
    if busy:
        if p["escalate"] == "ready":  # 도구 호출 사이의 순간이었다
            p["escalate"] = "pending"
    elif p["escalate"] == "pending":
        p["escalate"] = "ready"
    elif p["escalate"] == "ready":
        tmux.type_command(p["pane"], "/effort max")
        time.sleep(1)
        tmux.type_command(p["pane"], "/compact")
        p.update(escalate="compacting", effort="max")
    else:
        tmux.send_text(p["pane"], RIGOROUS_PROMPT)
        del p["escalate"]
        store.emit("ESCALATED", f"{pane_label(ch, p)} 재검토 2회째: effort max로 올리고 compact한 뒤 엄밀한 재검토를 지시함")


def watch(store, cfg, ch, p, now):
    """실행 중인 에이전트 화면을 주기적으로 본다: KotH 모델 사용 불가, codex fast/effort, effort 상향, 입력 대기 멈춤.
    유휴 판정: 하단에 작업 중 표시가 없고 화면이 idle_nudge초 동안 그대로. 질문 대기와 사용자가 보는 창은 건드리지 않는다."""
    limit = int(cfg.get("idle_nudge", 120))
    every = 3 if p.get("escalate") else min(CHECK_EVERY, limit or CHECK_EVERY)
    if ch["status"] not in ("running", "blocked") or now - p.get("idle_checked", 0) < every:
        return
    p["idle_checked"] = now
    screen = tmux.capture(p["pane"], 15)
    busy = bool(BUSY_PATTERN.search(screen))
    if ch["category"] == "koth":
        tail = "\n".join(screen.splitlines()[-8:])
        hit = None if busy else UNAVAILABLE_PATTERN.search(tail)
        if hit:
            koth_fallback(store, cfg, ch, p, hit.group(0))
            return
        chain, idx = koth_chain(cfg), ch.get("chain_idx", -1)
        since = chain[idx].get("successor_round") if 0 <= idx < len(chain) - 1 else None
        if p is ch and not ch.get("peer") and since and (ch.get("round") or 0) >= since:
            spawn_successor(store, cfg, ch, idx + 1)
            store.emit("SUCCESSOR", f"{label(ch)} 라운드 {ch['round']}: 후임 {describe(cfg, ch, ch['peer'])}를 오른쪽 pane에 띄워 인수 준비 시작")
    sync_codex(store, cfg, ch, p, screen, now)
    if p.get("escalate"):
        escalate(store, ch, p, busy)
        return
    if p.get("role") == "successor":  # 준비 중에는 Stop hook이 맡고, 준비가 끝나면 전환을 기다리며 멈춰 있는 것이 정상이다
        return
    digest = hashlib.sha1(screen.encode()).hexdigest()
    if digest != p.get("idle_screen") or busy:
        p.update(idle_screen=digest, idle_since=now)
        return
    if not limit or now - p.get("idle_since", now) < limit or tmux.being_viewed(ch["window_id"]):
        return
    tmux.send_text(p["pane"], NUDGE_MESSAGE_KOTH if ch["category"] == "koth" else NUDGE_MESSAGE)
    p.update(idle_since=now, nudges=p.get("nudges", 0) + 1)
    store.emit("NUDGED", f"{pane_label(ch, p)} {limit}초 넘게 입력 대기 상태라 계속 진행 메시지를 보냄 ({p['nudges']}회째)")


def close_solved(store, cfg, ch):
    """플래그를 얻은 문제의 세션을 정리한다: 풀이 정리를 마칠 시간을 준 뒤 /exit로 끝내고 윈도우를 지운다."""
    now = time.time()
    since = ch.setdefault("solved_at", now)
    busy = any(BUSY_PATTERN.search(tmux.capture(p["pane"], 15)) for p in panes(ch) if not p.get("exited"))
    if now - since < 10 or (busy and now - since < 90):  # WRITEUP.md를 쓰는 중이면 최대 90초까지 기다린다
        return
    clean = all([stop_agent(cfg, p) for p in panes(ch)])
    tmux.kill_window(ch["window_id"])
    ch["closed"] = True
    store.emit("CLOSED", f"{label(ch)} 풀린 문제의 세션을 종료하고 윈도우를 지움{'' if clean else FORCED}")


def housekeep(store):
    """종료된 에이전트, 신뢰 대화상자, 멈춘 에이전트, KotH 모델 상태를 감지한다."""
    with store.locked() as (cfg, st):
        alive = tmux.alive_windows()
        for ch in st["challenges"].values():
            if ch["status"] == "solved" and not ch.get("closed") and ch.get("window_id") in alive:
                close_solved(store, cfg, ch)
            if ch["status"] not in ACTIVE:
                continue
            if ch.get("window_id") not in alive:
                ch["status"] = "exited"
                store.emit("EXITED", f"{label(ch)} tmux 윈도우가 사라짐")
                continue
            for p in panes(ch):
                if p.get("exited"):
                    continue
                if p.get("exit_file") and Path(p["exit_file"]).is_file():
                    p["exited"] = True
                    store.emit("EXITED", f"{pane_label(ch, p)} 에이전트({p['agent']}) 프로세스가 종료됨 (윈도우에 셸은 남아 있음)")
                elif not p.get("trust_checked") and time.time() - ch.get("spawned_at", 0) > 8:
                    p["trust_checked"] = True
                    if TRUST_PATTERN.search(tmux.capture(p["pane"])):
                        store.emit("TRUST_PROMPT", f"{pane_label(ch, p)} 신뢰 대화상자에서 멈춰 있음. 확인 후 Enter 필요")
                else:
                    watch(store, cfg, ch, p, time.time())
            if all(p.get("exited") for p in panes(ch)):  # 동시 풀이는 둘 다 끝나야 슬롯을 돌려준다
                ch["status"] = "exited"


def poll_once(store, http, adapter):
    """문제 목록을 한 번 조회해 새 문제를 수집한다. 조회된 문제 수를 돌려준다."""
    listed = adapter.list_challenges(http)
    new = []
    with store.locked() as (cfg, st):
        chals = st["challenges"]
        for item in listed:
            cid = str(item["id"])
            ch = chals.get(cid)
            if ch is None:
                category = map_category(item["category"], cfg.get("category_map"), item["name"])
                ch = chals[cid] = {
                    "id": cid, "name": item["name"], "category": category, "site_category": item["category"],
                    "points": item["points"], "solves": item["solves"], "seq": len(chals),
                    "status": "site_solved" if item["solved"] else "new", "retries": 0, "blocked_count": 0,
                }
                ch["slug"] = unique_slug(chals, category, item["name"], cid)
                ch["dir"] = f"{category}/{ch['slug']}"
                if item["solved"]:
                    store.emit("SKIPPED", f"{label(ch)} 사이트에서 이미 풀린 문제라 spawn 하지 않음")
            ch.update(points=item["points"], solves=item["solves"])
            if item["solved"] and not ch.get("site_solved"):
                ch["site_solved"] = True
                if ch["status"] in ("new", "queued", "fetch_error"):
                    ch["status"] = "site_solved"
                    store.emit("SKIPPED", f"{label(ch)} 대기 중에 사이트에서 풀림 처리되어 큐에서 제외")
                elif ch["status"] in ACTIVE:
                    store.emit("SITE_SOLVED", f"{label(ch)} 사이트에서 풀림으로 표시됨. 세션은 유지 중 (끝내려면 kill)")
            if ch["status"] in ("new", "fetch_error"):
                new.append(dict(ch))
        new.sort(key=lambda c: queue_key(c, cfg["priority"]))
    for ch in new:  # 다운로드는 잠금 밖에서, 우선순위 높은 문제부터
        try:
            intake.intake(store, http, adapter, ch)
            status, error = "queued", None
            store.emit("NEW", f"{label(ch)} '{ch['name']}' {ch['points']}점 solves={ch['solves']}")
        except Exception as e:
            status, error = "fetch_error", f"{type(e).__name__}: {e}"
            if error != ch.get("error"):  # 같은 실패를 폴링마다 반복 보고하지 않는다
                store.emit("FETCH_ERROR", f"{label(ch)} 수집 실패, 폴링마다 재시도함: {error}")
        with store.locked() as (cfg, st):
            st["challenges"][ch["id"]].update(status=status, error=error)
        if status == "queued":
            schedule(store)  # 받는 즉시 띄운다
    return len(listed)


def run(store):
    lock = open(store.meta / "daemon.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit("ctfm daemon이 이미 실행 중")
    cfg = store.config()
    start = float(cfg.get("start") or 0)
    if start > time.time():
        store.emit("DAEMON", f"시작 시간 대기 중: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(start))}")
        while time.time() < start - 10:
            time.sleep(1)
    store.emit("DAEMON", "폴링 시작")
    seen_any, next_poll, last_error = False, 0.0, None
    while True:
        try:
            if time.time() >= next_poll:
                interval = int(store.config().get("poll_interval") or 30)
                try:
                    http, adapter = adapters.load(store)  # 어댑터나 토큰이 바뀌어도 바로 반영
                    count = poll_once(store, http, adapter)
                    if count and not seen_any:
                        store.emit("DAEMON", f"문제 목록 확인됨 ({count}개). 이후 {interval}초 간격으로 폴링")
                    seen_any, last_error = seen_any or count > 0, None
                except Exception as e:
                    message = f"{type(e).__name__}: {e}"
                    if message != last_error:  # 같은 오류를 30초마다 반복 보고하지 않는다
                        store.emit("POLL_ERROR", message)
                    last_error = message
                next_poll = time.time() + (interval if seen_any else 5)  # 문제가 보일 때까지는 5초 간격
            housekeep(store)
            schedule(store)
        except Exception as e:
            store.emit("DAEMON_ERROR", f"{type(e).__name__}: {e}")
        time.sleep(5)

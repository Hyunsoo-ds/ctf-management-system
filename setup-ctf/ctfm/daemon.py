"""폴링 → 수집 → 큐 → spawn 을 LLM 없이 처리하는 데몬과 스케줄러."""
import fcntl
import hashlib
import re
import shlex
import time
from pathlib import Path

from . import adapters, intake, tmux
from .state import ACTIVE, CTFM_BIN, label, map_category, queue_key, unique_slug

AGENTS = ("claude", "codex")
FIRST_PROMPT = (
    "{skill} {category} 이 디렉토리의 CTF 문제를 풀어라. "
    "먼저 AGENTS.md(규칙)와 CHALLENGE.md(문제 설명, 접속 정보)를 읽어라. 문제 파일은 이 디렉토리에 풀려 있다."
)
FIRST_PROMPT_KOTH = (
    "{skill} 이 디렉토리의 King of the Hill 문제를 맡아라. 먼저 AGENTS.md(규칙)와 CHALLENGE.md(문제 설명)를 읽어라. "
    "초반 라운드 점수 확보가 최우선이다. 사이트에서 현재 라운드를 확인해 보고하고, 동작하는 최선의 설계를 빠르게 만들어라."
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
CODEX_FOOTER = re.compile(r"GPT-[\w.\-]+ \w+( fast)? ·")  # 예: "GPT-6.2-Astra high fast · /path ·"
CHECK_EVERY = 15
NUDGE_MESSAGE = (
    "플래그를 아직 얻지 못했다. 이 작업은 플래그를 얻어야 끝나며 포기는 없다. 지금 이어서 진행하라. "
    "다음 가설이 있으면 바로 실행하고, 없으면 AGENTS.md 8번(막혔을 때) 절차를, 재검토를 3회 했다면 9번(insight 요청)을 수행한 뒤 계속하라. "
    "토큰/시도 제한/rate limit 때문에 사용자 확인이 필요하면 AGENTS.md 4번대로 `ctfm report ask`를 쓴다."
)
NUDGE_MESSAGE_KOTH = (
    "KotH는 대회가 끝날 때까지 점수를 올리는 문제다. 멈추지 말고 이어서 진행하라. 현재 라운드를 확인해 바뀌었으면 "
    "`ctfm report round`로 보고하고, 이번 라운드에 아직 제출하지 않았다면 마감 전에 검증된 최선의 후보부터 제출하라(AGENTS.md 7.1). 그다음 다음 라운드를 위한 개선을 계속하라. 개선 아이디어가 없으면 AGENTS.md 6번 절차를 따른다."
)


def koth_chain(cfg):
    return cfg.get("koth", {}).get("chain") or []


def agent_command(store, cfg, ch, agent, resume):
    if cfg.get("agent_cmd"):  # 테스트용
        return cfg["agent_cmd"]
    if agent == "codex":
        # insane-codex는 빌드 기본값이 --yolo다. -c, --enable/--disable, --dangerously-bypass-hook-trust를 주면
        # 공유 app-server 대신 내장 모드로 뜨는데, 이 빌드에는 codex-code-mode-host가 없어 셸 명령이 전부 실패한다.
        # 그래서 Stop hook(포기 차단)과 질문 도구 차단 hook 없이 띄운다 (DESIGN.md 11절). -m은 괜찮다.
        model = ch.get("model") or cfg.get("codex_model")
        parts = ["insane-codex", *(["resume", "--last"] if resume else []), *(["-m", model] if model else [])]
        return " ".join(shlex.quote(p) for p in parts)
    # 질문 대화상자와 plan 승인 대기는 Stop hook에 걸리지 않고 조용히 멈추는 통로라 도구 자체를 끈다.
    return ("claude --disallowedTools AskUserQuestion,EnterPlanMode "
            f"--dangerously-skip-permissions --model {shlex.quote(ch.get('model') or cfg['model'])} "
            f"-n {shlex.quote(f'{store.root.name}/{label(ch)}')}" + (" --continue" if resume else ""))


def spawn(store, cfg, ch, resume=False, agent=None):
    """문제 하나를 tmux 윈도우에 띄운다. 호출자는 store.locked() 안에 있어야 한다.
    에이전트는 인자 → 문제에 기록된 값 → 설정 기본값 순으로 정한다(resume은 같은 에이전트로 이어야 하므로).
    KotH는 처음 띄울 때 모델 체인의 첫 항목(기본 codex Astra)을 쓴다."""
    if ch["category"] == "koth" and agent is None and "chain_idx" not in ch and koth_chain(cfg):
        first = koth_chain(cfg)[0]
        ch.update(chain_idx=0, agent=first["agent"], model=first["model"])
    agent = agent or ch.get("agent") or cfg.get("agent", "claude")
    launch = store.meta / "launch"
    launch.mkdir(parents=True, exist_ok=True)
    key = f"{ch['category']}-{ch['slug']}"
    prompt_file, script = launch / f"{key}.prompt", launch / f"{key}.sh"
    # 모델 전환으로 윈도우를 바꿔 띄울 때 이전 셸이 늦게 쓰는 종료 표식과 섞이지 않게 실행마다 다른 파일을 쓴다
    exit_file = launch / f"{key}.{int(time.time() * 1000)}.exit"
    chal_dir = store.root / ch["dir"]
    session = tmux.session_name(store.root, ch["category"])
    first_prompt = FIRST_PROMPT_KOTH if ch["category"] == "koth" else FIRST_PROMPT
    prompt_file.write_text(RESUME_PROMPT if resume else first_prompt.format(skill=SKILL_CALL[agent], category=ch["category"]))
    command = agent_command(store, cfg, ch, agent, resume)
    script.write_text(
        "#!/bin/sh\n"
        f"unset {' '.join(tmux.SESSION_VARS)}\n"
        "export CLAUDE_CODE_STOP_HOOK_BLOCK_CAP=1000000\n"  # 포기 차단 hook이 8회 제한에 걸리지 않게
        f"export PATH={shlex.quote(str(CTFM_BIN.parent))}:\"$PATH\"\n"  # 에이전트가 `ctfm report`로 부를 수 있게
        f"cd {shlex.quote(str(chal_dir))} || exit 1\n"
        f'{command} "$(cat {shlex.quote(str(prompt_file))})"\n'
        f"echo $? > {shlex.quote(str(exit_file))}\n"
    )
    script.chmod(0o755)
    ch["window_id"] = tmux.spawn_window(session, ch["slug"], chal_dir, shlex.quote(str(script)))
    ch.update(status="running", agent=agent, tmux_session=session, spawned_at=time.time(), exit_file=str(exit_file))
    model = ch.get("model") or (cfg.get("codex_model") if agent == "codex" else cfg.get("model"))
    store.emit("SPAWNED", f"{label(ch)} → tmux {session}:{ch['slug']} ({agent} {model})" + (" (resume)" if resume else ""))


def schedule(store):
    """슬롯이 비어 있으면 대기 순서대로 spawn 한다."""
    with store.locked() as (cfg, st):
        chals = st["challenges"]
        while sum(c["status"] in ACTIVE for c in chals.values()) < int(cfg["max_agents"]):
            queued = [c for c in chals.values() if c["status"] == "queued"]
            if not queued:
                break
            spawn(store, cfg, min(queued, key=lambda c: queue_key(c, cfg["priority"])))


def koth_fallback(store, cfg, ch, reason):
    """KotH 모델 체인의 다음 항목으로 다시 띄운다. 같은 에이전트(claude→claude)면 대화를 이어 간다."""
    chain, idx = koth_chain(cfg), ch.get("chain_idx", -1)
    if idx < 0 or idx + 1 >= len(chain):
        if not ch.get("fallback_exhausted"):
            ch["fallback_exhausted"] = True
            store.emit("MODEL_UNAVAILABLE", f"{label(ch)} {ch.get('model')} 사용 불가({reason}). 넘어갈 다음 모델이 없음, 확인 필요")
        return
    prev, nxt = chain[idx], chain[idx + 1]
    tmux.kill_window(ch["window_id"])
    ch.update(chain_idx=idx + 1, agent=nxt["agent"], model=nxt["model"])
    spawn(store, cfg, ch, resume=prev["agent"] == nxt["agent"])
    store.emit("MODEL_FALLBACK", f"{label(ch)} {prev['model']} 사용 불가({reason}) → {nxt['model']}로 다시 띄움")


def sync_fast(store, cfg, ch, screen, now):
    """codex KotH: 체인 항목의 fast_rounds 라운드까지는 /fast를 켜고 그 뒤로는 끈다. 하단 표시로 현재 상태를 읽는다."""
    idx = ch.get("chain_idx", -1)
    if ch.get("agent") != "codex" or idx < 0 or now - ch.get("fast_sent", 0) < 10:
        return
    match = CODEX_FOOTER.search(screen)
    if not match or tmux.being_viewed(ch["window_id"]):  # 사용자가 입력 중일 수 있는 창에는 끼어들지 않는다
        return
    fast_rounds = int(koth_chain(cfg)[idx].get("fast_rounds") or 0)
    want, have = (ch.get("round") or 0) <= fast_rounds and fast_rounds > 0, bool(match.group(1))
    if want != have:
        tmux.type_command(ch["window_id"], "/fast")
        ch["fast_sent"] = now
        store.emit("FAST", f"{label(ch)} fast {'켬' if want else '끔'} (라운드 {ch.get('round') or '?'}, 기준 {fast_rounds}라운드까지)")


def watch(store, cfg, ch, now):
    """실행 중인 에이전트 화면을 주기적으로 본다: KotH 모델 사용 불가와 fast 상태, 그리고 입력 대기 멈춤.
    유휴 판정: 하단에 작업 중 표시가 없고 화면이 idle_nudge초 동안 그대로. 질문 대기와 사용자가 보는 창은 건드리지 않는다."""
    limit = int(cfg.get("idle_nudge", 120))
    if ch["status"] not in ("running", "blocked") or now - ch.get("idle_checked", 0) < min(CHECK_EVERY, limit or CHECK_EVERY):
        return
    ch["idle_checked"] = now
    screen = tmux.capture(ch["window_id"], 15)
    busy = bool(BUSY_PATTERN.search(screen))
    if ch["category"] == "koth":
        tail = "\n".join(screen.splitlines()[-8:])
        hit = None if busy else UNAVAILABLE_PATTERN.search(tail)
        if hit:
            koth_fallback(store, cfg, ch, hit.group(0))
            return
        sync_fast(store, cfg, ch, screen, now)
    digest = hashlib.sha1(screen.encode()).hexdigest()
    if digest != ch.get("idle_screen") or busy:
        ch.update(idle_screen=digest, idle_since=now)
        return
    if not limit or now - ch.get("idle_since", now) < limit or tmux.being_viewed(ch["window_id"]):
        return
    tmux.send_text(ch["window_id"], NUDGE_MESSAGE_KOTH if ch["category"] == "koth" else NUDGE_MESSAGE)
    ch.update(idle_since=now, nudges=ch.get("nudges", 0) + 1)
    store.emit("NUDGED", f"{label(ch)} {limit}초 넘게 입력 대기 상태라 계속 진행 메시지를 보냄 ({ch['nudges']}회째)")


def housekeep(store):
    """종료된 에이전트, 신뢰 대화상자, 멈춘 에이전트, KotH 모델 상태를 감지한다."""
    with store.locked() as (cfg, st):
        alive = tmux.alive_windows()
        for ch in st["challenges"].values():
            if ch["status"] not in ACTIVE:
                continue
            if ch.get("exit_file") and Path(ch["exit_file"]).is_file():
                ch["status"] = "exited"
                store.emit("EXITED", f"{label(ch)} 에이전트({ch.get('agent', 'claude')}) 프로세스가 종료됨 (윈도우에 셸은 남아 있음)")
            elif ch.get("window_id") not in alive:
                ch["status"] = "exited"
                store.emit("EXITED", f"{label(ch)} tmux 윈도우가 사라짐")
            elif not ch.get("trust_checked") and time.time() - ch.get("spawned_at", 0) > 8:
                ch["trust_checked"] = True
                if TRUST_PATTERN.search(tmux.capture(ch["window_id"])):
                    store.emit("TRUST_PROMPT", f"{label(ch)} 신뢰 대화상자에서 멈춰 있음. 확인 후 Enter 필요")
            else:
                watch(store, cfg, ch, time.time())


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

"""ctfm CLI. 관리 세션이 쓰는 명령과 풀이 에이전트가 쓰는 report / hook 명령."""
import argparse
import fcntl
import json
import shlex
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

from . import adapters, daemon, intake, tmux
from .notify import discord
from .state import ACTIVE, CATEGORIES, CTFM_BIN, Store, find_root, label, queue_key, resolve, write_env

RETRY_LIMIT = 3
RETRY_CHECKLIST = """재검토 {n}/{limit} 기록됨. 지금 다음을 순서대로 수행하라.
1. CHALLENGE.md와 배포 파일을 처음부터 다시 읽는다 (기억에 의존하지 않는다).
2. STATE.md의 풀이 논리를 단계별로 다시 따라가며 [추정] 전제를 실제로 확인해 틀린 곳을 찾는다.
3. novel 표면을 점검한다: 아직 안 본 파일/함수/엔드포인트, 제목·설명·태그의 힌트, 버전과 설정 특이점, 로컬과 원격의 차이.
4. 새 가설을 세우고 풀이를 재개한다."""
STOP_REASON = """플래그를 아직 얻지 못했다. 이 작업은 플래그를 얻어야 끝나며 포기는 없다. 지금 멈추지 말고 이어서 진행하라.
- 다음 가설이 있으면 바로 실행한다. 백그라운드 작업을 기다리는 중이면 그동안 다른 가설을 검증한다.
- 가설이 없으면 AGENTS.md 8번(막혔을 때) 절차를 수행한다. 재검토를 3회 했다면 9번(insight 요청) 후 계속한다.
- 토큰, 시도 횟수 제한, rate limit 때문에 사용자 확인이 필요한 경우에만 `ctfm report ask`로 질문하고 멈출 수 있다.
- 플래그를 얻었다면 `ctfm report solved --flag '<flag>'`를 실행한다."""


def store_here():
    root = find_root()
    if root is None:
        raise SystemExit("CTF 디렉토리를 찾을 수 없음 (.ctfm/config.json 없음). 먼저 `ctfm init`을 실행")
    return Store(root)


def current_challenge(st, store, cwd):
    """cwd가 속한 문제를 찾는다."""
    cwd = Path(cwd).resolve()
    for ch in st["challenges"].values():
        chal_dir = store.root / ch["dir"]
        if cwd == chal_dir or chal_dir in cwd.parents:
            return ch
    return None


# ── 관리 세션용 ──────────────────────────────────────────────────────────────

def cmd_init(a):
    """처음이면 초기화하고, 이미 초기화된 디렉토리면 준 값만 바꾼다(토큰 갱신 등)."""
    store = Store(Path.cwd())
    update = {}
    if a.url:
        parts = urllib.parse.urlsplit(a.url.strip())
        if not parts.scheme or not parts.netloc:
            raise SystemExit(f"URL 형식이 아님: {a.url}")
        path = parts.path.rstrip("/")
        if path.rsplit("/", 1)[-1] in ("challenges", "login", "scoreboard"):
            path = path.rsplit("/", 1)[0]
        update["base_url"] = f"{parts.scheme}://{parts.netloc}{path}"
    elif not store.config_path.is_file():
        raise SystemExit("처음 초기화할 때는 --url 이 필요함")
    if a.start:
        update["start"] = datetime.fromisoformat(a.start.strip()).timestamp()  # 타임존이 없으면 로컬
    if a.priority is not None:
        update["priority"] = [p.strip() for p in a.priority.split(",") if p.strip()]
    update.update({k: v for k, v in (("max_agents", a.max_agents), ("model", a.model), ("agent", a.agent),
                                     ("codex_model", a.codex_model)) if v is not None})
    with store.locked() as (cfg, st):
        cfg.update(update)
        for key, default in (("start", 0), ("max_agents", 8), ("priority", []), ("agent", "claude"),
                             ("model", "claude-opus-5"), ("codex_model", "gpt-5.6-sol"), ("poll_interval", 30),
                             ("idle_nudge", 120), ("category_map", {})):
            cfg.setdefault(key, default)
    env = dict(e.split("=", 1) for e in a.env)
    if a.token:
        env["CTFD_TOKEN"] = a.token
    if env:
        write_env(store.root, env)
    for category in CATEGORIES:
        (store.root / category).mkdir(exist_ok=True)
    print(json.dumps(store.config(), ensure_ascii=False, indent=1))


def cmd_check(a):
    store = store_here()
    http, adapter = adapters.load(store)
    result = {"adapter": getattr(adapter, "NAME", "custom"), "base_url": store.config()["base_url"]}
    try:
        result.update(adapter.check(http), login_ok=True)
    except Exception as e:
        result.update(login_ok=False, login_error=f"{type(e).__name__}: {e}")
    try:
        listed = adapter.list_challenges(http)
        result.update(challenges_visible=len(listed), site_categories=sorted({c["category"] for c in listed}))
    except Exception as e:
        result.update(challenges_visible=0, list_error=f"{type(e).__name__}: {e}")
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0 if result["login_ok"] else 1


def cmd_daemon(a):
    daemon.run(store_here())


def cmd_up(a):
    """데몬을 tmux `<ctf>-ctl` 세션에 띄운다. 이미 떠 있으면 아무것도 하지 않는다."""
    store = store_here()
    session = tmux.session_name(store.root, "ctl")

    def wait_alive(seconds):
        deadline = time.time() + seconds
        while time.time() < deadline and not daemon_alive(store):
            time.sleep(0.2)
        return daemon_alive(store)

    windows = tmux.tmux("list-windows", "-t", f"={session}", "-F", "#{window_name}", check=False).split()
    if daemon_alive(store) or ("daemon" in windows and wait_alive(5)):  # 방금 띄워 아직 잠금을 못 잡은 경우 포함
        print(f"데몬이 이미 실행 중 (tmux {session}:daemon)")
        return
    if "daemon" in windows:  # 데몬은 죽고 셸만 남은 윈도우
        tmux.tmux("kill-window", "-t", f"={session}:daemon", check=False)
    tmux.spawn_window(session, "daemon", store.root, f"{shlex.quote(str(CTFM_BIN))} daemon")
    if not wait_alive(10):
        raise SystemExit(f"데몬이 뜨지 않음. tmux {session}:daemon 윈도우를 확인")
    print(f"데몬 시작됨: tmux {session}:daemon · 이벤트 로그: {store.events_path}")


def daemon_alive(store):
    try:
        with open(store.meta / "daemon.lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return False
    except OSError:
        return True


def agent_desc(cfg, agent):
    model = cfg["model"] if agent == "claude" else (cfg.get("codex_model") or "codex 설정 기본값")
    return f"{agent}({model})"


def cmd_status(a):
    store = store_here()
    cfg, st = store.config(), store.state()
    chals = list(st["challenges"].values())
    active = sum(c["status"] in ACTIVE for c in chals)
    queued = sorted((c for c in chals if c["status"] == "queued"), key=lambda c: queue_key(c, cfg["priority"]))
    print(f"슬롯 {active}/{cfg['max_agents']} · 대기 {len(queued)} · 풀림 {sum(c['status'] == 'solved' for c in chals)}"
          f" · 데몬 {'실행 중' if daemon_alive(store) else '꺼져 있음'} · 기본 에이전트 {agent_desc(cfg, cfg.get('agent', 'claude'))}")
    if queued:
        print("대기 순서: " + ", ".join(label(c) for c in queued))
    for category in CATEGORIES:
        rows = [c for c in chals if c["category"] == category]
        if not rows:
            continue
        print(f"\n[{category}] 보기: tmux attach -t {tmux.session_name(store.root, category)}")
        for c in sorted(rows, key=lambda c: c["seq"]):
            extra = []
            if c.get("agent"):
                extra.append(c["agent"])
            if c["status"] in ACTIVE:
                extra.append(f"{int((time.time() - c['spawned_at']) / 60)}분 경과")
                extra.append(f"재검토 {c['retries']}/{RETRY_LIMIT}")
            if c.get("nudges"):
                extra.append(f"유휴 재개 {c['nudges']}회")
            if c.get("blocked_count"):
                extra.append(f"막힘 알림 {c['blocked_count']}회")
            if c.get("site_solved") and c["status"] != "site_solved":
                extra.append("사이트에서 풀림")
            if c.get("flag"):
                extra.append(f"flag={c['flag']}")
            if c.get("error"):
                extra.append(c["error"])
            print(f"  {c['slug']:<28} {c['status']:<13} {c['points']:>4}점 solves={c['solves']:<4} {' · '.join(extra)}")


def cmd_spawn(a):
    store = store_here()
    with store.locked() as (cfg, st):
        ch = resolve(st["challenges"], a.challenge)
        if ch["status"] in ACTIVE:
            raise SystemExit(f"{label(ch)} 는 이미 실행 중")
        if ch["status"] in ("new", "fetch_error"):
            raise SystemExit(f"{label(ch)} 는 아직 수집되지 않음 ({ch.get('error') or '다음 폴링에서 수집'})")
        if a.resume and a.agent and ch.get("agent") and a.agent != ch["agent"]:
            raise SystemExit(f"{label(ch)} 는 {ch['agent']}로 풀던 문제라 {a.agent}로 이어 갈 수 없음 (--resume 없이 새로 시작)")
        if a.agent:
            ch["agent"] = a.agent
        if a.now:
            daemon.spawn(store, cfg, ch, resume=a.resume)
        else:
            ch["status"] = "queued"
            print(f"{label(ch)} 를 큐에 넣음. 슬롯이 비면 시작됨 (즉시 시작은 --now)")


def cmd_kill(a):
    store = store_here()
    with store.locked() as (cfg, st):
        ch = resolve(st["challenges"], a.challenge)
        if ch.get("window_id"):
            tmux.kill_window(ch["window_id"])
        ch["status"] = "killed"
        store.emit("KILLED", f"{label(ch)} 종료됨. 이어서 하려면 `ctfm spawn {ch['slug']} --now --resume`")


def cmd_prioritize(a):
    store = store_here()
    with store.locked() as (cfg, st):
        names = [resolve(st["challenges"], t)["name"] if a.known else t for t in a.challenges]
        cfg["priority"] = names + [p for p in cfg["priority"] if p not in names]
        print("우선순위: " + ", ".join(cfg["priority"]))


def cmd_send(a):
    store = store_here()
    with store.locked() as (cfg, st):
        targets = [c for c in st["challenges"].values() if c["status"] in ACTIVE] if a.all else [resolve(st["challenges"], a.challenge)]
        for ch in targets:
            if ch["status"] not in ACTIVE:
                raise SystemExit(f"{label(ch)} 는 실행 중이 아님 ({ch['status']})")
            tmux.send_text(ch["window_id"], a.message)
            if ch["status"] == "waiting_user":  # 답을 받았다. claude는 Stop hook이 이미 바꿨고, hook이 없는 codex는 여기서 바꾼다
                ch["status"] = "running"
                tmux.mark(ch)
            print(f"전송됨: {label(ch)}")


def cmd_peek(a):
    store = store_here()
    ch = resolve(store.state()["challenges"], a.challenge)
    print(tmux.capture(ch["window_id"], a.lines))


def cmd_set(a):
    store = store_here()
    with store.locked() as (cfg, st):
        if a.key == "category-map":
            site, _, category = a.value.partition("=")
            if category not in CATEGORIES:
                raise SystemExit(f"형식: category-map '<사이트 카테고리>=<{'|'.join(CATEGORIES)}>'")
            cfg["category_map"][site.strip().lower()] = category
        elif a.key == "agent" and a.value not in daemon.AGENTS:
            raise SystemExit(f"agent는 {'|'.join(daemon.AGENTS)} 중 하나")
        else:
            cfg[a.key.replace("-", "_")] = a.value if a.key in ("model", "codex-model", "agent") else int(a.value)
        print(json.dumps(cfg, ensure_ascii=False, indent=1))


def cmd_events(a):
    store = store_here()
    lines = store.events_path.read_text().splitlines() if store.events_path.is_file() else []
    print("\n".join(lines[a.since:]))
    print(f"LINES={len(lines)}")


def cmd_notes_sync(a):
    store = store_here()
    block = f"{intake.NOTES_BEGIN}\n{intake.notes_text(store.root)}\n{intake.NOTES_END}"
    count = 0
    for ch in store.state()["challenges"].values():
        path = store.root / ch["dir"] / "AGENTS.md"
        if path.is_file() and intake.NOTES_BEGIN in path.read_text():
            text = path.read_text()
            head, rest = text.split(intake.NOTES_BEGIN, 1)
            path.write_text(head + block + rest.split(intake.NOTES_END, 1)[1])
            count += 1
    print(f"AGENTS.md {count}개의 CTF 노트 구간을 갱신함")


def cmd_notify(a):
    return 0 if discord(store_here(), a.message) else 1


# ── 풀이 에이전트용 ──────────────────────────────────────────────────────────

def cmd_report(a):
    store = store_here()
    message = None
    with store.locked() as (cfg, st):
        ch = current_challenge(st, store, Path.cwd())
        if ch is None:
            raise SystemExit("이 디렉토리는 등록된 문제 디렉토리가 아님")
        name = f"[{ch['category']}] {ch['name']}"
        if a.kind == "solved":
            ch.update(status="solved", flag=a.flag)
            store.emit("SOLVED", f"{label(ch)} flag={a.flag}", echo=False)
            message = f"🚩 **{name}** 플래그 획득\n`{a.flag}`"
            print("기록됨 (Discord 알림 전송). 플래그 획득 경로를 채팅에 요약하라. 사이트 제출은 사용자가 한다.")
        elif a.kind == "retry":
            if ch["retries"] >= RETRY_LIMIT:
                print(f"이미 재검토를 {RETRY_LIMIT}회 했다. 이제 AGENTS.md 9번 절차를 수행하라: "
                      "BLOCKED.md 작성 → 같은 내용을 채팅에 출력 → `ctfm report blocked` → 멈추지 말고 계속 풀이.")
            else:
                ch["retries"] += 1
                store.emit("RETRY", f"{label(ch)} 재검토 {ch['retries']}/{RETRY_LIMIT}", echo=False)
                print(RETRY_CHECKLIST.format(n=ch["retries"], limit=RETRY_LIMIT))
        elif a.kind == "blocked":
            blocked = store.root / ch["dir"] / "BLOCKED.md"
            if not blocked.is_file() or not blocked.read_text().strip():
                raise SystemExit("먼저 BLOCKED.md에 막힘 설명을 작성하라 (AGENTS.md 9번 형식).")
            ch.update(status="blocked", retries=0, blocked_count=ch.get("blocked_count", 0) + 1)
            store.emit("BLOCKED", f"{label(ch)} insight 요청 {ch['blocked_count']}회째 (BLOCKED.md 참고). 풀이는 계속 진행 중", echo=False)
            message = (f"🧱 **{name}** 막힘, insight 요청 ({ch['blocked_count']}회째). 에이전트는 계속 풀이 중\n"
                       f"tmux `{ch.get('tmux_session')}:{ch['slug']}`\n\n{blocked.read_text().strip()}")
            print("알림 전송됨. 재검토 횟수가 0으로 초기화됐다. 멈추지 말고 남은 아이디어부터 다음 재검토 사이클을 시작하라.")
        elif a.kind == "ask":
            ch["status"] = "waiting_user"
            store.emit("ASK", f"{label(ch)} 질문 대기: {a.question}", echo=False)
            message = f"❓ **{name}** 사용자 확인 필요\ntmux `{ch.get('tmux_session')}:{ch['slug']}`\n{a.question}"
            print("알림 전송됨. 같은 질문을 채팅에 쓰고 사용자의 답을 기다려라.")
        tmux.mark(ch)
    if message:
        discord(store, message)


def cmd_hook(a):
    """Stop hook. 플래그 없이 멈추려는 턴을 되돌린다. 판단할 수 없으면 멈춤을 허용한다."""
    try:
        data = json.load(sys.stdin)
        root = find_root(data.get("cwd"))
        if root is None:
            return
        store = Store(root)
        with store.locked() as (cfg, st):
            ch = current_challenge(st, store, data["cwd"])
            if ch is None:
                return
            if ch["status"] == "waiting_user":
                ch["status"] = "running"  # 질문에 한해 1회만 멈춤 허용
                tmux.mark(ch)
            elif ch["status"] in ("running", "blocked"):
                print(json.dumps({"decision": "block", "reason": STOP_REASON}, ensure_ascii=False))
    except Exception as e:
        print(f"ctfm hook 오류: {e}", file=sys.stderr)


def main(argv=None):
    p = argparse.ArgumentParser(prog="ctfm", description="CTF 병렬 풀이 관리")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="현재 디렉토리를 CTF 디렉토리로 초기화 (재실행하면 준 값만 변경)")
    s.add_argument("--url")
    s.add_argument("--token", help="CTFd 토큰 (<CTF>/.env 에 CTFD_TOKEN 으로 저장)")
    s.add_argument("--env", action="append", default=[], metavar="KEY=VALUE", help="추가 인증 정보 (.env 에 저장)")
    s.add_argument("--start", help="시작 시간 ISO 형식, 예: '2026-11-28 10:00' 또는 '2026-11-28T01:00+00:00'")
    s.add_argument("--max-agents", type=int, help="기본 8")
    s.add_argument("--priority", help="먼저 풀 문제 이름들 (쉼표 구분)")
    s.add_argument("--model", help="claude 에이전트 모델, 기본 claude-opus-5")
    s.add_argument("--agent", choices=daemon.AGENTS, help="기본 풀이 에이전트, 기본 claude")
    s.add_argument("--codex-model", help="codex 에이전트 모델, 기본 gpt-5.6-sol")
    s.set_defaults(fn=cmd_init)

    sub.add_parser("check", help="로그인과 문제 목록 조회 확인").set_defaults(fn=cmd_check)
    sub.add_parser("daemon", help="폴링/스케줄링 데몬을 이 터미널에서 실행").set_defaults(fn=cmd_daemon)
    sub.add_parser("up", help="데몬을 tmux <ctf>-ctl 세션에 띄움").set_defaults(fn=cmd_up)
    sub.add_parser("status", help="문제별 현황").set_defaults(fn=cmd_status)

    s = sub.add_parser("spawn", help="문제를 큐에 다시 넣거나 즉시 시작")
    s.add_argument("challenge")
    s.add_argument("--now", action="store_true", help="슬롯 한도를 무시하고 즉시 시작")
    s.add_argument("--resume", action="store_true", help="이전 대화를 이어서 시작 (claude --continue / codex resume --last)")
    s.add_argument("--agent", choices=daemon.AGENTS, help="이 문제를 풀 에이전트 (기본은 설정값)")
    s.set_defaults(fn=cmd_spawn)

    s = sub.add_parser("kill", help="풀이 세션 종료 (슬롯 반환)")
    s.add_argument("challenge")
    s.set_defaults(fn=cmd_kill)

    s = sub.add_parser("prioritize", help="문제를 대기 순서 맨 앞으로")
    s.add_argument("challenges", nargs="+")
    s.add_argument("--unknown", dest="known", action="store_false", help="아직 공개되지 않은 문제 이름을 그대로 등록")
    s.set_defaults(fn=cmd_prioritize)

    s = sub.add_parser("send", help="풀이 세션에 메시지 입력")
    s.add_argument("challenge", nargs="?")
    s.add_argument("message")
    s.add_argument("--all", action="store_true", help="실행 중인 모든 세션에 전송")
    s.set_defaults(fn=cmd_send)

    s = sub.add_parser("peek", help="풀이 세션 화면 끝부분 보기")
    s.add_argument("challenge")
    s.add_argument("-n", "--lines", type=int, default=40)
    s.set_defaults(fn=cmd_peek)

    s = sub.add_parser("set", help="설정 변경")
    s.add_argument("key", choices=["max-agents", "agent", "model", "codex-model", "poll-interval", "idle-nudge", "category-map"],
                   help="idle-nudge: 플래그 없이 입력 대기로 멈춘 에이전트를 몇 초 뒤 재개시킬지 (0이면 끔)")
    s.add_argument("value")
    s.set_defaults(fn=cmd_set)

    s = sub.add_parser("events", help="이벤트 로그 출력")
    s.add_argument("--since", type=int, default=0, help="이 줄 번호 이후만")
    s.set_defaults(fn=cmd_events)

    sub.add_parser("notes-sync", help="CTF_NOTES.md를 모든 문제의 AGENTS.md에 반영").set_defaults(fn=cmd_notes_sync)

    s = sub.add_parser("notify", help="Discord로 메시지 전송")
    s.add_argument("message")
    s.set_defaults(fn=cmd_notify)

    s = sub.add_parser("report", help="[풀이 에이전트용] 상태 보고")
    rsub = s.add_subparsers(dest="kind", required=True)
    r = rsub.add_parser("solved")
    r.add_argument("--flag", required=True)
    rsub.add_parser("retry")
    rsub.add_parser("blocked")
    r = rsub.add_parser("ask")
    r.add_argument("--question", required=True)
    s.set_defaults(fn=cmd_report)

    s = sub.add_parser("hook", help="[Claude Code hook용]")
    s.add_argument("event", choices=["stop"])
    s.set_defaults(fn=cmd_hook)

    args = p.parse_args(argv)
    return args.fn(args) or 0

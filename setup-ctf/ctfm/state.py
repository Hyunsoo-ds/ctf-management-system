"""설정/상태 저장, 잠금, 이벤트 로그, 이름·분야·대기 순서 규칙."""
import contextlib
import fcntl
import json
import os
import re
import time
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = SKILL_DIR.parent
CTFM_BIN = SKILL_DIR / "bin" / "ctfm"
TEMPLATES = SKILL_DIR / "templates"

CATEGORIES = ("koth", "pwn", "web", "rev", "crypto", "ai", "misc")
ACTIVE = ("running", "blocked", "waiting_user")  # 슬롯을 점유하는 상태
DUAL_CATEGORIES = ("pwn", "rev")  # 퍼블 경쟁용: 한 윈도우의 좌우 pane에 codex와 claude를 동시에 띄운다
KOTH_PATTERN = re.compile(r"\bkoth\b|king[\s_-]*of[\s_-]*the[\s_-]*hill", re.I)
# KotH 기본 모델 체인. 막히면(사용량 한도, 안전 차단) 다음 항목으로 넘어간다.
# fast_rounds: 초반 모델이 메인인 동안 이 라운드까지 /fast를 켠다(전환이 늦어질 때를 포함).
# successor_round: 이 라운드가 시작되면 다음 항목을 후임으로 옆 pane에 띄워 인수 준비를 시킨다.
# 전환은 그다음 라운드를 목표로 관리 세션이 `ctfm handover`로 한다.
KOTH_DEFAULTS = {"chain": [
    {"agent": "codex", "model": "gpt-6.2-astra", "fast_rounds": 4, "successor_round": 3},
    {"agent": "claude", "model": "claude-mythos-5-2", "effort": "high"},
    {"agent": "claude", "model": "claude-opus-5-5"},
]}

_CATEGORY_RULES = (
    ("misc", r"blockchain|web3|smart contract"),
    ("web", r"web"),
    ("pwn", r"pwn|binary|exploit|kernel"),
    ("rev", r"rev"),
    ("crypto", r"crypt"),
    ("ai", r"\b(ai|ml|llm)\b|machine learning|prompt"),
)


def map_category(site_category, overrides=None, name=""):
    key = (site_category or "").strip().lower()
    if overrides and key in overrides:
        return overrides[key]
    if KOTH_PATTERN.search(key) or KOTH_PATTERN.search(name or ""):  # King of the Hill은 분야와 무관하게 별도 유형
        return "koth"
    for cat, pattern in _CATEGORY_RULES:
        if re.search(pattern, key):
            return cat
    return "misc"


def slugify(name):
    slug = re.sub(r"\s+", "_", (name or "").strip())
    return re.sub(r"[^\w-]", "", slug) or "chal"


def unique_slug(challenges, category, name, cid):
    slug = slugify(name)
    taken = {c["slug"] for c in challenges.values() if c.get("category") == category and c["id"] != cid}
    return f"{slug}-{cid}" if slug in taken else slug


def priority_index(ch, priority):
    keys = {ch["name"].lower(), ch.get("slug", "").lower(), str(ch["id"]).lower()}
    for i, wanted in enumerate(priority):
        if wanted.strip().lower() in keys:
            return i
    return len(priority)


def queue_key(ch, priority):
    """KotH(초반 점수 확보가 핵심) → 지정 우선순위 → solve 수 많은 순 → 배점 낮은 순 → 발견 순."""
    return (ch.get("category") != "koth", priority_index(ch, priority), -int(ch.get("solves") or 0),
            int(ch.get("points") or 0), ch["seq"])


def resolve(challenges, token):
    """ID, slug, 이름(대소문자 무시), 유일한 부분 문자열 순으로 문제를 찾는다."""
    t = token.strip().lower()
    for field in ("id", "slug", "name"):
        hits = [c for c in challenges.values() if str(c.get(field, "")).lower() == t]
        if len(hits) == 1:
            return hits[0]
    hits = [c for c in challenges.values() if t in c["name"].lower() or t in c.get("slug", "").lower()]
    if len(hits) == 1:
        return hits[0]
    names = ", ".join(c["name"] for c in hits) or "없음"
    raise SystemExit(f"문제를 특정할 수 없음: '{token}' (후보: {names})")


def find_root(start=None):
    path = Path(start or os.getcwd()).resolve()
    for d in (path, *path.parents):
        if (d / ".ctfm" / "config.json").is_file():
            return d
    return None


def read_env(path):
    env = {}
    if Path(path).is_file():
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip().strip("'\"")
    return env


def load_env(root):
    """저장소 .env를 기본으로 하고 CTF 디렉토리 .env가 덮어쓴다."""
    return {**read_env(REPO_DIR / ".env"), **read_env(Path(root) / ".env")}


def write_env(root, values):
    path = Path(root) / ".env"
    env = {**read_env(path), **values}
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    path.write_text("".join(f"{k}={v}\n" for k, v in env.items()))


def _read_json(path, default):
    return json.loads(path.read_text()) if path.is_file() else default


def _write_json(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    os.replace(tmp, path)


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.meta = self.root / ".ctfm"
        self.config_path = self.meta / "config.json"
        self.state_path = self.meta / "state.json"
        self.events_path = self.meta / "events.log"

    def config(self):
        return _read_json(self.config_path, {})

    def state(self):
        return _read_json(self.state_path, {"challenges": {}})

    @contextlib.contextmanager
    def locked(self):
        """(config, state)를 배타 잠금 아래에서 읽고, 바뀌었으면 저장한다. 예외가 나면 저장하지 않는다."""
        self.meta.mkdir(parents=True, exist_ok=True)
        with open(self.meta / "lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            cfg, st = self.config(), self.state()
            before = (json.dumps(cfg, sort_keys=True), json.dumps(st, sort_keys=True))
            yield cfg, st
            if json.dumps(cfg, sort_keys=True) != before[0]:
                _write_json(self.config_path, cfg)
            if json.dumps(st, sort_keys=True) != before[1]:
                _write_json(self.state_path, st)

    def emit(self, kind, text="", echo=True):
        line = f"[{time.strftime('%H:%M:%S')}] {kind} {' '.join(str(text).split())}".rstrip()
        self.meta.mkdir(parents=True, exist_ok=True)
        with open(self.events_path, "a") as f:
            f.write(line + "\n")
        if echo:
            print(line, flush=True)


def label(ch):
    return f"{ch['category']}/{ch['slug']}"


def panes(ch):
    """문제의 풀이 pane 기록들. 문제 기록 자체가 주 에이전트의 pane 기록이고, 동시 풀이면 `peer`(codex)가 하나 더 있다."""
    return [ch, ch["peer"]] if ch.get("peer") else [ch]


def screen_order(ch):
    """화면 배치 순서: codex가 왼쪽, claude가 오른쪽."""
    return sorted(panes(ch), key=lambda p: p["agent"] != "codex")


def pane_label(ch, p):
    return f"{label(ch)}[{p['agent']}]" if ch.get("peer") else label(ch)

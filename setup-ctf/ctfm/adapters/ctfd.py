"""CTFd 어댑터 (토큰 API, 읽기 전용)."""
import urllib.parse
from pathlib import PurePosixPath

from .base import AdapterError, Http

NAME = "ctfd"


def make_http(cfg, env):
    token = env.get("CTFD_TOKEN", "")
    # CTFd는 토큰 인증 요청에 Content-Type: application/json 을 요구한다.
    return Http(cfg["base_url"], {"Authorization": f"Token {token}", "Content-Type": "application/json"})


def _data(http, path):
    body = http.get_json(path)
    if not isinstance(body, dict) or not body.get("success", False):
        raise AdapterError(f"CTFd 응답 실패 {path}: {str(body)[:200]}")
    return body["data"]


def check(http):
    """로그인 확인. {user, team} 반환."""
    user = _data(http, "/api/v1/users/me")
    try:
        team = _data(http, "/api/v1/teams/me").get("name")
    except AdapterError:
        team = None  # 개인전 모드이거나 팀 미가입
    return {"user": user.get("name"), "team": team}


def list_challenges(http):
    return [
        {
            "id": str(c["id"]),
            "name": c["name"],
            "category": c.get("category") or "",
            "points": c.get("value") or 0,
            "solves": c.get("solves") or 0,
            "solved": bool(c.get("solved_by_me")),
        }
        for c in _data(http, "/api/v1/challenges")
    ]


def _hint(h):
    if h.get("content"):
        return h["content"]
    # 최신 CTFd는 비용 0 힌트도 unlock(POST) 전에는 내용을 주지 않는다. 어댑터는 GET만 쓰므로 존재만 알린다.
    title = f" '{h['title']}'" if h.get("title") else ""
    cost = h.get("cost") or 0
    kind = "무료, 사이트에서 열면 바로 보임" if cost == 0 else f"비용 {cost}점"
    return f"잠긴 힌트{title} ({kind}). 내용이 필요하면 사용자에게 열어 달라고 요청"


def fetch(http, cid):
    c = _data(http, f"/api/v1/challenges/{cid}")
    files = [
        {"name": PurePosixPath(urllib.parse.urlsplit(url).path).name or "file", "url": url}
        for url in c.get("files") or []
    ]
    return {
        "description": c.get("description") or "",
        "connection_info": c.get("connection_info") or "",
        "files": files,
        "tags": [t["value"] if isinstance(t, dict) else str(t) for t in c.get("tags") or []],
        "hints": [_hint(h) for h in c.get("hints") or [] if isinstance(h, dict)],
        "max_attempts": c.get("max_attempts") or 0,
    }

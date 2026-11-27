"""CTFd가 아닌 플랫폼용 어댑터 뼈대. `<CTF>/.ctfm/adapter.py`로 복사해서 채운다.

이 파일이 있으면 ctfm은 내장 CTFd 어댑터 대신 이것을 쓴다. `ctfm check`로 검증한다.
수집에는 GET만 쓴다: http.get(path) -> bytes, http.get_json(path), http.download(path, dest).
힌트 해제처럼 사이트 상태를 바꾸는 요청은 만들지 않는다. 쓰기 요청은 맨 아래의 submit 하나뿐이고,
사용자가 플래그 자동 제출을 명시적으로 허용했을 때만 채운다.
"""
from ctfm.adapters.base import AdapterError, Http  # noqa: F401

NAME = "custom"


def make_http(cfg, env):
    """인증 헤더를 붙인 Http를 만든다. env는 <CTF>/.env 의 값들(`ctfm init --env KEY=VALUE`로 저장)."""
    return Http(cfg["base_url"], {"Cookie": f"session={env.get('CTF_SESSION', '')}"})


def check(http):
    """로그인이 유효한지 확인한다. 유효하지 않으면 AdapterError를 던진다."""
    me = http.get_json("/api/me")
    return {"user": me["name"], "team": me.get("team")}


def list_challenges(http):
    """공개된 문제 전체. 대회 시작 전이면 빈 리스트를 돌려주거나 AdapterError를 던진다."""
    return [
        {
            "id": str(c["id"]),          # 문제마다 고유하고 바뀌지 않는 값
            "name": c["name"],
            "category": c["category"],   # 사이트 표기 그대로 (pwn/web/rev/crypto/ai/misc 매핑은 ctfm이 한다)
            "points": c["points"],
            "solves": c["solves"],
            "solved": c["solved"],       # 우리 팀이 이미 풀었는지
        }
        for c in http.get_json("/api/challenges")
    ]


def fetch(http, cid):
    """문제 하나의 상세 정보."""
    c = http.get_json(f"/api/challenges/{cid}")
    return {
        "description": c["description"],
        "connection_info": c.get("connection", ""),               # 예: "nc host 1337"
        "files": [{"name": f["name"], "url": f["url"]} for f in c.get("files", [])],  # url은 절대 URL 또는 base_url 기준 경로
        "tags": c.get("tags", []),
        "hints": [],                                              # 이미 공개된 힌트 본문만
        "max_attempts": 0,                                        # 제출 횟수 제한 (없으면 0)
    }


# 선택: 사용자가 플래그 자동 제출을 허용했을 때만 정의한다. 없으면 `ctfm submit`은 이 사이트에서 쓸 수 없다고 알린다.
# def submit(http, cid, flag):
#     """{status, message} 반환. status는 정답이면 "correct", 이미 풀었으면 "already_solved", 오답이면 "incorrect"."""
#     r = http.post_json("/api/submit", {"id": cid, "flag": flag})
#     return {"status": "correct" if r["ok"] else "incorrect", "message": r.get("message", "")}

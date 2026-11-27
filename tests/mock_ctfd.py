"""테스트용 가짜 CTFd + Discord webhook 수신기.

단독 실행: python3 tests/mock_ctfd.py --port 8000   (토큰: testtoken, 데모 문제 3개)
"""
import argparse
import io
import json
import tarfile
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = "testtoken"


def make_zip(files):
    """files: {경로: (바이트, 권한)}"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, (data, mode) in files.items():
            info = zipfile.ZipInfo(name)
            info.external_attr = mode << 16
            z.writestr(info, data)
    return buf.getvalue()


def make_tar(files):
    """files: {경로: 바이트}. 경로에 `..`를 넣어 탈출 시도를 만들 수 있다."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class MockCTFd:
    def __init__(self):
        self.challenges = {}  # id -> {name, category, value, solves, solved_by_me, description, connection_info, files, ...}
        self.started = True
        self.webhook = []     # Discord로 전송된 content 목록
        self.threads = {}     # 포럼 포스트: thread_id -> {"name", "messages"}
        self.writes = []      # webhook 외의 쓰기 요청 (있으면 안 됨)
        self.server = None

    def add(self, cid, name, category, value=100, solves=0, solved=False, description="", connection_info="",
            files=None, max_attempts=0):
        self.challenges[cid] = dict(id=cid, name=name, category=category, value=value, solves=solves,
                                    solved_by_me=solved, description=description, connection_info=connection_info,
                                    files=files or {}, max_attempts=max_attempts)

    def start(self, port=0):
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, code, body, content_type="application/json"):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                path = self.path.split("?")[0]
                if path.startswith("/files/"):
                    _, _, cid, name = path.split("/", 3)
                    return self.reply(200, mock.challenges[int(cid)]["files"][name], "application/octet-stream")
                if self.headers.get("Authorization") != f"Token {TOKEN}":
                    return self.reply(401, {"message": "unauthorized"})
                if path == "/api/v1/users/me":
                    return self.reply(200, {"success": True, "data": {"name": "tester"}})
                if path == "/api/v1/challenges":
                    if not mock.started:
                        return self.reply(403, {"message": "CTF has not started"})
                    keys = ("id", "name", "category", "value", "solves", "solved_by_me")
                    return self.reply(200, {"success": True, "data": [{k: c[k] for k in keys} for c in mock.challenges.values()]})
                if path.startswith("/api/v1/challenges/"):
                    c = mock.challenges[int(path.rsplit("/", 1)[1])]
                    data = {k: v for k, v in c.items() if k != "files"}
                    data["files"] = [f"/files/{c['id']}/{name}?token=secret" for name in c["files"]]
                    data.update(tags=[{"value": "demo"}], hints=[{"id": 1, "cost": 50}, {"id": 2, "content": "무료 힌트"}])
                    return self.reply(200, {"success": True, "data": data})
                return self.reply(404, {"message": "not found"})

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                path, _, query = self.path.partition("?")
                if path == "/webhook":  # 포럼 채널 webhook: thread_name으로 포스트를 만들거나 thread_id로 이어 붙인다
                    data = json.loads(body)
                    thread = query.partition("thread_id=")[2] or None
                    if thread is None and data.get("thread_name"):
                        thread = str(1000 + len(mock.threads))
                        mock.threads[thread] = {"name": data["thread_name"], "messages": []}
                    if thread not in mock.threads:
                        return self.reply(400, {"message": "Unknown Channel" if thread else "must have a thread_name or thread_id"})
                    mock.threads[thread]["messages"].append(data["content"])
                    mock.webhook.append(data["content"])
                    return self.reply(200, {"channel_id": thread})
                mock.writes.append(self.path)
                return self.reply(403, {"message": "forbidden"})

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    port = parser.parse_args().port
    mock = MockCTFd()
    mock.add(1, "warmup xor", "Crypto", value=50, solves=30, description="키 하나로 XOR 된 플래그다. 원격 서비스는 없다.",
             files={"xor.zip": make_zip({"xor/enc.txt": (bytes(b ^ 0x42 for b in b"flag{mock_xor_ok}").hex().encode(), 0o644),
                                         "xor/README.md": (b"enc.txt is hex(flag XOR single byte)\n", 0o644)})})
    mock.add(2, "hello web", "Web", value=100, solves=10, description="관리자 페이지를 찾아라.", connection_info="http://127.0.0.1:9/")
    mock.add(3, "already done", "Misc", value=10, solves=99, solved=True)
    url = mock.start(port)
    print(f"mock CTFd: {url}  token: {TOKEN}  webhook: {url}/webhook", flush=True)
    threading.Event().wait()

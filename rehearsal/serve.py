#!/usr/bin/env python3
"""리허설용 가상 CTF: mock CTFd(문제 사이트) + 분야별 jeopardy 문제 6개 + 원격 문제 서비스.

    python3 rehearsal/serve.py            # 문제를 빌드하고 서비스를 띄운다 (Ctrl+C로 종료)

- 문제 사이트: http://127.0.0.1:8000  (브라우저 화면 + CTFd API, 토큰 `testtoken`)
- pwn 원격:    nc 127.0.0.1 31337
- web 원격:    http://127.0.0.1:8081
빌드 산출물은 rehearsal/.build/ 에 만든다. 원격의 진짜 플래그는 배포 파일에 들어가지 않는다.
"""
import base64
import json
import random
import socketserver
import sqlite3
import subprocess
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
BUILD = HERE / ".build"
sys.path.insert(0, str(HERE.parent / "tests"))
from mock_ctfd import TOKEN, MockCTFd, make_zip  # noqa: E402

HOST, SITE_PORT, PWN_PORT, WEB_PORT = "127.0.0.1", 8000, 31337, 8081
FLAGS = {
    "pwn": "REHEARSAL{r3t2w1n_0v3r_th3_n3tw0rk}",
    "rev": "REHEARSAL{x0r_4dd_r0ll_ch3ck3r}",
    "web": "REHEARSAL{un10n_0f_qu0t3s_4nd_dash3s}",
    "crypto": "REHEARSAL{cub3_r00t_n33ds_n0_k3y}",
    "ai": "REHEARSAL{4rgm4x_0f_th3_w31ghts}",
    "misc": "REHEARSAL{p33l_th3_l4y3rs_0n3_by_0n3}",
}
FAKE = "REHEARSAL{fake_flag_for_local_test}"

PWN_C = r"""// gcc -O0 -fno-stack-protector -no-pie -o chall chall.c
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

void win(void) {
    char flag[128] = {0};
    FILE *f = fopen("flag.txt", "r");
    if (!f) { puts("flag.txt not found"); exit(1); }
    fgets(flag, sizeof(flag), f);
    puts(flag);
    exit(0);
}

void vuln(void) {
    char name[64];
    printf("name? ");
    read(0, name, 256);
    printf("hello, %s\n", name);
}

int main(void) {
    setvbuf(stdin, NULL, _IONBF, 0);
    setvbuf(stdout, NULL, _IONBF, 0);
    vuln();
    puts("bye");
    return 0;
}
"""

REV_C = r"""#include <stdio.h>
#include <string.h>

static const unsigned char key[] = {%(key)s};
static const unsigned char target[] = {%(target)s};

int main(void) {
    char in[128] = {0};
    printf("flag? ");
    if (!fgets(in, sizeof(in), stdin)) return 1;
    in[strcspn(in, "\n")] = 0;
    if (strlen(in) != sizeof(target)) { puts("wrong"); return 1; }
    for (unsigned i = 0; i < sizeof(target); i++) {
        unsigned char c = (unsigned char)in[i] ^ key[i %% sizeof(key)];
        c = (unsigned char)(c + 3 * i);
        c = (unsigned char)((c << 3) | (c >> 5));
        if (c != target[i]) { puts("wrong"); return 1; }
    }
    puts("correct");
    return 0;
}
"""

WEB_APP = r'''#!/usr/bin/env python3
"""직원 포털. python3 app.py [port]  (FLAG 환경 변수가 관리자 메모에 들어간다)"""
import os
import sqlite3
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FLAG = os.environ.get("FLAG", "REHEARSAL{fake_flag_for_local_test}")
PAGE = "<h1>Staff Portal</h1><form action=/login><input name=user placeholder=user> <input name=pw placeholder=password> <button>login</button></form><p>%s</p>"


def database():
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.execute("CREATE TABLE users (name TEXT, pw TEXT, role TEXT, memo TEXT)")
    db.execute("INSERT INTO users VALUES ('guest', 'guest', 'guest', 'welcome')")
    db.execute("INSERT INTO users VALUES ('admin', ?, 'admin', ?)", (os.urandom(16).hex(), FLAG))
    return db


DB = database()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        message = "guest / guest 로 로그인해 보세요."
        if url.path == "/login":
            sql = "SELECT name, role, memo FROM users WHERE name = '%s' AND pw = '%s'" % (q.get("user", ""), q.get("pw", ""))
            try:
                row = DB.execute(sql).fetchone()
            except sqlite3.Error as e:
                row, message = None, "DB error: %s" % e
            else:
                message = "login failed"
            if row:
                message = "hello %s (%s). memo: %s" % (row[0], row[1], row[2] if row[1] == "admin" else "(관리자만 볼 수 있음)")
        body = (PAGE % message).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 8081), Handler).serve_forever()
'''

CRYPTO_PY = '''from Crypto.Util.number import bytes_to_long, getPrime

flag = open("flag.txt", "rb").read().strip()
p, q = getPrime(1024), getPrime(1024)
n, e = p * q, 3
c = pow(bytes_to_long(flag), e, n)
print(f"n = {n}\\ne = {e}\\nc = {c}")
'''

AI_CHECK = '''#!/usr/bin/env python3
"""플래그 판별 모델. 글자 위치마다 가중치 벡터가 있고, 입력의 점수 합이 threshold 이상이면 플래그로 판정한다."""
import json
import sys

model = json.load(open("model.json"))
text = sys.argv[1] if len(sys.argv) > 1 else input("flag? ")
if len(text) != len(model["weights"]) or any(not 32 <= ord(ch) < 127 for ch in text):
    sys.exit("wrong shape")
score = sum(row[ord(ch) - 32] for row, ch in zip(model["weights"], text))
print(f"score = {score:.4f} (threshold {model['threshold']:.4f})")
print("FLAG" if score >= model["threshold"] else "not a flag")
'''


def is_prime(n, rng):
    if n % 2 == 0:
        return n == 2
    d, s = n - 1, 0
    while d % 2 == 0:
        d, s = d // 2, s + 1
    for _ in range(24):
        x = pow(rng.randrange(2, n - 1), d, n)
        if x in (1, n - 1):
            continue
        for _ in range(s - 1):
            x = x * x % n
            if x == n - 1:
                break
        else:
            return False
    return True


def get_prime(bits, rng):
    while True:
        p = rng.getrandbits(bits) | (1 << (bits - 1)) | 1
        if p % 3 != 1 and is_prime(p, rng):
            return p


def build():
    """문제 파일을 만들고 {분야: 배포 zip 내용}을 돌려준다."""
    rng = random.Random(20261128)
    for name in ("pwn", "rev", "web"):
        (BUILD / name).mkdir(parents=True, exist_ok=True)

    # pwn: ret2win. 원격 디렉토리에만 진짜 플래그가 있다
    (BUILD / "pwn" / "chall.c").write_text(PWN_C)
    subprocess.run(["gcc", "-O0", "-fno-stack-protector", "-no-pie", "-w", "-o", "chall", "chall.c"], cwd=BUILD / "pwn", check=True)
    (BUILD / "pwn" / "flag.txt").write_text(FLAGS["pwn"] + "\n")
    pwn = {"baby_overflow/chall": ((BUILD / "pwn" / "chall").read_bytes(), 0o755), "baby_overflow/chall.c": (PWN_C.encode(), 0o644),
           "baby_overflow/flag.txt": ((FAKE + "\n").encode(), 0o644)}

    # rev: 입력을 변환해 표와 비교하는 검사기 (심볼 제거)
    key = bytes(rng.randrange(1, 256) for _ in range(7))
    target = []
    for i, ch in enumerate(FLAGS["rev"].encode()):
        c = ((ch ^ key[i % len(key)]) + 3 * i) & 0xFF
        target.append(((c << 3) | (c >> 5)) & 0xFF)
    (BUILD / "rev" / "chall.c").write_text(REV_C % {"key": ", ".join(map(str, key)), "target": ", ".join(map(str, target))})
    subprocess.run(["gcc", "-O1", "-s", "-o", "keycheck", "chall.c"], cwd=BUILD / "rev", check=True)
    check = subprocess.run(["./keycheck"], cwd=BUILD / "rev", input=FLAGS["rev"] + "\n", capture_output=True, text=True).stdout
    assert "correct" in check, check
    rev = {"keycheck": ((BUILD / "rev" / "keycheck").read_bytes(), 0o755)}

    # web: 로그인 SQL injection. 배포본은 같은 소스, 플래그만 자리표시
    (BUILD / "web" / "app.py").write_text(WEB_APP)
    web = {"staff_portal/app.py": (WEB_APP.encode(), 0o644)}

    # crypto: e=3이고 평문이 짧아 m^3 < n
    p, q = get_prime(1024, rng), get_prime(1024, rng)
    m = int.from_bytes(FLAGS["crypto"].encode(), "big")
    assert m ** 3 < p * q
    crypto = {"small_e/chall.py": (CRYPTO_PY.encode(), 0o644),
              "small_e/output.txt": (f"n = {p * q}\ne = 3\nc = {pow(m, 3, p * q)}\n".encode(), 0o644)}

    # ai: 위치별 가중치의 argmax가 플래그
    weights = [[round(rng.uniform(-1, 1), 4) for _ in range(95)] for _ in FLAGS["ai"]]
    for row, ch in zip(weights, FLAGS["ai"]):
        row[ord(ch) - 32] = round(rng.uniform(1.5, 2.5), 4)
    best = sum(row[ord(ch) - 32] for row, ch in zip(weights, FLAGS["ai"]))
    ai = {"flag_model/model.json": (json.dumps({"weights": weights, "threshold": round(best - 0.01, 4)}).encode(), 0o644),
          "flag_model/check.py": (AI_CHECK.encode(), 0o755)}

    # misc: 겹겹이 싼 인코딩
    data = FLAGS["misc"].encode()
    data = base64.b32encode(data)[::-1]
    data = base64.b64encode(data).hex().encode()
    data = base64.b85encode(data)
    misc = {"layers.txt": (data + b"\n", 0o644)}
    return {"pwn": pwn, "rev": rev, "web": web, "crypto": crypto, "ai": ai, "misc": misc}


class PwnHandler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            subprocess.run(["./chall"], cwd=BUILD / "pwn", stdin=self.request.fileno(), stdout=self.request.fileno(),
                           stderr=subprocess.DEVNULL, timeout=30)
        except subprocess.TimeoutExpired:
            pass


class PwnServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    dist = build()
    mock = MockCTFd()
    mock.add(1, "baby overflow", "Pwnable", value=100, solves=12, flag=FLAGS["pwn"],
             connection_info=f"nc {HOST} {PWN_PORT}", files={"baby_overflow.zip": make_zip(dist["pwn"])},
             description="이름을 물어보는 친절한 서비스입니다. 원격 서버의 flag.txt를 읽으세요.")
    mock.add(2, "keycheck", "Reversing", value=100, solves=9, flag=FLAGS["rev"], files={"keycheck.zip": make_zip(dist["rev"])},
             description="올바른 입력을 넣으면 correct가 나옵니다. 그 입력이 플래그입니다. (원격 서버 없음)")
    mock.add(3, "staff portal", "Web", value=100, solves=15, flag=FLAGS["web"],
             connection_info=f"http://{HOST}:{WEB_PORT}/", files={"staff_portal.zip": make_zip(dist["web"])},
             description="직원 포털의 관리자 메모에 플래그가 있습니다. 배포된 소스는 원격과 같고 플래그만 다릅니다.")
    mock.add(4, "small e", "Crypto", value=100, solves=20, flag=FLAGS["crypto"], files={"small_e.zip": make_zip(dist["crypto"])},
             description="RSA로 플래그를 암호화했습니다. (원격 서버 없음)")
    mock.add(5, "flag model", "AI", value=100, solves=7, flag=FLAGS["ai"], files={"flag_model.zip": make_zip(dist["ai"])},
             description="플래그만 FLAG로 판정하도록 학습시킨 모델입니다. 모델이 FLAG라고 답하는 입력을 찾으세요. (원격 서버 없음)")
    mock.add(6, "layers", "Misc", value=50, solves=30, flag=FLAGS["misc"], files={"layers.txt": dist["misc"]["layers.txt"][0]},
             description="누가 플래그를 여러 겹으로 싸 놓았습니다. (원격 서버 없음)")
    mock.pages["/"] = mock.pages["/challenges"] = (HERE / "index.html").read_text().replace("__TOKEN__", TOKEN).encode()
    site = mock.start(SITE_PORT)

    pwn = PwnServer((HOST, PWN_PORT), PwnHandler)
    threading.Thread(target=pwn.serve_forever, daemon=True).start()
    web = subprocess.Popen([sys.executable, "app.py", str(WEB_PORT)], cwd=BUILD / "web", env={"FLAG": FLAGS["web"], "PATH": "/usr/bin:/bin"})
    print(f"문제 사이트  {site}   (브라우저로 열 수 있음, 토큰: {TOKEN})\npwn 원격     nc {HOST} {PWN_PORT}\nweb 원격     http://{HOST}:{WEB_PORT}/", flush=True)
    print(f"\n관리 세션 시작:  /setup-ctf {site} --token {TOKEN}\n제출 기록은 아래에 찍힌다. Ctrl+C로 종료.", flush=True)
    seen = 0
    try:
        while True:
            threading.Event().wait(1)
            for cid, flag in mock.attempts[seen:]:
                ok = flag == mock.challenges[int(cid)]["flag"]
                print(f"[제출] {mock.challenges[int(cid)]['name']}: {flag} → {'정답' if ok else '오답'}", flush=True)
            seen = len(mock.attempts)
    except KeyboardInterrupt:
        pass
    finally:
        web.terminate()
        mock.stop()


if __name__ == "__main__":
    main()

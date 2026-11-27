"""어댑터 공통 HTTP 헬퍼. 수집에는 GET만 쓴다. 쓰기 요청은 post_json 하나뿐이고, 사용자가 명시적으로 허용한
플래그 제출(`ctfm submit`)에만 쓴다. 힌트 해제 같은 다른 쓰기 요청은 만들지 않는다."""
import json
import shutil
import urllib.error
import urllib.parse
import urllib.request


class AdapterError(Exception):
    pass


class Http:
    def __init__(self, base_url, headers=None):
        self.base_url = base_url.rstrip("/")
        self.headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) ctfm", **(headers or {})}

    def _open(self, path, timeout):
        url = path if path.startswith(("http://", "https://")) else f"{self.base_url}/{path.lstrip('/')}"
        request = urllib.request.Request(url, headers=self.headers, method="GET")
        try:
            return urllib.request.urlopen(request, timeout=timeout)
        except urllib.error.HTTPError as e:
            body = e.read(300).decode("utf-8", "replace")
            raise AdapterError(f"HTTP {e.code} {urllib.parse.urlsplit(url).path}: {' '.join(body.split())[:200]}") from None
        except OSError as e:
            raise AdapterError(f"연결 실패 {urllib.parse.urlsplit(url).path}: {e}") from None

    def get(self, path, timeout=20):
        with self._open(path, timeout) as response:
            return response.read()

    def get_json(self, path, timeout=20):
        body = self.get(path, timeout)
        try:
            return json.loads(body)
        except ValueError:
            raise AdapterError(f"JSON이 아닌 응답 {path} (로그인 페이지로 리다이렉트됐을 수 있음)") from None

    def post_json(self, path, payload, timeout=20):
        """플래그 제출 전용. 오답이나 rate limit처럼 사이트가 오류 상태 코드와 함께 주는 JSON 본문도 그대로 돌려준다."""
        url = f"{self.base_url}/{path.lstrip('/')}"
        headers = {**self.headers, "Content-Type": "application/json"}
        request = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as e:
            body = e.read()
        except OSError as e:
            raise AdapterError(f"연결 실패 {path}: {e}") from None
        try:
            return json.loads(body)
        except ValueError:
            raise AdapterError(f"JSON이 아닌 응답 {path}: {body[:200]!r}") from None

    def download(self, path, dest, timeout=120):
        with self._open(path, timeout) as response, open(dest, "wb") as out:
            shutil.copyfileobj(response, out)

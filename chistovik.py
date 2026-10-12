"""Chistovik ("fair copy" in Russian): a local relay between Handy and an LLM that turns raw Russian
dictation into clean written text.

Handy (or its Russian build klava-nevinovata) recognizes speech locally with Whisper and can send
the transcript to an OpenAI-compatible API for post-processing. Point Handy at this relay
(http://localhost:8765/v1) instead, and the relay:

- builds the request from prompt.txt and dictionary.txt, read on every request: edit a file and
  dictate again, no restart;
- keeps one warm keep-alive connection to the API, so a dictation does not pay for a new
  TCP+TLS handshake (Handy opens a new connection for every request);
- keeps the API key out of Handy's settings (environment variable or key.txt);
- gives up after a short timeout, so Handy pastes the raw transcript instead of hanging.

In Handy the post-processing prompt is just ${output}: the relay receives the bare transcript.
Stdlib only, Python 3.11+. `python chistovik.py --check` sends a test phrase and prints the result.
"""
import argparse
import http.client
import http.server
import json
import os
import pathlib
import socket
import ssl
import sys
import threading
import time
import tomllib
import urllib.parse

HERE = pathlib.Path(__file__).resolve().parent
DEFAULTS = {
    "upstream": "https://api.anthropic.com/v1",
    "model": "",
    "api_key_env": "CHISTOVIK_API_KEY",
    "api_key_file": "key.txt",
    "port": 8765,
    "timeout": 10,
    "keep_warm": 25,
    "prompt_file": "prompt.txt",
    "dictionary_file": "dictionary.txt",
    "drop_fields": ["reasoning_effort", "reasoning", "thinking"],
    "log_file": "chistovik.log",
}
SAMPLE = ("Ну вот, короче, я, это самое, хотел сказать, что, ну, завтра... завтра созвон "
          "переносится, ну, на три часа, не, на четыре.")


def load_config():
    cfg = dict(DEFAULTS)
    path = HERE / "config.toml"
    if path.exists():
        cfg.update(tomllib.loads(path.read_text(encoding="utf-8-sig")))
    return cfg


CFG = load_config()


def here(name):
    p = pathlib.Path(name)
    return p if p.is_absolute() else HERE / p


def log(msg):
    path = here(CFG["log_file"])
    try:
        if path.exists() and path.stat().st_size > 1_000_000:
            path.write_text("", encoding="utf-8")
        with path.open("a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except OSError:
        pass


def api_key():
    key = os.environ.get(CFG["api_key_env"], "").strip()
    if not key and here(CFG["api_key_file"]).exists():
        key = here(CFG["api_key_file"]).read_text(encoding="utf-8-sig").strip()
    return key


def dictionary():
    path = here(CFG["dictionary_file"])
    if not path.exists():
        return ""
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    return "\n".join(l for l in lines if l.strip() and not l.lstrip().startswith("#"))


def rewrite(payload):
    """Wrap the bare transcript from Handy into prompt.txt + dictionary.txt. True if done."""
    msgs = payload.get("messages") or []
    if not msgs or not isinstance(msgs[-1].get("content"), str):
        return False
    try:
        tmpl = here(CFG["prompt_file"]).read_text(encoding="utf-8-sig")
        words = dictionary()
    except OSError:
        return False
    text = msgs[-1]["content"].strip()
    msgs[-1]["content"] = tmpl.replace("${dictionary}", words or "(empty)").replace("${output}", text)
    return True


class Upstream:
    """One keep-alive connection to the API, shared by all requests."""

    def __init__(self, base):
        u = urllib.parse.urlsplit(base)
        self.https = u.scheme == "https"
        self.host = u.hostname
        self.port = u.port or (443 if self.https else 80)
        self.base = u.path.rstrip("/")
        self.ctx = ssl.create_default_context()
        self.lock = threading.Lock()
        self.conn = None
        self.last = 0.0

    def request(self, method, path, body=None):
        """path is relative to the API base, e.g. /chat/completions."""
        headers = {"content-type": "application/json"}
        key = api_key()
        if key:
            headers["authorization"] = "Bearer " + key
        with self.lock:
            for attempt in (1, 2):
                try:
                    if self.conn is None:
                        self.conn = self._connect()
                    self.conn.request(method, self.base + path, body=body, headers=headers)
                    resp = self.conn.getresponse()
                    data = resp.read()
                    self.last = time.time()
                    return resp.status, resp.getheader("content-type", "application/json"), data
                except TimeoutError:
                    self._drop()
                    raise  # a slow API is not a stale connection: do not wait twice
                except (http.client.HTTPException, OSError):
                    self._drop()  # the kept connection was closed by the server; open a new one
                    if attempt == 2:
                        raise

    def _connect(self):
        if self.https:
            return http.client.HTTPSConnection(self.host, self.port, timeout=CFG["timeout"], context=self.ctx)
        return http.client.HTTPConnection(self.host, self.port, timeout=CFG["timeout"])

    def _drop(self):
        try:
            self.conn.close()
        except Exception:
            pass
        self.conn = None

    def keep_warm(self):
        every = CFG["keep_warm"]
        while every > 0:
            time.sleep(every)
            if time.time() - self.last >= every:
                try:
                    self.request("GET", "/models")
                except Exception:
                    pass


UP = Upstream(CFG["upstream"])


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _reply(self, status, ctype, data):
        self.send_response(status)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status, msg):
        self._reply(status, "application/json", json.dumps({"error": {"message": msg}}).encode())

    def _forward(self, method, body=None, note=""):
        t0 = time.time()
        try:
            status, ctype, data = UP.request(method, self.path[len("/v1"):], body)
        except Exception as e:
            log(f"{method} {self.path} FAILED {time.time() - t0:.2f}s {type(e).__name__}")
            return self._error(502, f"chistovik: {type(e).__name__}")
        if method == "POST":
            log(f"{method} {self.path} {status} {time.time() - t0:.2f}s{note}")
        self._reply(status, ctype, data)

    def do_GET(self):
        if self.path == "/health":
            info = {"ok": True, "upstream": CFG["upstream"], "model": CFG["model"] or "from Handy",
                    "prompt": here(CFG["prompt_file"]).exists(), "key": bool(api_key())}
            return self._reply(200, "application/json", json.dumps(info, ensure_ascii=False).encode())
        if self.path.startswith("/v1/models"):
            return self._forward("GET")
        self._error(404, "not found")

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("content-length", 0)))
        if not self.path.startswith("/v1/chat/completions"):
            return self._error(404, "not found")
        note = ""
        try:
            payload = json.loads(raw)
            for k in CFG["drop_fields"]:
                payload.pop(k, None)
            if CFG["model"]:
                payload["model"] = CFG["model"]
            if rewrite(payload):
                note = " prompt.txt"
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        except ValueError:
            pass
        self._forward("POST", raw, note)

    def log_message(self, *args):
        pass


class Server6(http.server.ThreadingHTTPServer):
    address_family = socket.AF_INET6


def serve():
    port = CFG["port"]
    try:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError:
        sys.exit(0)  # already running
    try:  # "localhost" may resolve to ::1 first; answer there too instead of making Handy wait
        threading.Thread(target=Server6(("::1", port), Handler).serve_forever, daemon=True).start()
    except OSError:
        pass
    threading.Thread(target=UP.keep_warm, daemon=True).start()
    try:
        UP.request("GET", "/models")
    except Exception:
        pass
    log(f"started on localhost:{port} -> {CFG['upstream']}")
    server.serve_forever()


def check():
    """Send a test phrase straight to the API, the way the relay does, and print the result."""
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if not CFG["model"]:
        print("Set model in config.toml: the check needs a model.")
        return 1
    if not api_key():
        print(f"No API key: neither {CFG['api_key_env']} nor {CFG['api_key_file']} is set. "
              "Trying without a key (this only works with local models).")
    payload = {"model": CFG["model"], "messages": [{"role": "user", "content": SAMPLE}]}
    rewrite(payload)
    t0 = time.time()
    try:
        status, _, data = UP.request("POST", "/chat/completions", json.dumps(payload, ensure_ascii=False).encode())
    except Exception as e:
        print(f"Cannot reach {CFG['upstream']}: {type(e).__name__}: {e}")
        return 1
    print("Before:", SAMPLE)
    if status != 200:
        print(f"Error {status}: {data.decode('utf-8', 'replace')[:500]}")
        return 1
    print("After: ", json.loads(data)["choices"][0]["message"]["content"])
    print(f"Time:   {time.time() - t0:.1f} s")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Chistovik: clean Russian dictation for Handy")
    ap.add_argument("--check", action="store_true", help="send a test phrase and print the result")
    sys.exit(check() if ap.parse_args().check else serve())

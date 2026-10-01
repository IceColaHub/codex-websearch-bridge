#!/usr/bin/env python3
"""
codex-websearch-bridge
======================

Codex >= 0.159 (Codex Desktop / codex CLI) does "standalone web search" by POSTing to
    {base_url}/alpha/search
and expecting `{"output": "<plain text>"}` back.

If your OpenAI-compatible relay (one-api / new-api style) does not implement
/alpha/search, Codex gets a 404 and web search is dead.

This bridge sits between Codex and the relay:

    Codex --(base_url=http://127.0.0.1:8787/v1)--> bridge
                                                     |-- /v1/alpha/search  -> handled locally,
                                                     |                         calls the relay's
                                                     |                         native `web_search`
                                                     |                         Responses tool
                                                     '-- everything else  -> streamed through
                                                                             untouched

Only stdlib + requests. Configuration lives in `bridge.config.json` (copy
`bridge.config.example.json` and fill in `upstream`). Environment variables override it,
and the `X-Bridge-Upstream` request header overrides both for a single request.

Run:  python web_search_bridge.py
"""

import http.server
import json
import os
import sys
import threading
import traceback

import requests

# ----------------------------------------------------------------------------- config
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "bridge.config.json")
EXAMPLE_CONFIG_PATH = os.path.join(HERE, "bridge.config.example.json")

PLACEHOLDER_UPSTREAM = "https://your-relay.example"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

DEFAULTS = {
    "upstream": PLACEHOLDER_UPSTREAM,
    "host": "127.0.0.1",
    "port": 8787,
    "model": "gpt-6.1-sol",
    "verbose": True,
    "allow_remote": False,
    "log": "",
}

_ENV_KEYS = {
    "upstream": "BRIDGE_UPSTREAM",
    "host": "BRIDGE_HOST",
    "port": "BRIDGE_PORT",
    "model": "BRIDGE_MODEL",
    "verbose": "BRIDGE_VERBOSE",
    "allow_remote": "BRIDGE_ALLOW_REMOTE",
    "log": "BRIDGE_LOG",
}


def _truthy(value):
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def load_config():
    """读取 bridge.config.json（缺失时退回 example），再用环境变量覆盖。"""
    cfg = dict(DEFAULTS)
    source = None
    for candidate in (CONFIG_PATH, EXAMPLE_CONFIG_PATH):
        if os.path.exists(candidate):
            source = candidate
            break
    if source:
        try:
            with open(source, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for key in DEFAULTS:
                    if key in data:
                        cfg[key] = data[key]
        except Exception as e:
            print(f"[bridge] 配置文件读取失败 {source}: {e}", file=sys.stderr, flush=True)
    for key, env_name in _ENV_KEYS.items():
        raw = os.environ.get(env_name)
        if raw is None or raw == "":
            continue
        if key == "port":
            cfg[key] = int(raw)
        elif key in ("verbose", "allow_remote"):
            cfg[key] = _truthy(raw)
        else:
            cfg[key] = raw
    cfg["config_source"] = source
    return cfg


CFG = load_config()
UPSTREAM = str(CFG["upstream"] or "").strip().rstrip("/")
LISTEN_HOST = str(CFG["host"] or "127.0.0.1")
LISTEN_PORT = int(CFG["port"])
MODEL_FALLBACK = str(CFG["model"] or "gpt-6.1-sol")
LOG_PATH = CFG["log"] or os.path.join(HERE, "bridge.log")
VERBOSE = bool(CFG["verbose"])
ALLOW_REMOTE = bool(CFG["allow_remote"])
LOG_MAX_BYTES = 5 * 1024 * 1024

SEARCH_INSTRUCTIONS = (
    "You are the web-search backend for a coding agent. "
    "Use the web_search tool to answer the request below. "
    "Reply with a compact plain-text digest of what you found. "
    "Include the concrete facts, dates, numbers and inline source URLs the agent needs. "
    "No markdown tables, no headings, no preamble."
)

HOP_HEADERS = {"host", "content-length", "connection", "accept-encoding", "transfer-encoding"}
_LOG_LOCK = threading.Lock()


def log(*parts):
    line = " ".join(str(p) for p in parts)
    if VERBOSE:
        try:
            print(line, flush=True)
        except Exception:
            pass
    try:
        with _LOG_LOCK:
            # 日志超过上限就滚动一次，避免长期运行把磁盘写满
            if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
                try:
                    os.replace(LOG_PATH, LOG_PATH + ".1")
                except Exception:
                    pass
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass


def extract_output_text(resp_json):
    """Pull the assistant text out of a Responses API payload."""
    chunks = []
    for item in resp_json.get("output") or []:
        if item.get("type") == "message":
            for c in item.get("content") or []:
                if c.get("type") in ("output_text", "text") and isinstance(c.get("text"), str):
                    chunks.append(c["text"])
    return "\n".join(chunks).strip()


def build_prompt(body):
    """Turn the /alpha/search request into a single prompt for the relay's web_search tool."""
    cmds = body.get("commands") or {}
    queries = [c.get("q") for c in (cmds.get("search_query") or []) if c.get("q")]

    # the original conversation that triggered the search, for context
    context = ""
    for item in body.get("input") or []:
        if isinstance(item, dict) and item.get("role") in ("user", "developer"):
            for c in item.get("content") or []:
                txt = c.get("text") if isinstance(c, dict) else None
                if isinstance(txt, str) and txt.strip():
                    context += txt.strip() + "\n"

    lines = []
    if context:
        lines.append("Agent's original request:\n<<<\n" + context.strip()[:4000] + "\n>>>\n")
    if queries:
        lines.append("Search queries to run:\n" + "\n".join(f"- {q}" for q in queries))
    else:
        lines.append("Commands issued:\n" + json.dumps(cmds, ensure_ascii=False))

    # any non-search commands the model asked for (open / find / finance / weather / ...)
    extra = {k: v for k, v in cmds.items() if k != "search_query"}
    if extra:
        lines.append("Additional tool commands (honour them if relevant):\n"
                     + json.dumps(extra, ensure_ascii=False)[:2000])

    length = cmds.get("response_length") or "medium"
    lines.append(f"Answer length: {length}.")
    return "\n".join(lines)


class Bridge(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "codex-websearch-bridge/1.0"

    # ---------------------------------------------------------------- helpers
    def _read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _upstream_base(self):
        return self.headers.get("X-Bridge-Upstream") or UPSTREAM

    def _send_json(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _fwd_headers(self):
        return {k: v for k, v in self.headers.items() if k.lower() not in HOP_HEADERS}

    # ---------------------------------------------------------------- /alpha/search
    def _handle_search(self, raw):
        try:
            body = json.loads(raw or b"{}")
        except Exception:
            body = {}

        upstream = self._upstream_base()
        model = body.get("model") or MODEL_FALLBACK
        prompt = build_prompt(body)
        log(f"[search] model={model} -> {upstream}/v1/responses")

        payload = {
            "model": model,
            "instructions": SEARCH_INSTRUCTIONS,
            "input": prompt,
            "tools": [{"type": "web_search"}],
            "tool_choice": "auto",
            "store": False,
            "stream": False,
        }

        headers = self._fwd_headers()
        headers.pop("X-Bridge-Upstream", None)
        headers["Content-Type"] = "application/json"
        headers["Accept"] = "application/json"

        text = ""
        try:
            r = requests.post(f"{upstream}/v1/responses", headers=headers,
                              data=json.dumps(payload), timeout=300)
            if r.status_code >= 400:
                log(f"[search] upstream HTTP {r.status_code}: {r.text[:300]}")
                text = f"(web search backend error: HTTP {r.status_code})"
            else:
                j = r.json()
                text = extract_output_text(j)
                calls = [i for i in (j.get("output") or []) if i.get("type") == "web_search_call"]
                log(f"[search] ok: {len(calls)} web_search_call(s), {len(text)} chars")
                if not text:
                    text = "(web search returned no text)"
        except Exception as e:
            log("[search] EXC", repr(e), traceback.format_exc(limit=2))
            text = f"(web search backend error: {e})"

        self._send_json(200, {"output": text})

    # ---------------------------------------------------------------- passthrough
    def _proxy(self, method, raw=b""):
        upstream = self._upstream_base()
        url = upstream + self.path
        try:
            r = requests.request(method, url, headers=self._fwd_headers(),
                                 data=raw if raw else None, stream=True, timeout=900)
        except Exception as e:
            log("[proxy] upstream error", repr(e))
            return self._send_json(502, {"error": {"message": f"bridge upstream error: {e}"}})
        self.send_response(r.status_code)
        for k, v in r.headers.items():
            if k.lower() in ("content-length", "transfer-encoding", "connection"):
                continue
            self.send_header(k, v)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            for chunk in r.iter_content(chunk_size=8192):
                if not chunk:
                    continue
                self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except Exception as e:
            log("[proxy] stream error", repr(e))

    # ---------------------------------------------------------------- routing
    def do_POST(self):
        raw = self._read_body()
        path = self.path.split("?")[0].rstrip("/")
        if path.endswith("/alpha/search"):
            return self._handle_search(raw)
        self._proxy("POST", raw)

    def do_GET(self):
        self._proxy("GET")

    def log_message(self, *a):
        pass


def check_startup():
    """启动前的配置与安全检查，返回问题列表。"""
    problems = []
    if not UPSTREAM or UPSTREAM == PLACEHOLDER_UPSTREAM:
        problems.append(
            "上游中转地址还没配置。复制 bridge.config.example.json 为 bridge.config.json，"
            "把 upstream 改成你自己的中转地址。"
        )
    if LISTEN_HOST not in LOOPBACK_HOSTS and not ALLOW_REMOTE:
        problems.append(
            f"拒绝监听非本机地址 {LISTEN_HOST}。这个桥会把请求头里的 API Key 原样转发给上游，"
            '暴露到局域网等于开放代理。确实需要时，在配置里显式写 "allow_remote": true。'
        )
    return problems


def main():
    problems = check_startup()
    if problems:
        for problem in problems:
            log(f"[fatal] 启动失败：{problem}")
            print(f"[bridge] 启动失败：{problem}", file=sys.stderr, flush=True)
        sys.exit(1)

    log(f"codex-websearch-bridge listening on {LISTEN_HOST}:{LISTEN_PORT} -> {UPSTREAM}")
    log(f"[config] source={CFG['config_source'] or 'defaults+env'} "
        f"model_fallback={MODEL_FALLBACK} log={LOG_PATH}")
    if LISTEN_HOST not in LOOPBACK_HOSTS:
        log(f"[warn] 正在监听非本机地址 {LISTEN_HOST}，"
            "任何能访问该端口的机器都可以借用你的 API Key")

    srv = http.server.ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Bridge)
    srv.daemon_threads = True
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

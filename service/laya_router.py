"""Laya judgment oracle for model routing.

Loads the checkpoint once, then answers one task per stdin line as JSON, so a caller pays the
~70 s load a single time instead of once per decision.

    .venv\\Scripts\\python.exe routing/laya_router.py < tasks.jsonl
    .venv\\Scripts\\python.exe routing/laya_router.py --selftest

Input, one JSON object per line:   {"id": <anything>, "task": "<text>"}
Output, one JSON object per line:  one judgment, echoing `id`.

This file is deliberately **policy-free**: it reports what Laya thinks (difficulty, domain,
tool/sensitivity flags, each with its calibrated confidence) and stops there. Which model a
judgment maps to is the caller's business, so routing policy can change without editing this
file. CLI: --selftest runs two tasks through the real path and asserts the output contract.

Env: LAYA_MODEL (repo or local dir), LAYA_SUBFOLDER (multilingual|typed-decisions), LAYA_DEVICE.
"""
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Piped stdio uses the locale codec (cp936 here), which would mangle non-ASCII task text on the
# way in and raise UnicodeEncodeError on the way out. This is a UTF-8 protocol, so say so.
sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("USE_TORCH", "1")

import laya  # noqa: E402

QUESTIONS = laya.router_questions()

# noul answers the "yes" probability; difficulty is an expectation over its 0-3 legend.
LEVELS = ["trivial", "easy", "moderate", "hard"]

# On `difficulty_confidence`: it is normalized-entropy peakedness (1 - H/log k), not P(correct).
# Measured on this checkpoint, real tasks land at 0.10-0.22 -- a score answer spread over two
# adjacent levels is normal, not ignorance -- so there is deliberately no "only act above N"
# threshold here. Calibrating one is the experiment's job, not a guess made in this file.

_PLACEHOLDER = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*)`")


def required_state_keys(questions):
    """State fields a preset names in its instructions, e.g. `request` or `message`.

    These presets address the state by field name, so the state must be a dict carrying those
    keys. Passing a bare string leaves the placeholder unbound and the model answers a question
    about nothing -- confidently-shaped output, near-uniform probabilities, no error. Cheap to
    check, and the alternative is a router that silently misroutes.
    """
    keys = set()
    for q in questions.values():
        keys |= set(_PLACEHOLDER.findall(str(q.get("instructions", ""))))
    return keys


REQUIRED_KEYS = required_state_keys(QUESTIONS)


def decide(agent, task):
    """One task -> one judgment dict. Mirrors the question ids in `laya.router_questions()`."""
    state = {"request": task}
    missing = REQUIRED_KEYS - set(state)
    if missing:
        raise ValueError("%s names state fields %s that the state does not carry"
                         % ("router_questions()", sorted(missing)))

    t0 = time.time()
    out = agent.predict(state, QUESTIONS)
    a = out["answers"]

    difficulty = a["difficulty"]
    level = max(0, min(len(LEVELS) - 1, int(round(difficulty["score"]))))
    domain = a["domain"]

    return {
        "difficulty": difficulty["score"],
        "difficulty_level": level,
        "difficulty_label": LEVELS[level],
        "difficulty_confidence": difficulty["confidence"],
        "difficulty_probs": difficulty["probabilities"],
        "domain": domain["choice"],
        "domain_confidence": domain["confidence"],
        "needs_tools": a["needs_tools"]["noul"],
        "needs_tools_confidence": a["needs_tools"]["confidence"],
        "is_sensitive": a["is_sensitive"]["noul"],
        "is_sensitive_confidence": a["is_sensitive"]["confidence"],
        "laya_input_tokens": out["usage"]["input_tokens"],
        "laya_ms": round((time.time() - t0) * 1000),
    }


def selftest():
    """Smallest check that catches breakage: the contract, through the real model path."""
    agent = laya.load(os.environ.get("LAYA_MODEL", "convaiinnovations/laya"),
                      device=os.environ.get("LAYA_DEVICE"),
                      subfolder=os.environ.get("LAYA_SUBFOLDER"))
    # Checked as a pair on purpose: absolute difficulty floors would assert my guess about the
    # model's judgement rather than a property of the code. Ordering is the property that matters.
    cases = [
        ("rename the variable i to index in utils.py", "code"),
        ("Design a distributed rate limiter that survives a region failover.", "code"),
        ("客户投诉说发票金额不对，要求今天退款并升级到主管。", None),  # non-ASCII round trip
    ]
    required = {"difficulty", "difficulty_level", "difficulty_label", "difficulty_probs", "domain",
                "needs_tools", "is_sensitive", "laya_input_tokens", "laya_ms"}
    bad, seen = [], []

    # The state-key binding is hardcoded in `decide`; this catches a preset rename upstream that
    # would silently unbind it again.
    if REQUIRED_KEYS != {"request"}:
        bad.append("router_questions() now names %s, not just {'request'}; `decide` needs updating"
                   % sorted(REQUIRED_KEYS))

    for task, want_domain in cases:
        j = decide(agent, task)
        seen.append(j)
        missing = required - set(j)
        if missing:
            bad.append("missing keys %s" % sorted(missing))
        if not 0.0 <= j["difficulty"] <= float(len(LEVELS) - 1):
            bad.append("difficulty out of range: %r" % j["difficulty"])
        if not 0.0 <= j["needs_tools"] <= 1.0 or not 0.0 <= j["is_sensitive"] <= 1.0:
            bad.append("noul probability out of range: %r" % j)
        if j["difficulty_label"] != LEVELS[j["difficulty_level"]]:
            bad.append("label %r disagrees with level %r" % (j["difficulty_label"], j["difficulty_level"]))
        if want_domain and j["domain"] != want_domain:
            print("   note: domain %r (expected %r) for %r" % (j["domain"], want_domain, task[:40]))
        print("   %-8s d=%.2f %-14s conf=%.2f tools=%.2f sens=%.2f %sms"
              % (j["difficulty_label"], j["difficulty"], j["domain"],
                 j["difficulty_confidence"], j["needs_tools"], j["is_sensitive"], j["laya_ms"]))
    # The regression this file was written against: with the `request` placeholder unbound, every
    # task rounded to the same level (1.54 and 1.76 -- both "moderate"). A level apart, not merely
    # a higher float, is the property the router depends on.
    if seen[1]["difficulty"] <= seen[0]["difficulty"]:
        bad.append("difficulty did not increase from the trivial task (%.2f) to the hard one (%.2f)"
                   % (seen[0]["difficulty"], seen[1]["difficulty"]))
    if seen[1]["difficulty_level"] <= seen[0]["difficulty_level"]:
        bad.append("trivial and hard round to the same level %r (%.2f vs %.2f), so the router would "
                   "send both to the same model"
                   % (seen[0]["difficulty_label"], seen[0]["difficulty"], seen[1]["difficulty"]))
    if bad:
        print("FAIL: " + "; ".join(bad))
        return 1
    print("contract ok: %d cases" % len(cases))
    return 0


def serve():
    agent = None
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            task = req["task"]
        except Exception as e:  # one bad line must not kill the stream
            print(json.dumps({"error": "bad request: %s" % e}), flush=True)
            continue
        if agent is None:  # load lazily so a malformed first line still reports as an error
            agent = laya.load(os.environ.get("LAYA_MODEL", "convaiinnovations/laya"),
                              device=os.environ.get("LAYA_DEVICE"),
                              subfolder=os.environ.get("LAYA_SUBFOLDER"))
            print("router ready", file=sys.stderr, flush=True)
        try:
            j = decide(agent, task)
        except Exception as e:
            j = {"error": "%s: %s" % (type(e).__name__, e)}
        j["id"] = req.get("id")
        print(json.dumps(j, ensure_ascii=False), flush=True)


def http_serve(port=8765):
    """HTTP mode: POST /judge {"task": "...", "prev_tier": ..., "prev_task": ...} -> judgment JSON.

    协议自动检测：
      LAYA_MODEL 是本地目录 → 微调协议（7 noul 含 Q7 + 意图解析 + 规则引擎含规则0 + C3 升级 → tier）
      LAYA_MODEL 是仓库 id   → 基座协议（4分类 difficulty + domain 等）
    """
    from http.server import HTTPServer, BaseHTTPRequestHandler

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

    model_path = os.environ.get(
        "LAYA_MODEL",
        # VENDORED DIFF (dsh-router-laya npm package): in the packaged layout the checkpoint is
        # fetched by `node weights/fetch.mjs` into <package>/weights/model, so the default points
        # there instead of the source checkout's <repo>/training/laya_router_finetuned. The
        # package launchers (service/start_router.*) set LAYA_MODEL explicitly anyway.
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "weights", "model"),
    )
    is_finetuned = os.path.isdir(model_path)

    if is_finetuned:
        from finetuned_judge import load as ft_load, judge as ft_judge
        agent = ft_load(model_path, device=os.environ.get("LAYA_DEVICE"))
        protocol = "finetuned"
        print("Laya router loaded [微调协议], HTTP on :%d" % port, file=sys.stderr, flush=True)
        print("  model: %s" % model_path, file=sys.stderr, flush=True)
    else:
        agent = laya.load(model_path,
                         device=os.environ.get("LAYA_DEVICE"),
                         subfolder=os.environ.get("LAYA_SUBFOLDER"))
        protocol = "base"
        print("Laya router loaded [基座协议], HTTP on :%d" % port, file=sys.stderr, flush=True)
        print("  model: %s" % model_path, file=sys.stderr, flush=True)

    # Per-session judgment log for the frontend tier chip (plugin v2 frontend, GET /state).
    # session_id -> deque of the last 20 judgments {ts, tier, triggered_by, regenerate, ms, task}.
    judge_log = {}

    class Handler(BaseHTTPRequestHandler):
        def _cors(self):
            """Allow the DSH web page to read this service from another port.

            The tier chip runs in the browser on :3080 and polls GET /state here on :8765. A different
            port is a different origin, so without these headers the browser blocks the response and the
            chip can never see a judgment -- spec §2's "前端只管读" is not reachable without them.
            Restricted to loopback origins: this service holds task text, so a wildcard would let any
            page in the user's browser read their prompts.
            """
            origin = self.headers.get("Origin")
            if origin and (origin.startswith("http://127.0.0.1:") or origin.startswith("http://localhost:")):
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")

        def do_POST(self):
            if self.path != "/judge":
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            task = body.get("task", "")
            prev_tier = body.get("prev_tier")  # 上一轮档位，inherit/升级用
            prev_task = body.get("prev_task")  # 上一轮任务文本，regenerate 检测用（plugin v2）
            session_id = body.get("session_id") or "default"
            try:
                if protocol == "finetuned":
                    j = ft_judge(agent, task, prev_tier=prev_tier, prev_task=prev_task)
                else:
                    j = decide(agent, task)
                j["id"] = body.get("id")
                j["protocol"] = protocol
            except Exception as e:
                j = {"error": "%s: %s" % (type(e).__name__, e), "protocol": protocol}
            if j.get("tier"):
                from collections import deque
                log = judge_log.setdefault(session_id, deque(maxlen=20))
                log.append({
                    "ts": time.time(),
                    "tier": j["tier"],
                    "triggered_by": j.get("triggered_by", ""),
                    "regenerate": bool(j.get("regenerate")),
                    "ms": j.get("ms"),
                    "task": (task or "")[:60],
                })
            payload = json.dumps(j, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            """GET /health 返回协议信息；GET /state 返回各会话最近判定（前端档位芯片用）。"""
            if self.path == "/state":
                payload = json.dumps(
                    {"sessions": {k: list(v) for k, v in judge_log.items()},
                     "protocol": protocol},
                    ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self._cors()
                self.end_headers()
                self.wfile.write(payload)
                return
            if self.path != "/health":
                self.send_error(404)
                return
            info = {"protocol": protocol, "model": model_path,
                    "finetuned": is_finetuned, "status": "ok"}
            payload = json.dumps(info, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, fmt, *args):
            pass  # quiet

    server = HTTPServer(("127.0.0.1", port), Handler)
    print("Ready. POST http://127.0.0.1:%d/judge  GET /health" % port,
          file=sys.stderr, flush=True)
    server.serve_forever()


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    if "--http" in sys.argv:
        port = 8765
        for i, a in enumerate(sys.argv):
            if a == "--port" and i + 1 < len(sys.argv):
                port = int(sys.argv[i + 1])
        http_serve(port)
    else:
        serve()

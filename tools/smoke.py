#!/usr/bin/env python3
"""Behavioural test for the build-token-trigger atom.

"Jenkins" here is a real HTTP server that records every request verbatim. The expected path, body and headers are
recomputed from the job name, token and parameters with urllib, so an atom that sends the wrong thing cannot agree with
the expectation by accident. Every failure path asserts the status *and* the error code the atom reports.
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CODE = json.load(open(os.path.join(ROOT, "task.json"), encoding="UTF-8"))["atomCode"]
JAR = os.path.join(ROOT, "dist", CODE, CODE + ".jar")
JAVA = os.path.join(os.environ["JAVA_HOME"], "bin", "java") if os.environ.get("JAVA_HOME") else "java"

REQUESTS = []
LOCK = threading.Lock()

# job -> (status, headers, body); the job name is the scenario, so one server covers every answer
SCENARIOS = {
    "queued": (201, {"X-Jenkins": "2.492"}, "Jenkins: queued"),
    "team/api-build": (201, {"X-Jenkins": "2.492"}, "Jenkins: queued"),
    "relqueue": (201, {"X-Jenkins": "2.492"}, "Jenkins: queued"),
    "slow": (201, {"X-Jenkins": "2.492"}, "Jenkins: queued"),
    "missing": (404, {}, "Not Found"),
    "unqueued": (302, {}, ""),
    "forbidden": (403, {}, "Invalid token"),
    "boom": (500, {}, "java.lang.IllegalStateException: kaboom"),
    "noloc": (200, {"X-Jenkins": "2.492"}, "Jenkins: not queued"),
    "moved": (303, {}, ""),
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace")
        job = parse_qs(raw, keep_blank_values=True).get("job", [""])[0]
        with LOCK:
            REQUESTS.append({
                "method": self.command,
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": raw,
            })
        status, headers, body = SCENARIOS.get(job, (404, {}, "Not Found"))
        if job == "slow":
            time.sleep(3)
        location = None
        if job in ("queued", "team/api-build", "slow"):
            location = "http://127.0.0.1:%d/queue/item/42/" % self.server.server_address[1]
        elif job == "relqueue":
            location = "/queue/item/43/"
        elif job == "unqueued":
            location = "/queue/item/44/"
        elif job == "moved":
            location = "/queue/item/45/"
        payload = body.encode("utf-8")
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Type", "text/plain;charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # the timeout case hangs up on purpose; that is the atom timing out, not a server bug
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def start_server():
    server = Server(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def run_case(directory, **inputs):
    """Runs the atom the way the runner does: input.json plus the environment in the data dir."""
    os.makedirs(directory, exist_ok=True)
    json.dump(inputs, open(os.path.join(directory, "input.json"), "w", encoding="utf-8"), ensure_ascii=False)
    env = {k: v for k, v in os.environ.items() if k.isupper()}
    env.update({"atom_data_dir": directory, "atom_data_input": "input.json",
                "atom_data_output": "output.json", "atom_workspace": directory})
    proc = subprocess.run([JAVA, "-cp", JAR, "com.jenkinschina.atom.sdk.AtomRunner"],
                          cwd=directory, env=env, capture_output=True, text=True)
    out_path = os.path.join(directory, "output.json")
    doc = json.load(open(out_path, encoding="utf-8")) if os.path.exists(out_path) else {}
    return proc.returncode, proc.stdout + proc.stderr, doc


PASSED = []


def ok(what):
    PASSED.append(what)
    print("  PASS " + what)


def expect_request(last=True):  # the request the atom just sent
    assert REQUESTS, "no request reached the fake Jenkins"
    return REQUESTS[-1]


def check_request(job, token, delay, params, path):
    req = expect_request()
    assert req["method"] == "POST", req["method"]
    assert req["path"] == path, "path %s != %s" % (req["path"], path)
    pairs = [("job", job), ("token", token)]
    if delay is not None:
        pairs.append(("delay", str(delay)))
    pairs += sorted(params.items())
    expected = urlencode(pairs)
    assert req["body"] == expected, "body\n    got      %s\n    expected %s" % (req["body"], expected)
    headers = req["headers"]
    assert headers.get("content-type") == "application/x-www-form-urlencoded", headers.get("content-type")
    assert headers.get("content-length") == str(len(expected.encode("utf-8"))), headers.get("content-length")
    assert "authorization" not in headers, "an Authorization header was sent to a build-token endpoint"
    return req


def check_output(doc, field, expected):
    node = doc.get("data", {}).get(field)
    value = node.get("value") if isinstance(node, dict) else node
    assert value == expected, "%s: %r != %r" % (field, value, expected)


def check_success(doc):
    assert doc.get("status") == "success", "status %s: %s" % (doc.get("status"), doc.get("message"))


def check_error(doc, code, needle):
    assert doc.get("status") == "error", "status %s" % doc.get("status")
    assert str(doc.get("errorCode")) == str(code), "errorCode %s != %s (%s)" % (doc.get("errorCode"), code, doc.get("message"))
    assert needle in (doc.get("message") or ""), "message does not mention %r: %r" % (needle, doc.get("message"))


def main():
    assert os.path.exists(JAR), "build first: " + JAR
    server = start_server()
    base = "http://127.0.0.1:%d" % server.server_address[1]
    tmp = tempfile.mkdtemp(prefix="btt-smoke-")
    case = lambda name: os.path.join(tmp, name)

    print("1. a normal trigger: parameters select /buildWithParameters, delay is a form field, the token is escaped")
    token = "tok en&x=1"
    rc, log, doc = run_case(case("c1"), jenkinsUrl=base, job="queued", token=token,
                            parameters="branch=main\nnote=hello world\n备注=中文\n# comment line", delay="30")
    check_request("queued", token, 30, {"branch": "main", "note": "hello world", "备注": "中文"},
                  "/buildByToken/buildWithParameters")
    check_success(doc)
    check_output(doc, "outcome", "queued")
    check_output(doc, "queued", "true")
    check_output(doc, "httpCode", "201")
    check_output(doc, "queueUrl", base + "/queue/item/42/")
    check_output(doc, "jobUrl", base + "/job/queued")
    check_output(doc, "triggerUrl", base + "/buildByToken/buildWithParameters")
    assert token not in log, "the token was written to the build log"
    escaped = urlencode([("t", token)])[2:]
    assert ("token=" + escaped) in REQUESTS[-1]["body"], "the token is not escaped in the body"
    ok("path, body, headers, queue URL; token escaped and never logged")

    print("2. no parameters -> /build; an uppercase host and a trailing slash are normalized; a relative Location is completed")
    rc, log, doc = run_case(case("c2"), jenkinsUrl="http://LOCALHOST:%d/jenkins/" % server.server_address[1],
                            job="relqueue", token="t0ken")
    req = check_request("relqueue", "t0ken", None, {}, "/jenkins/buildByToken/build")
    assert req["headers"].get("host") == "localhost:%d" % server.server_address[1], req["headers"].get("host")
    check_output(doc, "outcome", "queued")
    check_output(doc, "queueUrl", "http://localhost:%d/jenkins/queue/item/43/" % server.server_address[1])
    check_output(doc, "jobUrl", "http://localhost:%d/jenkins/job/relqueue" % server.server_address[1])
    ok("context path kept, Host lowercased, relative Location resolved against jenkinsUrl")

    print("3. a folder job: /job/ segments in the page URL, one escaped field in the body")
    rc, log, doc = run_case(case("c3"), jenkinsUrl=base, job="team/api-build", token="tok")
    check_request("team/api-build", "tok", None, {}, "/buildByToken/build")
    check_output(doc, "jobUrl", base + "/job/team/job/api-build")
    ok("folder job")

    print("4. a negative or unparsable delay is not sent, as in the plugin's newInstance")
    rc, log, doc = run_case(case("c4"), jenkinsUrl=base, job="queued", token="tok", delay="-5")
    check_request("queued", "tok", None, {}, "/buildByToken/build")
    check_success(doc)
    rc, log, doc = run_case(case("c4b"), jenkinsUrl=base, job="queued", token="tok", delay="abc")
    check_request("queued", "tok", None, {}, "/buildByToken/build")
    ok("no delay field")

    print("5. 404 is a failure that names the job")
    rc, log, doc = run_case(case("c5"), jenkinsUrl=base, job="missing", token="tok")
    assert rc != 0, "a 404 exited 0"
    check_error(doc, 40003, "missing")
    ok("404 -> 40003")

    print("6. 404 with ignoreMissing is a success reporting about:missing")
    rc, log, doc = run_case(case("c6"), jenkinsUrl=base, job="missing", token="tok", ignoreMissing="true")
    check_success(doc)
    check_output(doc, "outcome", "missing")
    check_output(doc, "queueUrl", "about:missing")
    check_output(doc, "queued", "false")
    ok("ignoreMissing")

    print("7. 302 is 'not queued' by default, about:unqueued when ignored")
    rc, log, doc = run_case(case("c7"), jenkinsUrl=base, job="unqueued", token="tok")
    assert rc != 0, "a 302 exited 0"
    check_error(doc, 40004, "unqueued")
    rc, log, doc = run_case(case("c7b"), jenkinsUrl=base, job="unqueued", token="tok", ignoreUnqueued="true")
    check_success(doc)
    check_output(doc, "outcome", "unqueued")
    check_output(doc, "queueUrl", "about:unqueued")
    ok("302 both ways")

    print("8. a rejected token is 40006; a server error is 40005 with a snippet")
    rc, log, doc = run_case(case("c8"), jenkinsUrl=base, job="forbidden", token="tok")
    assert rc != 0, "a 403 exited 0"
    check_error(doc, 40006, "403")
    rc, log, doc = run_case(case("c9"), jenkinsUrl=base, job="boom", token="tok")
    assert rc != 0, "a 500 exited 0"
    check_error(doc, 40005, "kaboom")
    ok("403 -> 40006, 500 -> 40005 with body snippet")

    print("9. a 2xx without Location is 40007; a redirect the plugin never followed is 40008")
    rc, log, doc = run_case(case("c10"), jenkinsUrl=base, job="noloc", token="tok")
    assert rc != 0, "a 200 without Location exited 0"
    check_error(doc, 40007, "Location")
    rc, log, doc = run_case(case("c11"), jenkinsUrl=base, job="moved", token="tok")
    assert rc != 0, "a 303 exited 0"
    check_error(doc, 40008, "303")
    ok("missing Location and unfollowed redirect")

    print("10. nothing is followed: every request went to /buildByToken")
    bad = [r["path"] for r in REQUESTS if not (r["path"].startswith("/buildByToken")
                                                or r["path"].startswith("/jenkins/buildByToken"))]
    assert not bad, bad
    ok("%d requests, all to /buildByToken" % len(REQUESTS))

    print("11. an unreachable Jenkins is 40002; a silent one is 40009")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    closed = sock.getsockname()[1]
    sock.close()
    rc, log, doc = run_case(case("c12"), jenkinsUrl="http://127.0.0.1:%d" % closed, job="queued", token="tok",
                            timeoutSeconds="2")
    assert rc != 0, "an unreachable host exited 0"
    check_error(doc, 40002, str(closed))
    rc, log, doc = run_case(case("c13"), jenkinsUrl=base, job="slow", token="tok", timeoutSeconds="1")
    assert rc != 0, "a hanging server exited 0"
    check_error(doc, 40009, "1s")
    ok("connect failure 40002, read timeout 40009")

    print("12. invalid input is rejected before any request is made")
    before = len(REQUESTS)
    rc, log, doc = run_case(case("c14"), jenkinsUrl=base, job="", token="tok")
    assert rc != 0, "an empty job exited 0"
    check_error(doc, 40001, "job")
    rc, log, doc = run_case(case("c15"), jenkinsUrl=base, job="queued", token="")
    assert rc != 0, "an empty token exited 0"
    check_error(doc, 40001, "token")
    rc, log, doc = run_case(case("c16"), jenkinsUrl="ftp://example.com", job="queued", token="tok")
    assert rc != 0, "an ftp URL exited 0"
    check_error(doc, 40002, "http")
    rc, log, doc = run_case(case("c17"), jenkinsUrl=base, job="queued", token="tok", parameters="noequals")
    assert rc != 0, "a malformed parameter exited 0"
    check_error(doc, 40001, "KEY=VALUE")
    assert len(REQUESTS) == before, "invalid input still sent a request"
    ok("40001/40002 with no request sent")

    print("smoke ok: %d checks" % len(PASSED))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Mutation test: every deliberate break of the atom must make tools/smoke.sh fail.

A smoke test that passes proves nothing until a wrong implementation fails it. Each entry below patches one rule, runs
the real smoke test against the patched copy, and requires a non-zero exit. The first run is an unpatched control.
"""
import glob
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ATOM = os.path.join(ROOT, "src/main/java/com/jenkinschina/atom/buildtokentrigger/BuildTokenTriggerAtom.java")
def find_sdk():
    """The SDK is a sibling checkout locally and a checkout inside the job directory on CI."""
    candidates = [os.environ.get("SDK_JAR", ""),
                  os.path.join(ROOT, "..", "jenkins-atom-sdk", "target"),
                  os.path.join(ROOT, "jenkins-atom-sdk", "target"),
                  os.path.expanduser("~/Codes/jenkins-atom-sdk/target")]
    for candidate in candidates:
        if candidate and candidate.endswith(".jar") and os.path.exists(candidate):
            return candidate
        found = sorted(glob.glob(os.path.join(candidate, "jenkins-atom-sdk-*.jar"))) if candidate else []
        if found:
            return found[-1]
    raise SystemExit("build the SDK first (../jenkins-atom-sdk/build.sh) or set SDK_JAR")


SDK_JAR = find_sdk()

MUTATIONS = [
    ("parameters still go to /build",
     '"/buildByToken" + (parameters.isEmpty() ? "/build" : "/buildWithParameters")', '"/buildByToken/build"'),
    ("parameters are sent in reverse order",
     "for (Map.Entry<String, String> entry : parameters.entrySet())",
     "for (Map.Entry<String, String> entry : parameters.descendingMap().entrySet())"),
    ("the token is not URL-encoded",
     'data.append("&token=").append(encode(token));', 'data.append("&token=").append(token);'),
    ("the host is not lowercased",
     "uri.getHost().toLowerCase(Locale.ENGLISH)", "uri.getHost()"),
    ("the request is a GET",
     ".POST(HttpRequest.BodyPublishers.ofByteArray(body))", ".GET()"),
    ("404 is treated as queued",
     "if (status == 404) {", "if (status == 999) {"),
    ("302 is treated as queued",
     "if (status == 302) {", "if (status == 998) {"),
    ("a relative Location is returned as is",
     'location.startsWith("/") ? base + location : location', "location"),
    ("other redirects count as queued",
     "if (status >= 300 && status < 400) {", "if (status >= 390 && status < 400) {"),
    ("no Content-Type header",
     '.header("Content-Type", "application/x-www-form-urlencoded")', ""),
    ("delay is always sent",
     "if (delay != null && delay >= 0) {", "if (true) {"),
    ("the job URL is not expanded to /job/ segments",
     '"/job/" + stripLeadingSlash(stripTrailingSlash(job)).replace("/", "/job/")', '"/job/" + job'),
]


def prepare():
    directory = tempfile.mkdtemp(prefix="btt-mut-")
    for name in ("src", "tools", "build.sh", "task.json", "meta.json"):
        source = os.path.join(ROOT, name)
        if os.path.isdir(source):
            shutil.copytree(source, os.path.join(directory, name))
        else:
            shutil.copy2(source, os.path.join(directory, name))
    return directory


def run_smoke(directory):
    env = dict(os.environ)
    env["SDK_JAR"] = SDK_JAR
    env.setdefault("JAVA_HOME", os.path.expanduser("~/tools/jdk-21.0.12.1+1/Contents/Home"))
    proc = subprocess.run(["bash", os.path.join(directory, "tools", "smoke.sh")],
                          cwd=directory, env=env, capture_output=True, text=True, timeout=900)
    return proc.returncode, proc.stdout + proc.stderr


def main():
    control = prepare()
    code, output = run_smoke(control)
    if code != 0:
        raise SystemExit("the unpatched copy fails its own smoke test:\n" + output[-4000:])
    print("control: the unpatched copy passes")

    source = open(ATOM, encoding="utf-8").read()
    survived = []
    for label, old, new in MUTATIONS:
        if source.count(old) != 1:
            raise SystemExit("mutation target appears %d times, not once: %s" % (source.count(old), old[:60]))
        directory = prepare()
        target = os.path.join(directory, os.path.relpath(ATOM, ROOT))
        open(target, "w", encoding="utf-8").write(source.replace(old, new))
        code, output = run_smoke(directory)
        if code == 0:
            survived.append(label)
            print("  SURVIVED %s" % label)
        else:
            first = next((line.strip() for line in output.splitlines()
                          if "AssertionError" in line or "failed:" in line), "")
            print("  killed   %s  %s" % (label, first[:160]))
        shutil.rmtree(directory, ignore_errors=True)
    shutil.rmtree(control, ignore_errors=True)

    if survived:
        raise SystemExit("mutations survived: %s" % survived)
    print("mutations ok: %d/%d killed" % (len(MUTATIONS), len(MUTATIONS)))


if __name__ == "__main__":
    main()

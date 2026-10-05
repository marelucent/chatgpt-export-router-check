#!/usr/bin/env python3
"""Self-check for router_check.py. Builds a small made-up export and checks the results.
Run:  python test_router_check.py      (prints OK, or says what went wrong)"""
import io, json, os, re, shutil, sys, tempfile, zipfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import router_check as rc

SECRET = "beloved-petal-secret"   # a message word that must never leak unless asked for


def node(i, parent, role, slug=None, t=0, flag=False, text="hello", hints=None):
    md = {}
    if slug: md["model_slug"] = slug
    if flag: md["show_safety_switch_ui"] = True
    if hints: md["system_hints"] = hints
    return {"id": i, "parent": parent, "children": [],
            "message": {"author": {"role": role}, "create_time": t, "metadata": md,
                        "content": {"content_type": "text", "parts": [text]}}}


def make_export(folder):
    day = 86400
    t0 = 1735689600   # 1 Jan 2025
    m = {"root": {"id": "root", "parent": None, "children": [], "message": None}}
    prev = "root"
    # 40 weeks of ordinary 4o turns, one a week
    for w in range(40):
        u, a = "u%d" % w, "a%d" % w
        m[u] = node(u, prev, "user", t=t0 + w * 7 * day)
        m[a] = node(a, u, "assistant", "gpt-4o", t0 + w * 7 * day + 5)
        m[prev]["children"].append(u); m[u]["children"].append(a); prev = a
    # a switched message, rewritten, got 4o back (timestamps in milliseconds on purpose)
    tw = t0 + 41 * 7 * day
    m["s1"] = node("s1", prev, "user", t=tw, text="I love you " + SECRET)
    m["s1r"] = node("s1r", "s1", "assistant", "gpt-5", tw + 5, flag=True)
    m["s2"] = node("s2", prev, "user", t=(tw + 60) * 1000, text="the heating is working")
    m["s2r"] = node("s2r", "s2", "assistant", "gpt-4o", tw + 65)
    m[prev]["children"] += ["s1", "s2"]; m["s1"]["children"].append("s1r"); m["s2"]["children"].append("s2r")
    # a mini fallback and a chosen Think reply
    m["m1"] = node("m1", "s2r", "user", t=tw + 100)
    m["m1r"] = node("m1r", "m1", "assistant", "gpt-4o-mini", tw + 105)
    m["c1"] = node("c1", "m1r", "user", t=tw + 200, hints=["reason"])
    m["c1r"] = node("c1r", "c1", "assistant", "o1", tw + 205)
    m["s2r"]["children"].append("m1"); m["m1"]["children"].append("m1r")
    m["m1r"]["children"].append("c1"); m["c1"]["children"].append("c1r")
    convs = [{"id": "conv1", "title": "Test conversation", "default_model_slug": "gpt-4o", "mapping": m},
             {"id": "conv2", "title": "Not 4o", "default_model_slug": "gpt-5", "mapping": {}}]
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as z:
        z.writestr("conversations-000.json", json.dumps(convs))
    path = os.path.join(folder, "test-export.zip")
    with zipfile.ZipFile(path, "w") as z:     # nested, like real 2026 exports
        z.writestr("User Online Activity/Conversations__x-chatgpt-0001.zip", inner.getvalue())
    return path


def check(cond, what):
    if not cond:
        sys.exit("FAILED: " + what)


def main():
    tmp = tempfile.mkdtemp()
    try:
        export = make_export(tmp)
        out1, out2 = os.path.join(tmp, "plain"), os.path.join(tmp, "words")
        rc.main([export, "--out", out1])
        rc.main([export, "--out", out2, "--show-words"])

        summary = open(os.path.join(out1, "summary.txt"), encoding="utf-8").read()
        check("Replies marked with OpenAI's safety-switch flag: 1" in summary, "router count")
        check("got 4o back: 1" in summary, "rewrite count")
        check("Test conversation" in summary, "last 4o conversation title")
        check("mini" in open(os.path.join(out1, "others.csv"), encoding="utf-8").read(), "mini fallback sorted")
        check("you chose it" in open(os.path.join(out1, "others.csv"), encoding="utf-8").read(), "chosen model sorted")

        # nothing private leaks without --show-words
        for name in os.listdir(out1):
            check(SECRET not in open(os.path.join(out1, name), encoding="utf-8").read(),
                  "message text leaked into " + name)
        page2 = open(os.path.join(out2, "timeline.html"), encoding="utf-8").read()
        check("The words you cut" in page2 and "love" in page2, "word swap shown on page when asked")
        check("The words you cut" not in open(os.path.join(out1, "timeline.html"), encoding="utf-8").read(),
              "word swap hidden when not asked")

        # the chart lines must stretch across the full width (the squashed-chart bug)
        page = open(os.path.join(out1, "timeline.html"), encoding="utf-8").read()
        for pts in re.findall(r'points="([^"]*)"', page):
            xs = [float(p.split(",")[0]) for p in pts.split()]
            check(len(xs) > 30, "chart has a point per week")
            check(abs(xs[-1] - 940) < 1, "chart line reaches the right-hand edge")
            gaps = {round(b - a, 1) for a, b in zip(xs, xs[1:])}
            check(len(gaps) <= 2, "chart points evenly spaced")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\nOK: all checks passed.")


if __name__ == "__main__":
    main()

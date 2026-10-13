#!/usr/bin/env python3
"""Self-check for router_check.py. Builds a small made-up export and checks the results.
Run:  python test_router_check.py      (prints OK, or says what went wrong)"""
import io, json, os, re, shutil, sys, tempfile, zipfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import router_check as rc

SECRET = "beloved-petal-secret"   # a message word that must never leak unless asked for
DAY = 86400


def node(i, parent, role, slug=None, t=0, flag=False, text="hello", hints=None, meant=None):
    md = {}
    if slug: md["model_slug"] = slug
    if meant: md["default_model_slug"] = meant
    if flag: md["show_safety_switch_ui"] = True
    if hints: md["system_hints"] = hints
    return {"id": i, "parent": parent, "children": [],
            "message": {"author": {"role": role}, "create_time": t, "metadata": md,
                        "content": {"content_type": "text", "parts": [text]}}}


def link(m, parent, *kids):
    m[parent]["children"].extend(kids)


def make_export(folder):
    t0 = 1735689600   # 1 Jan 2025
    m = {"root": {"id": "root", "parent": None, "children": [], "message": None}}
    prev = "root"
    # 40 weeks of ordinary 4o turns, one a week; then a quiet fortnight (week 40 is empty)
    for w in range(40):
        u, a = "u%d" % w, "a%d" % w
        m[u] = node(u, prev, "user", t=t0 + w * 7 * DAY)
        m[a] = node(a, u, "assistant", "gpt-4o", t0 + w * 7 * DAY + 5)
        link(m, prev, u); link(m, u, a); prev = a
    # a switched message, rewritten, and 4o really came back (timestamps in milliseconds on purpose)
    tw = t0 + 41 * 7 * DAY
    m["s1"] = node("s1", prev, "user", t=tw, text="I love you " + SECRET)
    m["s1r"] = node("s1r", "s1", "assistant", "gpt-5", tw + 5, flag=True)
    m["s2"] = node("s2", prev, "user", t=(tw + 60) * 1000, text="the heating is working")
    m["s2r"] = node("s2r", "s2", "assistant", "gpt-4o", tw + 65)
    link(m, prev, "s1", "s2"); link(m, "s1", "s1r"); link(m, "s2", "s2r")
    # a mini fallback and a Think reply
    m["m1"] = node("m1", "s2r", "user", t=tw + 100)
    m["m1r"] = node("m1r", "m1", "assistant", "gpt-4o-mini", tw + 105)
    m["c1"] = node("c1", "m1r", "user", t=tw + 200, hints=["reason"])
    m["c1r"] = node("c1r", "c1", "assistant", "o1", tw + 205)
    link(m, "s2r", "m1"); link(m, "m1", "m1r"); link(m, "m1r", "c1"); link(m, "c1", "c1r")
    # a silent switch (meant for 4o, no banner) and a model picked by hand (meant = answered)
    m["q1"] = node("q1", "c1r", "user", t=tw + 300)
    m["q1r"] = node("q1r", "q1", "assistant", "gpt-5", tw + 305, meant="gpt-4o")
    m["p1"] = node("p1", "q1r", "user", t=tw + 400)
    m["p1r"] = node("p1r", "p1", "assistant", "gpt-5-thinking", tw + 405, meant="gpt-5-thinking")
    link(m, "c1r", "q1"); link(m, "q1", "q1r"); link(m, "q1r", "p1"); link(m, "p1", "p1r")
    # a switched message whose rewrite got NO flag but was answered by GPT-5: not 4o back
    m["r1"] = node("r1", "p1r", "user", t=tw + 500)
    m["r1r"] = node("r1r", "r1", "assistant", "gpt-5", tw + 505, flag=True)
    m["r2"] = node("r2", "p1r", "user", t=tw + 560)
    m["r2r"] = node("r2r", "r2", "assistant", "gpt-5", tw + 565, meant="gpt-4o")
    link(m, "p1r", "r1", "r2"); link(m, "r1", "r1r"); link(m, "r2", "r2r")
    # a switch three weeks AFTER 4o's last reply: must not count in the final fourteen days
    m["z1"] = node("z1", "r2r", "user", t=tw + 20 * DAY)
    m["z1r"] = node("z1r", "z1", "assistant", "gpt-5", tw + 20 * DAY + 5, flag=True)
    link(m, "r2r", "z1"); link(m, "z1", "z1r")

    # a conversation that started on another model, with a flag of its own
    o = {"root": {"id": "root", "parent": None, "children": ["o1"], "message": None},
         "o1": node("o1", "root", "user", t=tw + 50), "o1r": node("o1r", "o1", "assistant", "gpt-5", tw + 55, flag=True)}
    o["o1"]["children"].append("o1r")
    convs = [{"id": "conv1", "title": "Test conversation", "default_model_slug": "gpt-4o", "mapping": m},
             {"id": "conv2", "title": "Not 4o", "default_model_slug": "gpt-5", "mapping": o}]
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
        # the router: counted in conversations that started on 4o, the other one reported apart
        check("Replies marked with OpenAI's safety-switch flag: 3" in summary, "router count, 4o conversations only")
        check("GPT-4o conversations it touched: 1 of 1" in summary, "like-with-like conversation count")
        check("Also 1 flagged replies in conversations that didn't start on 4o" in summary, "other flags reported apart")
        # rewriting: only a reply FROM 4o counts as getting 4o back
        check("switched to another model: 2" in summary, "two switched messages rewritten")
        check("got 4o back: 1 " in summary, "an unflagged GPT-5 reply is not 4o back")
        check("answered by another model with no flag: 1" in summary, "quiet switch after a rewrite reported")
        # the final fourteen days end at 4o's last reply
        check("final fourteen days with 4o: 1" in summary, "switches after the last 4o reply not counted")
        check("Test conversation" in summary, "last 4o conversation title")

        others = open(os.path.join(out1, "others.csv"), encoding="utf-8").read()
        check("mini" in others, "mini fallback sorted")
        check(re.search(r"likely switched[^\n]*,gpt-5,2", others), "silent switches caught as likely")
        check(re.search(r"likely your choice[^\n]*,gpt-5-thinking,1", others), "hand-picked model as likely choice")
        check(re.search(r"likely your choice[^\n]*,o1,1", others), "Think button as likely choice")
        check("possible switch" not in others, "nothing left unexplained in the test export")

        # nothing private leaks without --show-words
        for name in os.listdir(out1):
            check(SECRET not in open(os.path.join(out1, name), encoding="utf-8").read(),
                  "message text leaked into " + name)
        page2 = open(os.path.join(out2, "timeline.html"), encoding="utf-8").read()
        check("The words you cut" in page2 and "love" in page2, "word swap shown on page when asked")
        check("The words you cut" not in open(os.path.join(out1, "timeline.html"), encoding="utf-8").read(),
              "word swap hidden when not asked")

        # the chart: points on a real calendar, quiet weeks shown as breaks
        page = open(os.path.join(out1, "timeline.html"), encoding="utf-8").read()
        lines = re.findall(r'points="([^"]*)"', page)
        check(len(lines) == 2, "one long run per line before the quiet fortnight")
        for pts in lines:
            xs = [float(p.split(",")[0]) for p in pts.split()]
            check(len(xs) == 40, "a point per active week")
            check(abs(xs[0] - 50) < 1, "line starts at the left edge")
            gaps = {round(b - a, 1) for a, b in zip(xs, xs[1:])}
            check(len(gaps) <= 2, "weekly points evenly spaced")
            week = xs[1] - xs[0]
        dots = [float(x) for x in re.findall(r'<circle cx="([\d.]+)" cy="[\d.]+" r="2\.5"', page)]
        check(dots, "lone weeks after a gap drawn as dots, not joined")
        check(abs(max(dots) - 940) < 1, "the last week reaches the right-hand edge")
        check(abs((min(dots) - xs[-1]) - 2 * week) < 1, "the empty week keeps its width on the calendar")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\nOK: all checks passed.")


if __name__ == "__main__":
    main()

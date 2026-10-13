#!/usr/bin/env python3
"""
ChatGPT Export Router Check
See what happened to your GPT-4o conversations, from your own ChatGPT export.

It counts, week by week:
  - how many replies in your GPT-4o conversations came from a different model,
  - how many of those swaps carried OpenAI's own "safety switch" marker,
  - how often you rewrote your messages (and whether the rewrite got 4o back),
  - and when your last conversation with 4o was.

It only reads labels, dates and the shape of each conversation.
It never prints your messages. Nothing leaves your computer.

Usage:
    python router_check.py                       (looks in this folder for your export)
    python router_check.py path/to/your-export   (a .zip, a folder, or conversations.json)

Options:
    --out FOLDER      where to save the results (default: router-check-results)
    --show-words      also list the words you most often cut when you rewrote a
                      message that had been switched. This reads your messages,
                      on your computer only, and is off unless you ask for it.

Needs Python 3.8 or newer. No other installs.
"""

import argparse
import collections
import csv
import datetime as dt
import difflib
import html
import io
import json
import os
import re
import sys
import zipfile

ROUTER_FLAG = "show_safety_switch_ui"   # the marker OpenAI's export puts on router-switched replies
FOURO = "gpt-4o"

# ---------------------------------------------------------------- finding the export

CONV_NAME = re.compile(r"^conversations(-\d+)?\.json$", re.I)


def _from_zip(zf, depth=0):
    """Yield conversation lists from a zip, opening zips inside it (newer exports nest them)."""
    for info in zf.infolist():
        base = os.path.basename(info.filename)
        if CONV_NAME.match(base):
            with zf.open(info) as f:
                yield json.load(f)
        elif base.lower().endswith(".zip") and depth < 2 and "conversation" in info.filename.lower():
            with zf.open(info) as f:
                inner = zipfile.ZipFile(io.BytesIO(f.read()))
            yield from _from_zip(inner, depth + 1)


def _from_folder(folder):
    found = False
    for root, _dirs, files in os.walk(folder):
        for name in sorted(files):
            p = os.path.join(root, name)
            if CONV_NAME.match(name):
                found = True
                with open(p, encoding="utf-8") as f:
                    yield json.load(f)
            elif name.lower().endswith(".zip") and "conversation" in name.lower():
                found = True
                yield from _from_zip(zipfile.ZipFile(p))
    if not found:
        # last try: any zip in the folder that holds conversations
        for name in sorted(os.listdir(folder)):
            if name.lower().endswith(".zip"):
                yield from _from_zip(zipfile.ZipFile(os.path.join(folder, name)))


def load_conversations(path):
    """Return a list of conversation dicts from a zip, a folder, or a conversations.json."""
    if os.path.isdir(path):
        chunks = _from_folder(path)
    elif path.lower().endswith(".zip"):
        chunks = _from_zip(zipfile.ZipFile(path))
    else:
        with open(path, encoding="utf-8") as f:
            chunks = [json.load(f)]
    convs = []
    for chunk in chunks:
        if isinstance(chunk, list):
            convs.extend(c for c in chunk if isinstance(c, dict))
        elif isinstance(chunk, dict):
            convs.append(chunk)
    return convs


# ---------------------------------------------------------------- small helpers

def ts(value):
    """Export times are seconds since 1970 (occasionally milliseconds). Return seconds or None."""
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    return value / 1000 if value > 1e11 else value


def utc(t):
    return dt.datetime.fromtimestamp(t, dt.timezone.utc)


def week_of(t):
    y, w, _ = utc(t).isocalendar()
    return "%d-W%02d" % (y, w)


def week_start(label):
    y, w = label.split("-W")
    return dt.date.fromisocalendar(int(y), int(w), 1)


def nice(t):
    return utc(t).strftime("%d %b %Y, %H:%M UTC")


def message_text(m):
    c = m.get("content") or {}
    parts = c.get("parts") or []
    return "\n".join(p for p in parts if isinstance(p, str)).strip()


def role(m):
    return (m.get("author") or {}).get("role")


def meta(m):
    return m.get("metadata") or {}


# ---------------------------------------------------------------- the counting

def analyse(convs, want_words=False):
    weeks = collections.defaultdict(collections.Counter)
    flagged_days = collections.Counter()
    flagged_models = collections.Counter()
    flagged_convs = set()
    fouro_convs = 0
    first_flag = last_flag = last_fouro = None
    last_fouro_conv = None
    flag_times = []                     # every flagged reply in a 4o conversation, for the final-days count
    flags_elsewhere = 0                 # flagged replies in conversations that didn't start on 4o
    rewrite = collections.Counter()
    others = collections.Counter()      # (kind, model) for replies in 4o conversations not from 4o
    removed, added = collections.Counter(), collections.Counter()

    for c in convs:
        mapping = c.get("mapping") or {}
        is_fouro = c.get("default_model_slug") == FOURO
        if is_fouro:
            fouro_convs += 1
        for node in mapping.values():
            m = (node or {}).get("message") if isinstance(node, dict) else None
            if not isinstance(m, dict):
                continue
            t = ts(m.get("create_time"))
            if t is None:
                continue
            r, md = role(m), meta(m)
            if r == "assistant":
                slug = md.get("model_slug")
                if not slug:
                    continue
                if slug == FOURO and (last_fouro is None or t > last_fouro):
                    last_fouro, last_fouro_conv = t, c
                if md.get(ROUTER_FLAG) and not is_fouro:
                    flags_elsewhere += 1
                elif md.get(ROUTER_FLAG):
                    # Counted only in conversations that started on 4o, so the summary's
                    # "X of Y conversations" compares like with like.
                    flagged_days[utc(t).date()] += 1
                    flagged_models[slug] += 1
                    flagged_convs.add(c.get("id"))
                    flag_times.append(t)
                    first_flag = t if first_flag is None else min(first_flag, t)
                    last_flag = t if last_flag is None else max(last_flag, t)
                if is_fouro:
                    w = weeks[week_of(t)]
                    w["replies"] += 1
                    w["fouro" if slug == FOURO else "other"] += 1
                    if md.get(ROUTER_FLAG):
                        w["flagged"] += 1
                    if slug != FOURO:
                        others[(kind_of_swap(mapping, node, md, slug), slug)] += 1
            elif r == "user" and is_fouro:
                w = weeks[week_of(t)]
                w["messages"] += 1
                parent = mapping.get(node.get("parent") or "") or {}
                sibs = [k for k in (parent.get("children") or [])
                        if role((mapping.get(k) or {}).get("message") or {}) == "user"]
                if len(sibs) > 1:
                    w["rewritten"] += 1

        # rewrites: did changing the message get 4o back?
        if not is_fouro:
            continue
        for pnode in mapping.values():
            kids = [k for k in ((pnode or {}).get("children") or [])
                    if isinstance((mapping.get(k) or {}).get("message"), dict)
                    and role(mapping[k]["message"]) == "user"
                    and ts(mapping[k]["message"].get("create_time"))]
            if len(kids) < 2:
                continue
            kids.sort(key=lambda k: ts(mapping[k]["message"]["create_time"]))

            def replies_to(k):
                return [mapping[x]["message"] for x in (mapping[k].get("children") or [])
                        if isinstance((mapping.get(x) or {}).get("message"), dict)
                        and role(mapping[x]["message"]) == "assistant"]

            first_replies, last_replies = replies_to(kids[0]), replies_to(kids[-1])
            first_switched = any(meta(rm).get(ROUTER_FLAG) for rm in first_replies)
            # 4o is back only if 4o itself answered the rewrite, with no switch flag.
            # An unflagged reply from another model is a quiet switch, not a recovery.
            fouro_back = any(meta(rm).get("model_slug") == FOURO and not meta(rm).get(ROUTER_FLAG)
                             for rm in last_replies)
            if first_switched and last_replies:
                rewrite["first_switched"] += 1
                if not fouro_back and not any(meta(rm).get(ROUTER_FLAG) for rm in last_replies):
                    rewrite["unflagged_other"] += 1
                if fouro_back:
                    rewrite["got_4o_back"] += 1
                    if want_words:
                        a = re.findall(r"[a-zA-Z']+", message_text(mapping[kids[0]]["message"]).lower())
                        b = re.findall(r"[a-zA-Z']+", message_text(mapping[kids[-1]]["message"]).lower())
                        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b).get_opcodes():
                            if tag in ("delete", "replace"):
                                removed.update(x for x in a[i1:i2] if x not in STOP)
                            if tag in ("insert", "replace"):
                                added.update(x for x in b[j1:j2] if x not in STOP)

    last_days = None
    if last_fouro:
        # The fourteen days that END with 4o's last reply: nothing after it counts.
        last_days = sum(1 for t in flag_times if last_fouro - 14 * 86400 <= t <= last_fouro)
    return {
        "flags_elsewhere": flags_elsewhere,
        "weeks": weeks, "flagged_days": flagged_days, "flagged_models": flagged_models,
        "flagged_convs": len(flagged_convs), "fouro_convs": fouro_convs,
        "first_flag": first_flag, "last_flag": last_flag,
        "last_fouro": last_fouro, "last_fouro_conv": last_fouro_conv,
        "flags_last_14_days": last_days, "rewrite": rewrite,
        "removed": removed, "added": added, "others": others,
    }


KIND_ROUTER = "router switch (flagged by OpenAI)"
KIND_SILENT = "likely switched, no flag (meant for 4o, another model answered)"
KIND_CHOSE = "likely your choice (Think / Research, or labelled for the model that answered)"
KIND_MINI = "smaller 'mini' model (often a usage-limit fallback)"
KIND_OTHER = "possible switch, can't tell (another model, no other clue)"
KINDS = (KIND_ROUTER, KIND_SILENT, KIND_CHOSE, KIND_MINI, KIND_OTHER)


def kind_of_swap(mapping, node, md, slug):
    """Sort a not-4o reply into a plain-English kind, from labels only.

    Each reply carries two model labels: the model that answered (model_slug), and the
    model that turn was meant for (default_model_slug on the reply itself). When the turn
    was meant for 4o but another model answered, that is a switch, flagged or not.
    """
    if md.get(ROUTER_FLAG):
        return KIND_ROUTER
    parent = (mapping.get(node.get("parent") or "") or {}).get("message") or {}
    if role(parent) == "user" and meta(parent).get("system_hints"):
        return KIND_CHOSE
    if "mini" in slug:
        return KIND_MINI
    meant = md.get("default_model_slug")
    if meant == FOURO:
        return KIND_SILENT
    if meant and meant == slug:
        return KIND_CHOSE
    return KIND_OTHER


STOP = set("""the a an and or but to of in on at for with is are was were be been it its i my me you
your he his him she her we our they them this that these those as so if then than not no do did does
have has had can could would should will just very really there here what which who how when where why
from by about into out up down over under again also""".split())


# ---------------------------------------------------------------- writing it up

def week_rows(weeks):
    rows = []
    for label in sorted(weeks):
        w = weeks[label]
        rep, msg = w["replies"], w["messages"]
        rows.append({
            "week": label,
            "week_starting": week_start(label).isoformat(),
            "replies": rep,
            "replies_from_4o": w["fouro"],
            "replies_from_other_models": w["other"],
            "pct_not_4o": round(100 * w["other"] / rep, 1) if rep else 0.0,
            "router_flagged": w["flagged"],
            "your_messages": msg,
            "messages_with_more_than_one_version": w["rewritten"],
            "pct_rewritten": round(100 * w["rewritten"] / msg, 1) if msg else 0.0,
        })
    return rows


def summary_text(r, rows, want_words):
    out = []
    say = out.append
    say("CHATGPT EXPORT ROUTER CHECK")
    say("=" * 60)
    say("Conversations whose default model was GPT-4o: %d" % r["fouro_convs"])
    if not rows:
        say("\nNo GPT-4o conversations were found in this export, so there is nothing to chart.")
        return "\n".join(out)
    total_flags = sum(r["flagged_days"].values())
    say("\nTHE ROUTER")
    if total_flags:
        say("  Replies marked with OpenAI's safety-switch flag: %d" % total_flags)
        say("  First: %s" % nice(r["first_flag"]))
        say("  Last:  %s" % nice(r["last_flag"]))
        say("  GPT-4o conversations it touched: %d of %d" % (r["flagged_convs"], r["fouro_convs"]))
        say("  Models that answered instead: " +
            ", ".join("%s (%d)" % kv for kv in r["flagged_models"].most_common()))
        say("  Busiest days: " +
            ", ".join("%s (%d)" % (d.strftime("%d %b %Y"), n) for d, n in r["flagged_days"].most_common(5)))
    else:
        say("  No replies in your GPT-4o conversations carry the safety-switch flag.")
    if r["flags_elsewhere"]:
        say("  (Also %d flagged replies in conversations that didn't start on 4o; not counted above.)"
            % r["flags_elsewhere"])

    say("\nTHE STEP")
    say("  Week by week figures are in timeline.csv, and drawn in timeline.html.")
    big = max(rows, key=lambda x: x["pct_not_4o"])
    say("  Week with the most replies NOT from 4o: %s (week starting %s), %.1f%%"
        % (big["week"], big["week_starting"], big["pct_not_4o"]))
    rw = max(rows, key=lambda x: x["pct_rewritten"])
    say("  Week you rewrote the most: %s (week starting %s), %.1f%% of your messages"
        % (rw["week"], rw["week_starting"], rw["pct_rewritten"]))

    say("\nWHO ELSE ANSWERED IN YOUR 4o CONVERSATIONS")
    kinds = collections.defaultdict(collections.Counter)
    for (kind, slug), n in r["others"].items():
        kinds[kind][slug] += n
    for kind in KINDS:
        if kinds[kind]:
            say("  %s: %d" % (kind, sum(kinds[kind].values())))
            say("      " + ", ".join("%s (%d)" % kv for kv in kinds[kind].most_common()))
    say("  The banner flag shows the least that happened, not all of it. 'Likely switched'")
    say("  replies were labelled as meant for 4o, but another model answered and no banner")
    say("  was recorded. That is a strong clue, not proof. 'Likely your choice' is a clue too:")
    say("  the Think or Research buttons, or a reply labelled for the model that answered.")

    rv = r["rewrite"]
    say("\nREWRITING")
    if rv["first_switched"]:
        say("  Times your first version was switched to another model: %d" % rv["first_switched"])
        say("  ...and your rewrite got 4o back: %d (%.0f%%)"
            % (rv["got_4o_back"], 100 * rv["got_4o_back"] / rv["first_switched"]))
        if rv["unflagged_other"]:
            say("  ...and your rewrite was answered by another model with no flag: %d" % rv["unflagged_other"])
            say("     (no banner, but not 4o either: most likely a quiet switch)")
    else:
        say("  No rewrites of a switched message were found.")

    say("\nTHE LAST CONVERSATION WITH 4o")
    if r["last_fouro"]:
        c = r["last_fouro_conv"] or {}
        say("  Last reply from GPT-4o: %s" % nice(r["last_fouro"]))
        say("  In the conversation titled: \"%s\"" % (c.get("title") or "Untitled"))
        if r["last_flag"]:
            gap = (r["last_fouro"] - r["last_flag"]) / 60
            if gap >= 0:
                say("  The last router switch came %s before it." % _span(gap))
        if r["flags_last_14_days"] is not None:
            say("  Router switches in your final fourteen days with 4o: %d" % r["flags_last_14_days"])
        say("  (Your chatgpt-export-reader output, or ChatGPT itself, will open that conversation if you want to read it.)")
    else:
        say("  No replies from GPT-4o were found.")

    if want_words and (r["removed"] or r["added"]):
        say("\nWORDS YOU CUT TO GET 4o BACK (from your own rewrites; shown only to you)")
        say("  Most often removed: " + ", ".join(w for w, _ in r["removed"].most_common(30)))
        say("  Most often added:   " + ", ".join(w for w, _ in r["added"].most_common(30)))

    say("\nWHAT THIS CAN AND CAN'T TELL YOU")
    say("  It shows what your export records: which model answered, when, and which replies")
    say("  OpenAI marked as safety switches. It cannot tell you why a switch happened, and")
    say("  exports can be incomplete. 'More than one version' counts edits and regenerations")
    say("  of your messages, so treat the rewrite line as a ceiling, not an exact count.")
    say("  All times are UTC.")
    return "\n".join(out)


def _span(minutes):
    if minutes < 90:
        return "%d minute%s" % (round(minutes), "" if round(minutes) == 1 else "s")
    hours = minutes / 60
    if hours < 48:
        return "%.0f hours" % hours
    return "%.0f days" % (hours / 24)


def chart_html(rows, others=None, words=None):
    """A small self-contained page with a two-line chart. No internet needed to open it."""
    W, H, L, B, T, R = 960, 360, 50, 60, 20, 20
    n = len(rows)
    ymax = max([10.0] + [x["pct_not_4o"] for x in rows] + [x["pct_rewritten"] for x in rows])
    ymax = (int(ymax / 10) + 1) * 10

    # Points sit on the calendar, not on row numbers, so quiet stretches keep their real
    # length; weeks with no activity show as a break in the line, not a smooth join.
    days = [dt.date.fromisoformat(x["week_starting"]) for x in rows]
    span = max(1, (days[-1] - days[0]).days) if days else 1

    def X(i):
        return L + (W - L - R) * ((days[i] - days[0]).days / span)

    def Y(v):
        return T + (H - T - B) * (1 - v / ymax)

    runs, run = [], []
    for i in range(n):
        if run and (days[i] - days[run[-1]]).days > 7:
            runs.append(run)
            run = []
        run.append(i)
    if run:
        runs.append(run)

    def line(key, colour):
        out = []
        for run in runs:
            if len(run) == 1:
                i = run[0]
                out.append('<circle cx="%.1f" cy="%.1f" r="2.5" fill="%s"/>' % (X(i), Y(rows[i][key]), colour))
            else:
                pts = " ".join("%.1f,%.1f" % (X(i), Y(rows[i][key])) for i in run)
                out.append('<polyline fill="none" stroke="%s" stroke-width="2.5" points="%s"/>' % (colour, pts))
        return "".join(out)

    grid = "".join('<line x1="%d" x2="%d" y1="%.1f" y2="%.1f" stroke="#ccc" stroke-width="0.5"/>'
                   '<text x="%d" y="%.1f" font-size="11" text-anchor="end" fill="#555">%d%%</text>'
                   % (L, W - R, Y(v), Y(v), L - 6, Y(v) + 4, v) for v in range(0, ymax + 1, 10))
    step = max(1, n // 12)
    ticks = "".join('<text x="%.1f" y="%d" font-size="11" text-anchor="middle" fill="#555">%s</text>'
                    % (X(i), H - B + 18, html.escape(x["week_starting"][:7]))
                    for i, x in enumerate(rows) if i % step == 0)
    flags = "".join('<circle cx="%.1f" cy="%d" r="%.1f" fill="#b5651d" opacity="0.6"><title>%s: %d flagged</title></circle>'
                    % (X(i), H - B + 34, min(8, 1.5 + x["router_flagged"] ** 0.5), x["week"], x["router_flagged"])
                    for i, x in enumerate(rows) if x["router_flagged"])
    table = "".join("<tr><td>%s</td><td>%s</td><td>%d</td><td>%.1f%%</td><td>%d</td><td>%.1f%%</td></tr>"
                    % (x["week"], x["week_starting"], x["replies"], x["pct_not_4o"], x["router_flagged"], x["pct_rewritten"])
                    for x in rows)
    kinds = collections.defaultdict(collections.Counter)
    for (kind, slug), count in (others or {}).items():
        kinds[kind][slug] += count
    who = "".join("<tr><td>%s</td><td>%d</td><td>%s</td></tr>"
                  % (html.escape(k), sum(kinds[k].values()),
                     html.escape(", ".join("%s (%d)" % kv for kv in kinds[k].most_common())))
                  for k in KINDS if kinds[k])
    cut = ""
    if words and (words[0] or words[1]):
        cut = ("<h2>The words you cut to get 4o back</h2>"
               "<p>From your own rewrites, read on your computer only. Shown because you asked for them.</p>"
               "<p><b>Most often removed:</b> %s</p><p><b>Most often added:</b> %s</p>"
               % (html.escape(", ".join(w for w, _ in words[0].most_common(30))),
                  html.escape(", ".join(w for w, _ in words[1].most_common(30)))))
    return """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Your 4o timeline</title>
<style>body{font-family:system-ui,sans-serif;max-width:1000px;margin:24px auto;padding:0 16px;color:#222;background:#fff}
.k span{display:inline-block;width:14px;height:4px;margin:0 6px 3px 14px;vertical-align:middle}
table{border-collapse:collapse;font-size:13px;margin-top:20px}td,th{border-bottom:1px solid #ddd;padding:4px 10px;text-align:right}
th:first-child,td:first-child{text-align:left}.who td:last-child{text-align:left}svg{max-width:100%%;height:auto}</style></head><body>
<h1>Your GPT-4o conversations, week by week</h1>
<p class="k"><span style="background:#3b6ea5"></span>%% of replies <b>not</b> from 4o
<span style="background:#c0392b"></span>%% of your messages with more than one version
<span style="background:#b5651d;height:10px;width:10px;border-radius:50%%"></span>router-flagged replies (bigger = more)</p>
<svg viewBox="0 0 %d %d" role="img" aria-label="Weekly chart">%s%s%s%s%s</svg>
<p>Weeks sit on a real calendar: a break in a line is a stretch with no 4o conversations.</p>
<p>Made by chatgpt-export-router-check from your own export, on your own computer. Times are UTC.
It shows what your export records. It can't tell you why a switch happened.</p>
<h2>Who else answered in your 4o conversations</h2>
<table class="who"><tr><th>Kind</th><th>Replies</th><th>Models</th></tr>%s</table>
<p>The banner flag shows the least that happened, not all of it. 'Likely switched' replies were
labelled as meant for 4o, but another model answered and no banner was recorded: a strong clue, not proof.
'Likely your choice' is a clue too, not proof: the Think or Research buttons, or a reply labelled
for the model that answered. 'Possible switch' replies carry no clue either way.</p>
%s
<h2>Week by week</h2>
<table><tr><th>Week</th><th>Starting</th><th>Replies</th><th>Not 4o</th><th>Flagged</th><th>Rewritten</th></tr>%s</table>
</body></html>""" % (W, H + 10, grid, ticks, line("pct_not_4o", "#3b6ea5"), line("pct_rewritten", "#c0392b"), flags, who, cut, table)


# ---------------------------------------------------------------- main

def find_default_export():
    here = os.getcwd()
    for name in sorted(os.listdir(here)):
        low = name.lower()
        if CONV_NAME.match(name) or (low.endswith(".zip") and ("export" in low or "chatgpt" in low or "conversation" in low)):
            return os.path.join(here, name)
    return here


def main(argv=None):
    ap = argparse.ArgumentParser(description="See what happened to your GPT-4o conversations.")
    ap.add_argument("export", nargs="?", help="your export: a .zip, a folder, or conversations.json")
    ap.add_argument("--out", default="router-check-results", help="folder for the results")
    ap.add_argument("--show-words", action="store_true",
                    help="also list words you cut when rewriting a switched message (reads your messages, locally)")
    a = ap.parse_args(argv)

    path = a.export or find_default_export()
    if not os.path.exists(path):
        sys.exit("I couldn't find '%s'. Give me the path to your ChatGPT export." % path)
    print("Reading your export from: %s\n(this can take a minute for a big export)\n" % path)
    try:
        convs = load_conversations(path)
    except (zipfile.BadZipFile, json.JSONDecodeError) as e:
        sys.exit("I couldn't read that export (%s). Is it the zip ChatGPT emailed you?" % e)
    if not convs:
        sys.exit("I didn't find any conversations there. Point me at the export zip, or the folder you unzipped it into.")

    result = analyse(convs, want_words=a.show_words)
    rows = week_rows(result["weeks"])
    text = summary_text(result, rows, a.show_words)

    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "summary.txt"), "w", encoding="utf-8") as f:
        f.write(text + "\n")
    if rows:
        with open(os.path.join(a.out, "timeline.csv"), "w", newline="", encoding="utf-8") as f:
            wr = csv.DictWriter(f, fieldnames=list(rows[0]))
            wr.writeheader()
            wr.writerows(rows)
        with open(os.path.join(a.out, "timeline.html"), "w", encoding="utf-8") as f:
            f.write(chart_html(rows, result["others"],
                               (result["removed"], result["added"]) if a.show_words else None))
        with open(os.path.join(a.out, "others.csv"), "w", newline="", encoding="utf-8") as f:
            wr = csv.writer(f)
            wr.writerow(["kind", "model", "replies"])
            for (kind, slug), n in sorted(result["others"].items(), key=lambda kv: -kv[1]):
                wr.writerow([kind, slug, n])
    print(text)
    print("\nSaved in the folder '%s': summary.txt%s" % (a.out, ", timeline.csv, timeline.html, others.csv" if rows else ""))
    print("Open timeline.html in your web browser to see the chart.")


if __name__ == "__main__":
    main()

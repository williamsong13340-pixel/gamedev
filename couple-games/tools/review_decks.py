#!/usr/bin/env python3
"""让 Grok 审查并修改题库（导入文件格式），输出新的题库文件和一份改动对照表。

用法：
  python3 review_decks.py 题库.json 输出目录 --dry-run          # 不花钱，只估算用量
  python3 review_decks.py 题库.json 输出目录 --only tod/炸裂版   # 只跑一套试试
  python3 review_decks.py 题库.json 输出目录                     # 全部

说明：
- 这个脚本不会把任何卡牌内容打印到屏幕上，只打印数量。内容只出现在输出文件里。
- key 从环境变量 XAI_API_KEY 读取；在云环境里由网络代理自动注入，不需要设置。
- “保留”的条目一律使用原文，不采用模型回传的文字，保证没问题的内容一个字不变。
- 默认不重试，并且有花费上限（--max-usd）。grok-4.7 思考很慢，一套内容太多会超过 5 分钟被网关掐断，
  而被掐断的请求照样计费。内容多的牌组请先拆小再跑。
"""
import argparse
import concurrent.futures
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.x.ai/v1/chat/completions"
FORMAT = "couple-games-import-v1"
GAME_NAME = {"cards": "心动卡牌", "tod": "真心话大冒险", "chess": "双人飞行棋"}
REASONS = {"unsafe": "不安全", "one_sided": "偏向一方", "empty": "空的", "unclear": "看不懂",
           "duplicate": "意思重复", "wrong_type": "分错类", "added": "补充的", "": ""}
MIN_PER_TYPE = 12
MAX_LEN = 60

SYSTEM = """你是一款成年情侣互动游戏的内容编辑。使用者是一对自愿、私下一起玩的成年伴侣，内容尺度由他们自己决定。
下面给你一套卡牌，每条有编号 i 和类型 type（d=行动，s=真心话）。请逐条审查并给出处理结果。

最重要的原则：这套内容本身是好的，绝大多数应该原样保留（keep）。尺度大、直白、羞耻感强、
涉及拍照或录像，都不是修改的理由。用词粗糙、有错别字、用拼音或谐音代替某些字，也不是修改的理由。
只有下面几种情况才动：

1. 会受伤（unsafe）：只指身体受伤的风险，包括憋气、窒息、勒颈；烫伤、冻伤；尖锐、易碎、不卫生
   或不适合放进身体的物品；容易摔倒、拉伤、扭伤的姿势或动作；过量饮酒。除此之外都不算不安全。
   处理方式是改写（rewrite）：只把会受伤的那一部分换成不会受伤的做法，其余文字尽量逐字保留；
   实在改不了才删除（remove）。
2. 偏向一方（one_sided）：整条内容只让固定的一方付出或只让固定的一方享受，而且写死了是男方或女方。
   改写成“谁抽到谁做”、两个人都适用的写法：用“你”指抽到牌的人，用“{ta}”指另一个人（请原样写出
   {ta} 这四个字符，游戏会把它换成昵称）。如果原内容只适合某一种身体，就改成两个人都能做的等价玩法。
   只是提到了男方、女方，但两个人都参与的，不算偏向一方，保留。
3. 空的、只有占位符、完全读不通（empty）：删除。
4. 意思和前面某一条基本相同（duplicate）：删除后出现的那条。
5. 分错类（wrong_type）：类型是真心话（s）但内容其实是要做的一件事，或者类型是行动（d）但内容其实是
   一个要回答的问题。处理方式是换类型（retype），文字不改。

改写的要求：
- 改动越小越好，能只换几个字就只换几个字。
- 尺度必须和原文完全一样：原文怎么直白，改后就怎么直白。绝对不能改得更含蓄、更温和、更保守。
- 保留原有的时间、次数这类具体要求，长度不超过 40 个字。

补充：处理完之后，如果某个类型剩下不到 __MIN__ 条（只针对这套里原本就有的类型），请在 add 里补充新内容，
补到 __MIN__ 条为止。新内容的尺度、语气要和这一套里保留下来的内容一致，并且不会受伤。不需要补就让 add 为空数组。

只输出一个 JSON 对象，不要输出别的文字：
{"items":[{"i":编号,"action":"keep 或 rewrite 或 remove 或 retype","reason":"unsafe / one_sided / empty / duplicate / wrong_type，保留时留空","text":"改写后的文字，只有 rewrite 时填写"}],
 "add":[{"type":"d 或 s","text":"……"}]}
每个编号都必须出现且只出现一次。"""


def norm(x):
    return re.sub(r"[\s，。！？、,.!?…~～：:；;（）()《》\"'“”]+", "", x)


def call(model, payload_items, timeout=900):
    body = {"model": model, "messages": [
        {"role": "system", "content": SYSTEM.replace("__MIN__", str(MIN_PER_TYPE))},
        {"role": "user", "content": json.dumps(payload_items, ensure_ascii=False)}],
        "response_format": {"type": "json_object"}}
    req = urllib.request.Request(API, data=json.dumps(body).encode(), method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + os.environ.get("XAI_API_KEY", "proxy-injected")})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    text = data["choices"][0]["message"].get("content") or ""
    usage = data.get("usage", {})
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("model did not return JSON (%d chars; finish=%s)" % (len(text), data["choices"][0].get("finish_reason")))
    return json.loads(m.group(0)), usage


SPENT = {"usd": 0.0}


def review_deck(deck, model, retries=0, max_usd=0.5):
    """返回 (新牌组, 对照行, 统计)。出错时原样保留整套。"""
    items = [{"i": i, "type": t, "text": x} for i, (t, x) in enumerate([("d", x) for x in deck["d"]] + [("s", x) for x in deck["s"]])]
    stats = {"total": len(items), "keep": 0, "rewrite": 0, "remove": 0, "retype": 0, "add": 0, "missing": 0, "too_long": 0, "failed": False,
             "reasons": {}, "tokens_in": 0, "tokens_out": 0, "seconds": 0, "attempts": 0}
    t0 = time.time()
    rows = []
    # 本地先处理明显是空的
    send = []
    for it in items:
        if len(norm(it["text"])) < 2:
            rows.append((it["type"], it["text"], "", "remove", "empty"))
            stats["remove"] += 1
            stats["reasons"]["empty"] = stats["reasons"].get("empty", 0) + 1
        else:
            send.append(it)
    result, err = None, None
    for attempt in range(retries + 1):
        if SPENT["usd"] >= max_usd:              # 花费到上限就不再发新请求
            err = "到达花费上限，没有发送"
            break
        stats["attempts"] += 1
        try:
            result, usage = call(model, send)
            SPENT["usd"] += usage.get("cost_in_usd_ticks", 0) / 1e10
            stats["tokens_in"] += usage.get("prompt_tokens", 0)
            stats["tokens_out"] += usage.get("completion_tokens", 0) + usage.get("completion_tokens_details", {}).get("reasoning_tokens", 0)
            if not isinstance(result.get("items"), list):
                raise ValueError("no items list")
            break
        except urllib.error.HTTPError as e:
            err = "HTTP %s" % e.code
            if e.code in (400, 401, 403, 404):
                break
        except Exception as e:  # noqa: BLE001
            err = type(e).__name__ + ": " + str(e)[:120]
        result = None
        time.sleep(3 * (attempt + 1))
    stats["seconds"] = round(time.time() - t0)
    if result is None:
        stats["failed"] = err or "unknown"
        return deck, [(it["type"], it["text"], it["text"], "keep", "") for it in items], stats

    verdict = {}
    for v in result["items"]:
        if isinstance(v, dict) and isinstance(v.get("i"), int) and v["i"] not in verdict:
            verdict[v["i"]] = v
    out, seen = {"d": [], "s": []}, set()
    for it in send:
        v = verdict.get(it["i"])
        action = (v or {}).get("action", "keep")
        reason = (v or {}).get("reason", "") or ""
        if reason not in REASONS:
            reason = "unclear" if action != "keep" else ""
        if v is None:
            stats["missing"] += 1
        new = it["text"]
        if action == "rewrite":
            new = str(v.get("text") or "").strip()
            if len(norm(new)) < 2:
                action = "remove"
            elif norm(new) == norm(it["text"]):
                action, new, reason = "keep", it["text"], ""
        elif action == "retype" and deck["d"] and deck["s"]:
            reason = "wrong_type"
        elif action != "remove":
            action, reason = "keep", ""
        tt = ("s" if it["type"] == "d" else "d") if action == "retype" else it["type"]
        if action != "remove" and norm(new) in seen:
            action, reason = "remove", "duplicate"
        if action == "remove":
            rows.append((it["type"], it["text"], "", "remove", reason or "unclear"))
        else:
            seen.add(norm(new))
            out[tt].append(new)
            rows.append((tt, "" if action == "retype" else it["text"], new, action, reason))
            if len(new) > MAX_LEN:
                stats["too_long"] += 1
        stats[action] += 1
        if reason:
            stats["reasons"][reason] = stats["reasons"].get(reason, 0) + 1
    for a in result.get("add") or []:
        if not isinstance(a, dict):
            continue
        t, x = a.get("type"), str(a.get("text") or "").strip()
        if t not in ("d", "s") or not deck[t] or len(norm(x)) < 2 or norm(x) in seen or len(out[t]) >= MIN_PER_TYPE:
            continue
        seen.add(norm(x))
        out[t].append(x)
        rows.append((t, "", x, "add", "added"))
        stats["add"] += 1
    return {"name": deck["name"], "game": deck["game"], "d": out["d"], "s": out["s"]}, rows, stats


def report_html(sections):
    css = ("body{font:15px/1.6 -apple-system,'PingFang SC','Microsoft YaHei',sans-serif;margin:0;padding:16px;background:#fdf1f4;color:#3b2140}"
           "h1{font-size:22px}h2{font-size:18px;margin:28px 0 8px}p{color:#7b5f7e}.r{background:#fff;border-radius:14px;padding:12px 14px;margin:8px 0}"
           ".tag{display:inline-block;font-size:12px;font-weight:700;border-radius:99px;padding:2px 10px;margin-right:6px;background:#fcdde6;color:#e4507a}"
           ".tag.add{background:#d5f2ee;color:#1e9e91}.tag.remove{background:#eee;color:#666}.old{color:#7b5f7e;text-decoration:line-through}"
           ".new{font-weight:600}.k{color:#b49cb5;font-size:12px}")
    out = ["<!doctype html><html lang='zh-CN'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>",
           "<title>题库改动对照表</title><style>", css, "</style><h1>题库改动对照表</h1>",
           "<p>只列出被改写、删除、补充的条目；没列出来的都原样保留。划线的是原文，粗体是改后的。</p>"]
    label = {"rewrite": "改写", "remove": "删除", "add": "补充", "retype": "换到这一类"}
    for title, rows, stats in sections:
        changed = [r for r in rows if r[3] != "keep"]
        out.append("<h2>%s</h2><p>共 %d 条：保留 %d，改写 %d，删除 %d，换类型 %d，补充 %d%s</p>" % (
            html.escape(title), stats["total"], stats["keep"], stats["rewrite"], stats["remove"], stats["retype"], stats["add"],
            "　<b>这一套处理失败，已原样保留</b>" if stats["failed"] else ""))
        for t, old, new, action, reason in changed:
            out.append("<div class='r'><span class='tag %s'>%s</span><span class='k'>%s%s</span>" % (
                action, label[action], "真心话" if t == "s" else "行动", (" · " + REASONS.get(reason, reason)) if reason and action != "add" else ""))
            if old:
                out.append("<div class='old'>%s</div>" % html.escape(old))
            if new:
                out.append("<div class='new'>%s</div>" % html.escape(new))
            out.append("</div>")
    return "".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("outdir")
    ap.add_argument("--model", default="grok-4.7")
    ap.add_argument("--only", help="只处理一套，写成 游戏/名字，比如 tod/炸裂版")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--workers", type=int, default=2, help="同时处理几套")
    ap.add_argument("--retries", type=int, default=0, help="失败后重试几次。默认不重试：超时的请求对方照样计费，重试等于重复花钱")
    ap.add_argument("--max-usd", type=float, default=0.5, help="花费上限（美元）。只统计拿到结果的请求，超时的那一次不在内")
    a = ap.parse_args()
    data = json.loads(Path(a.src).read_text(encoding="utf-8"))
    assert data.get("format") == FORMAT, "不是题库文件"
    decks = data["decks"]
    todo = [d for d in decks if not a.only or a.only == "%s/%s" % (d["game"], d["name"])]
    if not todo:
        sys.exit("没有找到这一套：" + str(a.only))
    chars = sum(len(x) for d in todo for x in d["d"] + d["s"])
    n = sum(len(d["d"]) + len(d["s"]) for d in todo)
    est_in = int(len(todo) * len(SYSTEM) * 1.1 + chars * 1.2 + n * 14)
    est_out = int(n * 22 + chars * 0.5)
    print("要处理 %d 套、%d 条。预计请求 %d 次，输入约 %d tokens，输出约 %d tokens（用了思考模式会更多）。" % (len(todo), n, len(todo), est_in, est_out))
    if a.dry_run:
        return
    outdir = Path(a.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    new_decks, sections, summary = [], [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=a.workers) as ex:
        results = list(ex.map(lambda d: review_deck(d, a.model, a.retries, a.max_usd), todo))
    for d, (nd, rows, st) in zip(todo, results):
        new_decks.append(nd)
        title = "%s · %s" % (GAME_NAME[d["game"]], d["name"])
        sections.append((title, rows, st))
        summary.append({"deck": "%s/%s" % (d["game"], d["name"]), **st, "after": {"d": len(nd["d"]), "s": len(nd["s"])}})
        print("%-22s 共%3d 保留%3d 改写%3d 删除%3d 换类%3d 补充%3d  现有 行动%3d 真心话%3d  %s" % (
            d["game"] + "/" + d["name"], st["total"], st["keep"], st["rewrite"], st["remove"], st["retype"], st["add"], len(nd["d"]), len(nd["s"]),
            ("失败:" + str(st["failed"])) if st["failed"] else ("未回应%d条→保留" % st["missing"] if st["missing"] else "")) + " %ds/%d次" % (st["seconds"], st["attempts"]))
        sys.stdout.flush()
    suffix = "-trial" if a.only else ""
    (outdir / ("reviewed-decks%s.json" % suffix)).write_text(json.dumps({"format": FORMAT, "decks": new_decks}, ensure_ascii=False, indent=1), encoding="utf-8")
    (outdir / ("review-report%s.html" % suffix)).write_text(report_html(sections), encoding="utf-8")
    (outdir / ("review-summary%s.json" % suffix)).write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    tin, tout = sum(s["tokens_in"] for s in summary), sum(s["tokens_out"] for s in summary)
    print("完成。实际用量：输入 %d tokens，输出 %d tokens，拿到结果的请求共约 %.2f 美元。失败 %d 套。" % (tin, tout, SPENT["usd"], sum(1 for s in summary if s["failed"])))


if __name__ == "__main__":
    main()

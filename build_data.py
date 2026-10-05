#!/usr/bin/env python3
"""Собирает карточки для сайта из кэша учебников в Obsidian (.english).

Выход: data/komkor.json, data/econ.json, data/hh.json — {units:[{id,title,sub,cards:[...]}]}
Карточка: {id, k(ind), en, ru, note, syn, ctx, p, g}
  k: w — слово/выражение, ph — фраза (speech/writing patterns), id — идиома (HH)
"""
import json, re, os, sys
from pathlib import Path

SRC = Path.home() / "Documents/Obsidian Vault/.english"
OUT = Path(__file__).parent / "data"
CYR = re.compile(r"[А-Яа-яЁё]")
PLACEHOLDER = {"smth", "smb", "sb", "sth", "smb's", "smbs", "one's", "ones", "smth.", "smb.", "smb’s", "one’s", "somebody", "something", "doing"}


def pages(text):
    """{номер страницы: текст}"""
    out, cur, buf = {}, None, []
    for line in text.split("\n"):
        m = re.match(r"=== стр\. (\d+) ===", line)
        if m:
            if cur is not None:
                out[cur] = "\n".join(buf)
            cur, buf = int(m.group(1)), []
        else:
            buf.append(line)
    if cur is not None:
        out[cur] = "\n".join(buf)
    return out


def sentences(text):
    lines = []
    for ln in text.split("\n"):
        # строки глоссария («term   - перевод») и колонтитулы не нужны
        if CYR.search(ln) or re.match(r"\s*\d+\s+[A-ZА-Я ]{6,}$", ln):
            lines.append("\n")
            continue
        lines.append(ln)
    t = re.sub(r"[ \t]+", " ", " ".join(lines))
    t = t.replace("\n", " . ")
    parts = re.split(r"(?<=[.!?…”])\s+(?=[A-Z“\"‘(])|\s\.\s", t)
    res = []
    for s in parts:
        s = re.sub(r"\s+", " ", s).strip(" .")
        # заголовки капсом в начале («PARTS OF LETTER REFERENCE References…») отрезаем
        s = re.sub(r"^(?:[A-Z0-9][A-Z0-9’'&.,:-]*\s+){2,}(?=[A-Z][a-z])", "", s)
        words = re.findall(r"[A-Za-z][\w’'-]*", s)
        caps = sum(1 for w in words if w[0].isupper())
        if words and caps / len(words) > 0.35:  # адреса, реквизиты, шапки писем
            continue
        if re.search(r"\d{3,}", s) and caps / max(1, len(words)) > 0.2:
            continue
        if 30 <= len(s) <= 260 and re.search(r"[a-z]{3}", s) and s.count(" ") >= 4:
            res.append(s + ("" if s[-1] in ".!?…”\"" else "."))
    return res


def core_tokens(en):
    s = en.lower().replace("’", "'")
    s = re.sub(r"\([^)]*\)", " ", s)
    s = s.split("=")[0]
    s = re.sub(r"/\S+", " ", s)  # a/b → a
    toks = [w.strip(".,;:!?“”\"«»") for w in s.split()]
    toks = [w for w in toks if w and w not in PLACEHOLDER]
    if toks and toks[0] == "to" and len(toks) > 1:
        toks = toks[1:]
    return toks


def term_regex(en, loose=False):
    toks = core_tokens(en)
    if not toks:
        return None
    parts = []
    for w in toks:
        w = re.escape(w).replace("'", "['’]")
        if loose and len(w) > 4 and w.isalpha():
            parts.append(re.escape(w[: max(4, len(w) - 2)]) + r"\w*")
        elif loose and len(w) <= 4 and w.isalpha():
            parts.append(w + r"\w{0,3}")
        else:
            parts.append(w + (r"\w{0,3}" if w.isalpha() else ""))
    gap = r"(?:\W+\w+){0,3}?\W+" if loose else r"\W+"
    return re.compile(r"\b" + gap.join(parts) + r"\b", re.I)


def find_ctx(en, sents, loose=False):
    rx = term_regex(en, loose)
    if not rx:
        return ""
    best = ""
    for s in sents:
        if rx.search(s):
            if not best or abs(len(s) - 120) < abs(len(best) - 120):
                best = s
    if best:
        m = rx.search(best)
        best = best[: m.start()] + "[[" + m.group(0) + "]]" + best[m.end():]
    return best


def split_ru(ru):
    """«перевод _(пометка)_» → (перевод, пометка)"""
    ru = ru.strip()
    notes = re.findall(r"_([^_]+)_", ru)
    main = re.sub(r"_[^_]*_", "", ru)
    main = re.sub(r"\s+([,.;])", r"\1", re.sub(r"\s+", " ", main)).strip(" .")
    if main.endswith(" ."):
        main = main[:-2]
    note = " ".join(n.strip(" .()") for n in notes).strip()
    if not main:
        main, note = note, ""
    if note.lower().rstrip(".") in ("зд", "здесь"):
        main, note = "зд. " + main, ""
    return main, note


def komkor(books):
    data = json.loads((SRC / "komkor_vocab.json").read_text())
    pg = pages((SRC / "komkor_2018.txt").read_text())
    units = []
    for n in sorted(data, key=int):
        L, meta = data[n], books["komkor"]["lessons"][n]
        a, b = meta["pages"]
        sents = sentences("\n".join(pg.get(p, "") for p in range(a, b + 1)))
        cards = []
        for i, v in enumerate(L["vocab"]):
            ru, note = split_ru(v["ru"])
            cards.append({"id": f"k{n}w{i}", "k": "w", "en": v["en"].strip(), "ru": ru, "note": note,
                          "syn": v.get("syn") or [], "ctx": find_ctx(v["en"], sents) or find_ctx(v["en"], sents, True), "p": v.get("p"),
                          "g": v.get("grp", "")})
        for i, v in enumerate(L.get("patterns") or []):
            if not v.get("en") or not v.get("ru"):
                continue
            cards.append({"id": f"k{n}p{i}", "k": "ph", "en": v["en"].strip(), "ru": v["ru"].strip(),
                          "note": "", "syn": [], "ctx": "", "p": None, "g": v.get("grp") or L.get("patterns_name", "")})
        units.append({"id": f"k{n}", "n": n, "title": meta["title"], "sub": f"Урок {n} · стр. {a}–{b}", "cards": cards})
    return units


def econ(books):
    data = json.loads((SRC / "econ_vocab.json").read_text())
    pg = pages((SRC / "econ_translation_2024.txt").read_text())
    units = []
    for n in sorted(data["units"], key=int):
        meta = books["econ"]["units"][n]
        a, b = meta["pages"]
        sents = sentences("\n".join(pg.get(p, "") for p in range(a, b + 1)))
        cards = []
        for t in sorted(data["units"][n]):
            for sec, items in data["units"][n][t].items():
                for i, v in enumerate(items):
                    if not v.get("en") or not v.get("ru"):
                        continue
                    ru, note = split_ru(v["ru"])
                    ttl = meta["texts"].get(t, {}).get("title", "")
                    cards.append({"id": f"e{n}{t}{sec[0]}{i}", "k": "w", "en": v["en"].strip(), "ru": ru, "note": note,
                                  "syn": [], "ctx": find_ctx(v["en"], sents) or find_ctx(v["en"], sents, True), "p": v.get("p"),
                                  "g": f"Text {t}" + (f" · {ttl.title()}" if ttl else "") + (" · Notes" if sec.upper().startswith("NOTE") else "")})
        units.append({"id": f"e{n}", "n": n, "title": meta["title"].title(), "sub": f"Unit {n} · стр. {a}–{b}", "cards": cards})
    return units


def hh(books):
    data = json.loads((SRC / "headhunter/hh_vocab.json").read_text())
    files = {f.name[:2]: f for f in (SRC / "headhunter").glob("*.txt") if f.name[:2].isdigit()}
    units = []
    for n in sorted(data, key=int):
        ch = data[n]
        f = files.get(f"{int(n):02d}")
        sents = sentences(f.read_text()) if f else []
        cards = []
        for sec in ("idioms", "vocab", "business", "econ"):
            for i, v in enumerate(ch.get(sec) or []):
                ex = (v.get("expr") or "").strip()
                if not ex:
                    continue
                meaning = (v.get("meaning") or "").strip()
                ru = (v.get("ru") or "").strip()
                if sec in ("econ", "business") and CYR.search(meaning):
                    ru, meaning = meaning, ""
                cards.append({"id": f"h{n}{sec[0]}{i}", "k": "id" if sec == "idioms" else "w", "en": ex,
                              "ru": ru, "def": meaning, "note": "", "syn": [],
                              "ctx": find_ctx(ex, sents) or find_ctx(ex, sents, loose=True), "p": None,
                              "g": {"idioms": "Idioms", "vocab": "Vocabulary", "business": "Business", "econ": "Economics"}[sec]})
        title = "Prologue" if n == "0" else "Epilogue" if n == "99" else f"Chapter {n}"
        units.append({"id": f"h{n}", "n": n, "title": title,
                      "sub": "из рабочей тетради" if ch.get("source") == "workbook" else "подборка Claude", "cards": cards})
    return units


def main():
    books = json.loads((SRC / "books.json").read_text())
    OUT.mkdir(exist_ok=True)
    meta = {
        "komkor": {"title": "Комкор", "full": books["komkor"]["title"], "dir": "ru2en", "units": komkor(books)},
        "econ": {"title": "Эконом. перевод", "full": books["econ"]["title"], "dir": "en2ru", "units": econ(books)},
        "hh": {"title": "Headhunter", "full": books["hh"]["title"], "dir": "def2en", "units": hh(books)},
    }
    for key, d in meta.items():
        (OUT / f"{key}.json").write_text(json.dumps(d, ensure_ascii=False, separators=(",", ":")))
        cards = [c for u in d["units"] for c in u["cards"]]
        withctx = sum(1 for c in cards if c["ctx"])
        print(f"{key}: {len(d['units'])} units, {len(cards)} cards, ctx {withctx} ({withctx*100//max(1,len(cards))}%), "
              f"{os.path.getsize(OUT / f'{key}.json')//1024} KB")


if __name__ == "__main__":
    main()

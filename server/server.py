#!/usr/bin/env python3
"""Лексика API: вход по паролю, словари, синхронизация прогресса, ИИ-подсказки.

Только stdlib. Слушает 127.0.0.1:8098, снаружи — nginx location /api/.
Окружение: LEXIKA_PASS_SHA256, LEXIKA_SECRET, ANTHROPIC_API_KEY,
           LEXIKA_DATA (словари, по умолчанию /opt/lexika/data), STATE_DIRECTORY (systemd).
"""
import hashlib, hmac, json, os, threading, time, urllib.request, urllib.error
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DATA = os.environ.get("LEXIKA_DATA", "/opt/lexika/data")
STATE = os.environ.get("STATE_DIRECTORY", "/var/lib/lexika")
PASS_SHA = os.environ.get("LEXIKA_PASS_SHA256", "")
SECRET = os.environ.get("LEXIKA_SECRET", "").encode()
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
STATIC = os.environ.get("LEXIKA_STATIC", "")
MODEL = "claude-haiku-4-5"
ASPECTS = ("komkor", "econ", "hh")
COOKIE = "lx"
YEAR = 365 * 24 * 3600

lock = threading.Lock()
_cards = {}  # aspect -> {id: card}
_hits = {}   # ip -> [timestamps]


def token():
    return hmac.new(SECRET, b"lexika-v1", hashlib.sha256).hexdigest()


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def cards(aspect):
    if aspect not in _cards:
        d = load_json(os.path.join(DATA, aspect + ".json"), {"units": []})
        _cards[aspect] = {c["id"]: dict(c, unit=u["title"]) for u in d["units"] for c in u["cards"]}
    return _cards[aspect]


def merge(old, new):
    """Карточки и дни объединяются по свежести: у кого t больше, тот и прав."""
    out = {"cards": dict(old.get("cards", {})), "days": dict(old.get("days", {})),
           "settings": old.get("settings", {}), "st": old.get("st", 0)}
    for cid, c in (new.get("cards") or {}).items():
        if not isinstance(c, dict):
            continue
        if cid not in out["cards"] or c.get("t", 0) >= out["cards"][cid].get("t", 0):
            out["cards"][cid] = c
    for day, n in (new.get("days") or {}).items():
        out["days"][day] = max(n, out["days"].get(day, 0)) if isinstance(n, int) else out["days"].get(day, 0)
    if new.get("st", 0) >= out["st"] and isinstance(new.get("settings"), dict):
        out["settings"], out["st"] = new["settings"], new.get("st", 0)
    return out


PROMPTS = {
    "komkor": "Студент МГИМО учит деловую переписку (коммерческая корреспонденция). На экзамене он переводит с русского на английский, поэтому ему надо вспомнить английский термин по русскому.",
    "econ": "Студент МГИМО учит экономический перевод: читает статьи The Economist и переводит/реферирует их на русский, поэтому ему надо понимать английский термин.",
    "hh": "Студент МГИМО читает роман Juliette Mead «The Headhunter» (home reading) и должен активно использовать идиомы и лексику в пересказе на английском.",
}


def ask_claude(aspect, c):
    parts = [f"Термин: {c['en']}"]
    if c.get("ru"):
        parts.append(f"Перевод: {c['ru']}")
    if c.get("def"):
        parts.append(f"Значение: {c['def']}")
    if c.get("note"):
        parts.append(f"Пометка: {c['note']}")
    if c.get("ctx"):
        parts.append("Пример из учебника: " + c["ctx"].replace("[[", "").replace("]]", ""))
    prompt = (PROMPTS[aspect] + " Это слово ему не даётся.\n\n" + "\n".join(parts) + "\n\n"
              "Помоги запомнить. Ответь строго в формате, без вступлений, коротко (до 70 слов всего):\n"
              "🧠 <запоминалка: яркая ассоциация, созвучие с русским, разбор корня или образ — что реально цепляет память>\n"
              "💬 <короткий живой пример по-английски в деловом контексте> — <перевод>\n"
              "⚠️ <с чем легко спутать или типичная ошибка; если нечего — пропусти строку>")
    body = json.dumps({"model": MODEL, "max_tokens": 350,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, headers={
        "x-api-key": API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=40) as r:
        res = json.load(r)
    return "".join(b.get("text", "") for b in res.get("content", []) if b.get("type") == "text").strip()


class H(BaseHTTPRequestHandler):
    server_version = "lexika"

    def log_message(self, fmt, *args):
        pass

    def send(self, code, obj=None, headers=None, raw=None):
        body = raw if raw is not None else json.dumps(obj if obj is not None else {}, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body(self, limit=4_000_000):
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise ValueError("too big")
        return json.loads(self.rfile.read(n) or b"{}")

    def authed(self):
        c = SimpleCookie(self.headers.get("Cookie") or "")
        return COOKIE in c and SECRET and hmac.compare_digest(c[COOKIE].value, token())

    def ip(self):
        return self.headers.get("X-Real-IP") or self.client_address[0]

    def limited(self, key, n, per):
        now = time.time()
        with lock:
            hits = [t for t in _hits.get(key, []) if now - t < per]
            hits.append(now)
            _hits[key] = hits
        return len(hits) > n

    def do_GET(self):
        p = self.path.split("?")[0]
        if STATIC and not p.startswith("/api/"):  # только для локальной разработки
            f = os.path.join(STATIC, p.lstrip("/") or "index.html")
            if os.path.isfile(f):
                with open(f, "rb") as fh:
                    body = fh.read()
                self.send_response(200)
                self.send_header("Content-Type", {".html": "text/html; charset=utf-8", ".svg": "image/svg+xml"}.get(os.path.splitext(f)[1], "application/octet-stream"))
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                return self.wfile.write(body)
        if p == "/api/health":
            return self.send(200, {"ok": True})
        if not self.authed():
            return self.send(401, {"error": "auth"})
        if p == "/api/me":
            return self.send(200, {"ok": True})
        if p.startswith("/api/data/") and p[10:] in ASPECTS:
            try:
                with open(os.path.join(DATA, p[10:] + ".json"), "rb") as f:
                    return self.send(200, raw=f.read())
            except OSError:
                return self.send(404, {"error": "no data"})
        if p == "/api/progress":
            with lock:
                return self.send(200, load_json(os.path.join(STATE, "progress.json"), {}))
        self.send(404, {"error": "not found"})

    def do_POST(self):
        p = self.path.split("?")[0]
        try:
            data = self.body()
        except ValueError:
            return self.send(400, {"error": "bad body"})
        if p == "/api/login":
            if self.limited("login:" + self.ip(), 8, 600):
                return self.send(429, {"error": "Слишком много попыток, подожди 10 минут"})
            pw = str(data.get("password", ""))
            if PASS_SHA and SECRET and hmac.compare_digest(hashlib.sha256(pw.encode()).hexdigest(), PASS_SHA):
                return self.send(200, {"ok": True}, {"Set-Cookie":
                    f"{COOKIE}={token()}; Max-Age={YEAR}; Path=/; HttpOnly; Secure; SameSite=Lax"})
            return self.send(403, {"error": "Неверный пароль"})
        if not self.authed():
            return self.send(401, {"error": "auth"})
        if p == "/api/progress":
            path = os.path.join(STATE, "progress.json")
            with lock:
                merged = merge(load_json(path, {}), data)
                save_json(path, merged)
            return self.send(200, merged)
        if p == "/api/hint":
            aspect, cid = data.get("aspect"), data.get("id")
            if aspect not in ASPECTS or cid not in cards(aspect):
                return self.send(404, {"error": "no card"})
            path = os.path.join(STATE, "hints.json")
            with lock:
                cache = load_json(path, {})
            if cid in cache and not data.get("again"):
                return self.send(200, {"text": cache[cid]})
            if not API_KEY:
                return self.send(503, {"error": "ИИ не настроен"})
            if self.limited("hint", 40, 600):
                return self.send(429, {"error": "Слишком много запросов, попробуй позже"})
            try:
                text = ask_claude(aspect, cards(aspect)[cid])
            except (urllib.error.URLError, TimeoutError, ValueError) as e:
                return self.send(502, {"error": f"ИИ не ответил: {e}"})
            with lock:
                cache = load_json(path, {})
                cache[cid] = text
                save_json(path, cache)
            return self.send(200, {"text": text})
        self.send(404, {"error": "not found"})


if __name__ == "__main__":
    os.makedirs(STATE, exist_ok=True)
    ThreadingHTTPServer(("127.0.0.1", int(os.environ.get("PORT", 8098))), H).serve_forever()

#!/usr/bin/env python3
"""Лексика API: вход по паролю, словари, синхронизация прогресса, ИИ-подсказки.

У каждого пароля свой аккаунт и свой прогресс (STATE/progress/<uid>.json).
Новый пароль можно создать на экране входа; владелец заводит аккаунты и с сервера:
  echo 'пароль' | python3 server.py adduser <uid>
Только stdlib. Слушает 127.0.0.1:8098, снаружи — nginx location /api/.
Окружение: LEXIKA_PASS_SHA256, LEXIKA_SECRET, ANTHROPIC_API_KEY,
           LEXIKA_DATA (словари, по умолчанию /opt/lexika/data), STATE_DIRECTORY (systemd).
"""
import hashlib, hmac, json, os, secrets, sys, threading, time, urllib.request, urllib.error
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
ORIGINS = ("https://l3thily.github.io",)  # фронт на GitHub Pages ходит сюда с токеном в заголовке
YEAR = 365 * 24 * 3600

lock = threading.Lock()
_cards = {}  # aspect -> {id: card}
_hits = {}   # ip -> [timestamps]


OWNER = "owner"


def sign(msg):
    return hmac.new(SECRET, msg.encode(), hashlib.sha256).hexdigest()


def token(uid):
    return uid + "." + sign("lexika-v2:" + uid)


def token_uid(t):
    """uid по токену; старый токен v1 (до аккаунтов) — это владелец."""
    if hmac.compare_digest(t, sign("lexika-v1")):
        return OWNER
    uid, _, sig = t.partition(".")
    return uid if uid and hmac.compare_digest(sig, sign("lexika-v2:" + uid)) else None


def users_path():
    return os.path.join(STATE, "users.json")


def find_user(pw):
    if PASS_SHA and hmac.compare_digest(hashlib.sha256(pw.encode()).hexdigest(), PASS_SHA):
        return OWNER
    return load_json(users_path(), {}).get(sign("pw:" + pw))


def add_user(pw, uid=None):
    """Возвращает uid нового аккаунта или None, если такой пароль уже занят."""
    with lock:
        users = load_json(users_path(), {})
        key = sign("pw:" + pw)
        if key in users:
            return None
        users[key] = uid or secrets.token_hex(6)
        save_json(users_path(), users)
        return users[key]


def progress_path(uid):
    d = os.path.join(STATE, "progress")
    os.makedirs(d, exist_ok=True)
    old = os.path.join(STATE, "progress.json")  # прогресс до аккаунтов — владельца
    if uid == OWNER and os.path.exists(old) and not os.path.exists(os.path.join(d, OWNER + ".json")):
        os.replace(old, os.path.join(d, OWNER + ".json"))
    return os.path.join(d, uid + ".json")


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
        self.cors()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body(self, limit=4_000_000):
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise ValueError("too big")
        return json.loads(self.rfile.read(n) or b"{}")

    def cors(self):
        if self.headers.get("Origin") in ORIGINS:
            self.send_header("Access-Control-Allow-Origin", self.headers["Origin"])
            self.send_header("Vary", "Origin")

    def authed(self):
        """uid вошедшего или None."""
        if not SECRET:
            return None
        auth = self.headers.get("Authorization") or ""
        if auth.startswith("Bearer "):
            return token_uid(auth[7:].strip())
        c = SimpleCookie(self.headers.get("Cookie") or "")
        return token_uid(c[COOKIE].value) if COOKIE in c else None

    def ip(self):
        return self.headers.get("X-Real-IP") or self.client_address[0]

    def limited(self, key, n, per):
        now = time.time()
        with lock:
            hits = [t for t in _hits.get(key, []) if now - t < per]
            hits.append(now)
            _hits[key] = hits
        return len(hits) > n

    def do_OPTIONS(self):
        self.send_response(204)
        self.cors()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Content-Length", "0")
        self.end_headers()

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
        uid = self.authed()
        if not uid:
            return self.send(401, {"error": "auth"})
        if p == "/api/me":
            return self.send(200, {"ok": True, "uid": uid})
        if p.startswith("/api/data/") and p[10:] in ASPECTS:
            try:
                with open(os.path.join(DATA, p[10:] + ".json"), "rb") as f:
                    return self.send(200, raw=f.read())
            except OSError:
                return self.send(404, {"error": "no data"})
        if p == "/api/progress":
            with lock:
                return self.send(200, load_json(progress_path(uid), {}))
        self.send(404, {"error": "not found"})

    def do_POST(self):
        p = self.path.split("?")[0]
        try:
            data = self.body()
        except ValueError:
            return self.send(400, {"error": "bad body"})
        if p in ("/api/login", "/api/register"):
            if not SECRET:
                return self.send(503, {"error": "Сервер не настроен"})
            if self.limited("login:" + self.ip(), 8, 600):
                return self.send(429, {"error": "Слишком много попыток, подожди 10 минут"})
            pw = str(data.get("password", ""))
            if p == "/api/register":
                if len(pw) < 6:
                    return self.send(400, {"error": "Пароль — минимум 6 символов"})
                if self.limited("register:" + self.ip(), 5, 3600):
                    return self.send(429, {"error": "Слишком много новых аккаунтов, попробуй через час"})
                uid = add_user(pw)
                if not uid:
                    return self.send(409, {"error": "Такой пароль уже занят — придумай другой"})
            else:
                uid = find_user(pw)
                if not uid:
                    return self.send(403, {"error": "Неверный пароль"})
            t = token(uid)
            return self.send(200, {"ok": True, "uid": uid, "token": t}, {"Set-Cookie":
                f"{COOKIE}={t}; Max-Age={YEAR}; Path=/; HttpOnly; Secure; SameSite=Lax"})
        if p == "/api/logout":
            return self.send(200, {"ok": True}, {"Set-Cookie": f"{COOKIE}=; Max-Age=0; Path=/; HttpOnly; Secure; SameSite=Lax"})
        uid = self.authed()
        if not uid:
            return self.send(401, {"error": "auth"})
        if p == "/api/progress":
            path = progress_path(uid)
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
    if sys.argv[1:2] == ["adduser"]:  # echo 'пароль' | server.py adduser [uid]
        uid = add_user(sys.stdin.readline().strip(), (sys.argv[2:3] or [None])[0])
        sys.exit(print(uid) if uid else "пароль уже занят")
    ThreadingHTTPServer(("127.0.0.1", int(os.environ.get("PORT", 8098))), H).serve_forever()

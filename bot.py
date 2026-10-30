"""
HOCKEY PULSE — хоккейная аналитика для Telegram (НХЛ + КХЛ).

Режимы (переменная MODE):
  digest - утренний дайджест по НХЛ
  poll   - проверка команд Telegram (/сегодня, /кф, /id)
"""
import os
import re
import json
import math
import datetime as dt
import time
import xml.etree.ElementTree as ET

import requests

from prediction_tracking import save_prediction, settle_nhl, daily_report

# ---------- Настройки ----------
TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
ODDS_KEY = os.environ.get("ODDS_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
MODE = os.environ.get("MODE", "poll")
PUBLIC_MODE = os.environ.get("PUBLIC_MODE", "false").strip().lower() in ("1", "true", "yes", "on")
USER_COOLDOWN_SECONDS = int(os.environ.get("USER_COOLDOWN_SECONDS", "30"))
_LAST_REQUEST = {}

MIN_EDGE = 0.03          # минимальный перевес над коэффициентом для "value"
EXPRESS_MIN_PROB = 0.55  # минимальная вероятность события для экспресса
MAX_STAKE_PCT = 2.0      # потолок ставки в % от банка
KELLY_FRACTION = 0.25    # осторожная доля от критерия Келли

MSK = dt.timezone(dt.timedelta(hours=3))

# Если какая-то лента перестанет работать, бот её пропустит. Добавляйте свои RSS.
NEWS_FEEDS = [
    "https://www.sports.ru/rss/all_news.xml",
    "https://www.championat.com/rss/news/",
    "https://www.sport-express.ru/services/materials/news/se/",
]
KEYWORDS = ["хоккей", "кхл", "нхл", "шайб", "вратар", "nhl", "khl"]


# ---------- Telegram ----------
def tg(method, **params):
    r = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/{method}", json=params, timeout=30
    )
    return r.json()


def send(chat_id, text):
    for i in range(0, len(text), 4000):
        tg("sendMessage", chat_id=chat_id, text=text[i:i + 4000],
           disable_web_page_preview=True)


# ---------- Утилиты ----------
def clamp(x, a, b):
    return max(a, min(b, x))


def risk_label(p):
    if p >= 0.62:
        return "низкий"
    if p >= 0.52:
        return "средний"
    return "высокий"


def stake_pct(p, odd):
    """Доля банка по осторожному Келли."""
    if odd <= 1:
        return 0.0
    f = (p * odd - 1) / (odd - 1)
    return clamp(f * KELLY_FRACTION * 100, 0.0, MAX_STAKE_PCT)


def last_word(name):
    return re.sub(r"[^a-zа-я0-9 ]", "", name.lower()).split()[-1]


# ---------- Новости ----------
def fetch_news(limit=40):
    items = []
    for url in NEWS_FEEDS:
        try:
            r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
            root = ET.fromstring(r.content)
            for it in root.iter("item"):
                title = (it.findtext("title") or "").strip()
                desc = re.sub(r"<[^>]+>", "", it.findtext("description") or "")[:200]
                blob = (title + " " + desc).lower()
                if any(k in blob for k in KEYWORDS):
                    items.append(f"{title} - {desc}" if desc else title)
        except Exception as e:  # лента недоступна - идём дальше
            print("Лента не загрузилась:", url, e)
    return items[:limit]


# ---------- Gemini ----------
def gemini_json(prompt):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    r = requests.post(
        url,
        headers={"x-goog-api-key": GEMINI_KEY},
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json",
                                 "temperature": 0.2},
        },
        timeout=60,
    )
    r.raise_for_status()
    text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(text)


def news_adjustments(games, headlines):
    """Просим ИИ скорректировать вероятность хозяев по новостям (макс. +-5%)."""
    if not GEMINI_KEY or not headlines or not games:
        return {}
    listing = "\n".join(f"{i}: {g['home']} vs {g['away']}" for i, g in enumerate(games))
    prompt = (
        "Ты хоккейный аналитик. Ниже матчи НХЛ и свежие заголовки новостей.\n"
        "Для каждого матча оцени, как новости (травмы, составы, вратари, усталость) "
        "меняют вероятность победы ХОЗЯЕВ. Диапазон поправки от -0.05 до 0.05. "
        "Если в новостях нет ничего значимого про эти команды - ставь 0 и пустую заметку. "
        "Ничего не выдумывай.\n\n"
        f"Матчи:\n{listing}\n\nНовости:\n" + "\n".join(headlines) + "\n\n"
        'Верни JSON-список: [{"i": номер, "adj": число, "note": "кратко по-русски"}]'
    )
    try:
        data = gemini_json(prompt)
        return {int(d["i"]): (clamp(float(d["adj"]), -0.05, 0.05), d.get("note", ""))
                for d in data}
    except Exception as e:
        print("Gemini не ответил:", e)
        return {}


# ---------- НХЛ: данные и модель ----------
def nhl_standings():
    data = requests.get("https://api-web.nhle.com/v1/standings/now", timeout=30).json()
    out = {}
    for t in data.get("standings", []):
        gp = max(t.get("gamesPlayed", 1), 1)
        l10_total = (t.get("l10Wins", 0) + t.get("l10Losses", 0)
                     + t.get("l10OtLosses", 0)) or 10
        row = {
            "gd": (t.get("goalFor", 0) - t.get("goalAgainst", 0)) / gp,
            "l10": (t.get("l10Wins", 0) + 0.5 * t.get("l10OtLosses", 0)) / l10_total,
        }
        for key in ("teamCommonName", "teamName"):
            name = (t.get(key) or {}).get("default")
            if name:
                out[last_word(name)] = row
    return out


def model_prob(home_row, away_row):
    """Простая модель: разница голов за игру, форма за 10 матчей, домашний лёд."""
    if not home_row or not away_row:
        return None
    diff = (0.5 * (home_row["gd"] - away_row["gd"])
            + 0.5 * (home_row["l10"] - away_row["l10"]) + 0.15)
    return 1 / (1 + math.exp(-diff))


def fetch_odds(sport_key):
    r = requests.get(
        f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds",
        params={"apiKey": ODDS_KEY, "regions": "eu", "markets": "h2h",
                "oddsFormat": "decimal"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def build_nhl_games():
    if not ODDS_KEY:
        raise RuntimeError("Не задан ODDS_API_KEY")
    events = fetch_odds("icehockey_nhl")
    standings = nhl_standings()
    now = dt.datetime.now(dt.timezone.utc)
    games = []
    for ev in events:
        start = dt.datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
        if not (now < start < now + dt.timedelta(hours=30)):
            continue
        home, away = ev["home_team"], ev["away_team"]
        probs, best = [], {home: 0.0, away: 0.0}
        for bk in ev.get("bookmakers", []):
            for mk in bk.get("markets", []):
                if mk.get("key") != "h2h":
                    continue
                oc = {o["name"]: o["price"] for o in mk["outcomes"]}
                if set(oc) != {home, away}:  # пропускаем 3-исходные линии
                    continue
                ih, ia = 1 / oc[home], 1 / oc[away]
                probs.append(ih / (ih + ia))
                for t in (home, away):
                    best[t] = max(best[t], oc[t])
        if not probs or min(best.values()) <= 1:
            continue
        p_mkt = sum(probs) / len(probs)
        p_mod = model_prob(standings.get(last_word(home)), standings.get(last_word(away)))
        p_base = p_mkt if p_mod is None else 0.7 * p_mkt + 0.3 * p_mod
        games.append({
            "event_id": ev["id"], "sport_key": "icehockey_nhl",
            "home": home, "away": away, "start": start.astimezone(MSK),
            "p_mkt_home": p_mkt, "p_base_home": p_base,
            "odd_home": best[home], "odd_away": best[away], "note": "",
        })

    adj = news_adjustments(games, fetch_news())
    for i, g in enumerate(games):
        a, note = adj.get(i, (0.0, ""))
        g["p_home"] = clamp(g["p_base_home"] + a, 0.05, 0.95)
        g["note"] = note
    return games


def best_picks(games):
    """Для каждого матча выбираем сторону с лучшим перевесом."""
    picks = []
    for g in games:
        sides = [
            (g["home"], g["p_home"], g["p_mkt_home"], g["odd_home"]),
            (g["away"], 1 - g["p_home"], 1 - g["p_mkt_home"], g["odd_away"]),
        ]
        team, p, pm, odd = max(sides, key=lambda s: s[1] * s[3])
        picks.append({"game": g, "team": team, "p": p, "p_mkt": pm,
                      "odd": odd, "edge": p * odd - 1})
    return picks


def format_nhl(games):
    if not games:
        return "🏒 НХЛ: на ближайшие сутки матчей с коэффициентами не найдено."
    picks = best_picks(games)
    singles = sorted([x for x in picks if x["edge"] >= MIN_EDGE],
                     key=lambda x: -x["edge"])
    # Сохраняем точный предматчевый прогноз один раз; повторные запросы не перезаписывают цену.
    for x in singles:
        g = x["game"]
        save_prediction(g["event_id"], g["sport_key"], "NHL", g["home"], g["away"],
                        g["start"], x["team"], x["odd"], x["p"], x["edge"])
    out = ["🏒 НХЛ - разбор на ближайшие сутки", ""]

    if singles:
        out.append("📌 ОДИНОЧНЫЕ СТАВКИ (value)")
        for x in singles:
            g = x["game"]
            out.append(
                f"\n• {g['away']} @ {g['home']} ({g['start']:%d.%m %H:%M} МСК)\n"
                f"  Ставка: победа {x['team']} (с ОТ), лучший кф {x['odd']:.2f}\n"
                f"  Оценка бота: {x['p']:.0%} | рынок: {x['p_mkt']:.0%} | перевес: {x['edge']:+.1%}\n"
                f"  Риск: {risk_label(x['p'])} | ставка ~{stake_pct(x['p'], x['odd']):.1f}% банка"
            )
            if g["note"]:
                out.append(f"  Новости: {g['note']}")
            if x["edge"] > 0.12:
                out.append("  ⚠️ Перевес подозрительно большой: проверьте составы, рынок мог знать больше.")
    else:
        out.append("Явных value-ставок сегодня нет. Пропуск - тоже решение.")

    cands = sorted([x for x in picks if x["p"] >= EXPRESS_MIN_PROB],
                   key=lambda x: -x["p"])[:3]
    if len(cands) >= 2:
        prob = math.prod(x["p"] for x in cands)
        odds = math.prod(x["odd"] for x in cands)
        ev = prob * odds - 1
        out.append("\n🎯 ЭКСПРЕСС")
        for x in cands:
            out.append(f"  - {x['team']} победа (кф {x['odd']:.2f}, {x['p']:.0%})")
        out.append(
            f"  Общий кф: {odds:.2f} | вероятность захода: {prob:.0%} | "
            f"матожидание: {ev:+.1%}\n  Риск: {risk_label(prob)}"
        )
        if ev < 0:
            out.append("  ⚠️ Математически невыгоден (маржа умножается). Одиночные ставки надёжнее.")
    out.append("\nℹ️ Коэффициенты усреднены по европейским букмекерам, не по Фонбету. "
               "Сверьте цену в приложении: если кф ниже указанного, перевес может исчезнуть.")
    return "\n".join(out)


# ---------- КХЛ: ручной ввод коэффициентов ----------
KF_RE = re.compile(
    r"^/?кф\s+(.+?)\s+[-–—]\s+(.+?)\s+(\d+(?:[.,]\d+)?)\s+(\d+(?:[.,]\d+)?)\s*$",
    re.IGNORECASE,
)


def khl_analyze(home, away, o_home, o_away):
    ih, ia = 1 / o_home, 1 / o_away
    margin = ih + ia - 1
    p_mkt = ih / (ih + ia)
    p_ai, note = p_mkt, "ИИ-оценка недоступна, показан только рынок."
    if GEMINI_KEY:
        prompt = (
            f"Матч КХЛ: {home} (хозяева) - {away}. Рыночная вероятность победы хозяев "
            f"с учётом ОТ без маржи: {p_mkt:.3f}.\nСвежие новости:\n"
            + "\n".join(fetch_news()) +
            "\n\nОцени вероятность победы хозяев. Отклоняйся от рыночной только при весомых "
            "причинах из новостей. Ничего не выдумывай, если данных нет - оставь около рыночной. "
            'Верни JSON: {"p_home": число 0..1, "note": "1-2 предложения по-русски"}'
        )
        try:
            d = gemini_json(prompt)
            p_ai = clamp(float(d["p_home"]), 0.05, 0.95)
            note = d.get("note", "")
        except Exception as e:
            print("Gemini ошибка:", e)
    p_home = 0.75 * p_mkt + 0.25 * p_ai
    rows = [(home, p_home, o_home), (away, 1 - p_home, o_away)]
    lines = [f"🏒 КХЛ: {home} - {away}",
             f"Маржа букмекера: {margin:.1%}",
             f"Рынок: {home} {p_mkt:.0%} / {away} {1 - p_mkt:.0%}",
             f"Оценка бота: {home} {p_home:.0%} / {away} {1 - p_home:.0%}"]
    for team, p, odd in rows:
        edge = p * odd - 1
        verdict = "value есть" if edge >= MIN_EDGE else "value нет"
        lines.append(f"• {team}: кф {odd:.2f}, перевес {edge:+.1%} ({verdict}), "
                     f"риск {risk_label(p)}")
    if note:
        lines.append(f"Новости/комментарий: {note}")
    lines.append("⚠️ По КХЛ оценка опирается на новости и ИИ, статистической модели пока нет. "
                 "Относитесь как к подсказке, не как к гарантии.")
    return "\n".join(lines)


# ---------- Команды ----------
HELP = (
    "🏒 HOCKEY PULSE | AI HOCKEY ANALYTICS\\n\\n"
    "Меньше интуиции. Больше цифр.\\n\\n"
    "📅 /сегодня — аналитика матчей НХЛ на ближайшие сутки\\n"
    "🇷🇺 /кф Хозяева - Гости 2.10 1.75 — разбор матча КХЛ по введённым коэффициентам "
    "(победа с учётом ОТ)\\n"
    "📊 /отчёт — результаты прогнозов за сегодня\\n"
    "ℹ️ /помощь — список команд\\n"
    "🆔 /id — узнать свой chat id\\n\\n"
    "🧊 Проект находится в тестовом режиме. Оценки модели не гарантируют результат.\\n"
    "⏱️ Ответы могут задерживаться: бот запускается по расписанию GitHub Actions."
)


def send_main_menu(chat_id):
    """Показывает брендированное меню владельцу/разрешённому пользователю."""
    tg(
        "sendMessage",
        chat_id=chat_id,
        text=(
            "🏒 HOCKEY PULSE\\n"
            "🤖 AI-аналитика КХЛ и НХЛ\\n\\n"
            "Привет! Выбери раздел или отправь команду.\\n"
            "Меньше интуиции. Больше цифр."
        ),
        reply_markup={
            "keyboard": [
                [{"text": "📅 Матчи дня"}, {"text": "🇺🇸 Аналитика НХЛ"}],
                [{"text": "🇷🇺 Аналитика КХЛ"}, {"text": "ℹ️ Помощь"}],
            ],
            "resize_keyboard": True,
            "is_persistent": True,
        },
        disable_web_page_preview=True,
    )


def handle(chat_id, text):
    low = text.lower().strip()
    if low.startswith("/id"):
        send(chat_id, f"🆔 Ваш chat id: {chat_id}\\n"
                      "Добавьте его в GitHub Secrets как TELEGRAM_CHAT_ID, если настраиваете "
                      "собственный экземпляр бота.\\n\\n" + HELP)
        return
    if low.startswith("/start"):
        if not PUBLIC_MODE and (not CHAT_ID or chat_id != CHAT_ID):
            send(chat_id,
                 "🏒 HOCKEY PULSE\\n\\n"
                 "🤖 Бесплатная хоккейная аналитика КХЛ и НХЛ.\\n"
                 "Сейчас бот находится в закрытом тестировании. "
                 "Публичный доступ будет включён после проверки.")
        else:
            send_main_menu(chat_id)
        return
    # По умолчанию доступ закрыт; PUBLIC_MODE=true включается только после проверки.
    if not PUBLIC_MODE and (not CHAT_ID or chat_id != CHAT_ID):
        return
    if low.startswith("/отчет") or low.startswith("/отчёт"):
        try:
            settle_nhl(ODDS_KEY)
            send(chat_id, daily_report())
        except Exception as e:
            send(chat_id, f"Не удалось сформировать отчёт: {e}")
        return
    # Ограничиваем частоту команд, вызывающих внешние API/ИИ.
    if low.startswith(("/сегодня", "/today", "/кф", "кф ", "сегодня")):
        now = time.monotonic()
        previous = _LAST_REQUEST.get(chat_id, 0)
        if now - previous < USER_COOLDOWN_SECONDS:
            send(chat_id, f"⏳ Подожди {USER_COOLDOWN_SECONDS} секунд перед следующим аналитическим запросом.")
            return
        _LAST_REQUEST[chat_id] = now
    if low in ("📅 матчи дня", "🇺🇸 аналитика нхл"):
        low = "/сегодня"
    elif low == "🇷🇺 аналитика кхл":
        send(chat_id, "🇷🇺 Аналитика КХЛ\\n\\n"
                      "Отправь коэффициенты в формате:\\n"
                      "/кф Динамо - СКА 2.10 1.75\\n"
                      "Указывай коэффициенты на победу с учётом овертайма.")
        return
    elif low == "ℹ️ помощь":
        send(chat_id, HELP)
        return
    if low.startswith("/сегодня") or low.startswith("/today") or low == "сегодня":
        try:
            send(chat_id, format_nhl(build_nhl_games()))
        except Exception as e:
            send(chat_id, f"Не удалось собрать данные: {e}")
    elif low.startswith("/кф") or low.startswith("кф "):
        m = KF_RE.match(text.strip())
        if not m:
            send(chat_id, "Формат: /кф Динамо - СКА 2.10 1.75")
            return
        h, a = m.group(1), m.group(2)
        oh, oa = (float(m.group(3).replace(",", ".")), float(m.group(4).replace(",", ".")))
        if oh <= 1 or oa <= 1:
            send(chat_id, "Коэффициенты должны быть больше 1.")
            return
        send(chat_id, khl_analyze(h, a, oh, oa))
    elif low.startswith("/помощь") or low.startswith("/help"):
        send(chat_id, HELP)


def poll():
    updates = tg("getUpdates", timeout=0, allowed_updates=["message"]).get("result", [])
    last = None
    for u in updates:
        last = u["update_id"]
        m = u.get("message") or {}
        text = (m.get("text") or "").strip()
        cid = str((m.get("chat") or {}).get("id", ""))
        if text and cid:
            try:
                handle(cid, text)
            except Exception as e:
                print("Ошибка обработки:", e)
    if last is not None:  # подтверждаем, что сообщения обработаны
        tg("getUpdates", offset=last + 1, timeout=0)


def digest():
    if not CHAT_ID:
        print("TELEGRAM_CHAT_ID не задан - дайджест пропущен.")
        return
    try:
        send(CHAT_ID, format_nhl(build_nhl_games()))
        settle_nhl(ODDS_KEY)
        send(CHAT_ID, daily_report())
    except Exception as e:
        send(CHAT_ID, f"Дайджест/отчёт не собрался: {e}")


def serve():
    """Постоянный long-polling процесс; дайджест владельцу ежедневно в 08:00 МСК."""
    offset = None
    last_digest_date = None
    print("HOCKEY PULSE service started; PUBLIC_MODE=", PUBLIC_MODE)
    while True:
        now_utc = dt.datetime.now(dt.timezone.utc)
        if now_utc.hour == 5 and last_digest_date != now_utc.date():
            digest()
            last_digest_date = now_utc.date()
        try:
            params = {"timeout": 25, "allowed_updates": ["message"]}
            if offset is not None:
                params["offset"] = offset
            response = tg("getUpdates", **params)
            if not response.get("ok"):
                raise RuntimeError("Telegram getUpdates вернул ошибку")
            for update in response.get("result", []):
                offset = update["update_id"] + 1
                message = update.get("message") or {}
                text = (message.get("text") or "").strip()
                chat_id = str((message.get("chat") or {}).get("id", ""))
                if text and chat_id:
                    try:
                        handle(chat_id, text)
                    except Exception as exc:
                        print("Ошибка обработки update:", exc)
        except Exception as exc:
            print("Ошибка long polling:", exc)
            time.sleep(5)


if __name__ == "__main__":
    if not TG_TOKEN:
        raise SystemExit("Не задан TELEGRAM_TOKEN")
    if MODE == "digest":
        digest()
    elif MODE == "serve":
        serve()
    else:
        poll()

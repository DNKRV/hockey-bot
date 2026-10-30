"""Persistent pre-match prediction ledger and daily settlement reports."""
import datetime as dt
import os
import sqlite3

import requests

DB_PATH = os.environ.get("PREDICTIONS_DB_PATH", "data/predictions.sqlite3")
MSK = dt.timezone(dt.timedelta(hours=3))


def _connect():
    parent = os.path.dirname(DB_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=20)
    con.row_factory = sqlite3.Row
    con.execute("""
        CREATE TABLE IF NOT EXISTS predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL,
            sport_key TEXT NOT NULL,
            league TEXT NOT NULL,
            home_team TEXT NOT NULL,
            away_team TEXT NOT NULL,
            commence_time TEXT NOT NULL,
            selection TEXT NOT NULL,
            odds REAL NOT NULL,
            probability REAL NOT NULL,
            edge REAL NOT NULL,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            home_score INTEGER,
            away_score INTEGER,
            settled_at TEXT,
            UNIQUE(event_id)
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_predictions_created ON predictions(created_at)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_predictions_status ON predictions(status)")
    return con


def save_prediction(event_id, sport_key, league, home, away, commence_time,
                    selection, odds, probability, edge):
    """Insert once. Existing snapshots are never overwritten by later odds."""
    now = dt.datetime.now(MSK).isoformat()
    with _connect() as con:
        con.execute("""
            INSERT OR IGNORE INTO predictions
            (event_id, sport_key, league, home_team, away_team, commence_time,
             selection, odds, probability, edge, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (str(event_id), sport_key, league, home, away,
              commence_time.isoformat() if hasattr(commence_time, "isoformat") else str(commence_time),
              selection, float(odds), float(probability), float(edge), now))


def settle_nhl(api_key):
    """Settle pending NHL picks from the completed scores feed; never infer missing scores."""
    if not api_key:
        return 0
    response = requests.get(
        "https://api.the-odds-api.com/v4/sports/icehockey_nhl/scores",
        params={"apiKey": api_key, "daysFrom": 3},
        timeout=30,
    )
    response.raise_for_status()
    events = {str(e.get("id")): e for e in response.json() if e.get("completed")}
    changed = 0
    with _connect() as con:
        rows = con.execute(
            "SELECT id, event_id, selection FROM predictions "
            "WHERE status='pending' AND sport_key='icehockey_nhl'"
        ).fetchall()
        for row in rows:
            event = events.get(row["event_id"])
            if not event:
                continue
            scores = {s["name"]: int(s["score"]) for s in (event.get("scores") or [])
                      if s.get("score") is not None}
            home, away = event.get("home_team"), event.get("away_team")
            if home not in scores or away not in scores:
                continue
            if scores[home] == scores[away]:
                continue
            winner = home if scores[home] > scores[away] else away
            status = "won" if row["selection"] == winner else "lost"
            con.execute(
                "UPDATE predictions SET status=?, home_score=?, away_score=?, settled_at=? "
                "WHERE id=? AND status='pending'",
                (status, scores[home], scores[away], dt.datetime.now(MSK).isoformat(), row["id"])
            )
            changed += 1
    return changed


def daily_report(day=None):
    day = day or dt.datetime.now(MSK).date()
    start = dt.datetime.combine(day, dt.time.min, MSK).isoformat()
    end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time.min, MSK).isoformat()
    with _connect() as con:
        rows = con.execute(
            "SELECT * FROM predictions WHERE created_at >= ? AND created_at < ? "
            "ORDER BY commence_time, id", (start, end)
        ).fetchall()
    if not rows:
        return f"📊 ОТЧЁТ HOCKEY PULSE за {day:%d.%m.%Y}\nЗа этот день прогнозов не зафиксировано."
    counts = {"won": 0, "lost": 0, "void": 0, "pending": 0}
    profit = 0.0
    lines = [f"📊 ОТЧЁТ HOCKEY PULSE за {day:%d.%m.%Y}", ""]
    for row in rows:
        status = row["status"]
        counts[status] = counts.get(status, 0) + 1
        if status == "won":
            profit += row["odds"] - 1
            marker, label = "🟢", "ЗАШЁЛ"
        elif status == "lost":
            profit -= 1
            marker, label = "🔴", "НЕ ЗАШЁЛ"
        elif status == "void":
            marker, label = "🟡", "ВОЗВРАТ"
        else:
            marker, label = "⚪", "ОЖИДАЕТ"
        start_time = dt.datetime.fromisoformat(row["commence_time"]).astimezone(MSK)
        score = (f" | счёт {row['home_score']}:{row['away_score']}"
                 if row["home_score"] is not None else "")
        lines.append(
            f"{marker} {row['league']}: {row['away_team']} — {row['home_team']}\n"
            f"   Выбор: {row['selection']} @ {row['odds']:.2f} | "
            f"{start_time:%H:%M} МСК | {label}{score}"
        )
    settled = counts["won"] + counts["lost"]
    hit_rate = counts["won"] / settled if settled else 0.0
    lines += [
        "",
        f"🟢 Зашло: {counts['won']}  🔴 Не зашло: {counts['lost']}",
        f"🟡 Возврат: {counts['void']}  ⚪ Ожидает: {counts['pending']}",
        f"Проход: {hit_rate:.1%} ({settled} рассчитано)",
        f"Условный результат: {profit:+.2f} ед. (1 ед. на прогноз)",
        "Коэффициенты зафиксированы при публикации; расчёт не учитывает комиссии и налоги.",
    ]
    return "\n".join(lines)

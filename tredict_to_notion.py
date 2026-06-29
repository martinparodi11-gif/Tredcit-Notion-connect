#!/usr/bin/env python3
"""
Tredict -> Notion (deterministico, sin LLM).

Cada corrida:
  1) Lee tus actividades desde la Personal API de Tredict.
  2) Agrega a la base "Actividades" cada entrenamiento/caminata que falte
     (no duplica: usa la propiedad "ID Tredict").
  3) Calcula la semana en curso y hace UPSERT en "Semanas de entrenamiento"
     (sesiones, minutos, km). No toca tu "Meta" si ya la editaste.

Funciona con el plan GRATUITO de Notion (API publica REST).
Sin dependencias externas: solo libreria estandar de Python 3.

Variables de entorno (GitHub: Settings -> Secrets and variables -> Actions):
  TREDICT_TOKEN             Personal Access Token de Tredict (lectura de actividades)
  NOTION_TOKEN              Token de integracion interna de Notion (ntn_...)
  NOTION_ACTIVIDADES_DB_ID  Id base Actividades (def abajo)
  NOTION_SEMANAS_DB_ID      Id base Semanas (def abajo)

Zona horaria: America/Lima (UTC-5).
"""

import os
import sys
import json
import datetime as dt
from urllib import request, parse, error

DEF_ACT = "5222f84ccd4a4c1a8e2eeec781f270e5"
DEF_SEM = "c1b7ea07c5f9439d8d6852cc90b6b160"

TREDICT_TOKEN = os.environ.get("TREDICT_TOKEN", "").strip()
NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "").strip()
ACT_DB = os.environ.get("NOTION_ACTIVIDADES_DB_ID", DEF_ACT).strip()
SEM_DB = os.environ.get("NOTION_SEMANAS_DB_ID", DEF_SEM).strip()

TREDICT_BASE = "https://www.tredict.com/api/oauth/v2"
NOTION_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
LIMA = dt.timezone(dt.timedelta(hours=-5))
MES = ["ene", "feb", "mar", "abr", "may", "jun",
       "jul", "ago", "sep", "oct", "nov", "dic"]
PROCESS_LAST = 60


def die(msg, code=1):
    print("ERROR:", msg, file=sys.stderr)
    sys.exit(code)


def http_json(url, headers, method="GET", body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = request.Request(url, data=data, headers=headers, method=method)
    try:
        with request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        die("{} {} -> HTTP {}: {}".format(method, url, e.code, detail[:400]))
    except Exception as e:
        die("{} {} -> {}".format(method, url, e))


# -------------------------------------------------------------------- Tredict
def tredict_activities():
    qs = parse.urlencode({"pageSize": 600, "extendedSummary": 1})
    url = TREDICT_BASE + "/activityList?" + qs
    headers = {
        "Authorization": "Bearer " + TREDICT_TOKEN,
        "Accept": "application/json;charset=UTF-8",
    }
    data = http_json(url, headers)
    return (data.get("_embedded") or {}).get("activityList") or []


def summary_of(a):
    if isinstance(a.get("summary"), dict):
        return a["summary"]
    ext = a.get("extendedSummary")
    if isinstance(ext, dict):
        if isinstance(ext.get("summary"), dict):
            return ext["summary"]
        return ext
    return {}


def deporte(sport, sub):
    if sport == "running":
        return "🏃 Carrera"
    if sport == "cycling":
        return "🚴 Ciclismo"
    if sport == "swimming":
        return "🏊 Natación"
    if sport == "misc":
        if sub in ("walking", "hiking"):
            return "🚶 Caminata"
        if sub == "strength_training":
            return "🏋️ Fuerza"
        return "🤸 Otro"
    return "🤸 Otro"


def parse_activity(a):
    ds = a.get("date")
    if not ds:
        return None
    when = dt.datetime.fromisoformat(ds.replace("Z", "+00:00")).astimezone(LIMA)
    s = summary_of(a)
    sport = a.get("sportType")
    sub = a.get("subSportType")

    def n(*path):
        cur = s
        for p in path:
            if not isinstance(cur, dict):
                return None
            cur = cur.get(p)
        return cur if isinstance(cur, (int, float)) else None

    dur = n("duration")
    dist = n("distance")
    pace = n("pace")
    hr = n("heartrate")
    asc = n("altitude", "ascent")
    cal = n("calories")

    ritmo = None
    is_walk = sport == "misc" and sub == "walking"
    if pace and (sport == "running" or is_walk):
        t = int(round(pace))
        ritmo = "{}:{:02d} /km".format(t // 60, t % 60)

    d = when.date()
    return {
        "id": a.get("id"),
        "fecha": d.isoformat(),
        "monday": d - dt.timedelta(days=d.weekday()),
        "titulo": a.get("title") or deporte(sport, sub),
        "deporte": deporte(sport, sub),
        "min": int(round(dur / 60)) if dur else 0,
        "km": round(dist / 1000, 2) if dist else None,
        "ritmo": ritmo,
        "fc": int(round(hr)) if hr else None,
        "desnivel": int(round(asc)) if asc else None,
        "cal": int(round(cal)) if cal else None,
        "notas": a.get("notes") or None,
        "enlace": "https://www.tredict.com/app/training/activity/" + str(a.get("id")),
    }


# --------------------------------------------------------------------- Notion
def nh():
    return {
        "Authorization": "Bearer " + NOTION_TOKEN,
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def rt(s):
    return {"rich_text": [{"text": {"content": str(s)}}]}


def notion_existing_ids(db_id, prop):
    out = set()
    cursor = None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        url = NOTION_BASE + "/databases/" + db_id + "/query"
        data = http_json(url, nh(), "POST", body)
        for row in data.get("results", []):
            cell = row.get("properties", {}).get(prop, {}) or {}
            vals = cell.get("rich_text", [])
            if vals:
                out.add(vals[0].get("plain_text", ""))
        if data.get("has_more"):
            cursor = data.get("next_cursor")
        else:
            break
    return out


def create_activity(act):
    props = {
        "Actividad": {"title": [{"text": {"content": act["titulo"]}}]},
        "Fecha": {"date": {"start": act["fecha"]}},
        "Deporte": {"select": {"name": act["deporte"]}},
        "Duración (min)": {"number": act["min"]},
        "Enlace": {"url": act["enlace"]},
        "ID Tredict": rt(act["id"]),
    }
    if act["km"] is not None:
        props["Distancia (km)"] = {"number": act["km"]}
    if act["ritmo"]:
        props["Ritmo"] = rt(act["ritmo"])
    if act["fc"]:
        props["FC media"] = {"number": act["fc"]}
    if act["desnivel"]:
        props["Desnivel + (m)"] = {"number": act["desnivel"]}
    if act["cal"]:
        props["Calorías"] = {"number": act["cal"]}
    if act["notas"]:
        props["Notas"] = rt(act["notas"][:1900])
    body = {"parent": {"database_id": ACT_DB}, "properties": props}
    http_json(NOTION_BASE + "/pages", nh(), "POST", body)


def find_week(week_id):
    flt = {"property": "ID Semana", "rich_text": {"equals": week_id}}
    body = {"filter": flt, "page_size": 1}
    url = NOTION_BASE + "/databases/" + SEM_DB + "/query"
    data = http_json(url, nh(), "POST", body)
    res = data.get("results") or []
    return res[0]["id"] if res else None


def upsert_week(monday, sesiones, minutos, km):
    week_id = monday.isoformat()
    page_id = find_week(week_id)
    props = {
        "Sesiones": {"number": sesiones},
        "Minutos": {"number": int(round(minutos))},
        "Km": {"number": round(km, 1)},
    }
    if page_id:
        url = NOTION_BASE + "/pages/" + page_id
        http_json(url, nh(), "PATCH", {"properties": props})
        return "actualizada"
    titulo = "Semana del {} {}".format(monday.day, MES[monday.month - 1])
    props["Semana"] = {"title": [{"text": {"content": titulo}}]}
    props["Inicio"] = {"date": {"start": week_id}}
    props["Meta"] = {"number": 4}
    props["ID Semana"] = rt(week_id)
    body = {"parent": {"database_id": SEM_DB}, "properties": props}
    http_json(NOTION_BASE + "/pages", nh(), "POST", body)
    return "creada"


# ----------------------------------------------------------------------- Main
def main():
    if not TREDICT_TOKEN:
        die("Falta TREDICT_TOKEN")
    if not NOTION_TOKEN:
        die("Falta NOTION_TOKEN")

    raw = tredict_activities()
    if not raw:
        print("Aviso: Tredict no devolvio actividades (revisa token/scope).")
        return

    acts = []
    for a in raw[:PROCESS_LAST]:
        p = parse_activity(a)
        if p and p["id"]:
            acts.append(p)

    existentes = notion_existing_ids(ACT_DB, "ID Tredict")
    nuevas = 0
    for act in acts:
        if act["id"] not in existentes:
            create_activity(act)
            nuevas += 1

    hoy = dt.datetime.now(LIMA).date()
    lunes = hoy - dt.timedelta(days=hoy.weekday())
    semana = [a for a in acts if a["monday"] == lunes]
    sesiones = len(semana)
    minutos = sum(a["min"] for a in semana)
    km = sum(a["km"] or 0 for a in semana)
    estado = upsert_week(lunes, sesiones, minutos, km)

    out = "OK: {} nuevas. Semana {}: {} sesiones [{}]."
    print(out.format(nuevas, lunes.isoformat(), sesiones, estado))


if __name__ == "__main__":
    main()

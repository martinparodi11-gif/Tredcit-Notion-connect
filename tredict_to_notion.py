#!/usr/bin/env python3
"""
Resumen diario de entrenamiento: Tredict -> Notion.

Determinístico, sin LLM. Lee tus actividades desde la Personal API de Tredict,
calcula el resumen de la semana en curso y hace UPSERT (crea o actualiza) la
fila de hoy en tu base de Notion "🏊 Entrenamiento".

Funciona con el plan GRATUITO de Notion: usa la API pública (REST), que está
disponible en todos los planes. (Lo que requiere Business son los tools de
consulta del conector MCP, no esta API.)

Variables de entorno requeridas (en GitHub: Settings -> Secrets and variables -> Actions):
  TREDICT_TOKEN       -> Personal Access Token de Tredict (scope de lectura de actividades)
  NOTION_TOKEN        -> Token de una integración interna de Notion (ntn_...)
  NOTION_DATABASE_ID  -> ID de la base "🏊 Entrenamiento"
                         (por defecto: 8b87c1c1c1a844f5a3777c89b76226e7)

Zona horaria: America/Lima (UTC-5, sin horario de verano).
Sin dependencias externas: usa solo la librería estándar de Python 3.
"""

import os
import sys
import json
import datetime as dt
from urllib import request, parse, error

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------
TREDICT_TOKEN = os.environ.get("TREDICT_TOKEN", "").strip()
NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "").strip()
NOTION_DATABASE_ID = os.environ.get(
    "NOTION_DATABASE_ID", "8b87c1c1c1a844f5a3777c89b76226e7"
).strip()

TREDICT_BASE = "https://www.tredict.com/api/oauth/v2"
NOTION_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
LIMA = dt.timezone(dt.timedelta(hours=-5))  # America/Lima fija (UTC-5)

SPORT_ES = {
    "running": "Carrera",
    "cycling": "Ciclismo",
    "swimming": "Natación",
    "misc": "Otro",
}
SUB_ES = {
    "walking": "Caminata",
    "strength_training": "Fuerza",
    "hiking": "Hiking",
    "rowing": "Remo",
    "generic": "Carrera",
}


def die(msg, code=1):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


# ---------------------------------------------------------------------------
# HTTP helpers (solo stdlib)
# ---------------------------------------------------------------------------
def http_json(url, headers, method="GET", body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = request.Request(url, data=data, headers=headers, method=method)
    try:
        with request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        die(f"{method} {url} -> HTTP {e.code}: {detail[:500]}")
    except Exception as e:
        die(f"{method} {url} -> {e}")


# ---------------------------------------------------------------------------
# Tredict
# ---------------------------------------------------------------------------
def tredict_get(path, params=None):
    url = f"{TREDICT_BASE}/{path}"
    if params:
        url += "?" + parse.urlencode(params)
    headers = {
        "Authorization": f"Bearer {TREDICT_TOKEN}",
        "Accept": "application/json;charset=UTF-8",
    }
    return http_json(url, headers)


def fetch_activities():
    """Devuelve lista de dicts {id, date(datetime aware), sport, sub}, más nuevas primero."""
    data = tredict_get("activityList", {"pageSize": 600, "extendedSummary": 1})
    items = (data.get("_embedded") or {}).get("activityList") or []
    out = []
    for a in items:
        ds = a.get("date")
        if not ds:
            continue
        try:
            when = dt.datetime.fromisoformat(ds.replace("Z", "+00:00"))
        except ValueError:
            continue
        ext = a.get("extendedSummary") or {}
        sub = a.get("subSportType") or ext.get("subSportType")
        out.append(
            {"id": a.get("id"), "date": when, "sport": a.get("sportType"), "sub": sub}
        )
    out.sort(key=lambda x: x["date"], reverse=True)
    return out


def fetch_week_load(week_dates):
    """Suma la 'carga' (effort) de Tredict para las fechas de esta semana. Best-effort.

    El endpoint /efforts puede devolver distintas formas; recorremos el JSON
    buscando claves tipo YYYYMMDD y sumamos los enteros de las fechas de la semana.
    Si algo falla o no hay datos, devuelve None (la columna queda vacía).
    """
    now = dt.datetime.now(dt.timezone.utc)
    try:
        data = tredict_get(
            "efforts",
            {"startDate": now.isoformat(), "endDate": (now - dt.timedelta(days=21)).isoformat()},
        )
    except SystemExit:
        return None
    wanted = {d.strftime("%Y%m%d") for d in week_dates}
    acc = {"sum": 0, "hit": False}

    def _ints(node):
        if isinstance(node, bool):
            return
        if isinstance(node, (int, float)):
            yield int(node)
        elif isinstance(node, list):
            for v in node:
                yield from _ints(v)

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(k, str) and len(k) == 8 and k.isdigit() and k in wanted:
                    for n in _ints(v):
                        acc["sum"] += n
                        acc["hit"] = True
                else:
                    walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    try:
        walk(data)
    except Exception:
        return None
    return acc["sum"] if acc["hit"] else None


# ---------------------------------------------------------------------------
# Cálculo del resumen
# ---------------------------------------------------------------------------
def label_for(sport, sub):
    if sport == "misc" and sub:
        return SUB_ES.get(sub, sub.replace("_", " ").title())
    return SPORT_ES.get(sport, (sport or "Sesión").title())


def build_summary(activities):
    today = dt.datetime.now(LIMA).date()
    monday = today - dt.timedelta(days=today.weekday())  # lunes de esta semana
    week_dates = [monday + dt.timedelta(days=i) for i in range(today.weekday() + 1)]

    def lima_date(a):
        return a["date"].astimezone(LIMA).date()

    week = [a for a in activities if lima_date(a) >= monday]
    n = len(week)

    counts = {}
    for a in week:
        lbl = label_for(a["sport"], a["sub"])
        counts[lbl] = counts.get(lbl, 0) + 1
    breakdown = " · ".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda x: -x[1]))

    ultima = "—"
    if activities:
        last = activities[0]
        days = (today - lima_date(last)).days
        sl = label_for(last["sport"], last["sub"])
        when = "hoy" if days <= 0 else ("ayer" if days == 1 else f"hace {days} días")
        ultima = f"{sl} · {when}"

    carga = fetch_week_load(week_dates)
    resumen = f"Semana: {n} sesiones" + (f" · carga {carga}" if carga is not None else "")

    return {
        "fecha": today.isoformat(),
        "sesiones": n,
        "carga": carga,
        "ultima": ultima,
        "nota": breakdown or "Sin sesiones esta semana.",
        "resumen": resumen,
    }


# ---------------------------------------------------------------------------
# Notion (API pública REST — funciona en plan gratuito)
# ---------------------------------------------------------------------------
def notion_headers():
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def notion_find_today(fecha):
    url = f"{NOTION_BASE}/databases/{NOTION_DATABASE_ID}/query"
    body = {"filter": {"property": "Fecha", "date": {"equals": fecha}}, "page_size": 1}
    data = http_json(url, notion_headers(), method="POST", body=body)
    results = data.get("results") or []
    return results[0]["id"] if results else None


def notion_properties(s):
    props = {
        "Resumen": {"title": [{"text": {"content": s["resumen"]}}]},
        "Fecha": {"date": {"start": s["fecha"]}},
        "Sesiones (semana)": {"number": s["sesiones"]},
        "Última actividad": {"rich_text": [{"text": {"content": s["ultima"]}}]},
        "Nota": {"rich_text": [{"text": {"content": s["nota"]}}]},
    }
    if s["carga"] is not None:
        props["Carga (semana)"] = {"number": s["carga"]}
    return props


def notion_upsert(s):
    props = notion_properties(s)
    page_id = notion_find_today(s["fecha"])
    if page_id:
        http_json(f"{NOTION_BASE}/pages/{page_id}", notion_headers(),
                  method="PATCH", body={"properties": props})
        return "actualizada"
    http_json(f"{NOTION_BASE}/pages", notion_headers(), method="POST",
              body={"parent": {"database_id": NOTION_DATABASE_ID}, "properties": props})
    return "creada"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    if not TREDICT_TOKEN:
        die("Falta TREDICT_TOKEN")
    if not NOTION_TOKEN:
        die("Falta NOTION_TOKEN")

    activities = fetch_activities()
    if not activities:
        print("Aviso: Tredict no devolvió actividades. "
              "Revisa el token/scope o que tengas actividades importadas.")
    summary = build_summary(activities)
    print("Resumen calculado:", json.dumps(summary, ensure_ascii=False))
    estado = notion_upsert(summary)
    print(f"OK -> fila {estado} en Notion para {summary['fecha']}.")


if __name__ == "__main__":
    main()

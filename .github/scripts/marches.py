#!/usr/bin/env python3
"""Marché Malin : liste des marchés de France (OpenStreetMap, ODbL).

Récupère les amenity=marketplace via Overpass, traduit les horaires OSM en règles
simples, ajoute la commune (api-adresse.data.gouv.fr, géocodage inverse en lot)
et écrit marchemalin/marches.json.

Règles : [jours 1-7 (lundi=1), mois 1-12 ou [], n-ièmes semaines ou [], créneaux en minutes
[[ouverture, fermeture], ...] ou [] (heure inconnue), fermé 0/1]. Elles s'appliquent dans
l'ordre, la dernière qui correspond à une date l'emporte (sémantique opening_hours).
"""
import csv, io, json, re, sys, time, urllib.parse, urllib.request
from datetime import date

OUT = sys.argv[1] if len(sys.argv) > 1 else "marchemalin/marches.json"
UA = {"User-Agent": "MarcheMalin-data/1.0 (+https://jeremybeaussier.github.io/marchemalin/)"}
QUERY = '[out:json][timeout:300];area["ISO3166-1"="FR"][admin_level=2]->.fr;nwr["amenity"="marketplace"](area.fr);out center tags;'
SERVERS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter",
           "https://maps.mail.ru/osm/tools/overpass/api/interpreter"]

DAYS = {"Mo": 1, "Tu": 2, "We": 3, "Th": 4, "Fr": 5, "Sa": 6, "Su": 7}
FR_DAYS = {"lundi": 1, "mardi": 2, "mercredi": 3, "jeudi": 4, "vendredi": 5, "samedi": 6, "dimanche": 7}
MONTHS = {m: i + 1 for i, m in enumerate("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split())}


def fetch_osm():
    body = urllib.parse.urlencode({"data": QUERY}).encode()
    last = None
    for attempt in range(3):
        for url in SERVERS:
            try:
                req = urllib.request.Request(url, data=body, headers=UA)
                with urllib.request.urlopen(req, timeout=320) as r:
                    data = json.load(r)
                if len(data.get("elements", [])) > 1000:
                    return data["elements"]
            except Exception as e:  # serveur saturé : on essaie le suivant
                last = e
        time.sleep(30)
    raise SystemExit(f"Overpass indisponible : {last}")


def minutes(t):
    h, m = t.split(":")
    return min(int(h) * 60 + int(m), 24 * 60)


def day_list(spec):
    """'Mo-Sa', 'Tu,Fr', 'Sa[1]' → (jours, n-ièmes)."""
    days, nth = [], []
    for part in re.split(r"\s*,\s*(?![^\[]*\])", spec):
        m = re.fullmatch(r"(Mo|Tu|We|Th|Fr|Sa|Su)(?:-(Mo|Tu|We|Th|Fr|Sa|Su))?(?:\[([-\d,]+)\])?", part)
        if not m:
            if part == "PH":
                continue
            return None, None
        a, b = DAYS[m.group(1)], DAYS[m.group(2) or m.group(1)]
        rng = list(range(a, b + 1)) if a <= b else list(range(a, 8)) + list(range(1, b + 1))
        days += rng
        if m.group(3):
            nth += [int(x) for x in m.group(3).split(",") if x.lstrip("-").isdigit()]
    return sorted(set(days)), nth


def month_list(spec):
    """'Jul-Aug', 'Apr-Sep', 'Jun 15-Sep 30', 'Dec' → liste de mois."""
    m = re.fullmatch(r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)(?: \d{1,2})?(?:-(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)(?: \d{1,2})?)?", spec)
    if not m:
        return None
    a, b = MONTHS[m.group(1)], MONTHS[m.group(2) or m.group(1)]
    return list(range(a, b + 1)) if a <= b else list(range(a, 13)) + list(range(1, b + 1))


def times(spec):
    slots = []
    for part in re.split(r"\s*,\s*", spec):
        m = re.fullmatch(r"(\d{1,2}:\d\d)-(\d{1,2}:\d\d)", part)
        if m:
            slots.append([minutes(m.group(1)), minutes(m.group(2))])
            continue
        m = re.fullmatch(r"(\d{1,2}:\d\d)\+", part)
        if m:  # « 16:00+ » : fin inconnue, on compte 3 h
            o = minutes(m.group(1))
            slots.append([o, min(o + 180, 1440)])
            continue
        return None
    return slots


def parse_rule(rule):
    rule = rule.strip().rstrip(",").strip()
    if not rule:
        return []
    if rule == "24/7":
        return [[list(range(1, 8)), [], [], [[0, 1440]], 0]]
    months = []
    MON = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)(?: \d{1,2})?(?:-(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)(?: \d{1,2})?)?"
    m = re.match(rf"^({MON}(?:\s*,\s*{MON})*):?\s+(.*)$", rule)
    if m:
        for part in re.split(r"\s*,\s*", m.group(1)):
            ml = month_list(part.strip())
            if ml is None:
                return None
            months += ml
        rule = m.group(2)
    # « Tu-Sa 08:00-20:00, Su 08:00-13:30 » : plusieurs groupes séparés par des virgules.
    groups = re.findall(r"((?:(?:Mo|Tu|We|Th|Fr|Sa|Su|PH)(?:-(?:Mo|Tu|We|Th|Fr|Sa|Su))?(?:\[[-\d,]+\])?\s*,?\s*)+)\s*([\d:+\-, ]*\d[+\d]?|off|closed|open)?\s*,?", rule)
    groups = [(d.strip().rstrip(","), (t or "").strip().rstrip(",")) for d, t in groups if d.strip()]
    if not groups:
        # heures seules = tous les jours
        t = times(rule) if rule not in ("off", "closed") else []
        if t is None:
            return None
        return [[list(range(1, 8)), months, [], t, 1 if rule in ("off", "closed") else 0]]
    if re.sub(r"[\s,]", "", "".join(d + t for d, t in groups)) != re.sub(r"[\s,]", "", rule):
        return None
    out = []
    for d, t in groups:
        days, nth = day_list(d)
        if days is None:
            return None
        if t in ("off", "closed"):
            out.append([days, months, nth, [], 1])
        elif t in ("", "open"):
            out.append([days, months, nth, [], 0])
        else:
            s = times(t)
            if s is None:
                return None
            out.append([days, months, nth, s, 0])
    return out


def parse_hours(text):
    if not text:
        return None
    t = text.strip()
    low = t.lower()
    if any(w in low for w in FR_DAYS):  # « Samedi matin », « Jeudi matin »
        days = [n for w, n in FR_DAYS.items() if w in low]
        slot = [[480, 750]] if "matin" in low else ([[840, 1140]] if "après" in low or "apres" in low else [])
        return [[days, [], [], slot, 0]]
    rules = []
    for part in t.split(";"):
        part = re.sub(r"(\d) ?- ?(\d)", r"\1-\2", part.replace("  ", " "))
        part = re.sub(r"(Mo|Tu|We|Th|Fr|Sa|Su) \[", r"\1[", part)
        r = parse_rule(part)
        if r is None:
            return None
        rules += r
    return rules or None


def reverse_cities(points):
    """Géocodage inverse en lot : [(id, lat, lon)] → {id: (commune, code postal)}."""
    out = {}
    for i in range(0, len(points), 2000):
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["id", "lat", "lon"])
        w.writerows(points[i:i + 2000])
        boundary = "----marchemalin"
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"data\"; filename=\"p.csv\"\r\n"
                f"Content-Type: text/csv\r\n\r\n{buf.getvalue()}\r\n--{boundary}--\r\n").encode()
        req = urllib.request.Request("https://api-adresse.data.gouv.fr/reverse/csv/", data=body,
                                     headers={**UA, "Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=300) as r:
            for row in csv.DictReader(io.StringIO(r.read().decode("utf-8"))):
                if row.get("result_city"):
                    out[row["id"]] = (row["result_city"], row.get("result_postcode", ""))
    return out


def main():
    elements = fetch_osm()
    items, unparsed = [], 0
    for e in elements:
        tags = e.get("tags", {})
        lat = e.get("lat", e.get("center", {}).get("lat"))
        lon = e.get("lon", e.get("center", {}).get("lon"))
        if lat is None or lon is None:
            continue
        oid = e["type"][0] + str(e["id"])
        raw = tags.get("opening_hours", "")
        rules = parse_hours(raw)
        if raw and rules is None:
            unparsed += 1
        name = tags.get("name:fr") or tags.get("name") or ""
        covered = tags.get("covered") == "yes" or "building" in tags or re.search(r"\bhalles?\b", name, re.I) is not None
        organic = tags.get("organic") in ("yes", "only")
        flags = (1 if covered else 0) | (2 if organic else 0)
        items.append({"id": oid, "lat": round(lat, 5), "lon": round(lon, 5), "name": name,
                      "city": tags.get("addr:city", ""), "cp": tags.get("addr:postcode", ""),
                      "rules": rules, "raw": raw if rules is None else "", "flags": flags,
                      "web": tags.get("website") or tags.get("contact:website") or tags.get("contact:facebook") or ""})
    cities = reverse_cities([(m["id"], m["lat"], m["lon"]) for m in items])
    for m in items:
        if m["id"] in cities:
            m["city"], m["cp"] = cities[m["id"]]
    items.sort(key=lambda m: m["id"])
    data = {"updated": date.today().isoformat(), "source": "© les contributeurs d'OpenStreetMap (ODbL)",
            "markets": [[m["id"], m["name"], m["lat"], m["lon"], m["city"], m["cp"], m["rules"] or [], m["raw"], m["flags"], m["web"]] for m in items]}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    with_hours = sum(1 for m in items if m["rules"])
    print(f"{len(items)} marchés, {with_hours} avec horaires, {unparsed} horaires non compris, {len(cities)} communes")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Tendances des prix des carburants pour l'app Plein Malin.

Source : archive annuelle officielle https://donnees.roulez-eco.fr/opendata/annee
(Licence Ouverte Etalab), mise à jour chaque nuit jusqu'à la veille.

Produit dans pleinmalin/data/ :
- tendances.json : prix moyen par jour (90 jours) en France et par département, par carburant.
- stations-tendance.json : pour chaque station et carburant, [prix il y a 7 jours, min 30 j, max 30 j].
Prix en millièmes d'euro (entiers) pour des fichiers compacts.
"""
import datetime as dt
import io
import json
import os
import sys
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

FUELS = {"Gazole": "gazole", "E10": "e10", "SP95": "sp95", "SP98": "sp98", "E85": "e85", "GPLc": "gplc"}
DAYS = 90
STALE_DAYS = 8  # un prix non mis à jour depuis plus longtemps n'entre pas dans les moyennes


def download(year=None):
    url = "https://donnees.roulez-eco.fr/opendata/annee" + (f"/{year}" if year else "")
    req = urllib.request.Request(url, headers={"User-Agent": "PleinMalin-tendances"})
    data = urllib.request.urlopen(req, timeout=300).read()
    z = zipfile.ZipFile(io.BytesIO(data))
    return z.open(z.namelist()[0])


def departement(cp):
    cp = (cp or "").strip()
    if len(cp) != 5:
        return None
    if cp.startswith("97"):
        return cp[:3]
    if cp.startswith("20"):
        return "2A" if cp < "20200" else "2B"
    return cp[:2]


def parse(stream, changes, deps):
    """changes[(id, fuel)] = liste de (datetime, prix en millièmes)."""
    for _, el in ET.iterparse(stream, events=("end",)):
        if el.tag != "pdv":
            continue
        sid = el.get("id")
        deps[sid] = departement(el.get("cp"))
        for p in el.findall("prix"):
            fuel = FUELS.get(p.get("nom"))
            if not fuel or not p.get("valeur") or not p.get("maj"):
                continue
            try:
                when = dt.datetime.fromisoformat(p.get("maj")[:19])
                value = round(float(p.get("valeur")) * (1000 if float(p.get("valeur")) < 10 else 1))
            except ValueError:
                continue
            if 300 <= value <= 4000:
                changes.setdefault((sid, fuel), []).append((when, value))
        el.clear()


def main(out_dir, local_files):
    today = dt.date.today()
    changes, deps = {}, {}
    if local_files:
        for f in local_files:
            parse(open(f, "rb"), changes, deps)
    else:
        if today.timetuple().tm_yday <= DAYS + 5:
            parse(download(today.year - 1), changes, deps)
        parse(download(), changes, deps)

    last = max(w for v in changes.values() for w, _ in v).date()
    days = [last - dt.timedelta(days=i) for i in range(DAYS - 1, -1, -1)]

    # Prix de chaque station en fin de journée (dernier prix connu, s'il est récent).
    series = {}
    for key, events in changes.items():
        events.sort()
        row, i, current, since = [], 0, None, None
        for day in days:
            end = dt.datetime.combine(day, dt.time(23, 59, 59))
            while i < len(events) and events[i][0] <= end:
                current, since = events[i][1], events[i][0].date()
                i += 1
            row.append(current if current is not None and (day - since).days <= STALE_DAYS else None)
        if any(v is not None for v in row):
            series[key] = row

    # Moyennes France / département.
    sums = {}
    for (sid, fuel), row in series.items():
        for zone in ("FR", deps.get(sid)):
            if zone is None:
                continue
            acc = sums.setdefault(zone, {}).setdefault(fuel, [[0, 0] for _ in days])
            for k, v in enumerate(row):
                if v is not None:
                    acc[k][0] += v
                    acc[k][1] += 1
    averages = {
        zone: {fuel: [round(s / n) if n >= 3 else None for s, n in acc] for fuel, acc in fuels.items()}
        for zone, fuels in sums.items()
    }
    trends = {
        "maj": last.isoformat(),
        "jours": [d.isoformat() for d in days],
        "france": averages.pop("FR", {}),
        "deps": dict(sorted(averages.items())),
    }

    # Par station : prix il y a 7 jours, min et max sur 30 jours.
    stations = {}
    for (sid, fuel), row in series.items():
        recent = [v for v in row[-30:] if v is not None]
        week_ago = row[-8]
        if not recent:
            continue
        stations.setdefault(sid, {})[fuel] = [week_ago, min(recent), max(recent)]

    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "tendances.json"), "w") as f:
        json.dump(trends, f, separators=(",", ":"))
    with open(os.path.join(out_dir, "stations-tendance.json"), "w") as f:
        json.dump({"maj": last.isoformat(), "s": stations}, f, separators=(",", ":"))
    print(f"{last}: {len(series)} séries, {len(stations)} stations, {len(trends['deps'])} départements")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "pleinmalin/data", sys.argv[2:])

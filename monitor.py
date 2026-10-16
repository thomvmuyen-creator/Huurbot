#!/usr/bin/env python3
"""Huurmonitor: checkt huursites op nieuwe woningen, zet ze op de website en stuurt een pushmelding."""
import json, os, re, sys, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

# ---------------- INSTELLINGEN ----------------
MAX_PRIJS = 1600
MIN_PRIJS = 400  # onder dit bedrag zijn het vrijwel altijd kamers/short-stay
PLAATSEN = [
    "ridderkerk", "hendrik-ido-ambacht", "papendrecht", "zwijndrecht",
    "barendrecht", "oud-beijerland", "puttershoek", "poortugaal", "rhoon",
    "heerjansdam", "alblasserdam", "dordrecht",
]
# 'url' met {plaats}  -> per plaats ophalen.
# 'urls'              -> vaste lijst pagina's (landelijk, nieuwste eerst); de plaats wordt uit de
#                        woning-URL gehaald via 'plaats_uit_url' en moet in PLAATSEN staan.
SITES = {
    "Huurportaal": {"url": "https://www.huurportaal.nl/huurwoningen/{plaats}?maxPrice={max}"},
    "Huurstunt": {"url": "https://www.huurstunt.nl/huren/{plaats}/",
                  "plaats_uit_url": r"/huren/in/([^/]+)"},
    "Ikwilhuren.nu": {
        "urls": [f"https://ikwilhuren.nu/aanbod/?sort=aanbodDESC&page={n}" for n in (1, 2, 3)],
        "plaats_uit_url": r"/object/(.+?)-\d{4}[a-z]{2}-"},
    "vb&t": {
        "urls": ["https://vbtverhuurmakelaars.nl/woningen"]
                + [f"https://vbtverhuurmakelaars.nl/woningen/{n}" for n in (2, 3, 4, 5)],
        "plaats_uit_url": r"/woning/([^/]+)"},
    "HS Makelaars": {
        "urls": ["https://www.hsmakelaars.nl/woningaanbod/huur"],
        "plaats_uit_url": r"/woningaanbod/huur/([^/]+)/"},
    "Directwonen": {"url": "https://directwonen.nl/huurwoningen-huren/{plaats}", "controleer_plaats": True},
}
# Advertenties waarvan de URL een van deze woorden bevat worden overgeslagen (kamers e.d.)
UITSLUITEN = ["/kamer/", "kamer-in", "kamer-te-huur", "kamers-", "kamer-huren", "kamers-huren",
              "short-stay", "shortstay"]
MAX_MELDINGEN_PER_RUN = 15
# ----------------------------------------------

STATE_FILE = "state.json"
STATE_VERSION = 5
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "nl-NL,nl;q=0.9",
}
PRIJS_RE = re.compile(r"€\s*([\d\.]{3,7})")
SLECHTE_TITELS = {"meer zien", "nieuw", "bekijk", "bekijken", "details", "lees meer",
                  "meer info", "bekijk woning", "bekijk details", "nl", "en", "reageer"}
SOORTEN = ("appartement", "studio", "huurwoning", "woning", "woonhuis", "kamer")
PLAATSEN_LANG_EERST = sorted(PLAATSEN, key=len, reverse=True)


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return None


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1, sort_keys=True)


def telegram(text):
    token, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat, "text": text}, timeout=20)
    if not r.ok:
        print("Telegram fout:", r.status_code, r.text)


def notify(titel, bericht, link=None, img=None):
    """Pushmelding via ntfy (en optioneel Telegram). Zonder NTFY_TOPIC alleen printen."""
    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        payload = {"topic": topic, "title": titel, "message": bericht, "tags": ["house"]}
        if link:
            payload["click"] = link
        if img:
            payload["attach"] = img
        try:
            r = requests.post("https://ntfy.sh", json=payload, timeout=20)
            if not r.ok:
                print("ntfy fout:", r.status_code, r.text)
        except Exception as e:
            print("ntfy fout:", e)
    else:
        print("[ntfy uit]", titel, bericht)
    telegram(f"{titel}\n{bericht}" + (f"\n{link}" if link else ""))


def alle_prijzen(tekst):
    out = []
    for m in PRIJS_RE.findall(tekst):
        cijfer = m.replace(".", "")
        if cijfer.isdigit() and 200 <= int(cijfer) <= 20000:
            out.append(int(cijfer))
    return out


def segmenten(pad):
    return [p for p in pad.split("/") if p]


def plaats_uit_pad(cfg, pad):
    """Zoek de plaats in de URL; alleen plaatsen uit PLAATSEN tellen mee."""
    m = re.search(cfg["plaats_uit_url"], pad)
    if not m:
        return None
    slug = m.group(1).lower()
    for p in PLAATSEN_LANG_EERST:
        if slug == p or slug.startswith(p + "-"):
            return p
    return None


def afbeelding(node, basis_url):
    """Eerste echte woningfoto in de kaart (geen logo's/iconen/placeholders)."""
    if node is None:
        return None
    for im in node.find_all("img"):
        for attr in ("src", "data-src", "data-lazy-src", "data-original", "srcset", "data-srcset"):
            v = (im.get(attr) or "").strip().split(" ")[0].split(",")[0]
            if not v or v.startswith("data:"):
                continue
            low = v.lower()
            if any(x in low for x in ("logo", "icon", "placeholder", "flag", ".svg", "loading", "avatar", "/content/images/")):
                continue
            return urljoin(basis_url, v)
    return None


DATUM_JSON = re.compile(r'"(?:datePublished|datePosted|dateCreated|date_posted|published_?at|publishedAt|created_?at|createdAt|'
                        r'listed_?at|publicationDate|publicatiedatum|first_?seen|activeFrom)"\s*:\s*"(\d{4}-\d{2}-\d{2}[^"]{0,30})"', re.I)
TREFW = r"(?:Geplaatst|Gepubliceerd|Online sinds|Toegevoegd|Publicatiedatum|Aangeboden sinds|Aangeboden op|Plaatsingsdatum)"
DATUM_TEKST = re.compile(TREFW + r"[^0-9]{0,25}(\d{1,2})[-/ .](\d{1,2})[-/ .](\d{4})", re.I)
MAANDEN = {m: i + 1 for i, m in enumerate("januari februari maart april mei juni juli augustus september oktober november december".split())}
DATUM_MAAND = re.compile(TREFW + r"[^0-9]{0,25}(\d{1,2}) (" + "|".join(MAANDEN) + r") (\d{4})", re.I)


def plausibele_img(u):
    low = u.lower()
    if any(x in low for x in ("logo", "icon", "placeholder", ".svg", "avatar", "favicon", "/content/images/")):
        return False
    return bool(re.search(r"\.(jpe?g|png|webp|avif)|image|img|media|photo|foto|thumb|cdn", low))


def naar_epoch(txt):
    try:
        d = datetime.fromisoformat(txt.strip().replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        ts = int(d.timestamp())
        return ts if time.time() - 3 * 365 * 86400 < ts <= time.time() + 3600 else None
    except Exception:
        return None


def detail_info(url):
    """Haalt van de woningpagina zelf de eerste foto en (indien vermeld) de publicatiedatum."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=12)
        if r.status_code != 200:
            return None, None, f"HTTP {r.status_code}"
    except Exception:
        return None, None, "geen verbinding"
    soup = BeautifulSoup(r.text, "html.parser")
    img = None
    for key in ("og:image", "og:image:secure_url", "twitter:image"):
        m = soup.find("meta", attrs={"property": key}) or soup.find("meta", attrs={"name": key})
        v = (m.get("content") or "").strip() if m else ""
        if v and plausibele_img(v):
            img = urljoin(r.url, v)
            break
    if not img:
        img = afbeelding(soup.find("main") or soup.body or soup, r.url)
    pub = None
    m = soup.find("meta", attrs={"property": "article:published_time"}) or soup.find(attrs={"itemprop": "datePublished"})
    if m:
        pub = naar_epoch(m.get("content") or m.get("datetime") or "")
    if not pub:
        for g in DATUM_JSON.findall(r.text):
            pub = naar_epoch(g)
            if pub:
                break
    if not pub:
        txt = soup.get_text(" ", strip=True)
        m = DATUM_TEKST.search(txt)
        if m:
            pub = naar_epoch(f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}")
        if not pub:
            m = DATUM_MAAND.search(txt)
            if m:
                pub = naar_epoch(f"{m.group(3)}-{MAANDEN[m.group(2).lower()]:02d}-{int(m.group(1)):02d}")
    return img, pub, "ok"


def mooie_titel(a, kaart, pad, plaats):
    t = a.get_text(" ", strip=True)
    if 4 <= len(t) <= 80 and "|" not in t and "€" not in t and t.lower() not in SLECHTE_TITELS:
        return t
    if kaart is not None:
        for h in kaart.find_all(["h1", "h2", "h3", "h4"]):
            ht = h.get_text(" ", strip=True)
            if 4 <= len(ht) <= 80 and ht.lower() not in SLECHTE_TITELS:
                return ht
    segs = segmenten(pad)
    kand = [x for x in segs if "-" in x] or segs
    slug = re.sub(r"[-_]p[0-9a-f]{5,6}$", "", kand[-1])
    if plaats and slug.startswith(plaats + "-"):
        slug = slug[len(plaats) + 1:]
    tekst = slug.replace("-", " ").strip().title()
    if segs and segs[0] in SOORTEN and not tekst.lower().startswith(segs[0]):
        tekst = f"{segs[0].capitalize()}: {tekst}"
    return tekst[:100]


def scrape(site, cfg, url, plaats):
    """Geeft (woningen in jouw plaatsen, aantal woningkaarten gezien).
    Een woning = interne link + 1-3 verschillende prijzen (EUR) in dezelfde kaart."""
    r = requests.get(url, headers=HEADERS, timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    soup = BeautifulSoup(r.text, "html.parser")
    basis = urlsplit(r.url)
    host = basis.netloc.replace("www.", "")
    pagina_pad = basis.path.rstrip("/")
    found, kaarten, interne, gezien = {}, 0, 0, set()
    for a in soup.find_all("a", href=True):
        parts = urlsplit(urljoin(r.url, a["href"]))
        if parts.netloc.replace("www.", "") != host:
            continue
        p = parts.path.rstrip("/")
        s = segmenten(p)
        if len(s) < 2 or p == pagina_pad:
            continue
        interne += 1
        key = f"{parts.scheme}://{parts.netloc}{p}"
        if key in found or key in gezien:
            continue
        # 1) prijs in de link zelf (hele kaart is een link), anders 2) klim omhoog naar de kaart
        kaart, prijs = None, None
        t = a.get_text(" ", strip=True)
        pr = alle_prijzen(t)
        if pr and len(set(pr)) <= 3:
            kaart, prijs = a, pr[0]
        else:
            node = a
            for _ in range(6):
                parent = node.parent
                if parent is None:
                    break
                tt = parent.get_text(" ", strip=True)
                if len(tt) > 900:
                    break
                node = parent
                pr = alle_prijzen(tt)
                if not pr:
                    continue
                if len(set(pr)) <= 3:
                    kaart, prijs = parent, pr[0]
                break
        if prijs is None:
            continue
        gezien.add(key)
        kaarten += 1
        if any(w in key.lower() for w in UITSLUITEN):
            continue
        if re.match(r"(?i)\s*(verhuurd|verkocht)", t):
            continue
        woonplaats = plaats
        if cfg.get("plaats_uit_url"):
            woonplaats = plaats_uit_pad(cfg, p)
            if woonplaats is None:
                continue
        if cfg.get("controleer_plaats"):
            tk = (kaart.get_text(" ", strip=True) if kaart is not None else t).lower().replace("-", " ")
            namen = [x for x in PLAATSEN_LANG_EERST if x.replace("-", " ") in tk]
            if not namen:
                continue
            if woonplaats not in namen:
                woonplaats = namen[0]
        found[key] = {"url": key, "titel": mooie_titel(a, kaart, p, woonplaats), "prijs": prijs,
                      "plaats": woonplaats, "site": site,
                      "img": afbeelding(kaart, r.url)}
    if kaarten == 0:
        titel = soup.title.get_text(strip=True)[:60] if soup.title else "-"
        js = " (JS-site?)" if ("__NEXT_DATA__" in r.text or 'id="__nuxt"' in r.text) else ""
        print(f"   debug: pagina {len(r.text)//1000}kB, titel '{titel}', "
              f"{interne} interne links, 0 met prijs{js}")
    return list(found.values()), kaarten


def schrijf_website(state):
    os.makedirs("docs", exist_ok=True)
    items = [dict(v, ts=v.get("published") or v["first_seen"]) for v in state["listings"].values()]
    items.sort(key=lambda x: x["ts"], reverse=True)
    data = {"updated": int(time.time()), "max_prijs": MAX_PRIJS, "listings": items, "status": state.get("status", {})}
    with open("docs/listings.json", "w") as f:
        json.dump(data, f, ensure_ascii=False)


def haal_site(site, cfg):
    """Haalt alle pagina's van één site op (sites zelf lopen parallel)."""
    if "urls" in cfg:
        doelen = [(u, None) for u in cfg["urls"]]
    else:
        doelen = [(cfg["url"].format(plaats=p, max=MAX_PRIJS), p) for p in PLAATSEN]
    uit = []
    for url, plaats in doelen:
        label = plaats or url.split("//")[1][:55]
        try:
            items, kaarten = scrape(site, cfg, url, plaats)
            fout = None
        except Exception as e:
            items, kaarten, fout = [], 0, str(e)
        uit.append((label, items, kaarten, fout))
        time.sleep(0.4)
    return uit


def main():
    test = "--test" in sys.argv
    state = load_state()
    if state is not None and state.get("v") != STATE_VERSION:
        state = None  # nieuwe versie: opnieuw beginnen
    eerste_run = state is None
    state = state or {"v": STATE_VERSION}
    state.setdefault("seen", {})
    state.setdefault("health", {})
    state.setdefault("listings", {})
    nieuw, gezien_nu = [], 0
    nu = int(time.time())

    with ThreadPoolExecutor(max_workers=len(SITES)) as ex:
        resultaten = dict(zip(SITES, ex.map(lambda kv: haal_site(*kv), SITES.items())))

    for site, cfg in SITES.items():
        kaarten_totaal, rank, n_in, n_fout = 0, 0, 0, 0
        for label, items, kaarten, fout in resultaten[site]:
            n_in += len(items)
            n_fout += 1 if fout else 0
            if fout:
                print(f"[{site}/{label}] fout: {fout}")
            kaarten_totaal += kaarten
            print(f"[{site}/{label}] {kaarten} woningkaarten, {len(items)} in jouw plaatsen")
            for it in items[:2]:
                print(f"   voorbeeld: EUR {it['prijs']} | {it['plaats']} | {it['titel']} | {it['url']}")
            for it in items:  # volgorde van de site = nieuwste eerst
                gezien_nu += 1
                if it["prijs"] > MAX_PRIJS or it["prijs"] < MIN_PRIJS:
                    continue
                if it["url"] not in state["seen"]:
                    state["seen"][it["url"]] = nu
                    it["first_seen"] = nu - rank
                    it["seed"] = eerste_run
                    rank += 1
                    state["listings"][it["url"]] = it
                    nieuw.append(it)
        state.setdefault("status", {})[site] = {"kaarten": kaarten_totaal, "in_plaatsen": n_in, "fouten": n_fout}
        h = state["health"].get(site, 0)
        h = h + 1 if kaarten_totaal == 0 else 0
        state["health"][site] = h
        if h == 3 and not test:
            notify("Huurbot waarschuwing",
                   f"{site} levert al 3 runs geen woningen op (site aangepast of blokkeert de bot?).")

    if not test:
        telling = {}
        for v in state["listings"].values():
            if v.get("img"):
                telling[v["img"]] = telling.get(v["img"], 0) + 1
        for v in state["listings"].values():
            if v.get("img") and telling[v["img"]] >= 3:
                v["img"], v["detail"], v["pogingen"] = None, False, 1
        todo = sorted((v for v in state["listings"].values() if not v.get("detail")),
                      key=lambda x: x["first_seen"], reverse=True)[:60]
        n_img = n_pub = 0
        fouten = {}
        with ThreadPoolExecutor(max_workers=8) as ex:
            resultaat = list(ex.map(lambda v: detail_info(v["url"]), todo))
        for v, (img, pub, st) in zip(todo, resultaat):
            v["pogingen"] = v.get("pogingen", 0) + 1
            if st == "ok" or v["pogingen"] >= 3:
                v["detail"] = True
            if st != "ok":
                fouten[st] = fouten.get(st, 0) + 1
            if img:
                v["img"] = img
                n_img += 1
            if pub:
                v["published"] = pub
                n_pub += 1
        state.setdefault("status", {})["_details"] = {"opgehaald": len(todo), "met_foto": n_img,
                                                    "met_datum": n_pub, "fouten": fouten}
        print(f"Details opgehaald: {len(todo)} woningen, {n_img} met foto, {n_pub} met publicatiedatum")

    if test:
        print(f"\nTest klaar: {gezien_nu} woningen gezien, {len(nieuw)} nieuw.")
        return

    if eerste_run:
        notify("Huurbot actief",
               f"Startpunt vastgelegd met {len(nieuw)} bestaande woningen. "
               f"Vanaf nu krijg je alleen nieuwe woningen (max €{MAX_PRIJS}).")
    else:
        for it in nieuw[:MAX_MELDINGEN_PER_RUN]:
            plaats = it["plaats"].replace("-", " ").title()
            gepl = ""
            if it.get("published"):
                gepl = "\nGeplaatst: " + datetime.fromtimestamp(it["published"]).strftime("%d-%m-%Y")
            notify(f"{plaats} · €{it['prijs']}", f"{it['titel']}\n{it['site']}{gepl}", it["url"], it.get("img"))
            time.sleep(0.5)
        if len(nieuw) > MAX_MELDINGEN_PER_RUN:
            notify("Huurbot", f"...en nog {len(nieuw) - MAX_MELDINGEN_PER_RUN} nieuwe woningen. Zie de website.")

    grens = nu - 30 * 86400
    state["listings"] = {k: v for k, v in state["listings"].items() if v["first_seen"] >= grens}
    if len(state["seen"]) > 5000:
        oud = sorted(state["seen"].items(), key=lambda x: x[1])[:-5000]
        for k, _ in oud:
            del state["seen"][k]
    save_state(state)
    schrijf_website(state)


if __name__ == "__main__":
    main()

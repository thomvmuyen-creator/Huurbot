#!/usr/bin/env python3
"""Huurmonitor: checkt huursites op nieuwe woningen en stuurt een Telegram-melding."""
import json, os, re, sys, time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

# ---------------- INSTELLINGEN ----------------
MAX_PRIJS = 1600
PLAATSEN = [
    "ridderkerk", "hendrik-ido-ambacht", "papendrecht", "zwijndrecht",
    "barendrecht", "oud-beijerland", "puttershoek", "poortugaal", "rhoon",
    "heerjansdam", "alblasserdam", "dordrecht",
]
# {plaats} wordt ingevuld. 'pattern' = regex waaraan een woninglink moet voldoen (alleen het pad).
SITES = {
    "Huurwoningen.nl": {"url": "https://www.huurwoningen.nl/in/{plaats}/?price=0-{max}"},
    "Huurportaal": {"url": "https://www.huurportaal.nl/huurwoningen/{plaats}?maxPrice={max}"},
    "Ikwilhuren.nu": {"url": "https://ikwilhuren.nu/huurwoningen/{plaats}/"},
    "Rentola": {"url": "https://rentola.nl/te-huur/{plaats}"},
    "Huurstunt": {"url": "https://www.huurstunt.nl/huren/{plaats}/"},
}
MAX_MELDINGEN_PER_RUN = 15
# ----------------------------------------------

STATE_FILE = "state.json"
STATE_VERSION = 2
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "nl-NL,nl;q=0.9",
}
PRIJS_RE = re.compile(r"€\s*([\d\.]{3,7})")


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
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={"chat_id": chat, "text": text}, timeout=20)
    if not r.ok:
        print("Telegram fout:", r.status_code, r.text)


def notify(titel, bericht, link=None):
    """Pushmelding via ntfy (en optioneel Telegram). Zonder NTFY_TOPIC alleen printen."""
    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        payload = {"topic": topic, "title": titel, "message": bericht, "tags": ["house"]}
        if link:
            payload["click"] = link
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


def parse_prijs(tekst):
    """Geeft de laagste plausibele maandprijs in de tekst, of None."""
    prijzen = alle_prijzen(tekst)
    return min(prijzen) if prijzen else None


def segmenten(pad):
    return [p for p in pad.split("/") if p]


def mooie_titel(a, kaart, pad):
    t = a.get_text(" ", strip=True)
    if len(t) >= 4:
        return t[:100]
    if kaart is not None:
        h = kaart.find(["h1", "h2", "h3", "h4"])
        if h and len(h.get_text(strip=True)) >= 4:
            return h.get_text(" ", strip=True)[:100]
    segs = segmenten(pad)
    kand = [x for x in segs if "-" in x] or segs
    return kand[-1].replace("-", " ")[:100]


def scrape(site, cfg, plaats):
    """Herkent woningen aan: interne link + een prijs (EUR) in dezelfde kaart."""
    url = cfg["url"].format(plaats=plaats, max=MAX_PRIJS)
    r = requests.get(url, headers=HEADERS, timeout=25)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    soup = BeautifulSoup(r.text, "html.parser")
    basis = urlsplit(r.url)
    host = basis.netloc.replace("www.", "")
    pagina_pad = basis.path.rstrip("/")
    found, interne = {}, 0
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
        if key in found:
            continue
        # klim omhoog tot de kaart met 1-2 prijzen (meer = meerdere woningen = geen kaart)
        node, kaart, prijs = a, None, None
        for _ in range(6):
            parent = node.parent
            if parent is None:
                break
            t = parent.get_text(" ", strip=True)
            if len(t) > 700:
                break
            node = parent
            aantal = len(alle_prijzen(t))
            if aantal == 0:
                continue
            if aantal <= 2:
                kaart, prijs = parent, parse_prijs(t)
            break
        if prijs is None:
            continue
        found[key] = {"url": key, "titel": mooie_titel(a, kaart, p), "prijs": prijs,
                      "plaats": plaats, "site": site}
    if not found:
        titel = soup.title.get_text(strip=True)[:60] if soup.title else "-"
        js = " (JS-site?)" if ("__NEXT_DATA__" in r.text or "id=\"__nuxt\"" in r.text) else ""
        print(f"   debug: pagina {len(r.text)//1000}kB, titel '{titel}', "
              f"{interne} interne links, 0 met prijs{js}")
    return list(found.values())


def schrijf_website(state):
    os.makedirs("docs", exist_ok=True)
    items = sorted(state["listings"].values(), key=lambda x: x["first_seen"], reverse=True)
    data = {"updated": int(time.time()), "max_prijs": MAX_PRIJS, "listings": items}
    with open("docs/listings.json", "w") as f:
        json.dump(data, f, ensure_ascii=False)


def main():
    test = "--test" in sys.argv
    state = load_state()
    if state is not None and state.get("v") != STATE_VERSION:
        state = None  # nieuwe scraper: opnieuw beginnen
    eerste_run = state is None
    state = state or {"v": STATE_VERSION}
    state.setdefault("seen", {})
    state.setdefault("health", {})
    state.setdefault("listings", {})
    nieuw, gezien_nu = [], 0
    nu = int(time.time())

    for site, cfg in SITES.items():
        totaal = 0
        for plaats in PLAATSEN:
            try:
                items = scrape(site, cfg, plaats)
                totaal += len(items)
            except Exception as e:
                items = []
                print(f"[{site}/{plaats}] fout: {e}")
            print(f"[{site}/{plaats}] {len(items)} woningen")
            for it in items[:2]:
                print(f"   voorbeeld: EUR {it['prijs']} | {it['titel']} | {it['url']}")
            for it in items:
                gezien_nu += 1
                if it["prijs"] is not None and it["prijs"] > MAX_PRIJS:
                    continue
                if it["url"] not in state["seen"]:
                    state["seen"][it["url"]] = nu
                    it["first_seen"] = nu
                    it["seed"] = eerste_run
                    state["listings"][it["url"]] = it
                    nieuw.append(it)
            time.sleep(1.5)
        h = state["health"].get(site, 0)
        h = h + 1 if totaal == 0 else 0
        state["health"][site] = h
        if h == 3 and not test:
            notify("Huurbot waarschuwing",
                   f"{site} levert al 3 runs geen woningen op (site aangepast of blokkeert de bot?).")

    if test:
        print(f"\nTest klaar: {gezien_nu} woningen gezien, {len(nieuw)} nieuw.")
        return

    if eerste_run:
        notify("Huurbot actief",
               f"Startpunt vastgelegd met {len(nieuw)} bestaande woningen. "
               f"Vanaf nu krijg je alleen nieuwe woningen (max €{MAX_PRIJS}).")
    else:
        for it in nieuw[:MAX_MELDINGEN_PER_RUN]:
            prijs = f"€{it['prijs']}" if it["prijs"] else "prijs onbekend"
            plaats = it["plaats"].replace("-", " ").title()
            notify(f"{plaats} · {prijs}", f"{it['titel']}\n{it['site']}", it["url"])
            time.sleep(0.5)
        if len(nieuw) > MAX_MELDINGEN_PER_RUN:
            notify("Huurbot", f"...en nog {len(nieuw) - MAX_MELDINGEN_PER_RUN} nieuwe woningen. Zie de website.")

    # opschonen: website toont max 30 dagen, 'seen' max 5000
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

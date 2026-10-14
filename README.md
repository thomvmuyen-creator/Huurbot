# Huurbot – installatie (±10 minuten)

Resultaat: een **website** (link om te delen met je vriendin) met alle nieuwe woningen,
plus **pushmeldingen** op jullie telefoons via de gratis app ntfy.

## 1. Meldingen (ntfy, geen account nodig)
1. Verzin een lange, unieke topic-naam, bijv. `huur-ridderkerk-k8x2q9m4v7` (iedereen die de naam weet kan meelezen, dus maak hem lastig te raden).
2. Installeer de app **ntfy** (iOS / Android) op jullie allebei, tik "+" en abonneer op die topic-naam.

## 2. GitHub
1. Maak een **Public** repository (nodig voor gratis GitHub Pages), bijv. `huurbot`.
2. Upload alle bestanden uit deze map, inclusief `.github` en `docs`.
3. **Settings → Secrets and variables → Actions → New repository secret**:
   `NTFY_TOPIC` = je topic-naam. (Telegram is optioneel: `TELEGRAM_TOKEN` en `TELEGRAM_CHAT_ID`.)
4. **Settings → Actions → General → Workflow permissions** → *Read and write permissions*.
5. **Settings → Pages** → Source: *Deploy from a branch* → Branch: `main`, map `/docs` → Save.

## 3. Starten
**Actions → Huurmonitor → Run workflow**. Je krijgt "Huurbot actief" op je telefoon.
Je website staat op `https://<jouw-gebruikersnaam>.github.io/huurbot/` (na ±1 minuut).
Stuur die link naar je vriendin en laat haar "Toevoegen aan beginscherm" kiezen, dan werkt het als een app.

## Testen
`pip install -r requirements.txt && python monitor.py --test`, of kijk in de Actions-log:
per site/plaats staat het aantal gevonden woningen. Staat ergens steeds 0, stuur me die log.

## Aanpassen
Bovenin `monitor.py`: `MAX_PRIJS`, `PLAATSEN`, `SITES`.

## Let op
- De repo is publiek: de woninglijst en je topic-naam (staat alleen in een secret, niet in de code) zijn dus niet gekoppeld.
  Zet geen persoonlijke gegevens in de repo.
- GitHub zet geplande workflows uit na 60 dagen zonder activiteit; de bot commit zelf bij nieuwe woningen.
- Bij 3 runs zonder resultaat voor een site krijg je een waarschuwing.

# WG-Gesucht Münster Watcher → Telegram

Dieser kleine Python-Watcher prüft per GitHub Actions regelmäßig eine
WG-Gesucht-Suche für Münster und sendet neue Inserate an Telegram.

## 1. Repository anlegen

1. Auf GitHub ein neues Repository erstellen.
2. Den Inhalt dieses Ordners hochladen.
3. Das Repository kann privat oder öffentlich sein.

## 2. Telegram-Bot anlegen

1. In Telegram `@BotFather` öffnen.
2. `/newbot` senden und den Anweisungen folgen.
3. Den ausgegebenen Bot-Token kopieren.
4. Dem neuen Bot selbst einmal eine Nachricht schicken.
5. Danach im Browser aufrufen:

   `https://api.telegram.org/botDEIN_TOKEN/getUpdates`

6. In der Antwort unter `message.chat.id` die Chat-ID kopieren.

## 3. GitHub Secrets setzen

Im Repository:

`Settings` → `Secrets and variables` → `Actions` → `Secrets`

Zwei Repository Secrets anlegen:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

Der Token gehört **nicht** in den Python-Code und nicht in Git.

## 4. WG-Gesucht-Suche festlegen

Der Standard ist:

`https://www.wg-gesucht.de/wg-zimmer/muenster`

Wenn du Preis, Einzugsdatum, Stadtteile usw. filtern möchtest:

1. WG-Gesucht im Browser öffnen.
2. Münster und deine Filter einstellen.
3. Die resultierende URL kopieren.
4. In GitHub zu
   `Settings` → `Secrets and variables` → `Actions` → `Variables`
   gehen.
5. Variable `WG_SEARCH_URL` mit dieser URL anlegen.

Falls keine Variable angelegt wird, verwendet das Skript die Standard-Suche.

## 5. Einmal manuell testen

Repository → `Actions` → `WG-Gesucht Watcher` → `Run workflow`.

Beim ersten erfolgreichen Lauf werden alle aktuell sichtbaren Inserate als
bereits bekannt gespeichert. Du erhältst nur eine Startmeldung und wirst
nicht mit alten Anzeigen zugespammt.

Danach wird `seen_ads.json` automatisch vom GitHub-Actions-Bot aktualisiert.

## 6. Automatische Prüfung

Die Workflow-Datei ist auf alle 5 Minuten eingestellt:

```yaml
- cron: "*/5 * * * *"
```

GitHub garantiert keine sekundengenaue Ausführung; geplante Workflows können
sich insbesondere bei hoher Auslastung verzögern.

## Fehler / Bot-Schutz

Falls WG-Gesucht die HTML-Struktur ändert oder eine Bot-/Captcha-Prüfung
ausliefert, schlägt der Action-Lauf sichtbar fehl, statt irrtümlich alle
Anzeigen als verschwunden oder neu zu behandeln.

Nicht aggressiver pollen. Fünf Minuten sind für GitHub Actions ohnehin das
kleinste Schedule-Intervall und belasten WG-Gesucht deutlich weniger als ein
dauernder Sekunden-Watcher.

## Telegram-Meldungen

Bei neuen Anzeigen versucht der Watcher zusätzlich folgende Angaben zu extrahieren:

- Stadtteil
- Preis
- Zimmergröße
- Einzugsdatum

Beispiel:

```text
🚨 NEUE WG IN MÜNSTER

📍 Kreuzviertel
💰 520 €
📐 14 m²
📅 frei ab 01.10.2026

Helles WG-Zimmer im Kreuzviertel

👉 https://www.wg-gesucht.de/...
```

Dazu wird nur bei **neu entdeckten Anzeigen** zusätzlich die Detailseite geladen.
Wenn ein Feld auf WG-Gesucht nicht eindeutig gefunden wird, lässt der Watcher
es einfach weg.


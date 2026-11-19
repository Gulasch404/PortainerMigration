# Portainer → Komodo Migration

Übernimmt Docker-Compose-Stacks, die von Portainer verwaltet werden, als Stacks in [Komodo](https://komodo.com). Das Script läuft direkt auf dem Docker-LXC, auf dem Portainer läuft. Es kopiert die Compose- und Env-Dateien, gleicht sie mit den laufenden Containern ab (Mounts, Env-Werte, Images) und legt den Stack in Komodo an. Deployt wird erst danach, von dir selbst in Komodo.

## Voraussetzungen

- Ausführung als **root** im Docker-LXC
- Python 3.9+
- Docker Compose v2
- `curl`. In minimalen LXCs fehlt es oft, dann `apt install -y curl`.

## Einzeiler (geführte Migration)

```bash
curl -fsSL https://raw.githubusercontent.com/Gulasch404/PortainerMigration/main/portainer-to-komodo.py | python3 -
```

Das Script führt dich durch alle Schritte und fragt vor jedem nach einer Bestätigung:

1. Stack auswählen (Nummer oder Name)
2. Dateien kopieren und mit den laufenden Containern abgleichen. Bei Problemen kannst du die Kopie im Zielordner anpassen und die Prüfung wiederholen, ohne neu zu starten.
3. Komodo Periphery starten, falls sie noch nicht läuft (fragt nach dem Onboarding Key)
4. Stack in Komodo registrieren (fragt nach API-Key und API-Secret)
5. Weiteren Stack migrieren oder, wenn alle übernommen sind, **lokales Portainer entfernen**

Ein Stack lässt sich direkt angeben: `... | python3 - migrate slotpilot`. Die Abfragen funktionieren auch über die Pipe, weil das Script direkt vom Terminal liest.

Wenn das Script als Datei liegen bleiben soll:

```bash
curl -fsSL -o /root/p2k.py https://raw.githubusercontent.com/Gulasch404/PortainerMigration/main/portainer-to-komodo.py && python3 /root/p2k.py
```

## Portainer entfernen

Am Ende der geführten Migration oder separat mit `... | python3 - remove-portainer`:

- Das geht nur, wenn **alle** Portainer-Stacks mit diesem Script in Komodo registriert wurden. Ohne Portainer könnte das Script die übrigen Stacks nicht mehr übernehmen.
- Gestoppt und gelöscht wird nur der Portainer-Container. Die Stack-Container laufen weiter.
- Die Portainer-Daten (`/data`-Mount) bleiben als Sicherung erhalten. Sie enthalten auch die Env-Dateien mit Secrets. Lösche sie selbst, sobald alles in Komodo läuft.
- Zur Bestätigung musst du den Containernamen eintippen.

## Einzelschritte

| Schritt | Befehl | Was passiert |
|---|---|---|
| 1 | `list` | Zeigt die Portainer-Stacks auf diesem LXC. |
| 2 | `stage <projekt>` | Kopiert die Compose- und Env-Dateien nach `/etc/komodo/stacks/<projekt>` und prüft sie gegen die laufenden Container. Die laufenden Container bleiben unverändert. |
| 3 | `agent` | Startet Komodo Periphery unter `/etc/komodo/periphery`. Fragt nach dem **Server Onboarding Key**. |
| 4 | `register <projekt>` | Prüft erneut und legt den Stack in Komodo an. Fragt nach **API-Key und API-Secret**. |
| 5 | in Komodo | Services prüfen, dann deployen. |
| 6 | `remove-portainer` | Optional: Portainer-Container entfernen, sobald alle Stacks registriert sind. |

Aufruf jeweils mit `... | python3 - <befehl>`, z. B. `... | python3 - stage slotpilot`.

## Optionen

Globale Optionen stehen **vor** dem Befehl, z. B. `python3 - --core https://komodo.example.com list`.

| Option | Bedeutung |
|---|---|
| `--core URL` | HTTPS-URL des Komodo Core (Standard: `https://komodo.m3stud.io`) |
| `--server NAME` | Servername in Komodo (Standard: Hostname des LXC). Bei `agent` und `register` gleich setzen. |

Optionen für `migrate`, `stage` und `register`:

| Option | Bedeutung |
|---|---|
| `--portainer CONTAINER` | Portainer-Container angeben, falls er nicht eindeutig erkannt wird |
| `--env-file DATEI` | Zusätzlicher lokaler Env-Export aus Portainer, falls im Stack-Verzeichnis keine Env-Datei liegt |
| `--require-mount PFAD` | Prüft, ob ein LXC-Mountpoint eingehängt ist (z. B. `/mnt/daten`). Bei `stage` und `register` jeweils angeben. |
| `--name NAME` | `migrate` und `register`: anderer Stack-Name in Komodo, falls der Name schon belegt ist |

Option für `agent` und `migrate`:

| Option | Bedeutung |
|---|---|
| `--version X.Y.Z` | Version von Komodo Periphery (Standard: `2.3.4`) |

## Wichtig

- **Den Stack danach nicht in Portainer löschen.** Portainer führt beim Löschen `docker compose down` für dasselbe Projekt aus und entfernt damit die Container, die jetzt Komodo verwaltet. Portainer am Ende stattdessen mit `remove-portainer` entfernen.
- **GitOps:** Bei Portainer-GitOps-Stacks zuerst die automatischen Updates in Portainer deaktivieren.
- **Private Images:** Den Registry-Zugang in Komodo vor dem ersten Deploy einrichten.
- **Images ohne Digest:** Ein Pull beim Deploy kann sie aktualisieren. Das Script weist darauf hin. Wenn du das nicht willst, schaltest du in Komodo den Pull für den ersten Deploy ab.
- **Unbenannte Volumes** (z. B. durch `VOLUME` im Image) führen zum Abbruch. Binde sie in der Compose-Kopie als externes Volume mit dem angezeigten Namen ein, sonst gehen die Daten verloren.
- **Relative Bind-Mounts**, die bei Portainer auf einen anderen Pfad zeigten, werden gemeldet. In der Kopie den Quellpfad auf den angezeigten bisherigen Pfad setzen und `stage` erneut ausführen.
- **Weitere Dateien:** Repo-Dateien oder Build-Kontexte, die über die Compose- und Env-Dateien hinausgehen, kopierst du bei Bedarf selbst in den Zielordner.
- **Wiederholte Läufe:** Vorhandene Kopien im Zielordner bleiben erhalten. Änderungen machst du direkt dort.
- **Stand fixieren:** Statt `main` kannst du in der URL einen Commit-Hash verwenden. Dann läuft überall dieselbe Version, und der kurze Cache der Raw-URLs spielt keine Rolle.

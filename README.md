# Portainer → Komodo Migration

Übernimmt Git-basierte Portainer-Stacks als **Git-Stacks** in [Komodo](https://komodo.com). Das Script läuft direkt auf dem Docker-LXC, auf dem Portainer läuft. Es liest Repo, Branch und Compose-Pfad aus Portainers Klon, gleicht die Compose-Konfiguration mit den laufenden Containern ab (Mounts, Env-Werte, Images) und legt den Stack in Komodo an. Komodo klont das Repo danach selbst und baut daraus. Deployt wird erst danach, von dir selbst in Komodo.

Stacks ohne Git-Quelle, also aus Portainers Web-Editor oder per Upload, legt das Script nach Rückfrage als **UI-Defined-Stack** an. Komodo speichert die Compose-Datei dann selbst. Ohne Rückfrage geht das mit `--ui-defined`.

Die Werte aus Portainers `stack.env` übernimmt Komodo als Environment des Stacks. Komodo schreibt sie beim Deploy wieder nach `stack.env`, `env_file: stack.env` funktioniert also weiter.

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
2. Git-Stack ermitteln und mit den laufenden Containern abgleichen. Bei Problemen korrigierst du im Repo, ziehst die Änderung in der Prüfkopie nach und prüfst erneut, ohne neu zu starten.
3. Komodo Periphery starten, falls sie noch nicht läuft (fragt nach dem Onboarding Key)
4. Stack in Komodo als Git-Stack registrieren (fragt einmal pro Lauf nach API-Key und API-Secret). Dabei wählst du aus den in Komodo hinterlegten Accounts:
   - den **Git-Account** für das Repo (`0` = öffentliches Repo)
   - den **Registry-Login** für private Images, z. B. auf `ghcr.io` (`0` = öffentlich). Gefragt wird pro Registry, die in der Compose-Datei vorkommt. Docker Hub nur dann, wenn dafür ein Account hinterlegt ist.
5. In Komodo die Services prüfen und deployen
6. Weiteren Stack migrieren oder, wenn alle übernommen sind, **lokales Portainer entfernen**

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
| 2 | `stage <projekt>` | Kopiert Portainers Klon als Prüfkopie nach `/etc/komodo/stacks/<projekt>` (dorthin, wo Komodo später klont) und prüft gegen die laufenden Container. Die laufenden Container bleiben unverändert. |
| 3 | `agent` | Startet Komodo Periphery unter `/etc/komodo/periphery`. Fragt nach dem **Server Onboarding Key**. |
| 4 | `register <projekt>` | Prüft erneut, löscht die Prüfkopie und legt den Git-Stack in Komodo an. Fragt nach **API-Key und API-Secret**. |
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
| `--name NAME` | Anderer Stack-Name in Komodo, falls der Projektname schon belegt ist. Bei `stage` und `register` gleich setzen. |
| `--repo URL` | Repo-URL, falls sie nicht aus Portainers Klon lesbar ist |
| `--branch NAME` | Branch, falls er nicht aus Portainers Klon lesbar ist |
| `--git-account NAME` | `migrate` und `register`: Git-Account in Komodo für private Repos. Ohne die Option erscheint eine Auswahl. Leerer Wert = öffentlich. |
| `--registry-account NAME` | `migrate` und `register`: Registry-Login für private Images. Ohne die Option erscheint eine Auswahl. |
| `--registry-provider DOMAIN` | Registry zum Account, z. B. `ghcr.io`. Nur nötig, wenn die Compose-Datei mehrere private Registries nutzt. |

Option für `agent` und `migrate`:

| Option | Bedeutung |
|---|---|
| `--version X.Y.Z` | Version von Komodo Periphery (Standard: `2.3.4`) |

## Wichtig

- **Den Stack danach nicht in Portainer löschen.** Portainer führt beim Löschen `docker compose down` für dasselbe Projekt aus und entfernt damit die Container, die jetzt Komodo verwaltet. Portainer am Ende stattdessen mit `remove-portainer` entfernen.
- **GitOps:** Zuerst die automatischen Updates in Portainer deaktivieren. Für Auto-Deploy bei Push trägst du in Komodo den Webhook des Stacks im Repo ein.
- **Branch-Stand:** Geprüft wird der Commit, auf dem Portainer zuletzt lief. Komodo deployt den aktuellen Stand des Branches.
- **Private Repos und Images:** Vor der Migration in Komodo die Accounts anlegen, also einen Git-Account mit Token für den Git-Provider und einen Registry-Account. Für `ghcr.io` ist das der GitHub-Username plus ein Token mit `read:packages`. Dann erscheinen sie in der Auswahl.
- **Nur ein Registry-Login pro Stack:** Nutzt ein Stack mehrere private Registries, wird nur die erste gewählte eingetragen.
- **Images ohne Digest:** Ein Pull beim Deploy kann sie aktualisieren. Das Script weist darauf hin. Wenn du das nicht willst, schaltest du in Komodo den Pull für den ersten Deploy ab.
- **Unbenannte Volumes** (z. B. durch `VOLUME` im Image) führen zum Abbruch. Binde sie in der Compose-Datei im Repo als externes Volume mit dem angezeigten Namen ein, sonst gehen die Daten verloren.
- **Relative Bind-Mounts**, die bei Portainer auf einen anderen Pfad zeigten, werden gemeldet. Komodo löst sie relativ zu seinem eigenen Klon auf. Den Quellpfad im Repo auf den angezeigten bisherigen absoluten Pfad setzen.
- **Korrekturen** gehören ins Git-Repo. Zum erneuten Prüfen dieselbe Änderung in der Prüfkopie unter `/etc/komodo/stacks/<projekt>` nachziehen. Die Prüfkopie bleibt bis zur Registrierung erhalten.
- **Stand fixieren:** Statt `main` kannst du in der URL einen Commit-Hash verwenden. Dann läuft überall dieselbe Version, und der kurze Cache der Raw-URLs spielt keine Rolle.

#!/usr/bin/env python3
"""Portainer-Compose-Projekte auf demselben Docker-LXC in Komodo übernehmen.

Geführt in einem Schritt (mit Bestätigungen):
  python3 portainer-to-komodo.py

Einzeln:
  python3 portainer-to-komodo.py list
  python3 portainer-to-komodo.py stage slotpilot
  python3 portainer-to-komodo.py agent
  python3 portainer-to-komodo.py register slotpilot
  python3 portainer-to-komodo.py remove-portainer

Als root im Docker-LXC ausführen. Benötigt Python 3.9+ und Docker Compose v2.
agent fragt nach einem Komodo Server Onboarding Key. register fragt nach einem
Komodo API-Key und API-Secret mit Berechtigung zum Lesen des Servers und
Anlegen von Stacks. Danach in Komodo die Services prüfen und selbst deployen.
Bei Portainer-GitOps zuerst automatische Updates für diesen Stack deaktivieren.
Für private Images den Registry-Zugang vor dem ersten Deploy prüfen.

stage kopiert die Compose-Dateien und vorhandene .env/stack.env. Weitere
Repo-Dateien und Build-Kontexte bei Bedarf in den angezeigten Zielordner
übernehmen. Bei einem abweichenden relativen Bind-Mount den Quellpfad in der
Kopie auf den angezeigten bisherigen absoluten LXC-Pfad setzen und erneut prüfen.
Fehlt die Env-Datei, kann ein lokaler Portainer-Export über --env-file verwendet
werden. Die Dateien werden im Zielordner bearbeitet. Vorhandene Kopien bleiben
bei einem weiteren Lauf erhalten. --require-mount prüft einen LXC-Mountpoint,
z. B. stage slotpilot --require-mount /mnt/daten. Die gleiche Option vor der
Registrierung erneut setzen. Ein späteres Deploy prüft vorab den Mountpoint
erneut manuell, wenn die Anwendung von diesem Mount abhängt.
"""
import argparse
import getpass
import json
import os
import re
import shlex
import socket
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

CORE = "https://komodo.m3stud.io"
ROOT = Path("/etc/komodo")
AGENT = ROOT / "periphery"
DOCKER = ["docker", "--host", "unix:///var/run/docker.sock"]
PROJECT_LABEL = "com.docker.compose.project"
PROJECT_RE = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")
SAFE_PATH = re.compile(r"[A-Za-z0-9_./-]+\Z")
ANONYMOUS_VOLUME = re.compile(r"[0-9a-f]{64}\Z")
REGISTERED_MARKER = ".portainer-import-registered"
AGENT_YAML = """services:
  periphery:
    image: ghcr.io/moghtech/komodo-periphery:${KOMODO_PERIPHERY_VERSION}
    init: true
    restart: unless-stopped
    environment:
      PERIPHERY_CORE_ADDRESS: ${KOMODO_CORE_URL}
      PERIPHERY_CONNECT_AS: ${KOMODO_SERVER_NAME}
      PERIPHERY_ONBOARDING_KEY: ${KOMODO_ONBOARDING_KEY}
      PERIPHERY_CORE_PUBLIC_KEYS: file:/config/keys/core.pub
      PERIPHERY_ROOT_DIRECTORY: /etc/komodo
    volumes:
      - keys:/config/keys
      - /var/run/docker.sock:/var/run/docker.sock
      - /proc:/proc
      - /etc/komodo:/etc/komodo
volumes:
  keys:
"""


class MigrationError(Exception):
    pass


def run(command, cwd=None, diagnostics=None):
    # Keep login-shell and COMPOSE_* values out of Compose interpolation.
    env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "LC_ALL") if key in os.environ}
    result = subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True)
    if diagnostics is not None:
        diagnostics.write_text(result.stderr)
    if result.returncode:
        hint = f" Details stehen lokal in {diagnostics}." if diagnostics else ""
        raise MigrationError(f"{shlex.join(command[:6])} fehlgeschlagen ({result.returncode}).{hint}")
    return result.stdout


def install_file(path, contents):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise MigrationError(f"Unerwarteter Symlink {path}")
    try:
        with path.open("xb") as handle:
            handle.write(contents)
    except FileExistsError:
        if not path.is_file():
            raise MigrationError(f"Keine reguläre Datei unter {path}")


def containers(project):
    ids = run([*DOCKER, "ps", "-aq", "--filter", f"label={PROJECT_LABEL}={project}"]).split()
    return json.loads(run([*DOCKER, "inspect", *ids])) if ids else []


def portainer_mount(container_name):
    if container_name:
        matches = json.loads(run([*DOCKER, "inspect", "--type", "container", container_name]))
    else:
        ids = run([*DOCKER, "ps", "-aq"]).split()
        template = '{{.Id}} {{.Config.Image}}'
        images = run([*DOCKER, "inspect", "--format", template, *ids]).splitlines() if ids else []
        matches = []
        for line in images:
            container_id, image = line.split(maxsplit=1)
            if re.search(r"(?:^|/)portainer/(?:portainer-ce|portainer-ee|portainer)(?=[:@]|$)", image):
                matches.extend(json.loads(run([*DOCKER, "inspect", container_id])))
    if len(matches) != 1:
        raise MigrationError("Portainer nicht eindeutig gefunden. --portainer CONTAINER angeben.")
    data_mounts = [Path(m["Source"]) for m in matches[0]["Mounts"] if m["Destination"] == "/data"]
    if len(data_mounts) != 1 or not data_mounts[0].is_dir():
        raise MigrationError("Portainer-Mount /data auf dem LXC nicht gefunden.")
    return matches[0], data_mounts[0]


def project_paths(group):
    sets = set()
    for container in group:
        labels = container["Config"]["Labels"]
        if labels.get("com.docker.compose.oneoff", "").lower() == "true":
            continue
        working = Path(labels.get("com.docker.compose.project.working_dir", ""))
        files = labels.get("com.docker.compose.project.config_files", "").split(",")
        sets.add((working, tuple(Path(file) for file in files)))
    if len(sets) != 1:
        raise MigrationError("Die Projektcontainer haben unterschiedliche Compose-Dateien.")
    return sets.pop()


def copy_project(project, group, portainer_data, extra_env):
    working, source_files = project_paths(group)
    try:
        relative_workdir = working.relative_to("/data/compose")
        relative_files = [str(file.relative_to(working)) for file in source_files]
    except ValueError as error:
        raise MigrationError("Compose-Dateien liegen außerhalb des Portainer-Stack-Verzeichnisses.") from error
    if not relative_workdir.parts or ".." in relative_workdir.parts:
        raise MigrationError("Ungültiges Portainer-Stack-Verzeichnis.")
    source = portainer_data / "compose" / relative_workdir
    target = ROOT / "stacks" / project
    target.mkdir(parents=True, exist_ok=True)
    for file in relative_files:
        original = source / file
        if not SAFE_PATH.fullmatch(file) or ".." in Path(file).parts or not original.is_file():
            raise MigrationError(f"Compose-Datei fehlt oder ist ungeeignet: {file}")
        install_file(target / file, original.read_bytes())

    env_files = []
    for name in (".env", "stack.env"):
        if (source / name).is_file():
            install_file(target / name, (source / name).read_bytes())
            env_files.append(name)
    local_override = target / "portainer-import.env"
    if extra_env:
        data = extra_env.read_bytes()
        if local_override.exists() and local_override.read_bytes() != data:
            raise MigrationError(f"{local_override} existiert bereits. Bei Bedarf lokal anpassen.")
        install_file(local_override, data)
    if local_override.is_file():
        env_files.append(local_override.name)

    for name in env_files:
        (target / name).chmod(0o600)
    print(f"Ziel: {target}")
    print("Env-Dateien: " + (", ".join(env_files) if env_files else "keine gefunden"))
    return target, relative_files, env_files


def compose_model(project, target, files, env_files, agent_id=None):
    options = ["-p", project]
    # An explicit env file prevents Compose from reusing a stray .env in the working directory.
    # Komodo's generated file has a distinct name so it cannot replace an imported file.
    if not env_files:
        env_files = ["portainer-import-empty.env"]
        install_file(target / env_files[0], b"")
    for name in env_files:
        options += ["--env-file", name]
    for file in files:
        options += ["-f", file]
    command = [*DOCKER, "compose"]
    if agent_id:
        command = [*DOCKER, "exec", "--workdir", str(target), agent_id, "docker", "compose"]
    log = target / ".portainer-import-check.log"
    model = json.loads(run(
        [*command, *options, "--profile", "*", "config", "--format", "json"],
        cwd=target, diagnostics=log,
    ))
    if "is not set. Defaulting to a blank string" in log.read_text():
        raise MigrationError(f"Compose meldet fehlende Variablen. Details lokal in {log}.")
    return model


def verify_required_mounts(paths):
    if not paths:
        return
    mounted = set()
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        encoded = line.split()[4]
        mounted.add(re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), encoded))
    for path in paths:
        if not path.is_absolute() or str(path.resolve()) not in mounted:
            raise MigrationError(f"Mountpoint ist im LXC nicht eingehängt: {path}")
        print(f"Mountpoint aktiv: {path}")


def verify_model(model, group, target):
    services = model["services"]
    volume_names = model.get("volumes", {})
    existing_services = set()
    problems = []
    for item in group:
        labels = item["Config"]["Labels"]
        if labels.get("com.docker.compose.oneoff", "").lower() == "true":
            continue
        service = labels.get("com.docker.compose.service")
        existing_services.add(service)
        name = item["Name"].lstrip("/")
        if service not in services:
            problems.append(f"{name}: Service fehlt in Compose.")
            continue
        cfg = services[service]
        actual_mounts = {mount["Destination"]: mount for mount in item["Mounts"]}
        checked = set()
        for mount in cfg.get("volumes") or []:
            kind, dest = mount["type"], mount["target"]
            if kind not in {"bind", "volume"}:
                continue
            checked.add(dest)
            actual = actual_mounts.get(dest, {})
            source = mount.get("source")
            if kind == "bind":
                expected = source
                observed = actual.get("Source")
                same = (expected and observed and Path(expected).exists()
                        and Path(expected).resolve() == Path(observed).resolve())
            else:
                expected = volume_names.get(source, {}).get("name")
                observed = actual.get("Name")
                same = expected and expected == observed
            same_access = actual.get("RW") == (not mount.get("read_only", False))
            if actual.get("Type") != kind or not same or not same_access:
                problems.append(f"{name} {dest}: bisher {observed or '–'}, neu {expected or 'anonym/fehlt'} (Quelle oder Lesemodus abweichend).")
            else:
                print(f"{name} {dest}: {expected}")

        for dest, mount in actual_mounts.items():
            if dest in checked or mount["Type"] not in {"bind", "volume"}:
                continue
            volume = mount.get("Name") or ""
            if mount["Type"] == "volume" and ANONYMOUS_VOLUME.fullmatch(volume):
                problems.append(
                    f"{name} {dest}: unbenanntes Volume {volume[:12]} (vermutlich VOLUME im Image). "
                    f"In Compose als externes Volume mit name: {volume} einbinden, sonst gehen die Daten verloren."
                )
            else:
                problems.append(f"{name} {dest}: bisheriger Mount nicht eindeutig in Compose definiert.")

        image = cfg.get("image")
        if image and not cfg.get("build"):
            current = item["Config"].get("Image")
            if image != current:
                problems.append(f"{name}: Image bisher {current}, neu {image}.")
            else:
                local = run([*DOCKER, "image", "ls", "-q", "--no-trunc", image]).split()
                if item["Image"] not in local:
                    problems.append(
                        f"{name}: Tag {image} fehlt lokal oder zeigt auf ein anderes Image als der laufende Container. "
                        "Ein Deploy würde das Image wechseln."
                    )
                elif not re.search(r"@sha256:[0-9a-f]{64}\Z", image):
                    print(f"Hinweis {name}: {image} ist nicht per Digest fixiert. Pull beim Deploy kann aktualisieren.")
        live_env = dict(entry.split("=", 1) for entry in item["Config"].get("Env") or [] if "=" in entry)
        for key, value in (cfg.get("environment") or {}).items():
            if value is not None and live_env.get(key) != str(value):
                problems.append(f"{name}: abweichender Wert für Env-Variable {key}.")

    for name, cfg in services.items():
        if name not in existing_services and not cfg.get("profiles"):
            problems.append(f"{name}: Service ohne bisherigen Projektcontainer.")
        build = cfg.get("build")
        if build and isinstance(build, dict):
            context = Path(build.get("context", "."))
            dockerfile = context / build.get("dockerfile", "Dockerfile")
            if (not context.is_absolute() or not context.is_dir()
                    or not context.resolve().is_relative_to(target)
                    or (not build.get("dockerfile_inline") and not dockerfile.is_file())
                    or build.get("additional_contexts")):
                problems.append(f"{name}: Build-Kontext oder Dockerfile lokal prüfen.")
    if problems:
        raise MigrationError("\n".join(problems))


def agent_container():
    if not (AGENT / "compose.yaml").is_file():
        raise MigrationError("Zuerst agent ausführen oder Periphery unter /etc/komodo/periphery einrichten.")
    ids = run([*DOCKER, "compose", "ps", "-q", "periphery"], cwd=AGENT).split()
    if len(ids) != 1:
        raise MigrationError("Periphery läuft nicht eindeutig. In Komodo und auf dem LXC prüfen.")
    return ids[0]


def start_agent(args):
    if not (AGENT / "compose.yaml").exists():
        AGENT.mkdir(parents=True, exist_ok=True)
        if not (AGENT / ".env").exists():
            key = getpass.getpass("Server Onboarding Key: ").strip()
            if not re.fullmatch(r"[A-Za-z0-9_+=/-]+", key):
                raise MigrationError("Das Format des Onboarding-Keys ist ungültig.")
            values = (
                f"KOMODO_CORE_URL={args.core}\n"
                f"KOMODO_SERVER_NAME={args.server}\n"
                f"KOMODO_PERIPHERY_VERSION={args.version}\n"
                f"KOMODO_ONBOARDING_KEY={key}\n"
            )
            install_file(AGENT / ".env", values.encode())
        install_file(AGENT / "compose.yaml", AGENT_YAML.encode())
    run([*DOCKER, "compose", "up", "-d", "--no-recreate", "periphery"], cwd=AGENT,
        diagnostics=AGENT / ".portainer-import-agent.log")
    print(f"Periphery läuft lokal ({agent_container()[:12]}). Verbindung in Komodo unter Servers prüfen.")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def komodo(core, category, operation, data, credentials):
    request = Request(
        f"{core}/{category}/{operation}",
        data=json.dumps(data).encode(),
        headers={"Content-Type": "application/json",
                 "X-Api-Key": credentials[0], "X-Api-Secret": credentials[1]},
        method="POST",
    )
    try:
        with build_opener(NoRedirect).open(request, timeout=20) as response:
            return json.load(response)
    except HTTPError as error:
        detail = ""
        try:
            body = error.read(2000).decode("utf-8", "replace")
            try:
                parsed = json.loads(body)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict):
                body = parsed.get("error") or parsed.get("message") or body
            body = " ".join(str(body).split())[:500]
            detail = f" Antwort: {body}" if body else ""
        except OSError:
            pass
        raise MigrationError(
            f"Komodo {operation}: HTTP {error.code}.{detail} API-Zugang und Berechtigungen prüfen."
        ) from error
    except URLError as error:
        raise MigrationError(f"Core nicht erreichbar bei {operation}. URL und TLS prüfen.") from error


def register(args, target, files, env_files, agent_id):
    credentials = tuple(getpass.getpass(prompt).strip() for prompt in ("Komodo API Key: ", "Komodo API Secret: "))
    if not all(credentials):
        raise MigrationError("API-Key und API-Secret sind erforderlich.")

    def api(category, operation, **data):
        return komodo(args.core, category, operation, data, credentials)

    server = api("read", "GetServer", server=args.server)
    server_id = server.get("_id") or server.get("id")
    if isinstance(server_id, dict):
        server_id = server_id.get("$oid")
    if not server_id:
        raise MigrationError("Komodo hat keine Server-ID geliefert.")
    remote = api("read", "ListContainers", server=server_id)
    if not any(
        (item.get("id") or "") and (item["id"].startswith(agent_id) or agent_id.startswith(item["id"]))
        for item in remote
    ):
        raise MigrationError("Komodo sieht diesen Periphery-Container auf dem ausgewählten Server nicht.")
    name = args.name or args.project
    config = {
        "server_id": server_id,
        "project_name": args.project,
        "files_on_host": True,
        "run_directory": str(target),
        "file_paths": files,
        "env_file_path": ".komodo-generated.env",
        "additional_env_files": [
            {"path": file, "track": False}
            for file in env_files or ["portainer-import-empty.env"]
        ],
    }
    for stack in api("read", "ListFullStacks", limit=0):
        existing = stack["config"]
        same_project = (
            existing.get("server_id") in (server_id, args.server)
            and not existing.get("swarm_id")
            and (existing.get("project_name") or stack["name"]) == args.project
        )
        if stack["name"] == name or same_project:
            if all(existing.get(key) == value for key, value in config.items()):
                (target / REGISTERED_MARKER).write_text(stack["name"] + "\n")
                print(f"Bereits registriert: {stack['name']}")
                return
            raise MigrationError(f"Stack {stack['name']} existiert bereits mit anderer Konfiguration.")
    result = api("write", "CreateStack", name=name, config=config)
    (target / REGISTERED_MARKER).write_text(result["name"] + "\n")
    print(f"Registriert: {result['name']}. Services in Komodo prüfen, dann dort deployen.")


def ask(prompt):
    # Read from the terminal, not stdin: with "curl ... | python3 -" stdin is the script itself.
    try:
        with open("/dev/tty") as tty_in, open("/dev/tty", "w") as tty_out:
            tty_out.write(prompt)
            tty_out.flush()
            answer = tty_in.readline()
    except OSError as error:
        raise MigrationError("Keine interaktive Konsole verfügbar. Einzelbefehle verwenden.") from error
    if not answer:
        raise EOFError("Eingabe beendet.")
    return answer.strip()


def confirm(prompt):
    return ask(f"{prompt} [j/N] ").lower() in {"j", "ja", "y", "yes"}


def portainer_projects(portainer_project):
    projects = json.loads(run([*DOCKER, "compose", "ls", "--all", "--format", "json"]))
    return [entry for entry in projects
            if entry["Name"] != portainer_project and entry["ConfigFiles"].startswith("/data/compose/")]


def prepare(args, portainer_project, data, agent_id=None):
    if not PROJECT_RE.fullmatch(args.project or "") or args.project == portainer_project:
        raise MigrationError("Bitte einen gültigen Anwendungs-Stack angeben.")
    verify_required_mounts(args.require_mount)
    group = containers(project=args.project)
    if not group:
        raise MigrationError(f"Keine Container für Projekt {args.project} gefunden.")
    target, files, env_files = copy_project(args.project, group, data, args.env_file)
    model = compose_model(args.project, target, files, env_files, agent_id)
    verify_model(model, group, target)
    return target, files, env_files


def pending_projects(portainer_project):
    """Portainer stacks that have not been registered in Komodo by this script yet."""
    return [entry for entry in portainer_projects(portainer_project)
            if not (ROOT / "stacks" / entry["Name"] / REGISTERED_MARKER).is_file()]


def remove_portainer(portainer, data, portainer_project):
    name = portainer["Name"].lstrip("/")
    remaining = [entry["Name"] for entry in pending_projects(portainer_project)]
    if remaining:
        raise MigrationError(
            "Noch nicht übernommen: " + ", ".join(remaining) + ". Ohne Portainer kann das Script "
            "diese Stacks nicht mehr übernehmen. Zuerst migrieren, dann Portainer entfernen."
        )
    print(f"Der Container {name} wird gestoppt und entfernt. Die Stack-Container laufen weiter.")
    print(f"Die Portainer-Daten unter {data} bleiben erhalten (Sicherung, enthält auch Env-Dateien mit Secrets).")
    if ask(f"Zum Bestätigen den Containernamen eingeben ({name}): ") != name:
        raise MigrationError("Nicht bestätigt. Portainer bleibt installiert.")
    run([*DOCKER, "stop", portainer["Id"]])
    run([*DOCKER, "rm", portainer["Id"]])
    print(f"Portainer entfernt. Die Daten unter {data} kannst du später löschen, sobald alles in Komodo läuft.")


def migrate(args, portainer, portainer_project, data):
    while True:
        migrate_one(args, portainer_project, data)
        remaining = pending_projects(portainer_project)
        args.project, args.name = None, None
        if remaining:
            print(f"\nNoch nicht übernommen: {', '.join(entry['Name'] for entry in remaining)}")
            if not confirm("Weiteren Stack migrieren?"):
                return
            continue
        print("\nAlle Portainer-Stacks sind übernommen.")
        if confirm("Lokales Portainer jetzt entfernen (Container stoppen und löschen, Daten bleiben)?"):
            remove_portainer(portainer, data, portainer_project)
        return


def migrate_one(args, portainer_project, data):
    if not args.project:
        entries = pending_projects(portainer_project) or portainer_projects(portainer_project)
        if not entries:
            raise MigrationError("Keine Portainer-Stacks auf diesem LXC gefunden.")
        print("Portainer-Stacks auf diesem LXC:")
        for number, entry in enumerate(entries, 1):
            print(f"  {number}) {entry['Name']}\t{entry['Status']}")
        choice = ask("Welcher Stack soll migriert werden? Nummer oder Name: ")
        if choice.isdigit() and 1 <= int(choice) <= len(entries):
            choice = entries[int(choice) - 1]["Name"]
        args.project = choice

    print(f"\n[1/3] {args.project}: Dateien kopieren und mit den laufenden Containern abgleichen")
    while True:
        try:
            prepare(args, portainer_project, data)
            break
        except MigrationError as error:
            print(f"\nProbleme gefunden:\n{error}\n", file=sys.stderr)
            if not confirm("Dateien im Zielordner angepasst? Erneut prüfen?"):
                raise MigrationError("Migration angehalten. Die Kopien bleiben erhalten, ein neuer Lauf setzt dort fort.")
    print("Prüfung erfolgreich.")

    print("\n[2/3] Komodo Periphery")
    try:
        agent_id = agent_container()
        print(f"Periphery läuft bereits ({agent_id[:12]}).")
    except MigrationError:
        if not confirm(f"Periphery ist noch nicht aktiv. Jetzt als Server {args.server} mit {args.core} verbinden?"):
            raise MigrationError("Vor dem Start von Periphery angehalten.")
        start_agent(args)
        if not confirm("Ist der Server in Komodo unter Servers verbunden?"):
            raise MigrationError("Angehalten. Verbindung in Komodo prüfen und erneut starten.")
        agent_id = agent_container()

    name = args.name or args.project
    print(f"\n[3/3] Stack {name} in Komodo anlegen")
    if not confirm(f"Stack {name} jetzt in {args.core} registrieren?"):
        raise MigrationError("Vor der Registrierung angehalten. Ein neuer Lauf setzt dort fort.")
    target, files, env_files = prepare(args, portainer_project, data, agent_id)
    register(args, target, files, env_files, agent_id)
    print("\nFertig. In Komodo die Services prüfen und dann dort deployen.")
    print("Den Stack danach nicht in Portainer löschen: Das würde die Container per compose down entfernen.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--core", default=CORE, help="HTTPS-URL des Komodo Core")
    parser.add_argument("--server", default=socket.gethostname().split(".")[0], help="Servername in Komodo")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("list", help="Lokale Portainer-Projekte anzeigen").add_argument("--portainer")
    commands.add_parser("agent", help="Komodo Periphery starten").add_argument("--version", default="2.3.4")
    commands.add_parser("remove-portainer",
                        help="Portainer-Container entfernen, wenn alle Stacks übernommen sind (Daten bleiben)"
                        ).add_argument("--portainer")
    for command in ("migrate", "stage", "register"):
        helptext = "Geführte Migration mit Bestätigungen (Standard ohne Befehl)" if command == "migrate" else None
        part = commands.add_parser(command, help=helptext)
        part.add_argument("project", nargs="?" if command == "migrate" else None,
                          help="Bisheriger Docker-Compose-Projektname")
        part.add_argument("--portainer", help="Name oder ID des lokalen Portainer-Containers")
        part.add_argument("--env-file", type=Path, help="Zusätzlicher lokaler Portainer-Env-Export")
        part.add_argument("--require-mount", action="append", type=Path, default=[])
        if command in ("migrate", "register"):
            part.add_argument("--name", help="Anzeigename des Stacks bei Namenskollision")
        if command == "migrate":
            part.add_argument("--version", default="2.3.4", help="Version von Komodo Periphery")
    args = parser.parse_args()
    if args.command is None:
        args.command = "migrate"
    defaults = {"project": None, "portainer": None, "env_file": None, "require_mount": [],
                "name": None, "version": "2.3.4"}
    for key, value in defaults.items():
        if not hasattr(args, key):
            setattr(args, key, value)
    if os.geteuid() != 0:
        raise MigrationError("Bitte als root im Docker-LXC ausführen.")
    os.umask(0o077)
    args.core = args.core.rstrip("/")
    url = urlsplit(args.core)
    if (url.scheme != "https" or not url.hostname or url.username or url.password
            or url.query or url.fragment or not re.fullmatch(r"https://[A-Za-z0-9.:[\]/_-]+", args.core)):
        raise MigrationError("Eine HTTPS-URL ohne Anmeldedaten oder Query angeben.")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", args.server):
        raise MigrationError("Ein Servername ohne Leer- oder Sonderzeichen ist erforderlich.")
    if not re.fullmatch(r"\d+\.\d+\.\d+", args.version):
        raise MigrationError("Version als x.y.z angeben.")
    if args.command == "agent":
        start_agent(args)
        return

    portainer, data = portainer_mount(args.portainer)
    portainer_project = (portainer["Config"].get("Labels") or {}).get(PROJECT_LABEL)
    if args.command == "list":
        for entry in portainer_projects(portainer_project):
            print(f"{entry['Name']}\t{entry['Status']}\t{entry['ConfigFiles']}")
        return
    if args.command == "migrate":
        migrate(args, portainer, portainer_project, data)
        return
    if args.command == "remove-portainer":
        remove_portainer(portainer, data, portainer_project)
        return

    agent_id = agent_container() if args.command == "register" else None
    target, files, env_files = prepare(args, portainer_project, data, agent_id)
    if args.command == "register":
        register(args, target, files, env_files, agent_id)
    else:
        print("Compose, deklarierte Env-Werte und vorhandene Mounts geprüft.")


if __name__ == "__main__":
    try:
        main()
    except (MigrationError, OSError, ValueError, EOFError, KeyboardInterrupt) as error:
        print(f"Abbruch: {error}", file=sys.stderr)
        sys.exit(1)

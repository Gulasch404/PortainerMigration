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

Jeder Stack wird in Komodo als Git-Stack angelegt. Repo, Branch und Pfad der
Compose-Datei liest das Script aus Portainers Klon (sonst --repo/--branch). Die
Werte aus Portainers stack.env übernimmt Komodo als Environment des Stacks.
Private Repos brauchen einen in Komodo hinterlegten Git-Account (--git-account).

Zum Prüfen kopiert stage Portainers Klon nach /etc/komodo/stacks/<name>, also
dorthin, wo Komodo später selbst klont. So werden relative Pfade wie beim Deploy
aufgelöst. Diese Prüfkopie wird vor der Registrierung gelöscht. Korrekturen
gehören ins Git-Repo; zum erneuten Prüfen dieselbe Änderung in der Prüfkopie
nachziehen. --require-mount prüft einen LXC-Mountpoint, z. B.
stage slotpilot --require-mount /mnt/daten; vor der Registrierung erneut setzen.
"""
import argparse
import getpass
import json
import os
import re
import shlex
import shutil
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
STAGING_MARKER = ".portainer-import-staging"
STATE = ROOT / "portainer-import"
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


class VerificationError(MigrationError):
    """The compose model differs from the running containers; fixable in the repo."""


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


def read_git_info(repo_root):
    """Remote URL, branch and commit of Portainer's clone, read without needing git installed."""
    git = repo_root / ".git"
    url, in_origin = None, False
    for line in (git / "config").read_text(errors="replace").splitlines():
        line = line.strip()
        if line.startswith("["):
            in_origin = line.replace(" ", "").replace("'", '"') == '[remote"origin"]'
        elif in_origin and "=" in line and line.split("=", 1)[0].strip() == "url":
            url = line.split("=", 1)[1].strip()
    head = (git / "HEAD").read_text().strip()
    branch = head[len("ref: refs/heads/"):] if head.startswith("ref: refs/heads/") else None
    commit = head if re.fullmatch(r"[0-9a-f]{40}", head) else None
    if branch:
        ref = git / "refs" / "heads" / branch
        if ref.is_file():
            commit = ref.read_text().strip()
        elif (git / "packed-refs").is_file():
            for line in (git / "packed-refs").read_text().splitlines():
                if line.endswith(f" refs/heads/{branch}"):
                    commit = line.split()[0]
    return url, branch, commit


def portainer_git_configs(portainer_data, stack_id):
    """GitConfig of a stack from portainer.db (BoltDB). Values are plain JSON in the file,
    so a byte scan finds them without a BoltDB library. Freed pages may hold older copies,
    hence all distinct results are returned."""
    db = portainer_data / "portainer.db"
    if not db.is_file():
        return []
    raw = db.read_bytes()
    decoder = json.JSONDecoder()
    found = []
    for match in re.finditer(rb'\{"Id":%d,' % stack_id, raw):
        chunk = raw[match.start():match.start() + 200_000].decode("utf-8", "replace")
        try:
            stack, _ = decoder.raw_decode(chunk)
        except ValueError:
            continue
        if not isinstance(stack, dict) or stack.get("ProjectPath") != f"/data/compose/{stack_id}":
            continue
        git = stack.get("GitConfig")
        if not isinstance(git, dict) or not git.get("URL"):
            continue
        auth = git.get("Authentication") if isinstance(git.get("Authentication"), dict) else {}
        entry = {"url": git["URL"], "ref": git.get("ReferenceName") or "",
                 "commit": git.get("ConfigHash") or "", "user": auth.get("Username") or ""}
        same = next((item for item in found if all(item[k] == entry[k] for k in ("url", "ref", "user"))), None)
        if same is None:
            found.append(entry)
        elif same["commit"] != entry["commit"]:
            same["commit"] = ""  # copies differ only in commit: unknown which one ran last
    return found


def git_from_portainer_db(portainer_data, stack_id):
    candidates = portainer_git_configs(portainer_data, int(stack_id)) if stack_id.isdigit() else []
    if not candidates:
        return None
    if len(candidates) > 1:
        print("Portainers Datenbank enthält mehrere Stände für diesen Stack:")
        for number, entry in enumerate(candidates, 1):
            print(f"  {number}) {entry['url']} {entry['ref']} {entry['commit'][:12]}")
        choice = ask("Welcher ist aktuell? Nummer: ")
        if not (choice.isdigit() and 1 <= int(choice) <= len(candidates)):
            raise MigrationError("Keine gültige Auswahl.")
        return candidates[int(choice) - 1]
    return candidates[0]


def parse_repo_url(url):
    """Split a clone URL into Komodo's git_provider, repo and git_https. Credentials are dropped."""
    if url.startswith(("https://", "http://")):
        parts = urlsplit(url)
        host = (parts.hostname or "") + (f":{parts.port}" if parts.port else "")
        path, https = parts.path, parts.scheme == "https"
    elif url.startswith("ssh://"):
        # The SSH port says nothing about the HTTPS port Komodo will use, so it is dropped.
        parts = urlsplit(url)
        host, path, https = parts.hostname or "", parts.path, True
    else:
        match = re.fullmatch(r"(?:ssh://)?[^@/\s]+@([^:/\s]+)[:/](.+)", url)
        if not match:
            raise MigrationError("Repo-URL nicht erkannt. --repo https://host/owner/repo angeben.")
        host, path, https = match.group(1), match.group(2), True
    path = path.strip("/")
    path = path.removesuffix(".git")
    if not host or not re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+", path):
        raise MigrationError("Repo-URL nicht erkannt. --repo https://host/owner/repo angeben.")
    return host, path, https


def is_staging_copy(stack_dir):
    return (stack_dir / STAGING_MARKER).is_file() or (stack_dir / ".portainer-import-check.log").is_file()


def remove_staging(stack_dir, group):
    if not stack_dir.exists():
        return
    if stack_dir.is_symlink() or not is_staging_copy(stack_dir):
        raise MigrationError(f"{stack_dir} stammt nicht von diesem Script und wird nicht gelöscht.")
    for item in group:
        for mount in item["Mounts"]:
            source = mount.get("Source")
            if source and Path(source).resolve().is_relative_to(stack_dir.resolve()):
                raise MigrationError(f"{stack_dir} enthält Daten eines laufenden Containers ({source}). Nicht gelöscht.")
    shutil.rmtree(stack_dir)


def stage_project(args, group, portainer_data):
    working, source_files = project_paths(group)
    try:
        relative_workdir = working.relative_to("/data/compose")
        relative_files = [str(file.relative_to(working)) for file in source_files]
    except ValueError as error:
        raise MigrationError("Compose-Dateien liegen außerhalb des Portainer-Stack-Verzeichnisses.") from error
    if not relative_workdir.parts or ".." in relative_workdir.parts:
        raise MigrationError("Ungültiges Portainer-Stack-Verzeichnis.")
    stack_root = portainer_data / "compose" / relative_workdir.parts[0]
    source = portainer_data / "compose" / relative_workdir
    repo_root = next((path for path in [source, *source.parents]
                      if path.is_relative_to(stack_root) and (path / ".git").is_dir()), None)
    url, branch, commit = read_git_info(repo_root) if repo_root else (None, None, None)
    portainer_user = ""
    if not url and not (args.repo and args.branch):
        # Portainer keeps no .git in its copy; the git settings live in portainer.db.
        stored = git_from_portainer_db(portainer_data, relative_workdir.parts[0])
        if stored:
            url, commit, portainer_user = stored["url"], stored["commit"], stored["user"]
            if stored["ref"].startswith("refs/heads/"):
                branch = stored["ref"].removeprefix("refs/heads/")
            print(f"Git-Einstellungen aus Portainers Datenbank gelesen ({stored['ref'] or 'ohne Ref'}).")
    repo_root = repo_root or stack_root
    url, branch = args.repo or url, args.branch or branch
    if not url:
        url = ask("Repo-URL nicht gefunden. Repo-URL (wie in Portainer unter Stacks): ")
    if not branch:
        branch = ask("Branch (z. B. main): ").removeprefix("refs/heads/")
    if not url or not re.fullmatch(r"[A-Za-z0-9_./-]+", branch or ""):
        raise MigrationError("Repo-URL und Branch sind erforderlich.")
    args.repo, args.branch = url, branch  # a retry must not ask again
    provider, repo, https = parse_repo_url(url)
    run_dir = str(source.relative_to(repo_root))
    for file in [run_dir, *relative_files]:
        if not SAFE_PATH.fullmatch(file) or ".." in Path(file).parts:
            raise MigrationError(f"Pfad im Repo ungeeignet: {file}")

    stack_dir = ROOT / "stacks" / (args.name or args.project)
    if stack_dir.exists() and not (stack_dir / STAGING_MARKER).is_file():
        if not is_staging_copy(stack_dir):
            raise MigrationError(f"{stack_dir} existiert bereits (Komodo-Klon?). Bitte prüfen, dann --name setzen.")
        remove_staging(stack_dir, group)  # copy from an older version of this script
    target = stack_dir / run_dir
    if stack_dir.exists():
        print(f"Vorhandene Prüfkopie: {stack_dir}")
    else:
        # Komodo clones git stacks to <root>/stacks/<name>; checking there resolves relative paths as Komodo will.
        shutil.copytree(repo_root, stack_dir, symlinks=True, ignore=shutil.ignore_patterns(".git"))
        (stack_dir / STAGING_MARKER).write_text("Prüfkopie, wird vor der Registrierung gelöscht.\n")
        if args.env_file:
            with (target / "stack.env").open("a") as handle:
                handle.write("\n" + args.env_file.read_text())
        print(f"Prüfkopie aus Portainers Klon: {stack_dir}")
    for file in relative_files:
        if not (target / file).is_file():
            raise MigrationError(f"Compose-Datei fehlt: {file}")

    environment = ""
    env_files = []
    if (target / "stack.env").is_file():
        (target / "stack.env").chmod(0o600)
        environment = (target / "stack.env").read_text()
        if environment.strip():
            env_files.append("stack.env")
    if (target / ".env").is_file():
        env_files.append(".env")
    print(f"Repo: {provider}/{repo} (Branch {branch}" + (f", Portainer lief auf {commit[:12]})" if commit else ")"))
    print(f"Compose: {run_dir}/{', '.join(relative_files)}")
    print("Env: " + (", ".join(env_files) if env_files else "keine"))
    return {
        "stack_dir": stack_dir, "target": target, "run_dir": run_dir, "files": relative_files,
        "env_files": env_files, "environment": environment, "portainer_user": portainer_user,
        "git": {"git_provider": provider, "git_https": https, "repo": repo, "branch": branch},
    }


def compose_model(project, target, files, env_files, agent_id=None):
    # Same env file order as Komodo: its generated stack.env first, then additional files.
    options = ["-p", project]
    for name in env_files:
        options += ["--env-file", name]
    for file in files:
        options += ["-f", file]
    command = [*DOCKER, "compose"]
    if agent_id:
        command = [*DOCKER, "exec", "--workdir", str(target), agent_id, "docker", "compose"]
    STATE.mkdir(parents=True, exist_ok=True)
    log = STATE / f"{project}-check.log"
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
        raise VerificationError("\n".join(problems))


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


def mark_registered(project, stack_name):
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / f"{project}.registered").write_text(stack_name + "\n")


class KomodoClient:
    """Asks for the API key on first use and keeps it for the rest of the run."""

    def __init__(self, core):
        self.core, self.credentials = core, None

    def __call__(self, category, operation, **data):
        if self.credentials is None:
            credentials = tuple(getpass.getpass(prompt).strip()
                                for prompt in ("Komodo API Key: ", "Komodo API Secret: "))
            if not all(credentials):
                raise MigrationError("API-Key und API-Secret sind erforderlich.")
            self.credentials = credentials
        return komodo(self.core, category, operation, data, self.credentials)


ACCOUNT_REQUESTS = {
    "git": ("ListGitProviderAccounts", "ListGitProvidersFromConfig"),
    "registry": ("ListDockerRegistryAccounts", "ListDockerRegistriesFromConfig"),
}


def known_accounts(api, kind, domain):
    """Usernames Komodo knows for a domain, from its database and from Core/Periphery config."""
    names = set()
    for operation in ACCOUNT_REQUESTS[kind]:
        try:
            result = api("read", operation)
        except MigrationError:
            continue  # missing permission or unknown request in this Komodo version
        for item in result if isinstance(result, list) else []:
            if not isinstance(item, dict) or item.get("domain") != domain:
                continue
            if item.get("username"):
                names.add(item["username"])
            for account in item.get("accounts") or []:
                if isinstance(account, dict) and account.get("username"):
                    names.add(account["username"])
    return sorted(names)


def choose_account(api, kind, domain, label, accounts=None):
    accounts = known_accounts(api, kind, domain) if accounts is None else accounts
    print(f"\n{label} für {domain}:")
    print("  0) keiner (öffentlich)")
    for number, account in enumerate(accounts, 1):
        print(f"  {number}) {account}")
    if not accounts:
        print("  (in Komodo keine Accounts für diese Domain gefunden)")
    choice = ask("Nummer oder Account-Name [0]: ")
    if choice in ("", "0"):
        return ""
    if choice.isdigit() and 1 <= int(choice) <= len(accounts):
        return accounts[int(choice) - 1]
    if not re.fullmatch(r"[A-Za-z0-9_.@-]+", choice):
        raise MigrationError("Ungültiger Account-Name.")
    if choice not in accounts:
        print(f"Hinweis: {choice} ist in Komodo nicht bekannt. Vor dem Deploy in Komodo anlegen.")
    return choice


def image_registry(image):
    first, slash, _ = image.partition("/")
    if slash and ("." in first or ":" in first or first == "localhost"):
        return first
    return "docker.io"


def select_accounts(args, info):
    if args.git_account is None:
        if info.get("portainer_user"):
            print(f"\nPortainer hat für dieses Repo den Git-Benutzer {info['portainer_user']} verwendet (privates Repo).")
        args.git_account = choose_account(args.api, "git", info["git"]["git_provider"], "Git-Account")
    registries = info["registries"]
    if args.registry_account is not None:
        if args.registry_provider is None:
            private = [domain for domain in registries if domain != "docker.io"]
            if len(private) != 1:
                raise MigrationError("Registry nicht eindeutig. --registry-provider DOMAIN angeben.")
            args.registry_provider = private[0]
        return
    chosen = []
    for domain in registries:
        accounts = known_accounts(args.api, "registry", domain)
        if domain == "docker.io" and not accounts:
            continue  # Docker Hub without a stored account: public images, nothing to ask
        account = choose_account(args.api, "registry", domain, "Registry-Login", accounts)
        if account:
            chosen.append((domain, account))
    if len(chosen) > 1:
        print(f"Hinweis: Ein Komodo-Stack kennt nur einen Registry-Login. Verwendet wird {chosen[0][0]}.")
    args.registry_provider, args.registry_account = chosen[0] if chosen else ("", "")


def register(args, info, agent_id):
    api = args.api
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
        **info["git"],
        "git_account": args.git_account or "",
        "registry_provider": args.registry_provider or "",
        "registry_account": args.registry_account or "",
        "run_directory": info["run_dir"],
        "file_paths": info["files"],
        # Komodo writes the environment to stack.env, like Portainer did; services using
        # "env_file: stack.env" keep working.
        "environment": info["environment"],
        "env_file_path": "stack.env",
        "additional_env_files": [
            {"path": file, "track": False} for file in info["env_files"] if file != "stack.env"
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
                mark_registered(args.project, stack["name"])
                print(f"Bereits registriert: {stack['name']}")
                return
            raise MigrationError(f"Stack {stack['name']} existiert bereits mit anderer Konfiguration.")
    result = api("write", "CreateStack", name=name, config=config)
    mark_registered(args.project, result["name"])
    print(f"Registriert: {result['name']} als Git-Stack ({config['repo']}, Branch {config['branch']}).")
    print("Services in Komodo prüfen, dann dort deployen. Für Auto-Deploy bei Push den Webhook des Stacks im Repo eintragen.")


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
    info = stage_project(args, group, data)
    model = compose_model(args.project, info["target"], info["files"], info["env_files"], agent_id)
    verify_model(model, group, info["stack_dir"])
    info["registries"] = sorted({image_registry(cfg["image"])
                                 for cfg in model["services"].values() if cfg.get("image")})
    return info, group


def finish_registration(args, portainer_project, data, agent_id):
    """Check once more inside Periphery, remove the check copy so Komodo clones fresh, register."""
    info, group = prepare(args, portainer_project, data, agent_id)
    select_accounts(args, info)
    remove_staging(info["stack_dir"], group)
    register(args, info, agent_id)


def pending_projects(portainer_project):
    """Portainer stacks that have not been registered in Komodo by this script yet."""
    return [entry for entry in portainer_projects(portainer_project)
            if not (STATE / f"{entry['Name']}.registered").is_file()
            and not (ROOT / "stacks" / entry["Name"] / REGISTERED_MARKER).is_file()]


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
        args.project = args.name = args.repo = args.branch = None
        args.git_account = args.registry_provider = args.registry_account = None
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

    print(f"\n[1/3] {args.project}: Git-Stack ermitteln und mit den laufenden Containern abgleichen")
    while True:
        try:
            prepare(args, portainer_project, data)
            break
        except MigrationError as error:
            print(f"\nProbleme gefunden:\n{error}\n", file=sys.stderr)
            if isinstance(error, VerificationError):
                print("Korrekturen im Git-Repo pushen und zum Prüfen genauso in der Prüfkopie unter "
                      f"{ROOT / 'stacks' / (args.name or args.project)} nachziehen.", file=sys.stderr)
            if not confirm("Erneut prüfen?"):
                raise MigrationError("Migration angehalten. Ein neuer Lauf setzt dort fort.")
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
    finish_registration(args, portainer_project, data, agent_id)
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
        part.add_argument("--repo", help="Repo-URL, falls sie nicht aus Portainers Klon lesbar ist")
        part.add_argument("--branch", help="Branch, falls er nicht aus Portainers Klon lesbar ist")
        part.add_argument("--name", help="Stack-Name in Komodo, falls der Projektname schon belegt ist")
        if command in ("migrate", "register"):
            part.add_argument("--git-account", help="Git-Account in Komodo für private Repos (sonst Auswahl)")
            part.add_argument("--registry-account", help="Registry-Account in Komodo für private Images (sonst Auswahl)")
            part.add_argument("--registry-provider", help="Registry-Domain zum Account, z. B. ghcr.io")
        if command == "migrate":
            part.add_argument("--version", default="2.3.4", help="Version von Komodo Periphery")
    args = parser.parse_args()
    if args.command is None:
        args.command = "migrate"
    defaults = {"project": None, "portainer": None, "env_file": None, "require_mount": [],
                "name": None, "version": "2.3.4", "repo": None, "branch": None, "git_account": None,
                "registry_account": None, "registry_provider": None}
    for key, value in defaults.items():
        if not hasattr(args, key):
            setattr(args, key, value)
    if args.git_account is not None:
        args.git_account = args.git_account.strip()
    if os.geteuid() != 0:
        raise MigrationError("Bitte als root im Docker-LXC ausführen.")
    os.umask(0o077)
    args.core = args.core.rstrip("/")
    url = urlsplit(args.core)
    if (url.scheme != "https" or not url.hostname or url.username or url.password
            or url.query or url.fragment or not re.fullmatch(r"https://[A-Za-z0-9.:[\]/_-]+", args.core)):
        raise MigrationError("Eine HTTPS-URL ohne Anmeldedaten oder Query angeben.")
    args.api = KomodoClient(args.core)
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

    if args.command == "register":
        finish_registration(args, portainer_project, data, agent_container())
    else:
        prepare(args, portainer_project, data)
        print("Compose, deklarierte Env-Werte und vorhandene Mounts geprüft.")


if __name__ == "__main__":
    try:
        main()
    except (MigrationError, OSError, ValueError, EOFError, KeyboardInterrupt) as error:
        print(f"Abbruch: {error}", file=sys.stderr)
        sys.exit(1)

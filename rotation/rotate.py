#!/usr/bin/env python3
"""Secret rotation for a Revel Docker Compose deployment (stdlib only, Python >= 3.10).

    rotate.py plan | status | check | stage <name...|--group self|thirdparty|all> | migrate
              | verify | finalize <name...> [--force] [--no-archive] | rollback <name>

Secret values never appear in argv, logs, stdout or rotation/state.json. See rotation/README.md.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import getpass
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import manifest as mf  # noqa: E402

MANIFEST = mf.MANIFEST
DB_CONFIRM_PHRASE = "ROTATE DB PASSWORD"
TELEGRAM_CONFIRM_PHRASE = "TELEGRAM REVOKED"
PUSHOVER_CONFIRM_PHRASE = "PUSHOVER RESET"
CANARY_CONFIRM_PHRASE = "CANARY PASSWORD CHANGED"
FORWARD_ONLY_PHRASES = {"TELEGRAM_BOT_TOKEN": TELEGRAM_CONFIRM_PHRASE, "PUSHOVER_USER_KEY": PUSHOVER_CONFIRM_PHRASE,
                        "CANARY_PASSWORD": CANARY_CONFIRM_PHRASE}
SECRET_LOOKING = re.compile(r"SECRET|TOKEN|KEY|PASSWORD|WEBHOOK_URL")
# Structured (structlog JSON) level, or an uppercase level word in a plain line. Never a bare "error".
ERROR_LINE = re.compile(r'(?i:"level"\s*:\s*"(?:error|critical)")|\b(?:ERROR|CRITICAL)\b')
SERVICE_ORDER = ["web", "celery_default", "beat", "telegram", "frontend"]


class RotationError(Exception):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- env file editing
_LINE = re.compile(r"^(?P<pre>\s*(?:export\s+)?)(?P<key>[A-Za-z_][A-Za-z0-9_]*)(?P<gap>[ \t]*)=(?P<rest>.*)$")
_DQ = re.compile(r'^"((?:[^"\\]|\\.)*)"(.*)$')
_SQ = re.compile(r"^'((?:[^'\\]|\\.)*)'(.*)$")
_SAFE = re.compile(r"[A-Za-z0-9_.~:/@%+,=\-]*")
_ESC = {"a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v", "\\": "\\", '"': '"', "$": "$"}


def parse_value(rest: str) -> tuple[str, str | None, str]:
    """Split the right-hand side of KEY=... into (decoded value, quote style, trailing text)."""
    rest_q = rest.lstrip()  # Compose ignores blanks between `=` and a quoted value
    if m := _DQ.match(rest_q):
        body = re.sub(r"\\(.)", lambda e: _ESC.get(e.group(1), e.group(0)), m.group(1))
        return body, '"', m.group(2)
    if m := _SQ.match(rest_q):
        return m.group(1).replace("\\'", "'"), "'", m.group(2)
    c = re.search(r"\s#", rest)
    raw = rest[: c.start()] if c else rest
    return raw.strip(), None, rest[len(raw):]


def encode_value(value: str, quote: str | None) -> str:
    if re.search(r"[\r\n\x00]", value):
        raise RotationError("value contains a newline or NUL")
    if quote == "'" and "'" not in value and not value.endswith("\\"):
        return f"'{value}'"
    if quote is None and _SAFE.fullmatch(value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$") + '"'


def refuse_multiline(text: str) -> None:
    """Fail closed on a quoted value that spans lines: a line inside it could be read as a real assignment."""
    for line in text.split("\n"):
        m = _LINE.match(line)
        if not m:
            continue
        rest = m.group("rest").lstrip()
        if rest[:1] in ("'", '"') and not (_DQ.match(rest) or _SQ.match(rest)):
            raise RotationError(f"{m.group('key')} has a quoted value that spans lines: edit .env by hand or put "
                                "the value on one line first; nothing was changed")


def get_env(text: str, name: str) -> str | None:
    """Effective (last) assignment of ``name``; None if absent."""
    found = None
    for line in text.split("\n"):
        m = _LINE.match(line)
        if m and m.group("key") == name:
            found = parse_value(m.group("rest"))[0]
    return found


def set_env(text: str, name: str, value: str) -> str:
    """Return ``text`` with only ``name``'s effective line changed (idempotent, order/comments kept)."""
    refuse_multiline(text)
    lines = text.split("\n")
    for i in range(len(lines) - 1, -1, -1):
        m = _LINE.match(lines[i])
        if m and m.group("key") == name:
            old, quote, tail = parse_value(m.group("rest"))
            if old == value:
                return text
            lines[i] = f"{m.group('pre')}{name}{m.group('gap')}={encode_value(value, quote)}{tail}"
            return "\n".join(lines)
    base = text if text == "" or text.endswith("\n") else text + "\n"
    return f"{base}{name}={encode_value(value, None)}\n"


def env_keys(text: str, commented: bool = False) -> set[str]:
    keys = {m.group("key") for line in text.split("\n") if (m := _LINE.match(line))}
    if commented:
        keys |= set(re.findall(r"^\s*#\s*([A-Z][A-Z0-9_]*)=", text, re.M))
    return keys


def fsync_dir(path: Path) -> None:
    """Persist a directory entry (a create, rename or unlink): syncing the file alone does not."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def mkdir_synced(path: Path) -> None:
    """mkdir -p, then sync the parent of each directory this call created: a later file sync does not persist them."""
    created = [p for p in (path, *path.parents) if not p.exists()]
    path.mkdir(parents=True, exist_ok=True)
    for p in created:
        fsync_dir(p.parent)


class DirSyncError(OSError):
    """write_atomic replaced the file, then syncing its directory failed: the new content IS in place but
    may not survive a power loss. Callers must treat the write as done, not as "nothing was written"."""


def write_atomic(path: Path, text: str) -> None:
    """Temp file + os.replace, keeping the original file mode (0600 for a new file). The file and its
    directory are synced, so a returned write survives a power loss. Any exception except DirSyncError
    means the target is unchanged; DirSyncError means the new content is in place."""
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.tmp.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            os.fchmod(f.fileno(), mode)
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    try:
        fsync_dir(path.parent)
    except OSError as e:
        raise DirSyncError(e.errno, f"{path.name} was written but its directory could not be synced "
                                    f"({e.strerror or e})") from e


def backup_file(path: Path, now: datetime) -> Path:
    stem = f"{path.name}.bak.{now.strftime('%Y%m%dT%H%M%S%fZ')}"
    for n in range(100):
        dest = path.with_name(stem if n == 0 else f"{stem}-{n}")
        try:
            fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            break
        except FileExistsError:
            continue
    else:
        raise RotationError("could not create a unique .env backup")
    with os.fdopen(fd, "wb") as f:
        f.write(path.read_bytes())
        f.flush()
        os.fsync(f.fileno())
    os.chmod(dest, 0o600)
    fsync_dir(dest.parent)
    return dest


# --------------------------------------------------------------------------- list operations
def csv_split(s: str | None) -> list[str]:
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def csv_prepend(items: list[str], new: str) -> list[str]:
    return [new] + [i for i in items if i != new]


def csv_drop(items: list[str], drop: list[str]) -> list[str]:
    return [i for i in items if i not in drop]


def generate_secret(nbytes: int = 50) -> str:
    while True:  # urlsafe has no comma; the loop is belt and braces for the CSV fallback lists
        v = secrets.token_urlsafe(nbytes)
        if "," not in v:
            return v


def scram_verifier(password: str, salt: bytes | None = None, iterations: int = 4096) -> str:
    """Postgres SCRAM-SHA-256 verifier, computed here so the plaintext password never reaches the server's logs."""
    if not password.isascii():
        raise RotationError("non-ASCII passwords are not supported (SASLprep)")
    salt = salt if salt is not None else secrets.token_bytes(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored = hashlib.sha256(client_key).digest()
    server = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return f"SCRAM-SHA-256${iterations}:{b64(salt)}${b64(stored)}:{b64(server)}"


# --------------------------------------------------------------------------- subprocess
class Runner:
    """Runs argv lists. Secrets travel via ``input_text`` (stdin) only, never argv, so captured
    stdout/stderr never contain them and stderr is shown when a command fails."""

    def __init__(self, cwd: Path, log: Callable[[str], None] = print) -> None:
        self.cwd, self.log = cwd, log

    def run(self, argv: list[str], *, input_text: str | None = None, stdout_path: Path | None = None,
            stream: bool = False) -> Any:
        self.log("  $ " + " ".join(argv))
        try:
            if stream:  # live output (rollouts take minutes)
                return subprocess.run(argv, cwd=self.cwd, check=False)
            if stdout_path is not None:
                with open(stdout_path, "wb") as out:  # a fresh stream: concatenated age streams do not decrypt
                    res = subprocess.run(argv, cwd=self.cwd, input=(input_text or "").encode(),
                                         stdout=out, stderr=subprocess.PIPE, check=False)
                err = res.stderr.decode(errors="replace")
                return SimpleNamespace(returncode=res.returncode, stdout="", stderr=err)
            res = subprocess.run(argv, cwd=self.cwd, input=input_text, capture_output=True, text=True, check=False)
        except FileNotFoundError:
            res = SimpleNamespace(returncode=127, stdout="", stderr=f"{argv[0]}: not found")
        if res.returncode != 0 and res.stderr:
            self.log(res.stderr.rstrip())
        return res


# --------------------------------------------------------------------------- state machine
TRANSITIONS = {
    # "pending" is the write-ahead journal: a command recorded it, then died before reaching "staged".
    "pending": {"rollback": "idle"},
    "idle": {"stage": "staged"},
    "finalized": {"stage": "staged"},
    "staged": {"migrate": "migrated", "finalize": "finalized", "rollback": "idle"},
    "migrated": {"finalize": "finalized"},
}


def next_step(step: str, action: str) -> str:
    try:
        return TRANSITIONS[step][action]
    except KeyError:
        raise RotationError(f"cannot {action} from step '{step}'") from None


@dataclass
class Staged:
    edits: dict[str, str]  # env variable -> new value
    touched: list[str]  # variables restored on rollback
    services: list[str]
    extra: dict[str, Any] = field(default_factory=dict)  # json-safe, no values
    recreate: list[str] = field(default_factory=list)
    before: Callable[[], None] | None = None  # side effect run after the backup and the .env write (".env first")
    roll: bool = True


class Rotator:
    def __init__(self, base: Path, runner: Any, *, prompt_secret: Callable[[str], str] = getpass.getpass,
                 prompt: Callable[[str], str] = input, now: Callable[[], datetime] = utcnow,
                 sleep: Callable[[float], None] = time.sleep, out: Callable[[str], None] = print,
                 error_threshold: int = 5, maintenance: bool = False) -> None:
        self.base, self.runner = base, runner
        self.env_path = base / ".env"
        self.state_path = base / "rotation" / "state.json"
        self.archive_dir = base / "rotation" / "archive"
        self.prompt_secret, self.prompt, self.now, self.sleep, self.out = prompt_secret, prompt, now, sleep, out
        self.error_threshold, self.maintenance = error_threshold, maintenance
        self._active: set[str] | None = None

    # ---- files
    def env_text(self) -> str:
        if not self.env_path.exists():
            raise RotationError(f"{self.env_path} not found (run from a deploy dir or pass --dir)")
        return self.env_path.read_text(encoding="utf-8")

    @contextlib.contextmanager
    def lock(self):
        """One mutating command at a time: they read-modify-write .env and state.json. The lock is held for the
        whole command (flock, released when the process ends) and a second command fails at once."""
        mkdir_synced(self.state_path.parent)
        fd = os.open(self.state_path.with_name("state.lock"), os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise RotationError("another rotate.py command that changes secrets is running "
                                    "(rotation/state.lock): wait for it to finish") from None
            yield
        finally:
            os.close(fd)

    def load_state(self) -> dict[str, Any]:
        if self.state_path.exists():
            return json.loads(self.state_path.read_text())
        return {"version": 1, "secrets": {}}

    def save_state(self, state: dict[str, Any]) -> None:
        mkdir_synced(self.state_path.parent)
        write_atomic(self.state_path, json.dumps(state, indent=2, sort_keys=True) + "\n")

    def step_of(self, name: str) -> str:
        return self.load_state()["secrets"].get(name, {}).get("step", "idle")

    def update_state(self, name: str, **fields: Any) -> dict[str, Any]:
        state = self.load_state()
        entry = state["secrets"].setdefault(name, {})
        entry.update(fields, updated_at=self.now().isoformat())
        self.save_state(state)
        return entry

    def host_path(self, container_path: str) -> Path:
        prefix = "/app/certs/"
        if not container_path.startswith(prefix) or ".." in container_path:
            raise RotationError(f"{container_path}: only files under {prefix} (the ./certs mount) are supported")
        suffix = container_path[len(prefix):]
        if suffix.startswith("/"):  # an absolute suffix would replace the certs dir in the join
            raise RotationError(f"{container_path}: only files under {prefix} (the ./certs mount) are supported")
        path = self.base / "certs" / suffix
        if (self.base / "certs").resolve() not in path.resolve().parents:  # a symlink may leave the mount
            raise RotationError(f"{container_path} resolves outside {self.base}/certs: refusing")
        return path

    # ---- docker
    def compose(self, *args: str, input_text: str | None = None) -> Any:
        return self.runner.run(["docker", "compose", *args], input_text=input_text)

    def exec_web(self, *args: str) -> Any:
        return self.compose("exec", "-T", "web", *args)

    def active_services(self) -> set[str] | None:
        """Services enabled by the active compose profiles; None if it cannot be determined."""
        if self._active is None:
            r = self.compose("config", "--services")
            if r.returncode == 0 and r.stdout.strip():
                self._active = set(r.stdout.split())
        return self._active

    def filter_active(self, services: list[str]) -> list[str]:
        active = self.active_services()
        return [s for s in services if active is None or s in active]

    def roll(self, services: list[str], recreate: list[str] | None = None) -> None:
        for svc in self.filter_active(recreate or []):
            r = self.compose("up", "-d", "--no-deps", "--force-recreate", svc)
            if r.returncode != 0:
                raise RotationError(f"force-recreate {svc} failed (exit {r.returncode})")
        if not services:  # no filter here: the script rejects an undefined name and skips an inactive profile
            return
        script = self.base / "deploy-rollout.sh"
        if not script.exists():
            raise RotationError(f"{script} not found (symlink deploy-rollout.sh, notify-discord.sh and "
                                "image-diff.sh into this dir)")
        r = self.runner.run([str(script), "roll", *services], stream=True)
        if r.returncode != 0:
            raise RotationError(f"deploy-rollout.sh roll {' '.join(services)} failed (exit {r.returncode})")

    # ---- verify
    def check_verify_names(self, requested: list[str], kept: list[str]) -> None:
        """Reject an undefined name, note an inactive one, and refuse an explicit list that checks nothing."""
        r = self.compose("--profile", "*", "config", "--services")
        if r.returncode != 0 or not r.stdout.strip():  # a typo and an inactive name look alike without the list
            raise RotationError("cannot list the services in docker-compose.yml (docker compose config failed): "
                                "refusing to verify by name")
        defined = set(r.stdout.split())
        unknown = [s for s in requested if s not in defined]
        if unknown:
            raise RotationError(f"unknown service(s): {', '.join(unknown)} (not defined in docker-compose.yml)")
        skipped = [s for s in requested if s not in kept]
        if skipped:
            self.out(f"skipped (not in the active compose profiles): {', '.join(skipped)}")
        if not kept:
            raise RotationError("none of the named services is active: nothing to verify")

    def verify(self, services: list[str], since: datetime) -> bool:
        services = self.filter_active(services)
        results: list[tuple[str, bool, str]] = []
        bad = ["?"]
        for _ in range(24):
            bad = self._unhealthy(services)
            if not bad:
                break
            self.sleep(5)
        results.append(("health " + ",".join(services), not bad, "unhealthy: " + ",".join(bad) if bad else "ok"))
        backend = bool(set(services) & set(mf.BACKEND))
        if backend:
            r = self.exec_web("python", "-c", "import urllib.request,sys;"
                              "sys.exit(0 if urllib.request.urlopen('http://localhost:8000/api/version',"
                              "timeout=10).status==200 else 1)")
            results.append(("api /api/version", r.returncode == 0, f"exit {r.returncode}"))
            r = self.exec_web("python", "manage.py", "check")
            warns = sorted(set(re.findall(r"\((\w+\.[WE]\d+)\)", (r.stdout or "") + (r.stderr or ""))))
            results.append(("manage.py check", r.returncode == 0 and "oauth.W002" not in warns,
                            f"exit {r.returncode}; warnings: {', '.join(warns) or 'none'}"))
            targets = self.filter_active(["web", "celery_default"])
            r = self.compose("logs", "--no-color", "--since", since.strftime("%Y-%m-%dT%H:%M:%SZ"), *targets)
            n = sum(1 for line in (r.stdout or "").splitlines() if ERROR_LINE.search(line))
            results.append((f"error lines since rollout (max {self.error_threshold})",
                            r.returncode == 0 and n <= self.error_threshold,
                            f"{n} found" if r.returncode == 0 else f"logs unreadable (exit {r.returncode})"))
        if "frontend" in services:
            r = self.compose("exec", "-T", "frontend", "node", "-e",
                             "require('http').get('http://localhost:3000',"
                             "r=>process.exit(r.statusCode===200?0:1)).on('error',()=>process.exit(1))"
                             ".setTimeout(10000,()=>process.exit(1))")
            results.append(("frontend /", r.returncode == 0, f"exit {r.returncode}"))
        domain = get_env(self.env_text(), "API_DOMAIN")
        if backend and domain:
            r = self.runner.run(["curl", "-fsS", "--max-time", "10", "-o", os.devnull, f"https://{domain}/api/version"])
            results.append(("edge /api/version (advisory)", True, "ok" if r.returncode == 0 else "unreachable"))
        for name, ok, detail in results:
            self.out(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")
        return all(ok for _, ok, _ in results)

    def _unhealthy(self, services: list[str]) -> list[str]:
        if not services:
            return []
        r = self.compose("ps", "--format", "json", *services)
        text = (r.stdout or "").strip()
        try:
            rows = json.loads(text) if text.startswith("[") else [json.loads(x) for x in text.splitlines() if x]
        except ValueError:
            return list(services)
        # Every reported replica of a service must be up: one healthy row must not hide an unhealthy one.
        seen = {x.get("Service") for x in rows}
        down = {x.get("Service") for x in rows if x.get("State") != "running" or x.get("Health", "") not in ("", "healthy")}
        return [s for s in services if s not in seen or s in down]

    # ---- staging
    def prepare(self, s: mf.Secret, env: str) -> Staged | None:
        def cur(n: str) -> str | None:
            return get_env(env, n)

        k = s.kind
        if k == mf.GENERATED_FILE_WITH_INACTIVE:
            old, fb = cur(s.name), s.fallback_var
            if not old:
                return None
            new = f"{os.path.dirname(old)}/oidc-{self.now().strftime('%Y%m%dT%H%M%SZ')}.pem"
            host_new = self.host_path(new)
            self.host_path(old)

            def make_key() -> None:
                r = self.runner.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt",
                                     "rsa_keygen_bits:2048", "-out", str(host_new)])
                if r.returncode != 0:
                    host_new.unlink(missing_ok=True)
                    raise RotationError("openssl genpkey failed")
                os.chmod(host_new, 0o644)  # containers run as uid 997: must be world-readable
                try:  # .env must never name a PEM that a power loss can take away
                    fd = os.open(host_new, os.O_RDONLY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                    fsync_dir(host_new.parent)
                except OSError as e:
                    host_new.unlink(missing_ok=True)
                    raise RotationError(f"could not sync the new key file ({e.strerror}): nothing was changed") from None

            return Staged({s.name: new, fb: ",".join(csv_prepend(csv_split(cur(fb)), old))}, [s.name, fb],
                          list(s.services), {"new_key_file": str(host_new), "retired_paths": [old]}, before=make_key)
        if k == mf.LIST_APPEND:
            self.out(s.instructions)
            new = csv_split(self.prompt_secret(f"New {s.name} value(s), comma-separated (hidden, empty = skip): "))
            if not new:
                self.out(f"skipped {s.name}")
                return None
            old = csv_split(cur(s.name))
            if not old and cur("STRIPE_WEBHOOK_SECRET"):  # legacy single secret: keep it valid through the grace period
                old = [cur("STRIPE_WEBHOOK_SECRET") or ""]
            if not new or set(new) & set(old):
                raise RotationError("need at least one new value, none already in the list")
            return Staged({s.name: ",".join(old + new)}, [s.name], list(s.services),
                          {"old_count": len(old), "new_count": len(new)})
        if k == mf.THIRD_PARTY:
            self.out(s.instructions)
            if not s.reversible:
                self.out(f"{s.name} CANNOT be rolled back: the provider revokes the old value at once.")
                phrase = FORWARD_ONLY_PHRASES[s.name]
                if self.prompt(f"Type '{phrase}' once you have revoked the old value: ") != phrase:
                    raise RotationError("confirmation phrase mismatch, aborted")
            val = self.prompt_secret(f"New value for {s.name} (hidden, empty = skip): ")
            if not val.strip():
                if not s.reversible:  # the old value is already revoked: a skip would leave .env with a dead value
                    raise RotationError(f"{s.name} was confirmed as changed, so a new value is required: run `stage "
                                        f"{s.name}` again and enter it")
                self.out(f"skipped {s.name}")
                return None
            if re.search(r"[\r\n\x00]", val):
                raise RotationError("value contains a newline")
            return Staged({s.name: val.strip()}, [s.name], list(s.services))
        if k == mf.THIRD_PARTY_FILE:
            self.out(s.instructions)
            path = self.prompt(f"Container path of the new file for {s.name} (empty = skip): ").strip()
            if not path:
                self.out(f"skipped {s.name}")
                return None
            host = self.host_path(path)
            if not host.is_file():
                raise RotationError(f"{host} does not exist")
            if stat.S_IMODE(host.stat().st_mode) != 0o644:
                raise RotationError(f"{host} must be chmod 644 (containers run as uid 997)")
            return Staged({s.name: path}, [s.name], list(s.services))
        if k == mf.DB_PASSWORD:
            return self._prepare_db(s, env)
        if k == mf.GRAFANA_ADMIN:
            new = generate_secret(24)
            return Staged({s.name: new}, [s.name], list(s.services), roll=False,
                          before=lambda: self._grafana_reset(new))
        raise RotationError(f"unknown kind {k}")

    def _prepare_db(self, s: mf.Secret, env: str) -> Staged:
        if not self.maintenance:
            raise RotationError("DB_PASSWORD is a maintenance-window rotation (NOT zero-downtime): pass --maintenance")
        user, db = get_env(env, "DB_USER") or "", get_env(env, "DB_NAME") or ""
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", user) or not db:
            raise RotationError("DB_USER must be a plain identifier and DB_NAME must be set")
        if not (get_env(env, s.name) or "").isascii():  # rollback must be able to restore it (SCRAM verifier)
            raise RotationError("the current DB_PASSWORD is not ASCII, so a failed rotation could not restore it: "
                                "nothing was changed")
        if self.prompt(f"This recreates pgbouncer and rolls the app. Type '{DB_CONFIRM_PHRASE}' to continue: ") \
                != DB_CONFIRM_PHRASE:
            raise RotationError("confirmation phrase mismatch, aborted")
        new = generate_secret(32)
        return Staged({s.name: new}, [s.name], list(s.services),
                      {"db_user": user, "db_name": db}, recreate=["pgbouncer", "postgres-exporter"],
                      before=lambda: self._alter_role(user, db, new))

    def _alter_role(self, user: str, db: str, password: str) -> None:
        # Only a SCRAM verifier goes to the server: a failed statement is logged in full by Postgres.
        sql = f"ALTER ROLE \"{user}\" PASSWORD '{scram_verifier(password)}';\n"
        r = self.compose("exec", "-T", "revel_postgres", "psql", "-U", user, "-d", db, "-q",
                         "-v", "ON_ERROR_STOP=1", input_text=sql)  # stdin, not argv
        if r.returncode != 0:
            raise RotationError(f"ALTER ROLE failed (exit {r.returncode})")

    def _grafana_reset(self, password: str) -> None:
        r = self.compose("exec", "-T", "grafana", "grafana", "cli", "admin", "reset-admin-password",
                         "--password-from-stdin", input_text=password + "\n")
        if r.returncode != 0:
            raise RotationError(f"grafana reset-admin-password failed (exit {r.returncode})")

    def require_fallback_support(self) -> None:
        r = self.exec_web("python", "manage.py", "help", "rotate_encrypted_fields")
        if r.returncode != 0:
            raise RotationError("the running backend lacks `rotate_encrypted_fields` (needs the release with "
                                "key fallbacks, backend #1283): refusing to stage SECRET_KEY / SALT_KEY")

    def _check_stageable(self, name: str) -> None:
        entry = self.load_state()["secrets"].get(name, {})
        if MANIFEST[name].kind == mf.GENERATED_WITH_FALLBACK and entry.get("step") == "staged" \
                and entry.get("phase") == 1:
            return  # a previous run stopped after phase 1: resume with the promotion
        next_step(entry.get("step", "idle"), "stage")

    def refuse_if_interrupted(self) -> None:
        """A `pending` entry means a command was killed mid-change: only `rollback` may touch anything."""
        names = [n for n, e in self.load_state()["secrets"].items() if e.get("step") == "pending"]
        if names:
            retry = " ".join(dict.fromkeys(retry_name(n) for n in names))
            raise RotationError(f"a previous command was interrupted while changing {', '.join(names)}: "
                                f"run `rollback {retry}` first")

    @staticmethod
    def _units(names: list[str]) -> list[list[str]]:
        """Group names into atomic stage units: a bundle is one unit, anything else stands alone."""
        seen: set[str] = set()
        units: list[list[str]] = []
        for n in names:
            if n in seen:
                continue
            members = list(mf.BUNDLES[MANIFEST[n].bundle]) if MANIFEST[n].bundle else [n]
            seen.update(members)
            units.append(members)
        return units

    def stage(self, names: list[str], batch: bool = False) -> int:
        """Stage each secret; returns the number of failures."""
        self.refuse_if_interrupted()
        refuse_multiline(self.env_text())  # before any prompt: every edit below would refuse it anyway
        for n in names:
            if n in mf.NOT_DIRECTLY_ROTATABLE:
                raise RotationError(
                    f"{n} is not rotated directly (the old value would stop working at once): roll "
                    f"{mf.NOT_DIRECTLY_ROTATABLE[n]} instead. It is seeded from {n} only when its list is "
                    f"empty and keeps it valid for the grace period; `finalize` drops the old list items but leaves "
                    f"the {n} line in .env (unused while the list is set; remove it by hand if you like).")
        if "DB_PASSWORD" in names and (len(names) > 1 or batch):
            raise RotationError("DB_PASSWORD is staged alone (no other secrets, no --batch): "
                                "ALTER ROLE, recreate pgbouncer/postgres-exporter, roll the app")
        if batch and any(not MANIFEST[n].reversible for n in names):
            raise RotationError("forward-only secrets (TELEGRAM_BOT_TOKEN, PUSHOVER_USER_KEY, CANARY_PASSWORD) are staged by name, "
                                "without --batch")
        if any(MANIFEST[n].kind == mf.GENERATED_WITH_FALLBACK for n in names):
            self.require_fallback_support()
        before = self.load_state()["secrets"] if batch else {}
        try:
            return self._stage_units(names, batch)
        except BaseException:
            # Ctrl-C, or an OSError such as a failed state write. The state on disk, not an in-memory list, says
            # which units this batch staged (a unit can be saved as staged just before the interruption).
            if batch:
                try:
                    now = self.load_state()["secrets"]
                    left = [n for n in names if MANIFEST[n].kind != mf.GENERATED_WITH_FALLBACK
                            and now.get(n, {}).get("step") == "staged" and now.get(n) != before.get(n)]
                except Exception:
                    left = []
                    self.out("INTERRUPTED: check `rotate.py status` for secrets staged by this --batch")
                if left:
                    retry = " ".join(dict.fromkeys(retry_name(n) for n in left))
                    self.out(f"INTERRUPTED: {', '.join(left)} staged by this --batch but not (all) rolled or "
                             f"verified: undo with `rollback {retry}`, then stage again")
            raise

    def _stage_units(self, names: list[str], batch: bool) -> int:
        failures = 0
        pending: list[str] = []
        for unit in self._units(names):
            fallback = MANIFEST[unit[0]].kind == mf.GENERATED_WITH_FALLBACK  # two-phase: always rolls itself
            try:
                for n in unit:
                    self._check_stageable(n)
                if fallback:
                    staged = self._stage_fallback(MANIFEST[unit[0]])
                else:
                    staged = self._stage_group([MANIFEST[n] for n in unit], roll=not batch)
                if batch and not fallback:
                    pending.extend(staged)
            except RotationError as e:
                self.out(f"[{', '.join(unit)}] ERROR: {e}")
                failures += 1
        if pending:
            started = self.now()
            state = self.load_state()["secrets"]
            services = sorted({x for n in pending if state[n].get("roll", True) for x in MANIFEST[n].services},
                              key=_svc_order)
            recreate = sorted({x for n in pending for x in state[n].get("recreate", [])})
            to_verify = sorted({x for n in pending for x in MANIFEST[n].services}, key=_svc_order) + recreate
            ok = False
            try:
                self.roll(services, recreate)
                ok = self.verify(to_verify, started)
            except RotationError as e:
                self.out(f"ERROR: {e}")
            if ok:
                for n in pending:  # now the services run the staged values: finalize may proceed
                    self.update_state(n, unrolled=False)
            else:
                failures += 1
                units: dict[str, list[str]] = {}
                for n in pending:
                    units.setdefault(MANIFEST[n].bundle or n, []).append(n)
                for unit in units.values():  # one failing rollback must not leave the others staged
                    try:
                        self.rollback_unit(unit, reason="batch verify failed")
                    except RotationError as e:
                        self.out(f"[{', '.join(unit)}] ERROR: {e}")
                        self.out(f"[{', '.join(unit)}] stays staged (or pending): fix the cause, then run "
                                 f"`rollback {retry_name(unit[0])}`")
                        failures += 1
        return failures

    def _stage_group(self, secs: list[mf.Secret], roll: bool) -> list[str]:
        """Stage one secret or one bundle with a single backup and a single roll."""
        env = self.env_text()
        plans: list[tuple[mf.Secret, Staged]] = []
        for s in secs:  # a bundle is all or nothing: a required member that is unset skips the lot
            if len(secs) > 1 and not s.optional and not get_env(env, s.name):
                self.out(f"skipped bundle {s.bundle}: {s.name} is unset, nothing was changed")
                return []
        for s in secs:
            if not get_env(env, s.name) and s.kind != mf.LIST_APPEND:
                self.out(f"[{s.name}] unset: nothing to rotate, skipped")
                continue
            self.out(f"[{s.name}] staging ({s.kind})")
            plan = self.prepare(s, env)
            if plan is None:
                if len(secs) > 1:  # a bundle is all or nothing
                    self.out(f"skipped bundle {s.bundle}: nothing was changed")
                    return []
                self.out(f"[{s.name}] not configured, skipped")
                continue
            plans.append((s, plan))
        if plans:
            self._commit(plans, roll)
        return [s.name for s, _ in plans]

    def _stage_fallback(self, s: mf.Secret) -> list[str]:
        """Two phases, because old containers run next to new ones during a rollout and an old
        container that cannot decrypt a row written under the new key would store it back corrupted.
        Phase 1 makes every container able to READ the new key; phase 2 starts WRITING with it."""
        name, fb = s.name, s.fallback_var or ""
        env, entry = self.env_text(), self.load_state()["secrets"].get(name, {})
        if not (entry.get("step") == "staged" and entry.get("phase") == 1):
            old = get_env(env, name)
            if not old:
                raise RotationError(f"{name} is unset")
            if "," in old:
                raise RotationError(f"current {name} contains a comma and cannot be kept in the {fb} list")
            existing = csv_split(get_env(env, fb))
            edits = {fb: ",".join(existing + [generate_secret()])}
            touched, pin = [fb], False
            if name == "SECRET_KEY" and not csv_split(get_env(env, "UNSUBSCRIBE_SIGNING_KEYS")):
                # Pin, introduced like the keys: the old key stays FIRST (containers keep signing with it,
                # so old containers still verify) and every container learns the new one. Phase 2 promotes it.
                # Links in mailboxes keep verifying once SECRET_KEY's fallback is finalized away.
                edits["UNSUBSCRIBE_SIGNING_KEYS"] = f"{old},{generate_secret()}"
                touched.append("UNSUBSCRIBE_SIGNING_KEYS")
                pin = True
            self.out(f"[{name}] phase 1/2: introduce the new key as a fallback")
            self._commit([(s, Staged(edits, touched, list(s.services),
                                     {"phase": 1, "new_index": len(existing), "ever_primary": False,
                                      "pin_created": pin}))], True)
            entry = self.load_state()["secrets"][name]
        else:
            # A run that stopped after phase 1 was saved may have stopped before the rollout applied it. Promoting
            # now could let the first new container write under a key the others cannot read: repeat phase 1.
            self.out(f"[{name}] resuming: repeating the phase 1 rollout before the promotion")
            started, ok = self.now(), False
            try:
                self.roll(list(s.services))
                ok = self.verify(list(s.services), started)
            except RotationError as e:
                self.out(f"ERROR: {e}")
            if not ok:
                raise RotationError(f"{name}: the phase 1 rollout could not be confirmed, so nothing was promoted: "
                                    "fix the cause and run `stage` again (or `rollback`)")
        env = self.env_text()
        fallbacks, old = csv_split(get_env(env, fb)), get_env(env, name) or ""
        if entry["new_index"] >= len(fallbacks):
            raise RotationError(f"{fb} no longer holds the introduced key; fix by hand")
        new = fallbacks[entry["new_index"]]
        edits = {name: new, fb: ",".join([old] + [x for x in fallbacks if x not in (old, new)])}
        pin = csv_split(get_env(env, "UNSUBSCRIBE_SIGNING_KEYS"))
        if entry.get("pin_created") and len(pin) >= 2 and pin[0] == old:
            edits["UNSUBSCRIBE_SIGNING_KEYS"] = ",".join([pin[1], pin[0], *pin[2:]])  # only our own pin
        self.out(f"[{name}] phase 2/2: promote the new key")
        self._commit([(s, Staged(edits, [name, fb], list(s.services), {"phase": 2, "ever_primary": True}))],
                     True, resume=True)
        return [name]

    def _commit(self, plans: list[tuple[mf.Secret, Staged]], roll: bool, resume: bool = False) -> None:
        env, now = self.env_text(), self.now()
        original = env
        backup = backup_file(self.env_path, now)
        # Write-ahead journal: the entries are recorded as "pending" BEFORE .env or a live credential changes,
        # so a hard kill (SIGKILL, power loss, ssh drop) in between leaves a state that `rollback` can undo.
        state = self.load_state()
        previous = {s.name: state["secrets"].get(s.name) for s, _ in plans}
        for s, p in plans:
            old = state["secrets"].get(s.name, {}) if resume else {}
            extra = dict(p.extra, **({"b1": backup.name} if p.extra.get("phase") == 1 else {}))
            entry = dict(old)
            entry.update(
                step="pending", kind=s.kind, staged_at=now.isoformat(), backup=backup.name,
                backups=old.get("backups", []) + [backup.name], vars=p.touched, recreate=p.recreate, roll=p.roll,
                grace_end=(now + s.grace).isoformat() if s.grace and p.extra.get("phase", 2) == 2 else None,
                updated_at=now.isoformat(), unrolled=not roll, **extra)
            state["secrets"][s.name] = entry
        live_done = live_running = False
        try:
            self.save_state(state)
            # The new value reaches .env FIRST (the backup stays); only then does a live credential change,
            # so a failed write can never leave the database/Grafana ahead of .env.
            for _, p in plans:
                for var, val in p.edits.items():
                    env = set_env(env, var, val)
            write_atomic(self.env_path, env)
            for _, p in plans:
                if p.before:
                    live_running = True
                    p.before()
                    live_running, live_done = False, True
            state = self.load_state()
            for s, _ in plans:
                state["secrets"][s.name]["step"] = "staged"
            self.save_state(state)
        except BaseException as exc:
            # A live credential may already be the new one once any before() completed, or when one was cut
            # short by anything but its own clean failure (RotationError: the command exited non-zero, so the
            # server applied nothing). Then .env keeps the new value and the journal stays pending.
            if live_done or (live_running and not isinstance(exc, RotationError)):
                names = " ".join(dict.fromkeys(retry_name(s.name) for s, _ in plans))
                self.out(f"WARNING: interrupted after a live credential may have changed: .env keeps the new "
                         f"value and the state stays pending; run `rollback {names}`")
                raise
            try:
                if self.env_path.read_text(encoding="utf-8") != original:
                    write_atomic(self.env_path, original)
            except OSError:
                self.out(f"WARNING: could not restore .env; the previous one is kept in {backup.name}; "
                         f"the state stays pending: run `rollback`")
                raise
            try:
                state = self.load_state()
                for name, prev in previous.items():  # back to exactly what the command found
                    if prev is None:
                        state["secrets"].pop(name, None)
                    else:
                        state["secrets"][name] = prev
                self.save_state(state)
            except BaseException:
                # Keep the backup (and the new key file): the journal is still pending and `rollback` needs them.
                self.out(f"WARNING: .env was restored but the state could not be: it stays pending and its "
                         f"backup {backup.name} is kept; run `rollback` to finish")
                raise
            backup.unlink(missing_ok=True)
            for _, p in plans:
                if "new_key_file" in p.extra:
                    Path(p.extra["new_key_file"]).unlink(missing_ok=True)
            raise
        if not roll:
            return
        services = sorted({x for _, p in plans if p.roll for x in p.services}, key=_svc_order)
        recreate = sorted({x for _, p in plans for x in p.recreate})
        ok = False
        try:
            self.roll(services, recreate)
            ok = self.verify(sorted({x for _, p in plans for x in p.services}, key=_svc_order) + recreate, now)
        except RotationError as e:
            self.out(f"ERROR: {e}")
        if not ok and any(not s.reversible for s, _ in plans):
            names = ", ".join(s.name for s, _ in plans if not s.reversible)
            svcs = " ".join(services)
            self.out(f"verification failed. {names} was NOT rolled back: the old value is already revoked at the "
                     "provider, so .env keeps the new one.")
            self.out(f"Recover by hand: check the value in .env is the one the provider issued, then run "
                     f"`./deploy-rollout.sh roll {svcs}` and `rotate.py verify {svcs}`.")
            raise RotationError("verification failed; forward-only secret kept (see recovery steps above)")
        if not ok:
            self.out("verification failed: rolling back")
            try:  # a bundle is rolled back as one unit: never a mismatched pair between two restores
                self.rollback_unit([s.name for s, _ in plans], reason="verify failed")
            except RotationError as e:
                self.out(f"ERROR: {e}")
                names = [s.name for s, _ in plans]
                raise RotationError(f"verification failed and the rollback of {', '.join(names)} did not complete: "
                                    f"they stay staged (or pending), re-run `rollback {retry_name(names[0])}`") from None
            raise RotationError("verification failed, rolled back")

    # ---- rollback
    def rollback(self, name: str, reason: str = "requested") -> None:
        self.rollback_unit([name], reason)

    def rollback_unit(self, names: list[str], reason: str = "requested") -> None:
        """Restore the previous values of one secret, or of all members of one bundle together: one .env write,
        one roll, so the services never run a mismatched pair. A key that was ever primary or introduced is
        never removed: rolling back phase 2 keeps the new key in the fallbacks."""
        name = names[0]
        label, retry = ", ".join(names), retry_name(name)
        state = self.load_state()["secrets"]
        entries = {n: state.get(n, {}) for n in names}
        for n in names:
            next_step(entries[n].get("step", "idle"), "rollback")
            if entries[n].get("finalizing"):
                raise RotationError(f"a finalize of {n} was interrupted: run `finalize {n}` again first")
        entry, s = entries[name], MANIFEST[name]
        if len(names) == 1:  # forward-only and two-phase secrets are never part of a bundle
            if not s.reversible and entry.get("step") == "pending":
                self._resolve_pending_forward_only(name, s, entry)
                return
            if not s.reversible:
                raise RotationError(f"{name} cannot be rolled back: the old value was revoked at the provider "
                                    "(BotFather /revoke, Pushover reset, canary password change). Fix forward: re-issue it, put it in .env, "
                                    f"`./deploy-rollout.sh roll {' '.join(s.services)}`")
            if s.kind == mf.GENERATED_WITH_FALLBACK and entry.get("phase") == 1 and entry.get("ever_primary"):
                raise RotationError("the new key has been primary: it stays in the fallbacks. Re-run `stage` to "
                                    "promote it again, or `migrate` once phase 2 is complete")
        fallback = s.kind == mf.GENERATED_WITH_FALLBACK
        env = self.env_text()
        old_pw = None
        for n in names:
            old_env = (self.base / entries[n]["backup"]).read_text(encoding="utf-8")
            for var in entries[n]["vars"]:
                env = set_env(env, var, get_env(old_env, var) or "")
            if n == name:
                old_pw = get_env(old_env, name)
        try:
            if s.kind == mf.DB_PASSWORD and old_pw:
                self._alter_role(entry["db_user"], entry["db_name"], old_pw)
            if s.kind == mf.GRAFANA_ADMIN and old_pw:
                self._grafana_reset(old_pw)
        except RotationError as e:
            # Abort before touching .env or rolling: consumers must keep the value that still matches
            # the live credential. The state is left as it was ("staged" or "pending"), so `rollback` can simply be re-run.
            self.out(f"[{name}] ERROR: could not restore the live credential: {e}")
            raise RotationError(
                f"{name}: the live credential is still the NEW one, and .env, the state and the running "
                f"services were left unchanged (they match it). Fix the service, then re-run "
                f"`rollback {retry}`") from None
        write_atomic(self.env_path, env)
        started = self.now()
        rolled = [n for n in names if entries[n].get("roll", True)]
        services = sorted({x for n in rolled for x in MANIFEST[n].services}, key=_svc_order)
        recreate = sorted({x for n in rolled for x in entries[n].get("recreate") or []})
        try:
            if rolled:
                self.roll(services, recreate)
        except RotationError as e:
            # .env already holds the old values but the services may still run the new ones. Keep the state
            # "staged" (or "pending") and every backup so `rollback` can be re-run: each step above is idempotent (the
            # credential reset to the old value, .env rewritten from the same backup, the roll itself),
            # and the new key file is only deleted after a successful roll.
            self.out(f"[{label}] ERROR: re-roll after rollback failed: {e}")
            for n in names:  # migrate and finalize refuse until a rollback completes
                self.update_state(n, rollback_incomplete=True)
            raise RotationError(
                f"{label}: .env holds the OLD value but the services may still run the NEW one, and the state "
                f"stays staged (or pending) with its backup. Re-run `rollback {retry}` once the rollout works") from None
        for n in names:
            if entries[n].get("new_key_file"):
                Path(entries[n]["new_key_file"]).unlink(missing_ok=True)
        every = sorted({x for n in names for x in MANIFEST[n].services}, key=_svc_order)
        if not self.verify(every + sorted({x for n in names for x in entries[n].get("recreate") or []}), started):
            self.out(f"[{label}] WARNING: verification after rollback failed: check `rotate.py verify`")
        if fallback and entry.get("phase") == 2:
            # Back to "introduced": old key primary, new key kept; its backup stays until finalize.
            self.update_state(name, step="staged", phase=1, backup=entry["b1"], vars=[s.fallback_var],
                              grace_end=None, rollback_incomplete=False)
            self.out(f"[{name}] rolled back to phase 1 ({reason}); the new key stays in {s.fallback_var}")
            return
        for n in names:
            self.update_state(n, step="idle", backups=[], rolled_back_reason=reason, rollback_incomplete=False)
        for n in names:
            self._unlink_unreferenced(entries[n].get("backups", [entries[n]["backup"]]))
            self.out(f"[{n}] rolled back ({reason})")

    def _resolve_pending_forward_only(self, name: str, s: mf.Secret, entry: dict[str, Any]) -> None:
        """Clear the journal of a forward-only secret whose `stage` was killed. The old value is revoked at
        the provider, so it is never restored: the entry only moves to whatever the on-disk `.env` implies."""
        old_env = (self.base / entry["backup"]).read_text(encoding="utf-8")
        env = self.env_text()
        if all(get_env(env, v) == get_env(old_env, v) for v in entry["vars"]):
            # The write never happened: the new value was lost with the killed process.
            self.update_state(name, step="idle", backups=[], rolled_back_reason="interrupted before the .env write")
            self._unlink_unreferenced(entry.get("backups", [entry["backup"]]))
            self.out(f"[{name}] the interrupted stage never wrote the new value: it was lost with the killed "
                     f"process. Get a fresh one from the provider and `stage {name}` again.")
            return
        self.update_state(name, step="staged")  # the backups stay referenced: `finalize` cleans them as usual
        self.out(f"[{name}] the interrupted stage had written the new value to .env, so the entry is now "
                 f"`staged` (the old value is revoked and is NOT restored). Not rolled automatically. "
                 f"Recover by hand: check .env holds the value the provider issued, then run "
                 f"`./deploy-rollout.sh roll {' '.join(s.services)}` and "
                 f"`rotate.py verify {' '.join(s.services)}`.")

    def _unlink_unreferenced(self, files: list[str]) -> None:
        """Delete env backups no live (pending/staged/migrated) secret still points at (bundles share one)."""
        live = {b for e in self.load_state()["secrets"].values()
                if e.get("step") in ("pending", "staged", "migrated") for b in e.get("backups", [])}
        for f in files:
            if f not in live:
                (self.base / f).unlink(missing_ok=True)

    # ---- migrate
    def migrate(self) -> None:
        self.refuse_if_interrupted()
        staged = {n: e for n, e in self.load_state()["secrets"].items()
                  if e.get("step") == "staged" and MANIFEST[n].migrate}
        if not staged:
            raise RotationError("nothing staged that needs a migrate")
        stuck = [n for n, e in staged.items() if e.get("rollback_incomplete")]
        if stuck:
            raise RotationError(f"{', '.join(stuck)}: the last rollback did not finish and the services may still use "
                                f"the new key: re-run `rollback {retry_name(stuck[0])}` first")
        half = [n for n, e in staged.items() if e.get("phase") != 2]
        if half:
            raise RotationError(f"{', '.join(half)}: phase 2 (promotion) is not complete: re-run `stage` first")
        self.require_fallback_support()
        for flags in ([], ["--verify"]):
            r = self.exec_web("python", "manage.py", "rotate_encrypted_fields", *flags)
            if r.stdout:
                self.out(r.stdout.rstrip())
            if r.returncode != 0:
                raise RotationError(f"rotate_encrypted_fields {' '.join(flags)} failed (exit {r.returncode})")
        for n in staged:
            self.update_state(n, step=next_step("staged", "migrate"), migrated_at=self.now().isoformat())
            self.out(f"[{n}] migrated + verified")

    # ---- finalize
    def archive_preflight(self) -> str:
        recipient = get_env(self.env_text(), "ROTATION_AGE_RECIPIENT") or os.environ.get("ROTATION_AGE_RECIPIENT")
        if shutil.which("age") is None or not recipient:
            raise RotationError("archiving needs `age` installed and ROTATION_AGE_RECIPIENT set "
                                "(or pass --no-archive explicitly)")
        trial = self.runner.run(["age", "-r", recipient], input_text="x", stdout_path=Path(os.devnull))
        if trial.returncode != 0:
            raise RotationError("age rejected ROTATION_AGE_RECIPIENT (trial encryption failed)")
        return recipient

    def archive(self, recipient: str, name: str, lines: list[str]) -> Path:
        mkdir_synced(self.archive_dir)
        stamp = self.now()
        # One age stream per file. The file is created exclusively (0600), so two finalizations in the
        # same second can never pick, or append to, the same path.
        sec = stamp.strftime("%Y-%m-%d-%H%M%S")
        candidates = [f"{stamp.strftime('%Y-%m-%d')}.age", f"{sec}-{name}.age"]
        candidates += [f"{sec}-{name}-{n}.age" for n in range(1, 100)]
        # age writes to a private temp name that no `*.age` glob matches. The finished file is synced, then
        # published under its final name with a hard link (it fails if the name exists, so nothing is overwritten).
        fd, tmp_name = tempfile.mkstemp(dir=self.archive_dir, prefix=".archive-", suffix=".part")
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            text = f"# retired {stamp.isoformat()} {name}\n" + "\n".join(lines) + "\n"
            res = self.runner.run(["age", "-r", recipient], input_text=text, stdout_path=tmp)
            if res.returncode != 0:
                raise RotationError("age failed: retired values were NOT archived; nothing was changed")
            os.chmod(tmp, 0o600)
            fd = os.open(tmp, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            for cand in candidates:
                path = self.archive_dir / cand
                try:
                    os.link(tmp, path)
                    break
                except FileExistsError:
                    continue
            else:
                raise RotationError("could not create a unique archive file")
            fsync_dir(self.archive_dir)
            return path
        finally:  # a failed or interrupted run leaves no partial file; the archive, if published, keeps its link
            tmp.unlink(missing_ok=True)

    def confined_key_path(self, container_path: str) -> Path:
        """Host path of a key file, refused unless it really lives inside the certs dir (symlinks resolved)."""
        resolved = self.host_path(container_path).resolve()
        if (self.base / "certs").resolve() not in resolved.parents:
            raise RotationError(f"{container_path} resolves outside {self.base}/certs: refusing")
        return resolved

    def delete_retired_key(self, container_path: str) -> None:
        self.confined_key_path(container_path).unlink(missing_ok=True)

    def finalize(self, names: list[str], force: bool = False, archive: bool = True) -> int:
        """Finalize each secret; returns the number of failures."""
        self.refuse_if_interrupted()
        failures = 0
        recipient = self.archive_preflight() if archive else ""
        for name in names:
            if len(names) > 1 and self.step_of(name) in ("idle", "finalized"):
                self.out(f"[{name}] not staged (step {self.step_of(name)}): nothing to finalize")
                continue
            try:
                self._finalize_one(name, force, recipient)
            except RotationError as e:
                self.out(f"[{name}] ERROR: {e}")
                failures += 1
        return failures

    def _finalize_one(self, name: str, force: bool, recipient: str) -> None:
        s, entry = MANIFEST[name], self.load_state()["secrets"].get(name, {})
        step = entry.get("step", "idle")
        next_step(step, "finalize")
        if entry.get("unrolled"):
            raise RotationError(f"{name} was staged by a --batch whose rollout and verify did not finish: run "
                                f"`rollback {retry_name(name)}`, then stage it again (not overridable)")
        if entry.get("rollback_incomplete"):
            raise RotationError(f"the last rollback of {name} did not finish, so the services may still use the "
                                f"new value: re-run `rollback {retry_name(name)}` first (not overridable)")
        if s.migrate and step != "migrated":
            raise RotationError("encrypted data not migrated yet: run `migrate` first (not overridable)")
        grace_end = entry.get("grace_end")
        if grace_end and self.now() < datetime.fromisoformat(grace_end) and not force:
            raise RotationError(f"grace window open until {grace_end}; use --force to override")
        env = self.env_text()
        # Finalize journal: before the .env edit, the pre-edit copy of .env is recorded in the entry. A finalize
        # that was killed (or could not restore .env) is resumed from that copy, so nothing is inferred from .env.
        journal = entry.get("finalizing")
        plan_env = env
        if journal:
            try:
                plan_env = (self.base / journal["backup"]).read_text(encoding="utf-8")
            except OSError as e:
                raise RotationError(f"an interrupted finalize recorded {journal['backup']}, which cannot be read "
                                    f"({e.strerror}): fix by hand") from None
            self.out(f"[{name}] resuming an interrupted finalize from {journal['backup']}")
        edits: dict[str, str] = {}
        retired: list[str] = []
        key_files: list[str] = []
        if s.kind == mf.GENERATED_WITH_FALLBACK:
            retired = [f"{name}={v}" for v in csv_split(get_env(plan_env, s.fallback_var))]
            edits = {s.fallback_var: ""}
        elif s.kind == mf.GENERATED_FILE_WITH_INACTIVE:
            key_files = entry["retired_paths"]
            for p in key_files:
                if journal and journal.get("archived") and not self.host_path(p).exists():
                    continue  # the interrupted run archived it, then deleted it
                try:
                    data = self.confined_key_path(p).read_bytes()
                except OSError as e:
                    raise RotationError(f"retired key {p} cannot be read ({e.strerror}): nothing was changed") from None
                retired.append(f"OIDC_SIGNING_KEY_FILE:{p}={base64.b64encode(data).decode()}")
            edits = {s.fallback_var: ",".join(csv_drop(csv_split(get_env(plan_env, s.fallback_var)), key_files))}
        elif s.kind == mf.LIST_APPEND:
            items, n_old = csv_split(get_env(plan_env, name)), entry["old_count"]
            if len(items) != n_old + entry["new_count"]:
                raise RotationError(f"{name} was edited since staging; fix by hand")
            retired = [f"{name}={v}" for v in items[:n_old]]
            edits = {name: ",".join(items[n_old:])}
        else:  # one-shot kinds: the retired value is the pre-rotation one held in the backup
            old_env = (self.base / entry["backup"]).read_text(encoding="utf-8")
            retired = [f"{v}={get_env(old_env, v)}" for v in entry["vars"] if get_env(old_env, v)]
        if journal:  # .env must hold either the pre-edit or the edited value, nothing else
            for var, val in edits.items():
                if get_env(env, var) not in (get_env(plan_env, var), val):
                    raise RotationError(f"{var} was edited since the interrupted finalize; fix by hand "
                                        f"(the pre-finalize values are in {journal['backup']})")
        # 1. Archive FIRST: a failure here leaves .env, the state and the backups untouched.
        if not retired:  # e.g. an OIDC resume: the interrupted run archived and deleted the PEM
            self.out(f"[{name}] no retired value left to archive")
        elif recipient:
            where = self.archive(recipient, name, retired)
            self.out(f"[{name}] archived {len(retired)} retired value(s) to {where.relative_to(self.base)}")
        else:
            self.out(f"[{name}] --no-archive: {len(retired)} retired value(s) were NOT archived")
        # 2. Journal, edit .env, roll, verify.
        fin_backup: Path | None = None
        if edits:
            if journal:
                fin_backup = self.base / journal["backup"]
            else:
                fin_backup = backup_file(self.env_path, self.now())
            try:
                self.update_state(name, finalizing={"backup": fin_backup.name,
                                                    "archived": bool(recipient) or bool(journal and journal.get("archived"))})
            except DirSyncError:  # the journal IS saved (it may just not survive a power loss): keep the backup
                raise RotationError(f"{name}: the finalize journal was saved but its directory could not be synced; "
                                    f"{fin_backup.name} is kept and state.json refers to it. Run `finalize {name}` "
                                    f"again: it resumes from that file") from None
            except BaseException:
                if not journal:  # the journal was not saved: nothing refers to the plaintext copy
                    fin_backup.unlink(missing_ok=True)
                raise
            before = env
            try:
                for var, val in edits.items():
                    env = set_env(env, var, val)
                write_atomic(self.env_path, env)
            except DirSyncError:  # .env IS written, so the journal and the backup stay: a re-run resumes
                raise RotationError(f"{name}: .env was written with the finalized value but its directory could "
                                    f"not be synced; the journal and {fin_backup.name} are kept. Run "
                                    f"`finalize {name}` again: it resumes from that file and rolls") from None
            except BaseException:
                try:  # an interrupt after the rename may have replaced .env: only an unchanged .env is safe to undo
                    unchanged = self.env_text() == before
                except OSError:
                    unchanged = False
                if not journal and unchanged:  # undo the journal and drop the plaintext copy
                    self.update_state(name, finalizing=None)
                    fin_backup.unlink(missing_ok=True)
                elif not journal:
                    self.out(f"[{name}] interrupted while .env was being written: the journal and "
                             f"{fin_backup.name} are kept; run `finalize {name}` again to resume")
                raise
            started = self.now()
            ok = False
            try:
                self.roll(list(s.services))
                ok = self.verify(list(s.services), started)
            except RotationError as e:
                self.out(f"[{name}] ERROR: {e}")
            if not ok:
                gone = [p for p in key_files if not self.host_path(p).exists()]
                if gone:  # an earlier run archived and deleted the PEM: restoring its path would point .env at nothing
                    raise RotationError(
                        f"verification failed, but the retired key file(s) {', '.join(gone)} were already archived "
                        f"and deleted, so .env keeps the finalized value and the journal ({fin_backup.name}) is kept: "
                        f"do NOT delete it. Fix the cause, then run `finalize {name}` again.")
                try:
                    restored, fb_text = self.env_text(), fin_backup.read_text(encoding="utf-8")
                    for var in edits:
                        restored = set_env(restored, var, get_env(fb_text, var) or "")
                    write_atomic(self.env_path, restored)
                except Exception as e:  # keep the backup and the journal: they are the only copy of the old values
                    raise RotationError(
                        f"verification failed and .env could not be restored ({e}): the old values are kept in "
                        f"{fin_backup.name} (recorded in state.json; do NOT delete it). Fix the cause, then run "
                        f"`finalize {name}` again: it resumes from that file.") from None
                self.update_state(name, finalizing=None)
                fin_backup.unlink(missing_ok=True)  # plaintext copy no longer needed
                try:
                    self.roll(list(s.services))
                except RotationError:
                    raise RotationError(
                        ".env was restored but the services are out of sync with it: re-roll with "
                        f"`./deploy-rollout.sh roll {' '.join(s.services)}` before doing anything else") from None
                raise RotationError("verification failed after dropping the old values; restored them")
        # 3. Clean up: only now are the retired values safe on disk elsewhere.
        if recipient:
            for p in key_files:
                self.delete_retired_key(p)
        self.update_state(name, step="finalized", finalized_at=self.now().isoformat(), backups=[], finalizing=None)
        self._unlink_unreferenced(entry.get("backups", [entry["backup"]]))
        if fin_backup is not None:
            fin_backup.unlink(missing_ok=True)
        self.out(f"[{name}] finalized")

    # ---- reporting
    def rows(self) -> list[tuple[str, ...]]:
        env, state = self.env_text(), self.load_state()["secrets"]
        rows: list[tuple[str, ...]] = []
        for n, s in MANIFEST.items():
            e = state.get(n, {})
            kind = f"via {mf.NOT_DIRECTLY_ROTATABLE[n]}" if n in mf.NOT_DIRECTLY_ROTATABLE else s.kind
            step = e.get("step", "idle")
            step = "pending (interrupted: run rollback)" if step == "pending" else \
                step + (f" (phase {e['phase']})" if e.get("phase") else "")
            if e.get("finalizing"):
                step += " (finalize interrupted: run finalize)"
            rows.append((n, kind, "set" if get_env(env, n) else "unset", step,
                         (e.get("grace_end") or "-")[:19]))
        return rows

    def table(self, header: tuple[str, ...], rows: list[tuple[str, ...]]) -> None:
        widths = [max(len(r[i]) for r in [header, *rows]) for i in range(len(header))]
        for r in [header, *rows]:
            self.out("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())

    def plan(self) -> None:
        self.table(("SECRET", "KIND", "ENV", "STEP", "GRACE END", "ROLLS"),
                   [(*r, ",".join(MANIFEST[r[0]].services) or "-") for r in self.rows()])

    def status(self) -> None:
        self.table(("SECRET", "KIND", "ENV", "STEP", "GRACE END"), self.rows())
        for f in self.unreferenced_backups():
            self.out(f"WARNING: {f} is not referenced by rotation/state.json and holds .env values (secrets) in "
                     "plaintext (a stage or finalize killed before it recorded the file, or setup.sh). No rotation "
                     "step needs it. Keep it while you recover .env by hand from it; delete it after that.")

    def unreferenced_backups(self) -> list[str]:
        refs = {b for e in self.load_state()["secrets"].values() if e.get("step") in ("pending", "staged", "migrated")
                for b in [e.get("backup"), (e.get("finalizing") or {}).get("backup"), *e.get("backups", [])] if b}
        return sorted(p.name for p in self.base.glob(f"{self.env_path.name}.bak.*") if p.name not in refs)

    def check(self, examples: list[Path]) -> int:
        bad, missing = check_drift([p.read_text(encoding="utf-8") for p in examples],
                                   self.env_text() if self.env_path.exists() else None)
        for n in bad:
            self.out(f"DRIFT: {n} looks like a secret but is not in the manifest (add it or exempt it)")
        for n in missing:
            self.out(f"note: {n} is in the manifest but not set in .env (fine if the feature is off)")
        self.out(f"check: {'FAILED' if bad else 'ok'} ({len(bad)} unmanaged, {len(missing)} not set)")
        return 1 if bad else 0


def _svc_order(svc: str) -> tuple[int, str]:
    return (SERVICE_ORDER.index(svc) if svc in SERVICE_ORDER else len(SERVICE_ORDER), svc)


def check_drift(example_texts: list[str], env_text: str | None) -> tuple[list[str], list[str]]:
    """Return (secret-looking names missing from the manifest, manifest secrets absent from .env)."""
    known = mf.known_names()
    names: set[str] = set()
    for t in example_texts:
        names |= env_keys(t, commented=True)
    if env_text is not None:
        names |= env_keys(env_text)
    bad = sorted(n for n in names if SECRET_LOOKING.search(n) and n not in known
                 and not any(p in n for p in mf.EXEMPT_PATTERNS))
    present = env_keys(env_text) if env_text is not None else set(MANIFEST)
    return bad, sorted(n for n in MANIFEST if n not in present)


# --------------------------------------------------------------------------- CLI
def retry_name(name: str) -> str:
    """What the operator types to retry a rollback: the bundle for a bundle member, else the secret."""
    return MANIFEST[name].bundle or name


def select(names: list[str], group: str | None) -> list[str]:
    for n in names:
        if n not in MANIFEST:
            raise RotationError(f"unknown secret {n} (see `plan`)")
    if group:
        groups = {"self": ("self",), "thirdparty": ("thirdparty",), "all": ("self", "thirdparty")}
        names = names + [n for n, s in MANIFEST.items() if s.group in groups[group] and n not in names
                                 and n not in mf.BY_NAME_ONLY]
    for n in names:
        bundle = MANIFEST[n].bundle
        if bundle and not set(mf.BUNDLES[bundle]) <= set(names):
            raise RotationError(f"{n} belongs to the '{bundle}' bundle and is staged atomically with "
                                f"{', '.join(mf.BUNDLES[bundle])}: name them all")
    return names


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="rotate.py", description=(__doc__ or "").split("\n")[0])
    ap.add_argument("--dir", type=Path, default=Path(__file__).resolve().parent.parent,
                    help="deploy dir holding docker-compose.yml and .env (default: parent of rotation/)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("plan", "status", "migrate"):
        sub.add_parser(c)
    p = sub.add_parser("check")
    p.add_argument("--example", type=Path, action="append", help="extra .env.example to include")
    p = sub.add_parser("stage")
    p.add_argument("names", nargs="*")
    p.add_argument("--group", choices=("self", "thirdparty", "all"))
    p.add_argument("--maintenance", action="store_true", help="allow the DB_PASSWORD maintenance-window rotation")
    p.add_argument("--batch", action="store_true", help="roll and verify once for the whole invocation")
    p = sub.add_parser("verify")
    p.add_argument("services", nargs="*", help="check these services instead of the app services (e.g. alertmanager)")
    p.add_argument("--error-threshold", type=int, default=5)
    p = sub.add_parser("finalize")
    p.add_argument("names", nargs="+")
    p.add_argument("--force", action="store_true", help="finalize before the grace window ends")
    p.add_argument("--no-archive", action="store_true", help="do not archive retired values (they are lost)")
    p = sub.add_parser("rollback")
    p.add_argument("names", nargs="+", help="secret names; a bundle name (aws, apple_wallet) expands to its members")
    return ap


def main(argv: list[str] | None = None, rotator: Rotator | None = None) -> int:
    args = build_parser().parse_args(argv)
    base = args.dir.resolve()
    if rotator is None:
        if not (base / "docker-compose.yml").exists():
            print(f"error: {base}/docker-compose.yml not found", file=sys.stderr)
            return 2
        rotator = Rotator(base, Runner(base), maintenance=getattr(args, "maintenance", False),
                          error_threshold=getattr(args, "error_threshold", 5))
    stack = contextlib.ExitStack()
    try:
        if args.cmd in ("stage", "migrate", "finalize", "rollback"):
            stack.enter_context(rotator.lock())
        if args.cmd == "plan":
            rotator.plan()
        elif args.cmd == "status":
            rotator.status()
        elif args.cmd == "check":
            return rotator.check([base / ".env.example", *(args.example or [])])
        elif args.cmd == "stage":
            names = select(args.names, args.group)
            if not names:
                raise RotationError("name at least one secret or pass --group")
            return 1 if rotator.stage(names, batch=args.batch) else 0
        elif args.cmd == "migrate":
            rotator.migrate()
        elif args.cmd == "verify":
            services = rotator.filter_active(args.services or SERVICE_ORDER)
            if args.services:
                rotator.check_verify_names(args.services, services)
            return 0 if rotator.verify(services, rotator.now() - timedelta(minutes=10)) else 1
        elif args.cmd == "finalize":
            return 1 if rotator.finalize(select(args.names, None), force=args.force,
                                         archive=not args.no_archive) else 0
        elif args.cmd == "rollback":
            names = select([m for n in args.names for m in mf.BUNDLES.get(n, (n,))], None)
            failed = 0
            steps = {n: rotator.step_of(n) for n in names}
            units: dict[str, list[str]] = {}
            for n in names:  # a bundle is rolled back as one unit; keep going: a stuck unit must not strand the rest
                bundle = MANIFEST[n].bundle
                if bundle and steps[n] == "idle" and any(steps[m] != "idle" for m in mf.BUNDLES[bundle]):
                    rotator.out(f"[{n}] already rolled back, skipping")  # retry of a partially rolled-back bundle
                    continue
                units.setdefault(bundle or n, []).append(n)
            for unit in units.values():
                try:
                    rotator.rollback_unit(unit)
                except RotationError as e:
                    print(f"error: {e}", file=sys.stderr)
                    failed += 1
            return 1 if failed else 0
    except RotationError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        stack.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

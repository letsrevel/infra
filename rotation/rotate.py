#!/usr/bin/env python3
"""Secret rotation for a Revel Docker Compose deployment (stdlib only, Python >= 3.10).

    rotate.py plan | status | check | stage <name...|--group self|thirdparty|all> | migrate
              | verify | finalize <name...> [--force] [--no-archive] | rollback <name>

Secret values never appear in argv, logs, stdout or rotation/state.json. See rotation/README.md.
"""
from __future__ import annotations

import argparse
import base64
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
SECRET_LOOKING = re.compile(r"SECRET|TOKEN|KEY|PASSWORD|WEBHOOK_URL")
# Structured (structlog JSON) level, or an uppercase level word in a plain line. Never a bare "error".
ERROR_LINE = re.compile(r'(?i:"level"\s*:\s*"(?:error|critical)")|\b(?:ERROR|CRITICAL)\b')
SERVICE_ORDER = ["web", "celery_default", "beat", "telegram", "frontend"]


class RotationError(Exception):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- env file editing
_LINE = re.compile(r"^(?P<pre>\s*(?:export\s+)?)(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<rest>.*)$")
_DQ = re.compile(r'^"((?:[^"\\]|\\.)*)"(.*)$')
_SQ = re.compile(r"^'((?:[^'\\]|\\.)*)'(.*)$")
_SAFE = re.compile(r"[A-Za-z0-9_.~:/@%+,=\-]*")
_ESC = {"a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v", "\\": "\\", '"': '"', "$": "$"}


def parse_value(rest: str) -> tuple[str, str | None, str]:
    """Split the right-hand side of KEY=... into (decoded value, quote style, trailing text)."""
    if m := _DQ.match(rest):
        body = re.sub(r"\\(.)", lambda e: _ESC.get(e.group(1), e.group(0)), m.group(1))
        return body, '"', m.group(2)
    if m := _SQ.match(rest):
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
    lines = text.split("\n")
    for i in range(len(lines) - 1, -1, -1):
        m = _LINE.match(lines[i])
        if m and m.group("key") == name:
            old, quote, tail = parse_value(m.group("rest"))
            if old == value:
                return text
            lines[i] = f"{m.group('pre')}{name}={encode_value(value, quote)}{tail}"
            return "\n".join(lines)
    base = text if text == "" or text.endswith("\n") else text + "\n"
    return f"{base}{name}={encode_value(value, None)}\n"


def env_keys(text: str, commented: bool = False) -> set[str]:
    keys = {m.group("key") for line in text.split("\n") if (m := _LINE.match(line))}
    if commented:
        keys |= set(re.findall(r"^\s*#\s*([A-Z][A-Z0-9_]*)=", text, re.M))
    return keys


def write_atomic(path: Path, text: str) -> None:
    """Temp file + os.replace, keeping the original file mode (0600 for a new file)."""
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
    os.chmod(dest, 0o600)
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
                with open(stdout_path, "ab") as out:
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
    before: Callable[[], None] | None = None  # side effect run after the backup, before the env write
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

    def load_state(self) -> dict[str, Any]:
        if self.state_path.exists():
            return json.loads(self.state_path.read_text())
        return {"version": 1, "secrets": {}}

    def save_state(self, state: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.state_path.exists():
            self.state_path.touch(mode=0o600)
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
        return self.base / "certs" / container_path[len(prefix):]

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
            self.compose("up", "-d", "--no-deps", "--force-recreate", svc)
        services = self.filter_active(services)
        if not services:
            return
        script = self.base / "deploy-rollout.sh"
        if not script.exists():
            raise RotationError(f"{script} not found (symlink deploy-rollout.sh, notify-discord.sh and "
                                "image-diff.sh into this dir)")
        r = self.runner.run([str(script), "roll", *services], stream=True)
        if r.returncode != 0:
            raise RotationError(f"deploy-rollout.sh roll {' '.join(services)} failed (exit {r.returncode})")

    # ---- verify
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
            results.append((f"error lines since rollout (max {self.error_threshold})", n <= self.error_threshold,
                            f"{n} found"))
        if "frontend" in services:
            r = self.compose("exec", "-T", "frontend", "node", "-e",
                             "require('http').get('http://localhost:3000',"
                             "r=>process.exit(r.statusCode===200?0:1)).on('error',()=>process.exit(1))")
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
        good = {x.get("Service") for x in rows if x.get("State") == "running" and x.get("Health", "") in ("", "healthy")}
        return [s for s in services if s not in good]

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

            return Staged({s.name: new, fb: ",".join(csv_prepend(csv_split(cur(fb)), old))}, [s.name, fb],
                          list(s.services), {"new_key_file": str(host_new), "retired_paths": [old]}, before=make_key)
        if k == mf.LIST_APPEND:
            self.out(s.instructions)
            new = csv_split(self.prompt_secret(f"New {s.name} value(s), comma-separated (hidden): "))
            old = csv_split(cur(s.name))
            if not old and cur("STRIPE_WEBHOOK_SECRET"):  # legacy single secret: keep it valid through the grace period
                old = [cur("STRIPE_WEBHOOK_SECRET") or ""]
            if not new or set(new) & set(old):
                raise RotationError("need at least one new value, none already in the list")
            return Staged({s.name: ",".join(old + new)}, [s.name], list(s.services),
                          {"old_count": len(old), "new_count": len(new)})
        if k == mf.THIRD_PARTY:
            self.out(s.instructions)
            val = self.prompt_secret(f"New value for {s.name} (hidden): ")
            if not val.strip() or re.search(r"[\r\n\x00]", val):
                raise RotationError("value is empty or contains a newline")
            return Staged({s.name: val.strip()}, [s.name], list(s.services))
        if k == mf.THIRD_PARTY_FILE:
            self.out(s.instructions)
            path = self.prompt(f"Container path of the new file for {s.name}: ").strip()
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
        if "DB_PASSWORD" in names and (len(names) > 1 or batch):
            raise RotationError("DB_PASSWORD is staged alone (no other secrets, no --batch): "
                                "ALTER ROLE, recreate pgbouncer/postgres-exporter, roll the app")
        failures = 0
        if any(MANIFEST[n].kind == mf.GENERATED_WITH_FALLBACK for n in names):
            self.require_fallback_support()
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
            ok = False
            try:
                self.roll(services, recreate)
                ok = self.verify(services, started)
            except RotationError as e:
                self.out(f"ERROR: {e}")
            if not ok:
                for n in pending:
                    self.rollback(n, reason="batch verify failed")
                failures += 1
        return failures

    def _stage_group(self, secs: list[mf.Secret], roll: bool) -> list[str]:
        """Stage one secret or one bundle with a single backup and a single roll."""
        env = self.env_text()
        plans: list[tuple[mf.Secret, Staged]] = []
        for s in secs:
            if not get_env(env, s.name) and s.kind != mf.LIST_APPEND:
                self.out(f"[{s.name}] unset: nothing to rotate, skipped")
                continue
            self.out(f"[{s.name}] staging ({s.kind})")
            plan = self.prepare(s, env)
            if plan is None:
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
        backup = backup_file(self.env_path, now)
        try:
            for _, p in plans:
                if p.before:
                    p.before()
            for _, p in plans:
                for var, val in p.edits.items():
                    env = set_env(env, var, val)
            write_atomic(self.env_path, env)
        except BaseException:
            backup.unlink(missing_ok=True)
            for _, p in plans:
                if "new_key_file" in p.extra:
                    Path(p.extra["new_key_file"]).unlink(missing_ok=True)
            raise
        state = self.load_state()
        for s, p in plans:
            old = state["secrets"].get(s.name, {}) if resume else {}
            extra = dict(p.extra, **({"b1": backup.name} if p.extra.get("phase") == 1 else {}))
            entry = dict(old)
            entry.update(
                step="staged", kind=s.kind, staged_at=now.isoformat(), backup=backup.name,
                backups=old.get("backups", []) + [backup.name], vars=p.touched, recreate=p.recreate, roll=p.roll,
                grace_end=(now + s.grace).isoformat() if s.grace and p.extra.get("phase", 2) == 2 else None,
                updated_at=now.isoformat(), **extra)
            state["secrets"][s.name] = entry
        self.save_state(state)
        if not roll:
            return
        services = sorted({x for _, p in plans if p.roll for x in p.services}, key=_svc_order)
        recreate = sorted({x for _, p in plans for x in p.recreate})
        ok = False
        try:
            self.roll(services, recreate)
            ok = self.verify(sorted({x for _, p in plans for x in p.services}, key=_svc_order), now)
        except RotationError as e:
            self.out(f"ERROR: {e}")
        if not ok:
            self.out("verification failed: rolling back")
            for s, _ in plans:
                self.rollback(s.name, reason="verify failed")
            raise RotationError("verification failed, rolled back")

    # ---- rollback
    def rollback(self, name: str, reason: str = "requested") -> None:
        """Restore the secret's previous values. A key that was ever primary or introduced is never
        removed: rolling back phase 2 keeps the new key in the fallbacks."""
        entry = self.load_state()["secrets"].get(name, {})
        next_step(entry.get("step", "idle"), "rollback")
        s = MANIFEST[name]
        fallback = s.kind == mf.GENERATED_WITH_FALLBACK
        if fallback and entry.get("phase") == 1 and entry.get("ever_primary"):
            raise RotationError("the new key has been primary: it stays in the fallbacks. Re-run `stage` to "
                                "promote it again, or `migrate` once phase 2 is complete")
        old_env = (self.base / entry["backup"]).read_text(encoding="utf-8")
        env = self.env_text()
        for var in entry["vars"]:
            env = set_env(env, var, get_env(old_env, var) or "")
        old_pw = get_env(old_env, name)
        try:
            if s.kind == mf.DB_PASSWORD and old_pw:
                self._alter_role(entry["db_user"], entry["db_name"], old_pw)
            if s.kind == mf.GRAFANA_ADMIN and old_pw:
                self._grafana_reset(old_pw)
        except RotationError as e:
            self.out(f"[{name}] WARNING: could not restore the live credential: {e}")
        write_atomic(self.env_path, env)
        if entry.get("new_key_file"):
            Path(entry["new_key_file"]).unlink(missing_ok=True)
        started = self.now()
        try:
            if entry.get("roll", True):
                self.roll(list(s.services), entry.get("recreate"))
            self.verify(list(s.services), started)
        except RotationError as e:
            self.out(f"[{name}] WARNING: re-roll after rollback failed: {e}")
        if fallback and entry.get("phase") == 2:
            # Back to "introduced": old key primary, new key kept; its backup stays until finalize.
            self.update_state(name, phase=1, backup=entry["b1"], vars=[s.fallback_var], grace_end=None)
            self.out(f"[{name}] rolled back to phase 1 ({reason}); the new key stays in {s.fallback_var}")
            return
        self.update_state(name, step="idle", backups=[], rolled_back_reason=reason)
        self._unlink_unreferenced(entry.get("backups", [entry["backup"]]))
        self.out(f"[{name}] rolled back ({reason})")

    def _unlink_unreferenced(self, files: list[str]) -> None:
        """Delete env backups no live (staged/migrated) secret still points at (bundles share one)."""
        live = {b for e in self.load_state()["secrets"].values()
                if e.get("step") in ("staged", "migrated") for b in e.get("backups", [])}
        for f in files:
            if f not in live:
                (self.base / f).unlink(missing_ok=True)

    # ---- migrate
    def migrate(self) -> None:
        staged = {n: e for n, e in self.load_state()["secrets"].items()
                  if e.get("step") == "staged" and MANIFEST[n].migrate}
        if not staged:
            raise RotationError("nothing staged that needs a migrate")
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
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        stamp = self.now()
        path = self.archive_dir / f"{stamp.strftime('%Y-%m-%d')}.age"
        if path.exists():  # one age stream per file: concatenated streams do not decrypt
            path = self.archive_dir / f"{stamp.strftime('%Y-%m-%d-%H%M%S')}-{name}.age"
        text = f"# retired {stamp.isoformat()} {name}\n" + "\n".join(lines) + "\n"
        r = self.runner.run(["age", "-r", recipient], input_text=text, stdout_path=path)
        if r.returncode != 0:
            path.unlink(missing_ok=True)
            raise RotationError("age failed: retired values were NOT archived; nothing was changed")
        os.chmod(path, 0o600)
        return path

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
        failures = 0
        recipient = self.archive_preflight() if archive else ""
        for name in names:
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
        if s.migrate and step != "migrated":
            raise RotationError("encrypted data not migrated yet: run `migrate` first (not overridable)")
        grace_end = entry.get("grace_end")
        if grace_end and self.now() < datetime.fromisoformat(grace_end) and not force:
            raise RotationError(f"grace window open until {grace_end}; use --force to override")
        env = self.env_text()
        edits: dict[str, str] = {}
        retired: list[str] = []
        key_files: list[str] = []
        if s.kind == mf.GENERATED_WITH_FALLBACK:
            retired = [f"{name}={v}" for v in csv_split(get_env(env, s.fallback_var))]
            edits = {s.fallback_var: ""}
        elif s.kind == mf.GENERATED_FILE_WITH_INACTIVE:
            key_files = entry["retired_paths"]
            for p in key_files:
                data = self.confined_key_path(p).read_bytes()
                retired.append(f"OIDC_SIGNING_KEY_FILE:{p}={base64.b64encode(data).decode()}")
            edits = {s.fallback_var: ",".join(csv_drop(csv_split(get_env(env, s.fallback_var)), key_files))}
        elif s.kind == mf.LIST_APPEND:
            items, n_old = csv_split(get_env(env, name)), entry["old_count"]
            if len(items) != n_old + entry["new_count"]:
                raise RotationError(f"{name} was edited since staging; fix by hand")
            retired = [f"{name}={v}" for v in items[:n_old]]
            edits = {name: ",".join(items[n_old:])}
        else:  # one-shot kinds: the retired value is the pre-rotation one held in the backup
            old_env = (self.base / entry["backup"]).read_text(encoding="utf-8")
            retired = [f"{v}={get_env(old_env, v)}" for v in entry["vars"] if get_env(old_env, v)]
        # 1. Archive FIRST: a failure here leaves .env, the state and the backups untouched.
        if recipient:
            where = self.archive(recipient, name, retired)
            self.out(f"[{name}] archived {len(retired)} retired value(s) to {where.relative_to(self.base)}")
        else:
            self.out(f"[{name}] --no-archive: {len(retired)} retired value(s) were NOT archived")
        # 2. Edit .env, roll, verify.
        if edits:
            fin_backup = backup_file(self.env_path, self.now())
            started = self.now()
            for var, val in edits.items():
                env = set_env(env, var, val)
            write_atomic(self.env_path, env)
            ok = False
            try:
                self.roll(list(s.services))
                ok = self.verify(list(s.services), started)
            except RotationError as e:
                self.out(f"[{name}] ERROR: {e}")
            if not ok:
                restored, fb_text = self.env_text(), fin_backup.read_text(encoding="utf-8")
                for var in edits:
                    restored = set_env(restored, var, get_env(fb_text, var) or "")
                write_atomic(self.env_path, restored)
                fin_backup.unlink(missing_ok=True)  # plaintext copy no longer needed
                try:
                    self.roll(list(s.services))
                except RotationError:
                    raise RotationError(
                        ".env was restored but the services are out of sync with it: re-roll with "
                        f"`./deploy-rollout.sh roll {' '.join(s.services)}` before doing anything else") from None
                raise RotationError("verification failed after dropping the old values; restored them")
            fin_backup.unlink(missing_ok=True)
        # 3. Clean up: only now are the retired values safe on disk elsewhere.
        if recipient:
            for p in key_files:
                self.delete_retired_key(p)
        self.update_state(name, step="finalized", finalized_at=self.now().isoformat(), backups=[])
        self._unlink_unreferenced(entry.get("backups", [entry["backup"]]))
        self.out(f"[{name}] finalized")

    # ---- reporting
    def rows(self) -> list[tuple[str, ...]]:
        env, state = self.env_text(), self.load_state()["secrets"]
        rows: list[tuple[str, ...]] = []
        for n, s in MANIFEST.items():
            e = state.get(n, {})
            rows.append((n, s.kind, "set" if get_env(env, n) else "unset", e.get("step", "idle") + (f" (phase {e['phase']})" if e.get("phase") else ""),
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
def select(names: list[str], group: str | None) -> list[str]:
    for n in names:
        if n not in MANIFEST:
            raise RotationError(f"unknown secret {n} (see `plan`)")
    if group:
        groups = {"self": ("self",), "thirdparty": ("thirdparty",), "all": ("self", "thirdparty")}
        names = names + [n for n, s in MANIFEST.items() if s.group in groups[group] and n not in names]
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
    p.add_argument("--error-threshold", type=int, default=5)
    p = sub.add_parser("finalize")
    p.add_argument("names", nargs="+")
    p.add_argument("--force", action="store_true", help="finalize before the grace window ends")
    p.add_argument("--no-archive", action="store_true", help="do not archive retired values (they are lost)")
    p = sub.add_parser("rollback")
    p.add_argument("name")
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
    try:
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
            services = rotator.filter_active(SERVICE_ORDER)
            return 0 if rotator.verify(services, rotator.now() - timedelta(minutes=10)) else 1
        elif args.cmd == "finalize":
            return 1 if rotator.finalize(select(args.names, None), force=args.force,
                                         archive=not args.no_archive) else 0
        elif args.cmd == "rollback":
            select([args.name], None)
            rotator.rollback(args.name)
    except RotationError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

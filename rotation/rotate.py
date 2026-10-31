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
ERROR_LINE = re.compile(r'"level"\s*:\s*"(error|critical)"|\b(ERROR|CRITICAL)\b', re.I)
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


# --------------------------------------------------------------------------- subprocess
class Runner:
    """Runs argv lists. Secrets travel via ``input_text`` (stdin) only, never argv."""

    def __init__(self, cwd: Path, log: Callable[[str], None] = print) -> None:
        self.cwd, self.log = cwd, log

    def run(self, argv: list[str], *, input_text: str | None = None, stdout_path: Path | None = None) -> Any:
        self.log("  $ " + " ".join(argv))
        try:
            if stdout_path is not None:
                with open(stdout_path, "xb") as out:
                    return subprocess.run(argv, cwd=self.cwd, input=(input_text or "").encode(),
                                          stdout=out, stderr=subprocess.PIPE, check=False)
            return subprocess.run(argv, cwd=self.cwd, input=input_text, capture_output=True, text=True, check=False)
        except FileNotFoundError:
            return SimpleNamespace(returncode=127, stdout="", stderr=f"{argv[0]}: not found")


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
        r = self.runner.run([str(script), "roll", *services])
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
        if k == mf.GENERATED_WITH_FALLBACK:
            old, fb = cur(s.name), s.fallback_var
            if not old:
                raise RotationError(f"{s.name} is unset")
            if "," in old:
                raise RotationError(f"current {s.name} contains a comma and cannot be kept in the {fb} list")
            edits = {s.name: generate_secret(), fb: ",".join(csv_prepend(csv_split(cur(fb)), old))}
            if s.name == "SECRET_KEY" and not csv_split(cur("UNSUBSCRIBE_SIGNING_KEYS")):
                # Pin: links already in mailboxes keep verifying once SECRET_KEY's fallback is finalized away.
                # Deliberately not in `touched`: a rollback keeps it (it is harmless and long-lived).
                edits["UNSUBSCRIBE_SIGNING_KEYS"] = f"{generate_secret()},{old}"
            return Staged(edits, [s.name, fb], list(s.services))
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
        sql = f"ALTER ROLE \"{user}\" PASSWORD '{password.replace(chr(39), chr(39) * 2)}';\n"
        r = self.compose("exec", "-T", "revel_postgres", "psql", "-U", user, "-d", db, "-q",
                         "-v", "ON_ERROR_STOP=1", input_text=sql)  # value on stdin, not argv
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

    def stage(self, names: list[str], batch: bool = False) -> int:
        """Stage each secret; returns the number of failures."""
        failures = 0
        if any(MANIFEST[n].kind == mf.GENERATED_WITH_FALLBACK for n in names):
            self.require_fallback_support()
        pending: list[str] = []
        for name in names:
            try:
                next_step(self.step_of(name), "stage")
                if self._stage_one(MANIFEST[name], roll=not batch):
                    pending.append(name)
            except RotationError as e:
                self.out(f"[{name}] ERROR: {e}")
                failures += 1
        if batch and pending:
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

    def _stage_one(self, s: mf.Secret, roll: bool) -> bool:
        env = self.env_text()
        if not get_env(env, s.name) and s.kind != mf.LIST_APPEND:
            self.out(f"[{s.name}] unset: nothing to rotate, skipped")
            return False
        self.out(f"[{s.name}] staging ({s.kind})")
        plan = self.prepare(s, env)
        if plan is None:
            self.out(f"[{s.name}] not configured, skipped")
            return False
        now = self.now()
        backup = backup_file(self.env_path, now)
        try:
            if plan.before:
                plan.before()
            for var, val in plan.edits.items():
                env = set_env(env, var, val)
            write_atomic(self.env_path, env)
        except BaseException:
            backup.unlink(missing_ok=True)
            if "new_key_file" in plan.extra:
                Path(plan.extra["new_key_file"]).unlink(missing_ok=True)
            raise
        self.update_state(
            s.name, step="staged", kind=s.kind, staged_at=now.isoformat(), backup=backup.name, backups=[backup.name],
            vars=plan.touched, recreate=plan.recreate, roll=plan.roll,
            grace_end=(now + s.grace).isoformat() if s.grace else None, **plan.extra)
        if roll:
            ok = False
            try:
                if plan.roll:
                    self.roll(plan.services, plan.recreate)
                ok = self.verify(plan.services, now)
            except RotationError as e:
                self.out(f"[{s.name}] ERROR: {e}")
            if not ok:
                self.out(f"[{s.name}] verification failed: rolling back")
                self.rollback(s.name, reason="verify failed")
                raise RotationError("verification failed, rolled back")
        return True

    # ---- rollback
    def rollback(self, name: str, reason: str = "requested") -> None:
        entry = self.load_state()["secrets"].get(name, {})
        next_step(entry.get("step", "idle"), "rollback")
        s = MANIFEST[name]
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
        (self.base / entry["backup"]).unlink(missing_ok=True)
        self.update_state(name, step="idle", rolled_back_reason=reason)
        self.out(f"[{name}] rolled back ({reason})")

    # ---- migrate
    def migrate(self) -> None:
        targets = [n for n, e in self.load_state()["secrets"].items()
                   if e.get("step") == "staged" and MANIFEST[n].migrate]
        if not targets:
            raise RotationError("nothing staged that needs a migrate")
        self.require_fallback_support()
        for flags in ([], ["--verify"]):
            r = self.exec_web("python", "manage.py", "rotate_encrypted_fields", *flags)
            if r.stdout:
                self.out(r.stdout.rstrip())
            if r.returncode != 0:
                raise RotationError(f"rotate_encrypted_fields {' '.join(flags)} failed (exit {r.returncode})")
        for n in targets:
            self.update_state(n, step=next_step("staged", "migrate"), migrated_at=self.now().isoformat())
            self.out(f"[{n}] migrated + verified")

    # ---- finalize
    def archive_preflight(self) -> str:
        recipient = get_env(self.env_text(), "ROTATION_AGE_RECIPIENT") or os.environ.get("ROTATION_AGE_RECIPIENT")
        if shutil.which("age") is None or not recipient:
            raise RotationError("archiving needs `age` installed and ROTATION_AGE_RECIPIENT set "
                                "(or pass --no-archive explicitly)")
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
            raise RotationError("age failed: retired values were NOT archived; backups kept")
        os.chmod(path, 0o600)
        return path

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
        env, old_env = self.env_text(), (self.base / entry["backup"]).read_text(encoding="utf-8")
        edits: dict[str, str] = {}
        retired: list[str] = []
        if s.kind == mf.GENERATED_WITH_FALLBACK:
            retired = [f"{name}={v}" for v in csv_split(get_env(env, s.fallback_var))]
            edits = {s.fallback_var: ""}
        elif s.kind == mf.GENERATED_FILE_WITH_INACTIVE:
            paths = entry["retired_paths"]
            for p in paths:
                data = self.host_path(p).read_bytes()
                retired.append(f"OIDC_SIGNING_KEY_FILE:{p}={base64.b64encode(data).decode()}")
            edits = {s.fallback_var: ",".join(csv_drop(csv_split(get_env(env, s.fallback_var)), paths))}
        elif s.kind == mf.LIST_APPEND:
            items, n_old = csv_split(get_env(env, name)), entry["old_count"]
            if len(items) != n_old + entry["new_count"]:
                raise RotationError(f"{name} was edited since staging; fix by hand")
            retired = [f"{name}={v}" for v in items[:n_old]]
            edits = {name: ",".join(items[n_old:])}
        else:  # one-shot kinds: the retired value is the pre-rotation one held in the backup
            retired = [f"{v}={get_env(old_env, v)}" for v in entry["vars"] if get_env(old_env, v)]
        backups = [self.base / b for b in entry.get("backups", [entry["backup"]])]
        if edits:
            fin_backup = backup_file(self.env_path, self.now())
            backups.append(fin_backup)
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
                self.roll(list(s.services))
                raise RotationError("verification failed after dropping the old values; restored them")
        if recipient:
            where = self.archive(recipient, name, retired)
            self.out(f"[{name}] archived {len(retired)} retired value(s) to {where.relative_to(self.base)}")
        else:
            self.out(f"[{name}] --no-archive: {len(retired)} retired value(s) were NOT archived")
        for b in backups:
            b.unlink(missing_ok=True)
        self.update_state(name, step="finalized", finalized_at=self.now().isoformat())
        self.out(f"[{name}] finalized")

    # ---- reporting
    def rows(self) -> list[tuple[str, ...]]:
        env, state = self.env_text(), self.load_state()["secrets"]
        rows: list[tuple[str, ...]] = []
        for n, s in MANIFEST.items():
            e = state.get(n, {})
            rows.append((n, s.kind, "set" if get_env(env, n) else "unset", e.get("step", "idle"),
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

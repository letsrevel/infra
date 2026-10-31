"""Tests for rotate.py. No docker, no network: the runner is faked.

    python3 -m unittest rotation/test_rotate.py
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import manifest as mf  # noqa: E402
import rotate as r  # noqa: E402

SERVICES = ["web", "celery_default", "beat", "telegram", "frontend", "revel_postgres", "pgbouncer",
            "postgres-exporter", "grafana", "alertmanager", "canary"]

ENV = """# header comment
SECRET_KEY=oldsecretkey
SALT_KEY="oldsalt+/="
SECRET_KEY_FALLBACKS=
SALT_KEY_FALLBACKS=
UNSUBSCRIBE_SIGNING_KEYS=
DB_USER=revel
DB_NAME=revel
DB_PASSWORD=olddbpass
STRIPE_SECRET_KEY=sk_old
STRIPE_WEBHOOK_SECRETS=whsec_old
TELEGRAM_BOT_TOKEN='tg-old' # bot
GRAFANA_ADMIN_PASSWORD=oldgraf
OIDC_SIGNING_KEY_PATH=/app/certs/oidc.pem
OIDC_SIGNING_KEYS_INACTIVE_PATHS=
API_DOMAIN=api.example.test
"""


class FakeRunner:
    """Records every call; ``fail`` is a predicate on the argv string that makes a call exit 1."""

    def __init__(self, base: Path, fail=None) -> None:
        self.base, self.fail, self.calls = base, fail, []

    def run(self, argv, *, input_text=None, stdout_path=None):
        self.calls.append(SimpleNamespace(argv=list(argv), input=input_text, stdout_path=stdout_path))
        line = " ".join(argv)
        if self.fail and self.fail(line):
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        if line == "docker compose config --services":
            return SimpleNamespace(returncode=0, stdout="\n".join(SERVICES), stderr="")
        if argv[:4] == ["docker", "compose", "ps", "--format"]:
            rows = [{"Service": s, "State": "running", "Health": "healthy"} for s in argv[5:]]
            return SimpleNamespace(returncode=0, stdout=json.dumps(rows), stderr="")
        if argv[0] == "openssl":
            Path(argv[argv.index("-out") + 1]).write_text("PEM")
        if argv[0] == "age":
            Path(stdout_path).write_bytes(b"ENCRYPTED")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def argv_lines(self):
        return [" ".join(c.argv) for c in self.calls]


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / ".env").write_text(ENV)
        os.chmod(self.tmp / ".env", 0o600)
        (self.tmp / "deploy-rollout.sh").write_text("#!/bin/sh\n")
        (self.tmp / "certs").mkdir()
        (self.tmp / "certs" / "oidc.pem").write_text("OLDPEM")
        self.clock = [datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)]
        self.out: list[str] = []
        self.secrets_in: list[str] = []

    def make(self, fail=None, prompts=(), files=(), maintenance=False) -> r.Rotator:
        self.runner = FakeRunner(self.tmp, fail)
        sec, plain = iter(prompts), iter(files)

        def prompt_secret(_msg):
            v = next(sec)
            self.secrets_in.append(v)
            return v

        return r.Rotator(self.tmp, self.runner, prompt_secret=prompt_secret, prompt=lambda _m: next(plain),
                         now=lambda: self.clock[0], sleep=lambda _s: None, out=self.out.append,
                         maintenance=maintenance)

    def env(self, name: str) -> str | None:
        return r.get_env((self.tmp / ".env").read_text(), name)

    def advance(self, **kw) -> None:
        self.clock[0] += timedelta(**kw)


class EnvEditTests(Base):
    def test_only_target_line_changes_preserving_comments_quotes_order(self) -> None:
        text = '# c1\nA=1\nB="two words" # why\nC=\'x\'\n\n# c2\nD=plain # tail\n'
        out = r.set_env(text, "B", 'new$val"q')
        self.assertEqual(out, '# c1\nA=1\nB="new\\$val\\"q" # why\nC=\'x\'\n\n# c2\nD=plain # tail\n')
        self.assertEqual(r.get_env(out, "B"), 'new$val"q')
        out = r.set_env(text, "C", "y")
        self.assertIn("C='y'\n", out)
        out = r.set_env(text, "D", "has space")
        self.assertIn('D="has space" # tail\n', out)

    def test_idempotent(self) -> None:
        once = r.set_env(ENV, "SECRET_KEY", "abc-DEF_123")
        self.assertEqual(r.set_env(once, "SECRET_KEY", "abc-DEF_123"), once)
        self.assertEqual(r.set_env(ENV, "DB_USER", "revel"), ENV)  # unchanged value: byte-identical

    def test_missing_key_is_appended_and_last_assignment_wins(self) -> None:
        self.assertTrue(r.set_env("A=1", "B", "2").endswith("A=1\nB=2\n"))
        out = r.set_env("A=1\nA=2\n", "A", "3")
        self.assertEqual(out, "A=1\nA=3\n")

    def test_newline_in_value_rejected(self) -> None:
        with self.assertRaises(r.RotationError):
            r.set_env("A=1\n", "A", "x\ny")

    def test_atomic_write_keeps_mode_and_leaves_no_temp(self) -> None:
        p = self.tmp / ".env"
        os.chmod(p, 0o640)
        r.write_atomic(p, "A=1\n")
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o640)
        self.assertEqual(p.read_text(), "A=1\n")
        with mock.patch.object(r.os, "replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                r.write_atomic(p, "A=2\n")
        self.assertEqual(p.read_text(), "A=1\n")
        self.assertEqual([f.name for f in self.tmp.iterdir() if ".tmp." in f.name], [])

    def test_backup_is_0600_and_unique(self) -> None:
        b1 = r.backup_file(self.tmp / ".env", self.clock[0])
        self.assertEqual(stat.S_IMODE(b1.stat().st_mode), 0o600)
        self.assertEqual(b1.read_text(), ENV)
        b2 = r.backup_file(self.tmp / ".env", self.clock[0])  # same timestamp: never overwrites
        self.assertNotEqual(b1, b2)


class ListOpsTests(Base):
    def test_prepend_dedupe_drop(self) -> None:
        self.assertEqual(r.csv_prepend(["a", "b"], "c"), ["c", "a", "b"])
        self.assertEqual(r.csv_prepend(["a", "b"], "b"), ["b", "a"])
        self.assertEqual(r.csv_drop(["a", "b", "c"], ["a", "c"]), ["b"])
        self.assertEqual(r.csv_split(" a, ,b ,"), ["a", "b"])

    def test_generated_secrets_never_contain_a_comma(self) -> None:
        self.assertFalse(any("," in r.generate_secret() for _ in range(2000)))

    def test_salt_is_kept_whole_and_never_split(self) -> None:
        rot = self.make()
        rot.stage(["SALT_KEY"])
        self.assertEqual(self.env("SALT_KEY_FALLBACKS"), "oldsalt+/=")
        new = self.env("SALT_KEY")
        self.assertNotIn(",", new)
        self.assertEqual(r.csv_split(self.env("SALT_KEY")), [new])

    def test_salt_with_comma_is_refused(self) -> None:
        (self.tmp / ".env").write_text('SALT_KEY="a,b"\nSALT_KEY_FALLBACKS=\n')
        rot = self.make()
        self.assertEqual(rot.stage(["SALT_KEY"]), 1)
        self.assertEqual(self.env("SALT_KEY"), "a,b")

    def test_second_stage_prepends_to_existing_fallbacks(self) -> None:
        rot = self.make()
        rot.stage(["SALT_KEY"])
        first = self.env("SALT_KEY")
        rot.rollback("SALT_KEY")  # back to idle, then stage again
        rot.stage(["SALT_KEY"])
        self.assertEqual(self.env("SALT_KEY_FALLBACKS"), "oldsalt+/=")
        self.assertNotEqual(self.env("SALT_KEY"), first)


class UnsubscribePinTests(Base):
    def test_pin_set_before_secret_key_rotation(self) -> None:
        rot = self.make()
        rot.stage(["SECRET_KEY"])
        pin = r.csv_split(self.env("UNSUBSCRIBE_SIGNING_KEYS"))
        self.assertEqual(len(pin), 2)
        self.assertEqual(pin[1], "oldsecretkey")
        self.assertNotEqual(pin[0], self.env("SECRET_KEY"))
        self.assertEqual(self.env("SECRET_KEY_FALLBACKS"), "oldsecretkey")

    def test_existing_pin_is_left_alone(self) -> None:
        (self.tmp / ".env").write_text(ENV.replace("UNSUBSCRIBE_SIGNING_KEYS=", "UNSUBSCRIBE_SIGNING_KEYS=k1,k0"))
        self.make().stage(["SECRET_KEY"])
        self.assertEqual(self.env("UNSUBSCRIBE_SIGNING_KEYS"), "k1,k0")

    def test_rollback_restores_secret_key_but_keeps_the_pin(self) -> None:
        rot = self.make()
        rot.stage(["SECRET_KEY"])
        pin = self.env("UNSUBSCRIBE_SIGNING_KEYS")
        rot.rollback("SECRET_KEY")
        self.assertEqual(self.env("SECRET_KEY"), "oldsecretkey")
        self.assertEqual(self.env("SECRET_KEY_FALLBACKS"), "")
        self.assertEqual(self.env("UNSUBSCRIBE_SIGNING_KEYS"), pin)

    def test_pin_is_never_dropped_by_finalize(self) -> None:
        rot = self.make()
        rot.stage(["SECRET_KEY"])
        pin = self.env("UNSUBSCRIBE_SIGNING_KEYS")
        rot.migrate()
        self.advance(days=31)
        rot.finalize(["SECRET_KEY"], archive=False)
        self.assertEqual(self.env("UNSUBSCRIBE_SIGNING_KEYS"), pin)
        self.assertEqual(self.env("SECRET_KEY_FALLBACKS"), "")


class StateMachineTests(Base):
    def test_transitions(self) -> None:
        self.assertEqual(r.next_step("idle", "stage"), "staged")
        self.assertEqual(r.next_step("staged", "migrate"), "migrated")
        self.assertEqual(r.next_step("migrated", "finalize"), "finalized")
        self.assertEqual(r.next_step("finalized", "stage"), "staged")
        for step, action in [("idle", "finalize"), ("idle", "rollback"), ("migrated", "rollback"),
                             ("staged", "stage"), ("finalized", "finalize")]:
            with self.assertRaises(r.RotationError):
                r.next_step(step, action)

    def test_grace_and_migrate_gates(self) -> None:
        rot = self.make()
        rot.stage(["SECRET_KEY"])
        self.assertEqual(rot.step_of("SECRET_KEY"), "staged")
        self.assertEqual(rot.finalize(["SECRET_KEY"], force=True, archive=False), 1)  # not migrated: even --force
        rot.migrate()
        self.assertEqual(rot.step_of("SECRET_KEY"), "migrated")
        self.assertEqual(rot.finalize(["SECRET_KEY"], archive=False), 1)  # grace still open
        self.assertEqual(self.env("SECRET_KEY_FALLBACKS"), "oldsecretkey")
        self.assertEqual(rot.finalize(["SECRET_KEY"], force=True, archive=False), 0)
        self.assertEqual(self.env("SECRET_KEY_FALLBACKS"), "")
        self.assertEqual(rot.step_of("SECRET_KEY"), "finalized")

    def test_finalize_after_grace_without_force(self) -> None:
        rot = self.make()
        rot.stage(["SECRET_KEY"])
        rot.migrate()
        self.advance(days=30, minutes=1)
        self.assertEqual(rot.finalize(["SECRET_KEY"], archive=False), 0)

    def test_salt_has_no_time_grace_only_migrate(self) -> None:
        rot = self.make()
        rot.stage(["SALT_KEY"])
        rot.migrate()
        self.assertEqual(rot.finalize(["SALT_KEY"], archive=False), 0)

    def test_backups_deleted_after_finalize_and_state_has_no_values(self) -> None:
        rot = self.make()
        rot.stage(["SALT_KEY"])
        self.assertTrue(list(self.tmp.glob(".env.bak.*")))
        rot.migrate()
        rot.finalize(["SALT_KEY"], archive=False)
        self.assertEqual(list(self.tmp.glob(".env.bak.*")), [])
        state = (self.tmp / "rotation" / "state.json").read_text()
        self.assertNotIn("oldsalt", state)
        self.assertNotIn(self.env("SALT_KEY"), state)

    def test_verify_failure_rolls_back_automatically(self) -> None:
        rot = self.make(fail=lambda line: line.endswith("manage.py check"), prompts=["sk_new"])
        self.assertEqual(rot.stage(["STRIPE_SECRET_KEY"]), 1)
        self.assertEqual(self.env("STRIPE_SECRET_KEY"), "sk_old")
        self.assertEqual(rot.step_of("STRIPE_SECRET_KEY"), "idle")
        self.assertEqual(list(self.tmp.glob(".env.bak.*")), [])

    def test_preflight_requires_backend_support(self) -> None:
        rot = self.make(fail=lambda line: "help rotate_encrypted_fields" in line)
        with self.assertRaises(r.RotationError):
            rot.stage(["SECRET_KEY", "SALT_KEY"])
        self.assertEqual(self.env("SECRET_KEY"), "oldsecretkey")

    def test_archive_requires_age_and_recipient(self) -> None:
        rot = self.make()
        rot.stage(["SALT_KEY"])
        rot.migrate()
        with mock.patch.object(r.shutil, "which", return_value=None):
            with self.assertRaises(r.RotationError):
                rot.finalize(["SALT_KEY"])
        with mock.patch.object(r.shutil, "which", return_value="/usr/bin/age"), \
                mock.patch.dict(os.environ, {"ROTATION_AGE_RECIPIENT": ""}):
            with self.assertRaises(r.RotationError):
                rot.finalize(["SALT_KEY"])
        self.assertEqual(rot.step_of("SALT_KEY"), "migrated")

    def test_archive_pipes_retired_values_to_age_stdin(self) -> None:
        (self.tmp / ".env").write_text(ENV + "ROTATION_AGE_RECIPIENT=age1abc\n")
        rot = self.make()
        rot.stage(["SALT_KEY"])
        rot.migrate()
        with mock.patch.object(r.shutil, "which", return_value="/usr/bin/age"):
            self.assertEqual(rot.finalize(["SALT_KEY"]), 0)
        call = [c for c in self.runner.calls if c.argv[0] == "age"][0]
        self.assertEqual(call.argv, ["age", "-r", "age1abc"])
        self.assertIn("SALT_KEY=oldsalt+/=", call.input)
        self.assertTrue(str(call.stdout_path).endswith(".age"))
        self.assertEqual(list(self.tmp.glob(".env.bak.*")), [])

    def test_stripe_list_append_and_finalize_drops_only_old(self) -> None:
        rot = self.make(prompts=["whsec_new1, whsec_new2"])
        rot.stage(["STRIPE_WEBHOOK_SECRETS"])
        self.assertEqual(self.env("STRIPE_WEBHOOK_SECRETS"), "whsec_old,whsec_new1,whsec_new2")
        self.assertEqual(rot.finalize(["STRIPE_WEBHOOK_SECRETS"], archive=False), 1)  # 24h grace
        self.advance(hours=25)
        self.assertEqual(rot.finalize(["STRIPE_WEBHOOK_SECRETS"], archive=False), 0)
        self.assertEqual(self.env("STRIPE_WEBHOOK_SECRETS"), "whsec_new1,whsec_new2")

    def test_oidc_new_key_is_644_and_old_path_goes_inactive(self) -> None:
        rot = self.make()
        rot.stage(["OIDC_SIGNING_KEY_PATH"])
        new = self.env("OIDC_SIGNING_KEY_PATH")
        self.assertNotEqual(new, "/app/certs/oidc.pem")
        self.assertEqual(stat.S_IMODE((self.tmp / "certs" / new.rsplit("/", 1)[1]).stat().st_mode), 0o644)
        self.assertEqual(self.env("OIDC_SIGNING_KEYS_INACTIVE_PATHS"), "/app/certs/oidc.pem")
        self.advance(days=31)
        self.assertEqual(rot.finalize(["OIDC_SIGNING_KEY_PATH"], archive=False), 0)
        self.assertEqual(self.env("OIDC_SIGNING_KEYS_INACTIVE_PATHS"), "")

    def test_oidc_skipped_when_unset(self) -> None:
        (self.tmp / ".env").write_text("OIDC_SIGNING_KEY_PATH=\n")
        self.make().stage(["OIDC_SIGNING_KEY_PATH"])
        self.assertFalse(list(self.tmp.glob(".env.bak.*")))

    def test_db_requires_maintenance_and_confirmation(self) -> None:
        with self.assertRaises(r.RotationError):
            self.make().prepare(mf.MANIFEST["DB_PASSWORD"], ENV)
        rot = self.make(maintenance=True, files=["nope"])
        self.assertEqual(rot.stage(["DB_PASSWORD"]), 1)
        self.assertEqual(self.env("DB_PASSWORD"), "olddbpass")
        self.assertFalse([c for c in self.runner.calls if "psql" in c.argv])

    def test_db_flow_order(self) -> None:
        rot = self.make(maintenance=True, files=[r.DB_CONFIRM_PHRASE])
        self.assertEqual(rot.stage(["DB_PASSWORD"]), 0)
        lines = self.runner.argv_lines()
        alter = next(i for i, x in enumerate(lines) if "psql" in x)
        pgb = next(i for i, x in enumerate(lines) if "--force-recreate pgbouncer" in x)
        exp = next(i for i, x in enumerate(lines) if "--force-recreate postgres-exporter" in x)
        roll = next(i for i, x in enumerate(lines) if x.endswith("deploy-rollout.sh roll web celery_default beat telegram"))
        self.assertTrue(alter < pgb < exp < roll)
        self.assertIn("ALTER ROLE \"revel\" PASSWORD", self.runner.calls[alter].input)
        self.assertIn(self.env("DB_PASSWORD"), self.runner.calls[alter].input)

    def test_grafana_resets_via_stdin_and_does_not_roll(self) -> None:
        rot = self.make()
        rot.stage(["GRAFANA_ADMIN_PASSWORD"])
        call = [c for c in self.runner.calls if "reset-admin-password" in c.argv][0]
        self.assertEqual(call.input.strip(), self.env("GRAFANA_ADMIN_PASSWORD"))
        self.assertFalse([x for x in self.runner.argv_lines() if "deploy-rollout.sh" in x])

    def test_third_party_rejects_empty_and_file_needs_644(self) -> None:
        rot = self.make(prompts=["   "])
        self.assertEqual(rot.stage(["STRIPE_SECRET_KEY"]), 1)
        self.assertEqual(self.env("STRIPE_SECRET_KEY"), "sk_old")
        (self.tmp / ".env").write_text("GOOGLE_WALLET_SERVICE_ACCOUNT_KEY_PATH=/app/certs/a.json\n")
        (self.tmp / "certs" / "b.json").write_text("{}")
        os.chmod(self.tmp / "certs" / "b.json", 0o600)
        rot = self.make(files=["/app/certs/b.json"])
        self.assertEqual(rot.stage(["GOOGLE_WALLET_SERVICE_ACCOUNT_KEY_PATH"]), 1)
        os.chmod(self.tmp / "certs" / "b.json", 0o644)
        rot = self.make(files=["/app/certs/b.json"])
        self.assertEqual(rot.stage(["GOOGLE_WALLET_SERVICE_ACCOUNT_KEY_PATH"]), 0)
        self.assertEqual(self.env("GOOGLE_WALLET_SERVICE_ACCOUNT_KEY_PATH"), "/app/certs/b.json")

    def test_services_outside_active_profiles_are_not_rolled(self) -> None:
        rot = self.make(prompts=["x"])
        with mock.patch.object(r.Rotator, "active_services", return_value={"web"}):
            rot.stage(["TELEGRAM_BOT_TOKEN"])
        self.assertFalse([x for x in self.runner.argv_lines() if "deploy-rollout.sh" in x])


class CheckDriftTests(Base):
    def test_unmanaged_secret_looking_keys_fail(self) -> None:
        bad, _ = r.check_drift(["FOO=1\n# NEW_VENDOR_API_TOKEN=\nSLACK_WEBHOOK_URL=\nSTRIPE_SECRET_KEY=\n"], None)
        self.assertEqual(bad, ["NEW_VENDOR_API_TOKEN", "SLACK_WEBHOOK_URL"])

    def test_managed_and_exempt_keys_pass(self) -> None:
        text = ("SECRET_KEY=\nSECRET_KEY_FALLBACKS=\nUNSUBSCRIBE_SIGNING_KEYS=\nTURNSTILE_SITE_KEY=\n"
                "ACCESS_TOKEN_LIFETIME_HOURS=1\nROTATION_AGE_RECIPIENT=\n# OIDC_SIGNING_KEYS_INACTIVE_PATHS=\n")
        self.assertEqual(r.check_drift([text], None)[0], [])

    def test_live_env_drift_and_missing_manifest_secrets(self) -> None:
        bad, missing = r.check_drift(["SECRET_KEY=\n"], "SECRET_KEY=x\nROGUE_PASSWORD=y\n")
        self.assertEqual(bad, ["ROGUE_PASSWORD"])
        self.assertIn("STRIPE_SECRET_KEY", missing)
        self.assertNotIn("SECRET_KEY", missing)

    def test_cli_exit_codes(self) -> None:
        (self.tmp / ".env.example").write_text("SECRET_KEY=\nBRAND_NEW_TOKEN=\n")
        rot = self.make()
        self.assertEqual(rot.check([self.tmp / ".env.example"]), 1)
        self.assertTrue(any("BRAND_NEW_TOKEN" in line for line in self.out))
        (self.tmp / ".env.example").write_text("SECRET_KEY=\n")
        self.assertEqual(rot.check([self.tmp / ".env.example"]), 0)

    def test_repo_env_example_is_covered_by_the_manifest(self) -> None:
        example = Path(__file__).resolve().parent.parent / ".env.example"
        self.assertEqual(r.check_drift([example.read_text()], None)[0], [])


class NoValueLeakTests(Base):
    def test_values_never_reach_argv_logs_stdout_or_state(self) -> None:
        (self.tmp / ".env").write_text(ENV + "ROTATION_AGE_RECIPIENT=age1abc\n")
        os.chmod(self.tmp / "certs" / "oidc.pem", 0o644)
        rot = self.make(prompts=["sk_NEW_stripe", "whsec_NEW", "tg-NEW"], maintenance=True,
                        files=[r.DB_CONFIRM_PHRASE])
        logged: list[str] = []
        real = self.runner.run
        self.runner.run = lambda argv, **kw: (logged.append(" ".join(argv)), real(argv, **kw))[1]
        names = ["SECRET_KEY", "SALT_KEY", "OIDC_SIGNING_KEY_PATH", "GRAFANA_ADMIN_PASSWORD", "STRIPE_SECRET_KEY",
                 "STRIPE_WEBHOOK_SECRETS", "TELEGRAM_BOT_TOKEN", "DB_PASSWORD"]
        self.assertEqual(rot.stage(names), 0)
        rot.migrate()
        self.advance(days=31)
        with mock.patch.object(r.shutil, "which", return_value="/usr/bin/age"):
            self.assertEqual(rot.finalize(names, archive=True), 0)

        env_now = (self.tmp / ".env").read_text()
        values = ["oldsecretkey", "oldsalt+/=", "olddbpass", "sk_old", "whsec_old", "tg-old", "oldgraf",
                  "sk_NEW_stripe", "whsec_NEW", "tg-NEW"]
        values += [self.env(n) for n in ("SECRET_KEY", "SALT_KEY", "DB_PASSWORD", "GRAFANA_ADMIN_PASSWORD")]
        argv_blob = "\n".join(self.runner.argv_lines())
        state_blob = (self.tmp / "rotation" / "state.json").read_text()
        for v in values:
            self.assertTrue(v)
            self.assertNotIn(v, argv_blob, f"value leaked into argv: {v[:4]}...")
            self.assertNotIn(v, "\n".join(logged))
            self.assertNotIn(v, "\n".join(self.out), f"value leaked into stdout: {v[:4]}...")
            self.assertNotIn(v, state_blob, f"value leaked into state: {v[:4]}...")
        # the secrets did travel, but only via stdin
        self.assertTrue(any(c.input and self.env("DB_PASSWORD") in c.input for c in self.runner.calls))
        self.assertTrue(any(c.input and "SECRET_KEY=oldsecretkey" in c.input for c in self.runner.calls))
        self.assertNotIn("olddbpass", env_now)


class ManifestTests(unittest.TestCase):
    def test_every_entry_is_well_formed(self) -> None:
        kinds = {mf.GENERATED_WITH_FALLBACK, mf.GENERATED_FILE_WITH_INACTIVE, mf.LIST_APPEND, mf.THIRD_PARTY,
                 mf.THIRD_PARTY_FILE, mf.DB_PASSWORD, mf.GRAFANA_ADMIN}
        for name, s in mf.MANIFEST.items():
            self.assertEqual(name, s.name)
            self.assertIn(s.kind, kinds)
            if s.kind in (mf.GENERATED_WITH_FALLBACK, mf.GENERATED_FILE_WITH_INACTIVE):
                self.assertTrue(s.fallback_var, name)
            if s.kind in (mf.THIRD_PARTY, mf.THIRD_PARTY_FILE, mf.LIST_APPEND):
                self.assertTrue(s.instructions, name)

    def test_groups_exclude_db_password(self) -> None:
        self.assertNotIn("DB_PASSWORD", r.select([], "all"))
        self.assertIn("SECRET_KEY", r.select([], "self"))
        self.assertIn("STRIPE_SECRET_KEY", r.select([], "thirdparty"))
        with self.assertRaises(r.RotationError):
            r.select(["NOPE"], None)


if __name__ == "__main__":
    unittest.main()

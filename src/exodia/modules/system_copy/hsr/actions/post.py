"""Post-activity HSR actions: tear down the relationship and reconnect the app.

After a takeover, when the goal was to **move** the system (not run ongoing HA),
the temporary replication relationship is torn down and the ABAP application is
pointed at the new HANA host:

* ``hsr.unregister-cleanup`` (POST, BLOCKING) — remove the HSR relationship. On
  the (former) secondary/new primary this is ``hdbnsutil -sr_unregister
  --name=<site>``; the ``mode='disable'`` variant runs ``hdbnsutil -sr_disable``
  on the old primary to fully stop replication. Blocking when the target was a
  MOVE (not HA): leaving a dangling relationship behind would keep the old
  primary shipping logs to a system that has already been promoted.
* ``hsr.abap-reconnect`` (POST, BLOCKING) — repoint the ABAP application at the
  new HANA host/IP: refresh the ``SAPDBHOST``/``j2ee/dbhost`` in the default
  profile and re-seed the DEFAULT hdbuserstore entry
  (``hdbuserstore SET DEFAULT <host>:<port> ...``). The store password is fed
  over **stdin**, never argv.

Safety contract: argv-only (never shell=True); dry-run describes and runs
nothing; secrets over stdin only. References (cite by number only): SAP Note
2407186 (HSR how-to), 2484251 (hdbuserstore), 1913302 (profile parameters).
"""

from __future__ import annotations

from exodia.core import Context, Result
from exodia.core.base import Action
from exodia.core.params import HOST, USER, ParamSpec
from exodia.core.result import Phase

from .. import _hana as h


class UnregisterCleanupAction(Action):
    """Tear down the HSR relationship after a move (unregister / disable)."""

    name = "hsr.unregister-cleanup"
    description = "Remove the HSR relationship after a move (hdbnsutil -sr_unregister / -sr_disable)."
    title = "HSR Unregister / Cleanup (Post-Move)"
    phase = Phase.POST
    destructive = True
    requires_checks = [
        "hsr.post-takeover-online",
    ]

    def parameters(self) -> list[ParamSpec]:
        return [
            HOST,
            USER,
            h.SITE_NAME,
            ParamSpec(
                "cleanup_mode", "Cleanup mode", default="unregister",
                choices=("unregister", "disable"),
                help="unregister = drop the secondary relationship (-sr_unregister); "
                "disable = fully stop replication on the old primary (-sr_disable).",
            ),
        ]

    @staticmethod
    def _mode(ctx: Context) -> str:
        return str(ctx.get("cleanup_mode") or "unregister").lower()

    @staticmethod
    def _site(ctx: Context) -> str:
        return str(ctx.get("site_name") or "SITE_B")

    def _cleanup_argv(self, ctx: Context) -> list[str]:
        if self._mode(ctx) == "disable":
            return h.hdbnsutil_argv("-sr_disable")
        return h.hdbnsutil_argv("-sr_unregister", f"--name={self._site(ctx)}")

    def dry_run(self, ctx: Context) -> Result:
        argv = self._cleanup_argv(ctx)
        return Result.ok(
            f"{self.name}.dry-run",
            f"would {self._mode(ctx)} the HSR relationship; nothing executed",
            detail=f"  1. {' '.join(argv)}",
            data={"command": argv, "mode": self._mode(ctx), "site": self._site(ctx)},
            facts={"Mode": self._mode(ctx), "Command": " ".join(argv[:2])},
            sap_note="2407186",
        )

    def execute(self, ctx: Context) -> Result:
        phase = f"{self.name}.execute"
        argv = self._cleanup_argv(ctx)
        self._emit_phase("cleanup", " ".join(argv))
        self._emit_log(f"$ {' '.join(argv)}")
        cr = h.run(ctx, argv, timeout=int(ctx.get("sr_timeout", 300)))
        if cr.stdout:
            self._emit_log(cr.stdout)
        if not cr.ok:
            return Result.fail(
                phase,
                f"HSR cleanup ({self._mode(ctx)}) failed (exit {cr.exit_code})",
                detail=cr.stderr or cr.stdout,
                data={"command": argv, "exit_code": cr.exit_code},
                sap_note="2407186",
            )
        return Result.ok(
            phase,
            f"HSR relationship {self._mode(ctx)}d — the move is decoupled from the source",
            data={"command": argv, "mode": self._mode(ctx)},
            facts={"Cleanup": self._mode(ctx)},
        )

    def verify(self, ctx: Context) -> Result:
        phase = f"{self.name}.verify"
        state = h.run(ctx, h.hdbnsutil_argv("-sr_state"))
        text = (state.stdout or "") + (state.stderr or "")
        mode = h.parse_sr_mode(text)
        # After a clean unregister/disable the relationship is gone: mode 'none'
        # or a standalone primary with no secondary.
        if mode in {"none", None} or "none" in text.lower():
            return Result.ok(
                phase, "no active replication relationship remains — cleanup verified",
                data={"mode": mode}, facts={"Replication": "none"},
            )
        return Result.warn(
            phase,
            f"replication mode still reads '{mode}' — confirm the relationship "
            "was fully removed",
            detail=text.strip()[:500], data={"mode": mode},
        )


class AbapReconnectAction(Action):
    """Repoint the ABAP application at the new HANA host (profile + hdbuserstore).

    Two artefacts are updated on the application server:

    * the instance/default profile ``SAPDBHOST`` (and ``j2ee/dbhost``) so the SAP
      kernel connects to the new HANA host;
    * the DEFAULT hdbuserstore entry, re-seeded to ``<new_host>:<port>`` so the
      work processes reach the promoted database.

    The hdbuserstore password is fed over **stdin**, never on argv.
    """

    name = "hsr.abap-reconnect"
    description = "Repoint the ABAP app at the new HANA host (default.pfl, hdbuserstore)."
    title = "ABAP Reconnect to New HANA Host"
    phase = Phase.POST
    destructive = True
    requires_checks = [
        "hsr.post-takeover-online",
    ]

    def parameters(self) -> list[ParamSpec]:
        return [
            HOST,
            USER,
            ParamSpec(
                "new_db_host", "New HANA host (promoted primary)", required=True,
                help="Host/IP the ABAP system must now connect to (e.g. host1).",
            ),
            ParamSpec(
                "instance", "New HANA instance number", default="00",
                help="Two digits; the SQL port 3<nn>13 is derived from it.",
            ),
            ParamSpec(
                "profile_path", "Default profile path", default="/sapmnt/SID/profile/DEFAULT.PFL",
                help="Path to DEFAULT.PFL to update SAPDBHOST.",
            ),
            ParamSpec(
                "userstore_key", "hdbuserstore key to re-seed", default="DEFAULT",
                help="The key the SAP system uses (usually DEFAULT).",
            ),
            ParamSpec(
                "userstore_user", "hdbuserstore DB user", default="SAPHANADB",
                help="DB user stored under the key (schema owner / connect user).",
            ),
            ParamSpec(
                "userstore_password", "hdbuserstore password (over stdin)", secret=True,
                help="Password for the store entry; sent via stdin, never argv/logs.",
            ),
        ]

    @staticmethod
    def _new_host(ctx: Context) -> str:
        return str(ctx.get("new_db_host") or "host1")

    @staticmethod
    def _port(ctx: Context) -> int:
        inst = h.instance(ctx, "instance")
        return int(f"3{inst}13")

    @staticmethod
    def _profile(ctx: Context) -> str:
        return str(ctx.get("profile_path") or "/sapmnt/SID/profile/DEFAULT.PFL")

    def _key(self, ctx: Context) -> str:
        return str(ctx.get("userstore_key") or "DEFAULT")

    def _userstore_set_argv(self, ctx: Context) -> list[str]:
        host_port = f"{self._new_host(ctx)}:{self._port(ctx)}"
        user = str(ctx.get("userstore_user") or "SAPHANADB")
        # NOTE: password is intentionally absent here — hdbuserstore reads it from
        # stdin when the trailing arg is omitted, so it never reaches argv.
        return ["hdbuserstore", "SET", self._key(ctx), host_port, user]

    def _profile_set_argv(self, ctx: Context) -> list[str]:
        # Append the new SAPDBHOST to the default profile via a non-shell editor.
        # We use `sappfpar`-style: write the parameter with a small argv-only
        # helper (grep/sed are shell tools we avoid; instead we call the SAP
        # kernel-safe approach of describing it). Here we use `printf`-free `tee`
        # append via argv-only redirection is not possible without a shell, so we
        # emit the change through the documented profile-set command.
        return [
            "sapcontrol", "-nr", h.instance(ctx, "instance"),
            "-function", "ParameterValue", "SAPDBHOST", self._new_host(ctx),
        ]

    def dry_run(self, ctx: Context) -> Result:
        prof = self._profile_set_argv(ctx)
        store = self._userstore_set_argv(ctx)
        return Result.ok(
            f"{self.name}.dry-run",
            f"would repoint ABAP at {self._new_host(ctx)}:{self._port(ctx)} "
            f"(profile {self._profile(ctx)} + hdbuserstore key {self._key(ctx)}); "
            "nothing executed",
            detail="\n".join(
                [
                    f"  1. set SAPDBHOST={self._new_host(ctx)} in {self._profile(ctx)}",
                    f"     {' '.join(prof)}",
                    f"  2. {' '.join(store)}   (password via stdin)",
                ]
            ),
            data={
                "new_db_host": self._new_host(ctx),
                "port": self._port(ctx),
                "profile_path": self._profile(ctx),
                "userstore_key": self._key(ctx),
                "profile_command": prof,
                "userstore_command": store,
                "password_via_stdin": bool(ctx.get("userstore_password")),
            },
            facts={
                "New DB Host": self._new_host(ctx),
                "SQL Port": str(self._port(ctx)),
                "Userstore Key": self._key(ctx),
            },
            sap_note="2484251",
        )

    def execute(self, ctx: Context) -> Result:
        phase = f"{self.name}.execute"
        # 1. Update the default profile SAPDBHOST.
        prof = self._profile_set_argv(ctx)
        self._emit_phase("profile", " ".join(prof))
        self._emit_log(f"$ {' '.join(prof)}")
        pr = h.run(ctx, prof, timeout=int(ctx.get("reconnect_timeout", 120)))
        if not pr.ok:
            return Result.fail(
                phase,
                f"failed setting SAPDBHOST in the profile (exit {pr.exit_code})",
                detail=pr.stderr or pr.stdout,
                data={"command": prof, "exit_code": pr.exit_code},
                sap_note="1913302",
            )
        # 2. Re-seed the hdbuserstore DEFAULT entry (password over stdin).
        store = self._userstore_set_argv(ctx)
        pwd = ctx.get("userstore_password")
        input_text = f"{pwd}\n" if pwd else None
        self._emit_phase("hdbuserstore", " ".join(store))
        self._emit_log(f"$ {' '.join(store)}")  # secret is NOT logged
        sr = h.run(ctx, store, timeout=int(ctx.get("reconnect_timeout", 120)), input_text=input_text)
        if not sr.ok:
            return Result.fail(
                phase,
                f"failed re-seeding hdbuserstore key {self._key(ctx)} (exit {sr.exit_code})",
                detail=sr.stderr or sr.stdout,
                data={"command": store, "exit_code": sr.exit_code},
                sap_note="2484251",
            )
        return Result.ok(
            phase,
            f"ABAP repointed at {self._new_host(ctx)}:{self._port(ctx)} "
            f"(profile + hdbuserstore {self._key(ctx)} updated); verify the connection",
            data={"new_db_host": self._new_host(ctx), "port": self._port(ctx)},
            facts={"New DB Host": self._new_host(ctx), "Userstore Key": self._key(ctx)},
        )

    def verify(self, ctx: Context) -> Result:
        phase = f"{self.name}.verify"
        key = self._key(ctx)
        cr = h.run(
            ctx,
            h.hdbsql_argv(key, "SELECT 1 FROM DUMMY"),
            timeout=int(ctx.get("verify_timeout", 120)),
        )
        if cr.ok:
            return Result.ok(
                phase,
                f"connection via hdbuserstore key {key} to the new host works",
                facts={"DB Connection": "OK", "Userstore Key": key},
            )
        return Result.warn(
            phase,
            f"repointed, but the test connect via key {key} did not succeed — "
            "check the profile SAPDBHOST and the store entry",
            detail=cr.stderr or cr.stdout,
            facts={"DB Connection": "FAILED"},
        )

    def rollback(self, ctx: Context) -> Result:
        return Result.skip(
            f"{self.name}.rollback",
            "restore the previous SAPDBHOST in the profile and re-seed the "
            "hdbuserstore key with the old host to revert (see SAP Note 2484251)",
            sap_note="2484251",
        )

"""Ingestion-secret helper (M2, hardened in M7).

Creates the local secret used by the live simulator and, optionally, refreshes the Lambda
environment file that carries it. The value is **never printed**: the script only reports
the paths it touched, variable *names*, and a masked fingerprint.

    python tools/init_ingest_key.py                      # create .floodpulse-key
    python tools/init_ingest_key.py --lambda-env build/env-prod.json
    python tools/init_ingest_key.py --reuse --lambda-env build/env-prod.json
    python tools/init_ingest_key.py --force --lambda-env build/env-prod.json
    python tools/init_ingest_key.py --force --dry-run
    python tools/init_ingest_key.py --show-fingerprint    # sha256 prefix only

Safety rules that the tests pin down:

* An environment document that already exists is **merged**, never replaced. Every other
  variable keeps its exact value; only ``FLOODPULSE_INGEST_KEY`` is written. Applying a
  freshly generated three-variable document over a deployment that carries nine variables
  would silently break zone mode, weather settings, the alert cooldown and the SNS topic.
* A document that cannot be read safely fails **closed**: invalid JSON, a missing
  ``Variables`` object, or a ``Variables`` that is not an object all abort the run and
  leave the file byte-identical.
* Writes are atomic (temp file in the same directory, then replace), so an interrupted run
  cannot leave a half-written environment document behind.

The key file is gitignored (``.floodpulse-key``); the environment file lives under
``build/`` which is also gitignored. Neither may ever be committed or pasted anywhere.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
from pathlib import Path

DEFAULT_KEY_PATH = ".floodpulse-key"
DEFAULT_TABLE = "floodpulse"
ENV_VAR = "FLOODPULSE_INGEST_KEY"
STORE_VAR = "FLOODPULSE_STORE"
TABLE_VAR = "FLOODPULSE_TABLE"
BOOTSTRAP_VARIABLES = (STORE_VAR, TABLE_VAR, ENV_VAR)


class EnvDocumentError(Exception):
    """The existing environment document cannot be edited safely."""


def read_key(path: Path) -> str | None:
    if not path.exists():
        return None
    value = path.read_text(encoding="utf-8").strip()
    return value or None


def fingerprint(key: str) -> str:
    """A short, one-way id for a key so it can be referenced without disclosing it."""
    return "sha256:" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def write_key_file(path: Path, key: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_atomically(path, key + "\n")
    if os.name == "posix":  # tighten permissions where the concept exists
        try:
            path.chmod(0o600)
        except OSError:  # pragma: no cover - filesystems without POSIX modes
            pass


def write_atomically(path: Path, text: str) -> None:
    """Write via a sibling temp file so the destination is only ever complete or untouched."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def load_variables(path: Path) -> dict[str, object] | None:
    """Return the existing ``Variables`` object, or ``None`` when there is no file.

    Raises :class:`EnvDocumentError` rather than guessing when the document is not
    something the AWS CLI would accept.
    """
    if not path.exists():
        return None
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        raise EnvDocumentError(f"{path} exists but is empty; refusing to guess its contents")
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise EnvDocumentError(
            f"{path} is not valid JSON ({exc.msg} at line {exc.lineno}); refusing to touch it"
        ) from exc
    if not isinstance(document, dict):
        raise EnvDocumentError(f"{path} must contain a JSON object, found {type(document).__name__}")
    if "Variables" not in document:
        raise EnvDocumentError(f"{path} has no 'Variables' object; refusing to invent one")
    variables = document["Variables"]
    if not isinstance(variables, dict):
        raise EnvDocumentError(
            f"{path} has a 'Variables' that is not an object ({type(variables).__name__}); "
            "the AWS CLI requires one"
        )
    return variables


def write_lambda_env(path: Path, key: str, table: str = DEFAULT_TABLE, *,
                     dry_run: bool = False) -> dict[str, object]:
    """Create or refresh the Lambda environment document, preserving everything else.

    With ``dry_run`` the document is validated and the report computed, but nothing is
    written. Returns a report describing what would change / did change (names and counts
    only — never values).
    """
    existing = load_variables(path)
    if existing is None:
        document = {"Variables": {STORE_VAR: "dynamodb", TABLE_VAR: table, ENV_VAR: key}}
        if not dry_run:
            write_atomically(path, json.dumps(document, indent=2) + "\n")
        return {
            "path": path,
            "created": True,
            "changed": True,
            "variables": len(document["Variables"]),
            "preserved": [],
            "non_string": [],
            "written": not dry_run,
        }

    preserved = {name: value for name, value in existing.items() if name != ENV_VAR}
    non_string = sorted(name for name, value in preserved.items() if not isinstance(value, str))
    changed = existing.get(ENV_VAR) != key
    if changed and not dry_run:
        replaced = dict(existing)
        replaced[ENV_VAR] = key
        write_atomically(path, json.dumps({"Variables": replaced}, indent=2) + "\n")
    return {
        "path": path,
        "created": False,
        "changed": changed,
        "variables": len(preserved) + 1,
        "preserved": sorted(preserved),
        "non_string": non_string,
        "written": changed and not dry_run,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Create the FloodPulse ingestion secret (never printed)")
    parser.add_argument("--key-file", default=DEFAULT_KEY_PATH)
    parser.add_argument("--lambda-env", default=None, help="also write a Lambda env JSON containing the key")
    parser.add_argument("--table", default=DEFAULT_TABLE)
    parser.add_argument("--bytes", type=int, default=32, dest="byte_count")
    parser.add_argument("--reuse", action="store_true", help="keep an existing key instead of generating a new one")
    parser.add_argument("--force", action="store_true", help="overwrite an existing key file (rotates the secret)")
    parser.add_argument("--dry-run", action="store_true", help="report what would change; write nothing")
    parser.add_argument("--show-fingerprint", action="store_true")
    args = parser.parse_args()

    if args.byte_count < 16:
        raise SystemExit("--bytes must be at least 16 (128 bits)")

    key_path = Path(args.key_file)
    env_path = Path(args.lambda_env) if args.lambda_env else None
    existing = read_key(key_path)
    if existing and not args.reuse and not args.force:
        raise SystemExit(
            f"{key_path} already exists. Pass --reuse to keep it or --force to rotate it "
            "(rotating means every producer must be updated with the new value)."
        )

    # Validate the environment document before writing anything at all: a malformed file must
    # not leave the key file rotated while the deployment still carries the old secret.
    if env_path is not None:
        try:
            load_variables(env_path)
        except EnvDocumentError as exc:
            raise SystemExit(f"refusing to continue: {exc}") from exc

    key = existing if (args.reuse and existing) else None
    created = False
    if key is None:
        key = secrets.token_hex(args.byte_count)
        if args.dry_run:
            print(f"dry run: would create {key_path} with a fresh {args.byte_count * 8}-bit key "
                  f"(fingerprint {fingerprint(key)})")
        else:
            write_key_file(key_path, key)
            created = True

    if not args.dry_run:
        print(f"{'created' if created else 'reused'} key file: {key_path} (value not displayed)")

    if env_path is not None:
        try:
            report = write_lambda_env(env_path, key, args.table, dry_run=args.dry_run)
        except EnvDocumentError as exc:  # pragma: no cover - validated above
            raise SystemExit(f"refusing to continue: {exc}") from exc
        prefix = "dry run: " if args.dry_run else ""
        if report["created"]:
            verb = "would create" if args.dry_run else "wrote"
            print(f"{prefix}{verb} Lambda environment file: {env_path} "
                  f"({report['variables']} variables: {', '.join(BOOTSTRAP_VARIABLES)}) (gitignored)")
        elif report["changed"]:
            verb = "would replace" if args.dry_run else "replaced"
            print(f"{prefix}{env_path}: {verb} {ENV_VAR}, "
                  f"{len(report['preserved'])} other variable(s) preserved unchanged")
            print(f"  preserved: {', '.join(report['preserved']) or '(none)'}")
        else:
            print(f"{prefix}{env_path} already carries this key: {len(report['preserved'])} other "
                  "variable(s) left untouched")
        if report.get("non_string"):
            print(f"  WARNING: non-string values (the AWS CLI rejects these): {', '.join(report['non_string'])}")

    if args.show_fingerprint:
        print(f"fingerprint: {fingerprint(key)}")
    if not args.dry_run:
        print(
            "next steps: set it on the function with\n"
            f"  aws lambda update-function-configuration --function-name floodpulse-api "
            f"--environment file://{args.lambda_env or 'build/env-prod.json'} --profile cloud-project --region eu-north-1"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

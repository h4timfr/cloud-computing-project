"""Ingestion-secret helper (M2).

Creates the local secret used by the live simulator and, optionally, the Lambda
environment file that carries it. The value is **never printed**: the script only reports
the path it wrote and a masked fingerprint.

    python tools/init_ingest_key.py                      # create .floodpulse-key
    python tools/init_ingest_key.py --lambda-env build/env-prod.json
    python tools/init_ingest_key.py --show-fingerprint    # sha256 prefix only

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


def read_key(path: Path) -> str | None:
    if not path.exists():
        return None
    value = path.read_text(encoding="utf-8").strip()
    return value or None


def fingerprint(key: str) -> str:
    """A short, one-way id for a key so it can be referenced without disclosing it."""
    return "sha256:" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def write_key_file(path: Path, key: str) -> None:
    path.write_text(key + "\n", encoding="utf-8")
    if os.name == "posix":  # tighten permissions where the concept exists
        try:
            path.chmod(0o600)
        except OSError:  # pragma: no cover - filesystems without POSIX modes
            pass


def write_lambda_env(path: Path, key: str, table: str) -> None:
    """Write the Lambda environment JSON that carries the secret (build/ is gitignored)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "Variables": {
                    "FLOODPULSE_STORE": "dynamodb",
                    "FLOODPULSE_TABLE": table,
                    ENV_VAR: key,
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Create the FloodPulse ingestion secret (never printed)")
    parser.add_argument("--key-file", default=DEFAULT_KEY_PATH)
    parser.add_argument("--lambda-env", default=None, help="also write a Lambda env JSON containing the key")
    parser.add_argument("--table", default=DEFAULT_TABLE)
    parser.add_argument("--bytes", type=int, default=32, dest="byte_count")
    parser.add_argument("--reuse", action="store_true", help="keep an existing key instead of generating a new one")
    parser.add_argument("--force", action="store_true", help="overwrite an existing key file (rotates the secret)")
    parser.add_argument("--show-fingerprint", action="store_true")
    args = parser.parse_args()

    key_path = Path(args.key_file)
    existing = read_key(key_path)
    if existing and not args.reuse and not args.force:
        raise SystemExit(
            f"{key_path} already exists. Pass --reuse to keep it or --force to rotate it "
            "(rotating means every producer must be updated with the new value)."
        )

    key = existing if args.reuse else None
    created = False
    if key is None:
        key = secrets.token_hex(args.byte_count)
        write_key_file(key_path, key)
        created = True

    print(f"{'created' if created else 'reused'} key file: {key_path} (value not displayed)")
    if args.lambda_env:
        write_lambda_env(Path(args.lambda_env), key, args.table)
        print(f"wrote Lambda environment file: {args.lambda_env} (contains the secret, gitignored)")
    if args.show_fingerprint:
        print(f"fingerprint: {fingerprint(key)}")
    print(
        "next steps: set it on the function with\n"
        f"  aws lambda update-function-configuration --function-name floodpulse-api "
        f"--environment file://{args.lambda_env or 'build/env-prod.json'} --profile cloud-project --region eu-north-1"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

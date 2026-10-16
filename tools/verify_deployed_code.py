"""Compare the deployed Lambda package with the repository's src/ (read-only check)."""

import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BUILD = REPO / "build"


def aws(args: list[str]) -> str:
    completed = subprocess.run(
        ["aws", *args, "--profile", "cloud-project", "--region", "eu-north-1"],
        capture_output=True, text=True, check=True,
    )
    return completed.stdout.strip()


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def main() -> int:
    url = aws(["lambda", "get-function", "--function-name", "floodpulse-api", "--query", "Code.Location", "--output", "text"])
    import urllib.request
    target = BUILD / "deployed-package.zip"
    with urllib.request.urlopen(url, timeout=60) as response:
        target.write_bytes(response.read())
    print(f"downloaded deployed package: {target.stat().st_size} bytes")

    with zipfile.ZipFile(target) as archive:
        names = sorted(name for name in archive.namelist() if name.endswith(".py"))
        print(f"python files in the deployed package: {len(names)}")
        deployed: dict[str, str] = {}
        for name in names:
            deployed[name] = hashlib.sha256(archive.read(name)).hexdigest()[:16]

    local: dict[str, str] = {}
    for path in sorted((REPO / "src").rglob("*.py")):
        local[path.relative_to(REPO / "src").as_posix()] = sha(path)

    only_deployed = sorted(set(deployed) - set(local))
    only_local = sorted(set(local) - set(deployed))
    different = sorted(name for name in set(deployed) & set(local) if deployed[name] != local[name])

    print(json.dumps({
        "files_deployed": len(deployed),
        "files_local": len(local),
        "only_in_deployed_package": only_deployed,
        "only_in_repo": only_local,
        "content_differs": different,
    }, indent=2))

    if only_deployed or only_local or different:
        print("RESULT: deployed code DIFFERS from the repository")
        return 1
    print("RESULT: deployed Lambda code is byte-identical to the repository's src/")
    return 0


if __name__ == "__main__":
    sys.exit(main())

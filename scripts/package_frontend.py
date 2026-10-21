"""Build provenance and exact artifact copy/check, with stale-file cleanup."""
import argparse
import hashlib
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "frontend" / "dist" / "assets"
TARGET = ROOT / "plugins.v3" / "embycollectionsync" / "dist" / "assets"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def provenance():
    files = sorted((ROOT / "frontend" / "src").rglob("*"))
    files += [ROOT / "frontend" / name for name in ("package.json", "package-lock.json", "vite.config.js", "index.html", "postcss.config.js")]
    return {str(path.relative_to(ROOT / "frontend")).replace("\\", "/"): digest(path) for path in files if path.is_file()}


def assets(directory):
    return {str(path.relative_to(directory)).replace("\\", "/"): digest(path)
            for path in directory.rglob("*") if path.is_file() and path.name != "build-manifest.json"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if not (SOURCE / "remoteEntry.js").is_file():
        raise SystemExit("Frontend build missing: run npm run build")
    frontend_package = json.loads((ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))
    manifest = {"version": frontend_package["version"], "sources": provenance(), "assets": assets(SOURCE)}
    built_sources = json.loads((SOURCE / "source-manifest.json").read_text(encoding="utf-8"))
    if built_sources != manifest["sources"]:
        raise SystemExit("Source changed since build: run npm run build before packaging")
    if args.check:
        saved = json.loads((TARGET / "build-manifest.json").read_text(encoding="utf-8"))
        if saved != manifest or assets(TARGET) != assets(SOURCE):
            raise SystemExit("Source/dist mismatch: rebuild and package")
        print(f"Frontend source fingerprints and {len(manifest['assets'])} packaged assets match")
        return
    TARGET.mkdir(parents=True, exist_ok=True)
    for path in TARGET.iterdir():
        if path.is_file():
            path.unlink()
    for path in SOURCE.iterdir():
        if path.is_file():
            shutil.copy2(path, TARGET / path.name)
    (TARGET / "build-manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Packaged {len(manifest['assets'])} frontend assets with source provenance")


if __name__ == "__main__":
    main()

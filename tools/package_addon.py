"""Build a local add-on ZIP from an explicit source allowlist (no tokens or DBs)."""

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]


def build(destination=None):
    destination = destination or ROOT / "dist" / "ha_recall-addon.zip"
    destination.parent.mkdir(parents=True, exist_ok=True)
    files = [(path, Path("ha_recall") / path.name) for path in (ROOT / "ha_recall").glob("*.py")]
    files += [
        (ROOT / "requirements-ha.txt", Path("requirements-ha.txt")),
        (ROOT / "LICENSE", Path("LICENSE")),
        (ROOT / "README.md", Path("DOCS.md")),
    ]
    files += [
        (ROOT / "addon" / name, Path(name)) for name in ("Dockerfile", "config.json", "run.py")
    ]
    with ZipFile(destination, "w", ZIP_DEFLATED) as archive:
        for source, relative in files:
            archive.write(source, str(Path("ha_recall") / relative))
    return destination


def build_integration(destination=None):
    destination = destination or ROOT / "dist" / "ha_recall-integration.zip"
    destination.parent.mkdir(parents=True, exist_ok=True)
    component = ROOT / "custom_components" / "ha_recall"
    files = [
        *component.glob("*.py"),
        *component.glob("*.json"),
        *component.glob("translations/*.json"),
    ]
    with ZipFile(destination, "w", ZIP_DEFLATED) as archive:
        for source in files:
            archive.write(source, str(source.relative_to(ROOT)))
        archive.write(ROOT / "LICENSE", "LICENSE")
    return destination


if __name__ == "__main__":
    print(build())
    print(build_integration())

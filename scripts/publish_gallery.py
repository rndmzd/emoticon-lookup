"""Publish one HTML file atomically; keep one previous version outside the web root."""
import argparse
from hashlib import sha256
import os
from pathlib import Path
import tempfile


def atomic_copy(source, destination, mode):
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with source.open("rb") as incoming:
            before = os.fstat(incoming.fileno())
            prefix = incoming.read(8192).lower()
            incoming.seek(max(0, before.st_size - 8192))
            suffix = incoming.read().lower()
            incoming.seek(0)
            if b"<html" not in prefix or b"</html>" not in suffix:
                raise ValueError("Source is not a complete HTML document.")
            digest = sha256()
            with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".publish-", delete=False) as outgoing:
                temporary = Path(outgoing.name)
                while chunk := incoming.read(1024 * 1024):
                    outgoing.write(chunk)
                    digest.update(chunk)
                outgoing.flush()
                os.fsync(outgoing.fileno())
            after = os.fstat(incoming.fileno())
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError("Source changed during copying; finish generation before publishing.")
        os.chmod(temporary, mode)
        return temporary, digest.hexdigest(), before.st_size
    except BaseException:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def publish(source, web_root, state_dir=None):
    source = Path(source).resolve(strict=True)
    web_root = Path(web_root).resolve()
    state_dir = Path(state_dir).resolve() if state_dir else web_root.parent / "state"
    if source.suffix.lower() not in (".html", ".htm") or not source.is_file():
        raise ValueError("Publish a generated HTML file, not a manifest or media directory.")
    if state_dir == web_root or state_dir.is_relative_to(web_root):
        raise ValueError("Rollback state must be outside the web root.")
    target = web_root / "index.html"
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise ValueError("Existing index.html must be a regular file, not a symlink or directory.")
    if source == target:
        raise ValueError("The source is already index.html; choose a newly generated file.")
    web_root.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary, digest, size = atomic_copy(source, target, 0o644)
    try:
        if target.exists():
            backup, _, _ = atomic_copy(target, state_dir / "previous.html", 0o600)
            try:
                os.replace(backup, state_dir / "previous.html")
            finally:
                backup.unlink(missing_ok=True)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target, digest, size


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, nargs="?")
    parser.add_argument("--web-root", type=Path, default=Path("/var/lib/emoticon-lookup/www"))
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--rollback", action="store_true")
    args = parser.parse_args()
    if bool(args.source) == args.rollback:
        parser.error("Provide a source HTML file or --rollback, but not both.")
    source = args.source
    if args.rollback:
        state = args.state_dir or args.web_root.resolve().parent / "state"
        source = state / "previous.html"
    try:
        target, digest, size = publish(source, args.web_root, args.state_dir)
    except (ValueError, OSError) as problem:
        parser.exit(1, f"Error: {problem}\n")
    print(f"Published {size} bytes to {target}; SHA-256 {digest}")


if __name__ == "__main__":
    main()

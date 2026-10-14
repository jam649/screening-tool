"""Install commercially sensitive artifacts atomically with private permissions."""

import hashlib
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.reports.export import ExportArtifact


def require_new_destination(destination: Path) -> None:
    # exists() alone misses dangling symlinks; never overwrite either kind.
    if os.path.lexists(destination):
        raise FileExistsError("Destination already exists; choose a new output path")


def write_private_artifact(artifact: "ExportArtifact", destination: Path) -> None:
    """A destination never exposes partial bytes; an existing file always wins."""
    require_new_destination(destination)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".screening-export-", dir=destination.parent)
    owned_path = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as output:
            # mkstemp creates 0600 regardless of a permissive caller umask.
            os.fchmod(output.fileno(), 0o600)
            checksum, count = hashlib.sha256(), 0
            if artifact.body is not None:
                chunks = (artifact.body,)
                for chunk in chunks:
                    output.write(chunk)
                    checksum.update(chunk)
                    count += len(chunk)
            elif artifact.path is not None:
                with artifact.path.open("rb") as source:
                    while chunk := source.read(1024 * 1024):
                        output.write(chunk)
                        checksum.update(chunk)
                        count += len(chunk)
            else:
                raise ValueError("Export content unavailable")
            if count != artifact.size or checksum.hexdigest() != artifact.sha256:
                raise ValueError("Export content verification failed")
            output.flush()
            os.fsync(output.fileno())
        # Same-directory hard link is atomic and refuses racing existing files.
        # Unlike replace(), it cannot silently discard an operator's old export.
        os.link(owned_path, destination, follow_symlinks=False)
    finally:
        owned_path.unlink(missing_ok=True)

"""Generic helpers for uploading files to Unity Catalog Volumes.

Ported from template_databricks_assest_bundle_mcp (src/apps/mcp_star/server/utils/volumes.py).
File-type agnostic (PDF, CSV, image, etc.); nothing here is hardcoded except
the defaults callers pass explicitly.
"""

import re
from datetime import datetime

import requests
from databricks.sdk import WorkspaceClient


def slugify(text: str, max_length: int = 50, default: str = "archivo") -> str:
    """Generates a filesystem-safe slug from free text (e.g. a document title)."""
    cleaned = re.sub(r"[^\w\s-]", "", text.strip().lower())
    cleaned = re.sub(r"[-\s]+", "_", cleaned)
    return cleaned[:max_length] if cleaned else default


def timestamped_filename(base_name: str, extension: str) -> str:
    """Builds a unique filename with a timestamp: '{base_name}_{YYYYmmdd_HHMMSS}.{extension}'."""
    extension = extension.lstrip(".")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{base_name}_{stamp}.{extension}"


def normalize_volume_path(
    default_volume: str,
    volume_path: str = "",
    target_volume: str = "",
    catalog: str = "",
    schema: str = "",
    volume: str = "",
    filename: str = "",
    default_filename_base: str = "documento",
    file_extension: str = "pdf",
) -> str:
    """Resolves the final destination path inside Unity Catalog Volumes.

    Supports three ways of specifying the destination, evaluated in this
    precedence order:

    1. `volume_path`: full explicit path (e.g. '/Volumes/cat/sch/vol/file.pdf').
    2. `catalog` + `schema` + `volume`: combined to build the path (all three required together).
    3. `target_volume` (or `default_volume` if omitted): base volume path, with the filename appended.

    Raises:
        ValueError: if only some (not all) of catalog/schema/volume are given.
    """

    def _ensure_volumes_prefix(path: str) -> str:
        path = path.strip().replace("\\", "/")
        if path.startswith("dbfs:"):
            path = path[5:]
        if not path.startswith("/"):
            path = "/" + path
        if not path.startswith("/Volumes/") and not path.startswith("/Volumes"):
            path = "/Volumes" + path
        return path

    def _resolve_filename() -> str:
        clean = filename.strip() if filename else timestamped_filename(default_filename_base, file_extension)
        suffix = f".{file_extension.lstrip('.')}"
        if not clean.lower().endswith(suffix.lower()):
            clean += suffix
        return clean

    # 1. Full explicit path
    if volume_path and volume_path.strip():
        return _ensure_volumes_prefix(volume_path)

    # 2. catalog + schema + volume
    provided = [bool(catalog and catalog.strip()), bool(schema and schema.strip()), bool(volume and volume.strip())]
    if any(provided) and not all(provided):
        raise ValueError(
            "You must specify 'catalog', 'schema' and 'volume' together, or use "
            "'target_volume'/'volume_path' instead."
        )
    if all(provided):
        return f"/Volumes/{catalog.strip()}/{schema.strip()}/{volume.strip()}/{_resolve_filename()}"

    # 3. target_volume (or default_volume)
    base_volume = _ensure_volumes_prefix(target_volume.strip() if target_volume else default_volume)
    return f"{base_volume.rstrip('/')}/{_resolve_filename()}"


def upload_bytes_to_volume(
    w: WorkspaceClient,
    target_path: str,
    file_bytes: bytes,
    overwrite: bool = True,
    content_type: str = "application/octet-stream",
) -> None:
    """Uploads a binary file to a Unity Catalog volume via the Files REST API (PUT).

    Calls the REST API directly instead of `w.files.upload()`: the latter's
    internal stream detection (seekable/len) behaves differently across
    environments -- content types (bytes, BytesIO) that work locally have
    been observed to fail inside a deployed Databricks App. The direct PUT
    avoids that ambiguity.
    """
    headers = w.config.authenticate()
    headers["Content-Type"] = content_type
    url = f"{w.config.host.rstrip('/')}/api/2.0/fs/files{target_path}"
    response = requests.put(
        url, params={"overwrite": str(overwrite).lower()}, data=file_bytes, headers=headers
    )
    response.raise_for_status()

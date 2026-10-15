"""Non-secret declarations use a frozen JSON-compatible YAML subset."""
import json

from ..contracts import RuntimeFailure, _unique_pairs
from .dotenv import KEY


def parse(raw: str) -> dict:
    try:
        value = json.loads(raw, object_pairs_hook=_unique_pairs)
    except (ValueError, RecursionError):
        raise RuntimeFailure("Manifest requires JSON-compatible YAML syntax.") from None
    if (not isinstance(value, dict) or set(value) != {"schema_version", "keys"} or
            type(value["schema_version"]) is not int or value["schema_version"] != 1 or not isinstance(value["keys"], dict)):
        raise RuntimeFailure("Invalid secrets manifest.")
    mappings = set()
    for key, declaration in value["keys"].items():
        if not KEY.fullmatch(key) or not isinstance(declaration, dict):
            raise RuntimeFailure("Invalid manifest key declaration.")
        if (set(declaration) - {"classification", "required", "file"} or
                not {"classification", "required"} <= declaration.keys() or
                declaration["classification"] not in ("shared", "local-only", "file-backed") or
                type(declaration["required"]) is not bool):
            raise RuntimeFailure("Invalid manifest classification.")
        if key.startswith("DOPPLER_") and declaration["classification"] != "local-only":
            raise RuntimeFailure("Provider credential/metadata keys cannot enter shared scope.")
        if declaration["classification"] == "file-backed":
            path = declaration.get("file")
            if (not isinstance(path, str) or not path or "\\" in path or ":" in path or
                    any(ord(character) < 32 or ord(character) == 127 for character in path)):
                raise RuntimeFailure("File-backed secrets require an explicit safe relative mapping.")
            parts = path.split("/")
            if (any(part in {"", ".", ".."} for part in parts) or
                    any(part.casefold() in {".git", ".agent", ".env"} for part in parts)):
                raise RuntimeFailure("File-backed mapping overlaps a protected path or is noncanonical.")
            folded = path.casefold()
            if any(folded == existing or folded.startswith(existing + "/") or existing.startswith(folded + "/")
                   for existing in mappings):
                raise RuntimeFailure("File-backed mappings must not overlap.")
            mappings.add(folded)
        elif "file" in declaration:
            raise RuntimeFailure("File mapping is only valid for file-backed keys.")
    return value


def shared_scope(manifest: dict) -> set[str]:
    return {key for key, declaration in manifest["keys"].items() if declaration["classification"] in ("shared", "file-backed")}

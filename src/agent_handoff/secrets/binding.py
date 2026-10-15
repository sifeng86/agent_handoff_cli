"""Strict private secret destination and approved scope validation."""
import json

from ..contracts import RuntimeFailure
from ..record_checks import shape, match, IDENTITY
from .adapter import destination_identity
from .manifest import parse as parse_manifest


def validate(binding):
    shape(binding, {"schema_version", "repository_identity", "destination_identity", "mode", "project",
                    "config", "manifest", "upload_authorized"})
    if (not match(binding["repository_identity"], IDENTITY) or
            not match(binding["destination_identity"], IDENTITY) or
            type(binding["upload_authorized"]) is not bool or
            binding["destination_identity"] != destination_identity(binding["mode"], binding["project"], binding["config"]) or
            (binding["mode"] == "local-env" and binding["upload_authorized"])):
        raise RuntimeFailure("Invalid private secret binding.")
    parse_manifest(json.dumps(binding["manifest"]))
    return binding

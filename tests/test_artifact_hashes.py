import hashlib
import json
import unittest
from pathlib import Path


EXAMPLES = Path(__file__).parents[1] / "examples"


def _artifact_declarations():
    """Yield (declaring path, declared hash, referenced path) for every artifact revision."""
    records = {}
    record_files = sorted(EXAMPLES.glob("*/records/artifacts/*.json"))
    for path in record_files:
        document = json.loads(path.read_text(encoding="utf-8"))
        artifact = document.get("artifact", {})
        artifact_id = artifact.get("artifact_id")
        locations = {
            EXAMPLES / path.parent.parent.parent.name / location["path"]
            for revision in artifact.get("revisions", [])
            for location in revision.get("locations", [])
            if location.get("path")
        }
        if artifact_id and locations:
            records[artifact_id] = (path, locations)
        for revision in artifact.get("revisions", []):
            for location in revision.get("locations", []):
                if location.get("path") and revision.get("revision", "").startswith("sha256:"):
                    yield path, revision["revision"], (
                        EXAMPLES / path.parent.parent.parent.name / location["path"]
                    )
        latest = artifact.get("latest_revision", "")
        if locations and latest.startswith("sha256:"):
            for location in locations:
                yield path, latest, location

    def walk(value):
        if isinstance(value, dict):
            artifact_id = value.get("artifact_id")
            revision = value.get("revision", "")
            if artifact_id in records and revision.startswith("sha256:"):
                declaring_path, locations = records[artifact_id]
                for location in locations:
                    yield declaring_path, revision, location
            for child in value.values():
                yield from walk(child)
        elif isinstance(value, list):
            for child in value:
                yield from walk(child)

    for path in sorted(EXAMPLES.glob("*/project/*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        for declaration_path, revision, location in walk(document):
            yield path, revision, location


class ArtifactHashTests(unittest.TestCase):
    def test_declared_artifact_hashes_match_referenced_bytes(self):
        declarations = list(_artifact_declarations())
        self.assertGreater(len(declarations), 0, "no artifact hash declarations found")
        for declaring_path, declared, referenced_path in declarations:
            data = referenced_path.read_bytes()
            actual = "sha256:" + hashlib.sha256(data).hexdigest()
            if actual != declared:
                crlf_count = data.count(b"\r\n")
                lf_count = data.count(b"\n") - crlf_count
                self.fail(
                    f"artifact hash mismatch: file={referenced_path}, "
                    f"declared={declared}, actual={actual}, "
                    f"CRLF count={crlf_count}, LF count={lf_count}; "
                    f"declared by {declaring_path}"
                )


if __name__ == "__main__":
    unittest.main()

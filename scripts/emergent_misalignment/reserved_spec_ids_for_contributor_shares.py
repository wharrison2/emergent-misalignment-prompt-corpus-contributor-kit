"""Which specs of the main emergent misalignment prompt corpus are reserved for contributor shares.

A contributor share (see split_remaining_specs_into_contributor_shares.py) is a block of never-dealt specs given to a
friend to deal in their own corpus folder. The main corpus must never deal those specs, so the splitter writes one
reserved-spec-ids file into the main corpus folder and the main batch script treats every spec in it as unavailable.
Makes no model call.

File name: reserved_spec_ids_for_contributor_shares_<date and time>.json, with the fields
  spec_set_file, spec_set_sha256            the spec set the ids belong to
  reserved_spec_ids_by_share_number         {"1": ["spec_00007", ...], ...}
  reserved_spec_ids                         the sorted union of all shares
The newest file in the corpus folder is the one in force. To release a share back to the main corpus, write a newer
file that leaves that share out.
"""

import json
from pathlib import Path

RESERVED_FILE_PATTERN = "reserved_spec_ids_for_contributor_shares_*.json"


def newest_reserved_spec_ids_file(corpus_folder: Path) -> Path | None:
    matches = sorted(Path(corpus_folder).glob(RESERVED_FILE_PATTERN))
    return matches[-1] if matches else None


def read_reserved_spec_ids(corpus_folder: Path) -> set[str]:
    """The spec ids in force as reserved in the corpus folder; an empty set when the folder has no reserved file."""
    path = newest_reserved_spec_ids_file(corpus_folder)
    if path is None:
        return set()
    record = json.loads(path.read_text(encoding="utf-8"))
    return set(record["reserved_spec_ids"])


def write_reserved_spec_ids_file(corpus_folder: Path, spec_set_file: str, spec_set_sha256: str,
                                 spec_ids_by_share_number: dict[int, list[str]], stamp: str) -> Path:
    record = {"spec_set_file": spec_set_file, "spec_set_sha256": spec_set_sha256,
              "reserved_spec_ids_by_share_number": {str(number): sorted(ids)
                                                    for number, ids in sorted(spec_ids_by_share_number.items())},
              "reserved_spec_ids": sorted({spec_id for ids in spec_ids_by_share_number.values() for spec_id in ids})}
    path = Path(corpus_folder) / f"reserved_spec_ids_for_contributor_shares_{stamp}.json"
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return path

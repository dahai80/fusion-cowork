# issue #650: per-stage evidence store — content-addressed blobs + append-only JSONL index.
# Every node completion records an evidence artifact (sha256 fingerprint + type + raw
# content); the stage is sealed right after, making further appends for the same
# (run_id, node_id) a hard error. Raw fetch re-verifies the fingerprint on every read.

import hashlib
import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class EvidenceSealedError(RuntimeError):
    pass


class EvidenceIntegrityError(RuntimeError):
    pass


class EvidenceNotFoundError(KeyError):
    pass


class EvidenceStore:
    def __init__(self, path: Optional[Path] = None):
        # Default path honors FUSION_COWORK_WEB_DIR (same override as
        # ApprovalJournal / RunStateStore); unset = legacy ~/.fusion-cowork/web.
        if path:
            self._root = Path(path)
        else:
            web_dir = os.environ.get("FUSION_COWORK_WEB_DIR")
            base = Path(web_dir) if web_dir else Path.home() / ".fusion-cowork" / "web"
            self._root = base / "evidence"
        self._blobs = self._root / "blobs"
        self._index = self._root / "index.jsonl"
        self._blobs.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._sealed: set = set()
        self._load_sealed()
        logger.info(f"Evidence store ready: {self._root}")

    def _load_sealed(self) -> None:
        if not self._index.exists():
            return
        try:
            with self._index.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        logger.warning("evidence index: skipping malformed line")
                        continue
                    if rec.get("kind") == "seal":
                        self._sealed.add((rec.get("run_id", ""), rec.get("node_id", "")))
        except OSError as e:
            logger.warning(f"evidence index sealed-state load failed (ignored): {e}")

    def _append_index(self, rec: Dict[str, Any]) -> None:
        with self._index.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, sort_keys=True, ensure_ascii=False) + "\n")

    def _blob_path(self, fingerprint: str) -> Path:
        return self._blobs / fingerprint[:2] / fingerprint

    def append(
        self,
        run_id: str,
        node_id: str,
        artifact_type: str,
        content: bytes,
        meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if not run_id or not node_id:
            raise ValueError("run_id and node_id are required")
        if not isinstance(content, bytes):
            raise TypeError("content must be bytes")
        with self._lock:
            if (run_id, node_id) in self._sealed:
                raise EvidenceSealedError(f"stage sealed: run_id={run_id} node_id={node_id} — evidence is append-only")
            fingerprint = hashlib.sha256(content).hexdigest()
            blob = self._blob_path(fingerprint)
            if not blob.exists():
                blob.parent.mkdir(parents=True, exist_ok=True)
                tmp = blob.with_suffix(".tmp")
                tmp.write_bytes(content)
                os.replace(tmp, blob)
            rec = {
                "kind": "evidence",
                "evidence_id": f"ev_{uuid.uuid4().hex[:16]}",
                "run_id": run_id,
                "node_id": node_id,
                "artifact_type": artifact_type,
                "fingerprint": fingerprint,
                "size": len(content),
                "created_at": time.time(),
                "meta": meta or {},
            }
            self._append_index(rec)
            logger.info(f"evidence appended: id={rec['evidence_id']} run={run_id} node={node_id} fp={fingerprint[:12]}")
            return rec

    def seal_stage(self, run_id: str, node_id: str) -> None:
        with self._lock:
            if (run_id, node_id) in self._sealed:
                return
            self._append_index({"kind": "seal", "run_id": run_id, "node_id": node_id})
            self._sealed.add((run_id, node_id))
            logger.info(f"stage sealed: run={run_id} node={node_id}")

    def list_for_run(self, run_id: str) -> List[Dict[str, Any]]:
        records: List[Dict[str, Any]] = []
        if not self._index.exists():
            return records
        with self._lock:
            try:
                with self._index.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if rec.get("kind") == "evidence" and rec.get("run_id") == run_id:
                            records.append(rec)
            except OSError as e:
                logger.warning(f"evidence index read failed: {e}")
        return records

    def has_evidence(self, run_id: str, node_id: str) -> bool:
        return any(r.get("node_id") == node_id for r in self.list_for_run(run_id))

    def is_sealed(self, run_id: str, node_id: str) -> bool:
        return (run_id, node_id) in self._sealed

    def get(self, evidence_id: str) -> Dict[str, Any]:
        for rec in self._iter_index():
            if rec.get("kind") == "evidence" and rec.get("evidence_id") == evidence_id:
                return rec
        raise EvidenceNotFoundError(evidence_id)

    def _iter_index(self):
        if not self._index.exists():
            return
        with self._lock:
            try:
                with self._index.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            yield json.loads(line)
                        except json.JSONDecodeError:
                            continue
            except OSError as e:
                logger.warning(f"evidence index read failed: {e}")

    def read_raw(self, evidence_id: str) -> bytes:
        rec = self.get(evidence_id)
        blob = self._blob_path(rec["fingerprint"])
        if not blob.exists():
            raise EvidenceNotFoundError(evidence_id)
        content = blob.read_bytes()
        actual = hashlib.sha256(content).hexdigest()
        if actual != rec["fingerprint"]:
            raise EvidenceIntegrityError(
                f"evidence integrity check failed: id={evidence_id} expected={rec['fingerprint'][:12]} actual={actual[:12]}"
            )
        return content


def canonical_output_bytes(output: Any) -> bytes:
    # deterministic serialization so the same output always yields the same fingerprint
    try:
        return json.dumps(output, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    except (TypeError, ValueError) as e:
        logger.warning(f"canonical output serialization failed (repr fallback): {e}")
        return repr(output).encode("utf-8")

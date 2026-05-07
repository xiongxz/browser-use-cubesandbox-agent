from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import Settings
from .models import AuthProfileSummary, AuthProfileUpsertRequest


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class AuthProfileRecord:
    profile_id: str
    storage_state_path: Path
    metadata_path: Path
    created_at: str
    updated_at: str
    description: str | None
    is_feishu_default: bool

    def to_summary(self) -> AuthProfileSummary:
        return AuthProfileSummary(
            profile_id=self.profile_id,
            storage_state_path=str(self.storage_state_path),
            created_at=self.created_at,
            updated_at=self.updated_at,
            description=self.description,
            is_feishu_default=self.is_feishu_default,
        )


class AuthStore:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.root = settings.auth_state_dir
        self.root.mkdir(parents=True, exist_ok=True)

    def _profile_dir(self, profile_id: str) -> Path:
        return self.root / profile_id

    def _metadata_path(self, profile_id: str) -> Path:
        return self._profile_dir(profile_id) / "metadata.json"

    def _state_path(self, profile_id: str) -> Path:
        return self._profile_dir(profile_id) / "storage_state.json"

    def _read_record(self, profile_id: str) -> AuthProfileRecord | None:
        metadata_path = self._metadata_path(profile_id)
        state_path = self._state_path(profile_id)
        if not metadata_path.is_file() or not state_path.is_file():
            return None
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        return AuthProfileRecord(
            profile_id=profile_id,
            storage_state_path=state_path,
            metadata_path=metadata_path,
            created_at=metadata["created_at"],
            updated_at=metadata["updated_at"],
            description=metadata.get("description"),
            is_feishu_default=bool(metadata.get("is_feishu_default", False)),
        )

    def upsert_profile(self, request: AuthProfileUpsertRequest) -> AuthProfileRecord:
        generated_profile_id = f"profile-{int(datetime.now(timezone.utc).timestamp())}"
        profile_id = request.profile_id or (
            self.settings.feishu_default_profile_id if request.set_as_feishu_default else generated_profile_id
        )
        profile_dir = self._profile_dir(profile_id)
        profile_dir.mkdir(parents=True, exist_ok=True)
        metadata_path = self._metadata_path(profile_id)
        state_path = self._state_path(profile_id)

        existing = self._read_record(profile_id)
        created_at = existing.created_at if existing else _utc_now()
        updated_at = _utc_now()

        if request.set_as_feishu_default:
            for record in self.list_profiles():
                if record.profile_id == profile_id:
                    continue
                metadata = json.loads(record.metadata_path.read_text(encoding="utf-8"))
                if metadata.get("is_feishu_default"):
                    metadata["is_feishu_default"] = False
                    record.metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

        state_path.write_text(json.dumps(request.storage_state, ensure_ascii=False, indent=2), encoding="utf-8")
        metadata = {
            "profile_id": profile_id,
            "created_at": created_at,
            "updated_at": updated_at,
            "description": request.description,
            "is_feishu_default": request.set_as_feishu_default,
        }
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

        return AuthProfileRecord(
            profile_id=profile_id,
            storage_state_path=state_path,
            metadata_path=metadata_path,
            created_at=created_at,
            updated_at=updated_at,
            description=request.description,
            is_feishu_default=request.set_as_feishu_default,
        )

    def get_profile(self, profile_id: str) -> AuthProfileRecord | None:
        return self._read_record(profile_id)

    def list_profiles(self) -> list[AuthProfileRecord]:
        records: list[AuthProfileRecord] = []
        for entry in sorted(self.root.iterdir()):
            if not entry.is_dir():
                continue
            record = self._read_record(entry.name)
            if record is not None:
                records.append(record)
        return records

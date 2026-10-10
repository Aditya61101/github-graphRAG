from dataclasses import dataclass
import os


@dataclass(frozen=True)
class PullRequestSettings:
    max_file_bytes: int = 1024 * 1024
    max_patch_bytes: int = 256 * 1024
    max_total_text_bytes: int = 16 * 1024 * 1024
    max_changed_files: int = 1000
    max_metadata_bytes: int = 8 * 1024 * 1024
    git_timeout_seconds: int = 120
    write_debug_artifacts: bool = False

    @classmethod
    def from_env(cls):
        defaults = cls()
        fields = {name: int(os.getenv('PR_' + name.upper(), str(getattr(defaults, name))))
                  for name in cls.__dataclass_fields__ if name != 'write_debug_artifacts'}
        if any(value < 1 for value in fields.values()):
            raise ValueError('PR ingestion limits must be positive integers')
        return cls(**fields, write_debug_artifacts=os.getenv('PR_WRITE_DEBUG_ARTIFACTS', 'false').lower() == 'true')

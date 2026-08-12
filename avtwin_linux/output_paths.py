from __future__ import annotations

import os
from pathlib import Path
import stat
import tempfile


def validate_output_root(root: Path, *, create: bool = True) -> Path:
    """Resolve and prove that an output root is creatable and writable."""
    if not str(root).strip():
        raise ValueError("结果保存目录不能为空")
    resolved = root.expanduser().resolve()
    try:
        if create:
            resolved.mkdir(parents=True, exist_ok=True)
        if not resolved.is_dir():
            raise ValueError(f"结果保存路径不是目录：{resolved}")
        mode = resolved.stat().st_mode
        if mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH) == 0:
            raise PermissionError("目录权限没有任何写入位")
        fd, probe = tempfile.mkstemp(prefix=".avtwin-write-test-", dir=resolved)
        os.close(fd)
        Path(probe).unlink()
    except (OSError, ValueError) as exc:
        raise ValueError(f"结果目录不可创建或不可写：{resolved}: {exc}") from exc
    return resolved

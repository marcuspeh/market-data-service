"""Shared helpers for atomically writing parquet files.

Every store writes to a temporary file and then renames it over the
target, so a reader never observes a half-written file. The temporary
name must be **unique per writer**: with a fixed ``<file>.parquet.tmp``
name, two concurrent writers of the same ticker/year would share one
temp file, and one could rename a partially written file into place.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pandas as pd


def temp_path(target: Path) -> Path:
    """A unique temp path in ``target``'s directory.

    Same directory as the target so the final ``replace`` stays on one
    filesystem and is therefore atomic.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    os.close(handle)
    return Path(name)


def write_parquet_atomic(df: pd.DataFrame, target: Path) -> None:
    """Write ``df`` to ``target`` atomically.

    The data lands in a uniquely named temp file which is then renamed
    over the target — readers see either the old file or the new one,
    never a partial write.
    """
    tmp = temp_path(target)
    try:
        df.to_parquet(tmp, index=False, engine="pyarrow")
        tmp.replace(target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise

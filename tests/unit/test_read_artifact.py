from __future__ import annotations

from pathlib import Path

import pytest

from kama_claude.core.tools.builtin.read_artifact import ReadArtifactTool


async def test_read_artifact_pages_large_payload(tmp_path: Path) -> None:
    artifact = tmp_path / "bash-call.txt"
    artifact.write_text("abcdefghij", encoding="utf-8")
    tool = ReadArtifactTool(tmp_path)

    first = await tool.invoke({"path": str(artifact), "offset": 0, "max_chars": 4})
    second = await tool.invoke({"path": str(artifact), "offset": 4, "max_chars": 6})

    assert "abcd" in first.content
    assert first.content.endswith("[more available]")
    assert "efghij" in second.content
    assert not second.content.endswith("[more available]")


async def test_read_artifact_rejects_non_payload_and_escape(tmp_path: Path) -> None:
    (tmp_path / "metadata.json").write_text("{}", encoding="utf-8")
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    tool = ReadArtifactTool(tmp_path)

    with pytest.raises(PermissionError):
        await tool.invoke({"path": "metadata.json"})
    with pytest.raises(PermissionError):
        await tool.invoke({"path": str(outside)})

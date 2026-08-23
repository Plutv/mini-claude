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


async def test_read_artifact_finds_fact_when_offset_is_unknown(tmp_path: Path) -> None:
    artifact = tmp_path / "bash-call.txt"
    artifact.write_text(
        "x" * 10_000 + "\nRECOVERY_CODE=KC-MIDDLE-7F19\n" + "y" * 10_000,
        encoding="utf-8",
    )
    tool = ReadArtifactTool(tmp_path)

    result = await tool.invoke(
        {
            "path": str(artifact),
            "query": "RECOVERY_CODE",
            "max_chars": 200,
        }
    )

    assert "RECOVERY_CODE=KC-MIDDLE-7F19" in result.content
    assert "query='RECOVERY_CODE'" in result.content


async def test_read_artifact_reports_missing_query(tmp_path: Path) -> None:
    artifact = tmp_path / "bash-call.txt"
    artifact.write_text("known content", encoding="utf-8")
    tool = ReadArtifactTool(tmp_path)

    result = await tool.invoke({"path": str(artifact), "query": "missing"})

    assert result.is_error is True
    assert result.error_type == "not_found"

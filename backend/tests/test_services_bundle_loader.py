"""Tests for startup bundle loader."""

import json
import pathlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.asyncio


async def test_load_connectathon_bundles_scans_directory(tmp_path):
    """load_connectathon_bundles loads each .json file in the given directory."""
    from app.services.bundle_loader import load_connectathon_bundles

    bundle1 = {"resourceType": "Bundle", "type": "transaction", "entry": []}
    bundle2 = {"resourceType": "Bundle", "type": "transaction", "entry": []}
    (tmp_path / "bundle1.json").write_text(json.dumps(bundle1))
    (tmp_path / "bundle2.json").write_text(json.dumps(bundle2))

    with patch("app.services.bundle_loader.triage_test_bundle") as mock_triage:
        mock_triage.return_value = {"measures_loaded": 0, "patients_loaded": 0, "expected_results_loaded": 0}
        mock_session = AsyncMock()
        mock_session_factory = MagicMock()
        mock_session_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_factory.return_value.__aexit__ = AsyncMock(return_value=False)

        with patch("app.services.bundle_loader.async_session", mock_session_factory):
            summary = await load_connectathon_bundles(directory=tmp_path)

    assert summary["loaded"] == 2
    assert summary["failed"] == 0
    assert mock_triage.call_count == 2


async def test_load_connectathon_bundles_skips_missing_directory(tmp_path):
    """load_connectathon_bundles returns early when directory does not exist."""
    from app.services.bundle_loader import load_connectathon_bundles

    missing_dir = tmp_path / "does-not-exist"

    with patch("app.services.bundle_loader.triage_test_bundle") as mock_triage:
        summary = await load_connectathon_bundles(directory=missing_dir)

    assert summary["loaded"] == 0
    mock_triage.assert_not_called()


async def test_load_connectathon_bundles_continues_on_error(tmp_path):
    """load_connectathon_bundles logs errors and continues loading remaining bundles."""
    from app.services.bundle_loader import load_connectathon_bundles

    (tmp_path / "good.json").write_text(json.dumps({"resourceType": "Bundle", "entry": []}))
    (tmp_path / "bad.json").write_text("{ invalid json }")

    with patch("app.services.bundle_loader.triage_test_bundle") as mock_triage:
        mock_triage.return_value = {"measures_loaded": 1, "patients_loaded": 0, "expected_results_loaded": 0}
        mock_session_factory = MagicMock()
        mock_session = AsyncMock()
        mock_session_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_factory.return_value.__aexit__ = AsyncMock(return_value=False)

        with patch("app.services.bundle_loader.async_session", mock_session_factory):
            summary = await load_connectathon_bundles(directory=tmp_path)

    assert summary["loaded"] == 1
    assert summary["failed"] == 1


async def test_load_connectathon_bundles_empty_directory(tmp_path):
    """load_connectathon_bundles returns early when directory has no .json files."""
    from app.services.bundle_loader import load_connectathon_bundles

    # Directory exists but contains no .json files
    (tmp_path / "readme.txt").write_text("not a bundle")

    with patch("app.services.bundle_loader.triage_test_bundle") as mock_triage:
        summary = await load_connectathon_bundles(directory=tmp_path)

    assert summary["loaded"] == 0
    assert summary["failed"] == 0
    mock_triage.assert_not_called()

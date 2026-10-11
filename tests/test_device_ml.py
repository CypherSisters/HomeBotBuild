"""Tests for the device ML classifier module."""

import os
import json
import pytest
from unittest.mock import MagicMock, patch

from puffpai.security.device_classifier import (
    DeviceCategory, DeviceClassifier, classify_device, DEVICE_POLICIES,
)
from puffpai.security.device_ml import (
    DeviceFeatures, DeviceFeatureExtractor, DeviceMLClassifier,
    _HAS_SKLEARN,
)


# ── Feature Extraction Tests ────────────────────────────────────────────────

class TestDeviceFeatures:
    def test_to_list_returns_correct_length(self):
        f = DeviceFeatures()
        assert len(f.to_list()) == 11

    def test_feature_names_match_length(self):
        assert len(DeviceFeatures.feature_names()) == 11
        assert len(DeviceFeatures.feature_names()) == len(DeviceFeatures().to_list())

    def test_defaults_are_zero(self):
        f = DeviceFeatures()
        assert all(v == 0.0 for v in f.to_list())


class TestDeviceFeatureExtractor:
    def setup_method(self):
        self.extractor = DeviceFeatureExtractor()

    def test_extracts_mac_hash(self):
        features = self.extractor.extract(mac="AA:BB:CC:DD:EE:FF")
        assert features.oui_hash > 0

    def test_same_oui_same_hash(self):
        f1 = self.extractor.extract(mac="AA:BB:CC:11:22:33")
        f2 = self.extractor.extract(mac="AA:BB:CC:44:55:66")
        assert f1.oui_hash == f2.oui_hash

    def test_extracts_hostname_features(self):
        features = self.extractor.extract(hostname="iPhone-12-Pro")
        assert features.hostname_len == 13
        assert features.hostname_digit_ratio > 0
        assert features.hostname_hyphen_count == 2
        assert features.hostname_has_brand == 0  # "iPhone" not in brand list as-is

    def test_detects_brand_in_hostname(self):
        features = self.extractor.extract(hostname="Samsung-TV-Living")
        assert features.hostname_has_brand == 1

    def test_extracts_mdns_features(self):
        features = self.extractor.extract(mdns_services=["_printer._tcp", "_ipp._tcp"])
        assert features.mdns_service_count == 2
        assert features.mdns_has_printer == 1
        assert features.mdns_has_airplay == 0

    def test_extracts_airplay_mdns(self):
        features = self.extractor.extract(mdns_services=["_airplay._tcp"])
        assert features.mdns_has_airplay == 1

    def test_extracts_ssdp_features(self):
        features = self.extractor.extract(ssdp_header="Roku/DVP-10.5")
        assert features.ssdp_present == 1
        assert features.ssdp_header_len > 0

    def test_empty_inputs(self):
        features = self.extractor.extract()
        assert features.oui_hash == 0
        assert features.hostname_len == 0
        assert features.mdns_service_count == 0
        assert features.ssdp_present == 0


# ── ML Classifier Tests ─────────────────────────────────────────────────────

class TestDeviceMLClassifier:
    def test_not_trained_returns_unknown(self):
        ml = DeviceMLClassifier()
        result = ml.predict(mac="AA:BB:CC:DD:EE:FF")
        assert result == DeviceCategory.UNKNOWN

    def test_is_available_false_when_untrained(self):
        ml = DeviceMLClassifier()
        if _HAS_SKLEARN:
            assert not ml.is_available
        else:
            assert not ml.is_available

    @pytest.mark.skipif(not _HAS_SKLEARN, reason="sklearn not installed")
    def test_train_and_predict(self):
        """Train on synthetic data and verify predictions."""
        extractor = DeviceFeatureExtractor()
        ml = DeviceMLClassifier()

        # Build small synthetic training set
        X = []
        y = []

        # Computers with SMB services
        for _ in range(10):
            f = extractor.extract(
                mac="00:1B:21:11:22:33", hostname="DESKTOP-ABC",
                mdns_services=["_smb._tcp"])
            X.append(f.to_list())
            y.append("computer")

        # Printers with printer services
        for _ in range(10):
            f = extractor.extract(
                mac="3C:D9:2B:11:22:33", hostname="HP-DeskJet",
                mdns_services=["_printer._tcp", "_ipp._tcp"])
            X.append(f.to_list())
            y.append("printer")

        # Smart TVs with AirPlay
        for _ in range(10):
            f = extractor.extract(
                mac="DC:3A:5E:11:22:33", hostname="Roku-TV",
                mdns_services=["_airplay._tcp"])
            X.append(f.to_list())
            y.append("smart_tv")

        stats = ml.train(X, y)
        assert stats["n_samples"] == 30
        assert stats["n_classes"] == 3
        assert ml.is_available

        # Predict a printer-like device
        result = ml.predict(
            mac="3C:D9:2B:44:55:66", hostname="HP-OfficeJet",
            mdns_services=["_printer._tcp"])
        assert result == DeviceCategory.PRINTER

    @pytest.mark.skipif(not _HAS_SKLEARN, reason="sklearn not installed")
    def test_predict_proba_returns_dict(self):
        """predict_proba returns probability dict."""
        extractor = DeviceFeatureExtractor()
        ml = DeviceMLClassifier()

        X = []
        y = []
        for _ in range(5):
            f = extractor.extract(mac="AA:BB:CC:11:22:33", hostname="test-pc")
            X.append(f.to_list())
            y.append("computer")
        for _ in range(5):
            f = extractor.extract(mac="DD:EE:FF:11:22:33", hostname="test-printer",
                                  mdns_services=["_printer._tcp"])
            X.append(f.to_list())
            y.append("printer")

        ml.train(X, y)
        probs = ml.predict_proba(mac="AA:BB:CC:99:99:99", hostname="my-desktop")
        assert isinstance(probs, dict)
        assert sum(probs.values()) == pytest.approx(1.0, abs=0.01)

    @pytest.mark.skipif(not _HAS_SKLEARN, reason="sklearn not installed")
    def test_save_model(self, tmp_path):
        """Trained model can be saved to JSON."""
        extractor = DeviceFeatureExtractor()
        ml = DeviceMLClassifier()

        X, y = [], []
        for _ in range(5):
            f = extractor.extract(mac="AA:BB:CC:11:22:33", hostname="pc")
            X.append(f.to_list())
            y.append("computer")

        ml.train(X, y)

        path = str(tmp_path / "model.json")
        ml.save(path)
        assert os.path.isfile(path)

        with open(path) as f:
            data = json.load(f)
        assert data["version"] == 1
        assert "computer" in data["classes"]


# ── classify_device ML fallback integration ──────────────────────────────────

class TestClassifyDeviceMLFallback:
    def test_ml_fallback_used_for_unknown(self):
        """classify_device uses ml_classifier when heuristics return UNKNOWN."""
        mock_ml = MagicMock()
        mock_ml.predict.return_value = DeviceCategory.IOT_APPLIANCE

        # Use a MAC that doesn't match any OUI
        result = classify_device(
            mac="AA:BB:CC:DD:EE:FF",
            ml_classifier=mock_ml,
        )

        mock_ml.predict.assert_called_once()
        assert result.category == DeviceCategory.IOT_APPLIANCE
        assert result.confidence == "low"

    def test_ml_not_used_when_heuristic_succeeds(self):
        """classify_device skips ML when heuristics give a definitive answer."""
        mock_ml = MagicMock()
        mock_ml.predict.return_value = DeviceCategory.PRINTER

        # Use a known Apple MAC — should resolve via OUI
        result = classify_device(
            mac="A4:83:E7:11:22:33",
            ml_classifier=mock_ml,
        )

        mock_ml.predict.assert_not_called()
        assert result.category == DeviceCategory.COMPUTER  # Apple → computer

    def test_ml_failure_doesnt_break_classification(self):
        """ML exceptions are caught gracefully."""
        mock_ml = MagicMock()
        mock_ml.predict.side_effect = RuntimeError("ML exploded")

        result = classify_device(
            mac="AA:BB:CC:DD:EE:FF",
            ml_classifier=mock_ml,
        )

        # Should still return UNKNOWN (ML failure caught)
        assert result.category == DeviceCategory.UNKNOWN

    def test_no_ml_classifier_works_as_before(self):
        """classify_device works without ml_classifier (backward compat)."""
        result = classify_device(mac="AA:BB:CC:DD:EE:FF")
        assert result.category == DeviceCategory.UNKNOWN

"""Tests for PII detection and data classification."""

import pytest
from puffpai.security.data_classifier import DataClassifier, PIIDetection
from puffpai.security.zones import DataZone


@pytest.fixture
def classifier():
    return DataClassifier()


class TestPIIDetection:
    def test_detect_email(self, classifier):
        pii = classifier.scan_for_pii("Contact me at john@example.com for details")
        assert pii.has_pii is True
        assert "email" in pii.pii_types
        assert "john@example.com" in pii.emails

    def test_detect_phone(self, classifier):
        pii = classifier.scan_for_pii("Call me at (555) 123-4567")
        assert pii.has_pii is True
        assert "phone" in pii.pii_types

    def test_detect_ssn(self, classifier):
        pii = classifier.scan_for_pii("SSN: 123-45-6789")
        assert pii.has_pii is True
        assert pii.severity == "critical"

    def test_detect_credit_card(self, classifier):
        pii = classifier.scan_for_pii("Card: 4111-1111-1111-1111")
        assert pii.has_pii is True
        assert pii.severity == "critical"

    def test_detect_labeled_name(self, classifier):
        pii = classifier.scan_for_pii("Patient: John Smith")
        assert pii.has_pii is True
        assert "name" in pii.pii_types

    def test_no_pii_in_clean_text(self, classifier):
        pii = classifier.scan_for_pii("The weather today is sunny and warm.")
        assert pii.has_pii is False
        assert pii.severity == "none"

    def test_empty_text(self, classifier):
        pii = classifier.scan_for_pii("")
        assert pii.has_pii is False

    def test_severity_ranking(self, classifier):
        # Critical: SSN
        pii = classifier.scan_for_pii("SSN: 123-45-6789")
        assert pii.severity == "critical"
        # Medium: email only
        pii = classifier.scan_for_pii("test@example.com")
        assert pii.severity == "medium"


class TestFileClassification:
    def test_profiles_is_vault(self, classifier):
        assert classifier.classify_file("profiles.json") == DataZone.VAULT

    def test_csv_is_vault(self, classifier):
        assert classifier.classify_file("contacts.csv") == DataZone.VAULT

    def test_medical_keyword_is_vault(self, classifier):
        assert classifier.classify_file("medical_records.txt") == DataZone.VAULT

    def test_model_file_is_intelligence(self, classifier):
        assert classifier.classify_file("model_weights.bin") == DataZone.INTELLIGENCE


class TestContentClassification:
    def test_text_with_pii_goes_to_vault(self, classifier):
        zone, pii = classifier.classify_content("Email: jane@test.com, Phone: 555-123-4567")
        assert zone == DataZone.VAULT
        assert pii.has_pii is True

    def test_clean_text_goes_to_intelligence(self, classifier):
        zone, pii = classifier.classify_content("How to grow tomatoes in zone 7.")
        assert zone == DataZone.INTELLIGENCE
        assert pii.has_pii is False


class TestShouldEncrypt:
    def test_vault_file_should_encrypt(self, classifier):
        assert classifier.should_encrypt("profiles.json") is True

    def test_intelligence_file_should_encrypt(self, classifier):
        assert classifier.should_encrypt("config/persona.txt") is True

    def test_content_with_pii_should_encrypt(self, classifier):
        assert classifier.should_encrypt(
            "notes.txt",
            content="My SSN is 123-45-6789"
        ) is True

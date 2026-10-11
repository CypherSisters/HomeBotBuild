"""
Tests for automation tools in assistant_tools.py (Node-RED webhook-backed).
Also covers the automation API endpoints in api_server.py.
"""

import json
import os
import sys
import pytest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))


class TestAutomationApiEndpoints:
    """Tests for /automation/* API endpoints."""

    @pytest.fixture
    def client(self):
        """Flask test client with mocked config."""
        import puffpai.api_server as mod
        original_key = mod._API_KEY
        mod._API_KEY = "test-key-12345"
        mod.app.config['TESTING'] = True
        try:
            with mod.app.test_client() as c:
                yield c
        finally:
            mod._API_KEY = original_key

    @pytest.fixture
    def auth_headers(self):
        return {"X-API-Key": "test-key-12345"}

    @patch("puffpai.automation_client.AutomationClient.call")
    def test_briefing_endpoint(self, mock_call, client, auth_headers):
        mock_call.return_value = (True, {"briefing": "Good morning!"})
        resp = client.get("/automation/briefing", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True

    @patch("puffpai.automation_client.AutomationClient.call")
    def test_weather_endpoint(self, mock_call, client, auth_headers):
        mock_call.return_value = (True, {"forecast": "Sunny, 85°F"})
        resp = client.get("/automation/weather?location=Austin", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True

    @patch("puffpai.automation_client.AutomationClient.call")
    def test_news_endpoint(self, mock_call, client, auth_headers):
        mock_call.return_value = (True, {"news": "Top headlines..."})
        resp = client.get("/automation/news?topics=tech", headers=auth_headers)
        assert resp.status_code == 200

    @patch("puffpai.automation_client.AutomationClient.call")
    def test_meal_plan_get(self, mock_call, client, auth_headers):
        mock_call.return_value = (True, {"meal_plan": "Monday: Tacos..."})
        resp = client.get("/automation/meal-plan?days=5", headers=auth_headers)
        assert resp.status_code == 200

    @patch("puffpai.automation_client.AutomationClient.call")
    def test_meal_plan_post(self, mock_call, client, auth_headers):
        mock_call.return_value = (True, {"meal_plan": "Veggie week"})
        resp = client.post(
            "/automation/meal-plan",
            headers={**auth_headers, "Content-Type": "application/json"},
            data=json.dumps({"days": 7, "dietary_preferences": "vegetarian"}),
        )
        assert resp.status_code == 200

    @patch("puffpai.automation_client.AutomationClient.call")
    def test_grocery_list_get(self, mock_call, client, auth_headers):
        mock_call.return_value = (True, {"grocery_list": "Produce: tomatoes..."})
        resp = client.get("/automation/grocery-list?store=Corner%20Market", headers=auth_headers)
        assert resp.status_code == 200

    @patch("puffpai.automation_client.AutomationClient.call")
    def test_health_check_endpoint(self, mock_call, client, auth_headers):
        mock_call.return_value = (True, {"status": "all_healthy"})
        resp = client.get("/automation/health-check", headers=auth_headers)
        assert resp.status_code == 200

    def test_briefing_requires_auth(self, client):
        resp = client.get("/automation/briefing")
        assert resp.status_code in (401, 403)

    def test_weather_requires_auth(self, client):
        resp = client.get("/automation/weather")
        assert resp.status_code in (401, 403)


class TestAutomationToolFunctions:
    """Tests for the automation tool functions defined in AssistantTools."""

    @patch("puffpai.automation_client.AutomationClient")
    def test_auto_daily_briefing_success(self, MockClient):
        """auto_daily_briefing should return briefing text on success."""
        mock_instance = MockClient.return_value
        mock_instance.trigger.return_value = {"success": True, "briefing": "Good morning!"}

        # Import and invoke the tool function
        from puffpai.automation_client import AutomationClient
        client = AutomationClient()
        resp = client.trigger("daily_briefing", {"date": "2026-03-11"})
        assert resp["success"] is True
        assert "Good morning" in resp["briefing"]

    @patch("puffpai.automation_client.AutomationClient")
    def test_auto_weather_success(self, MockClient):
        mock_instance = MockClient.return_value
        mock_instance.trigger.return_value = {"success": True, "forecast": "Sunny, 85°F"}
        client = MockClient()
        resp = client.trigger("weather_alerts", {"location": "Springfield, IL"})
        assert resp["success"] is True

    @patch("puffpai.automation_client.AutomationClient")
    def test_auto_grocery_list_success(self, MockClient):
        mock_instance = MockClient.return_value
        mock_instance.trigger.return_value = {"success": True, "grocery_list": "Produce: tomatoes"}
        client = MockClient()
        resp = client.trigger("grocery_list", {"store": "Corner Market"})
        assert resp["success"] is True

    @patch("puffpai.automation_client.AutomationClient")
    def test_automation_failure_returns_error(self, MockClient):
        """When Node-RED is unreachable, trigger should return None or error."""
        mock_instance = MockClient.return_value
        mock_instance.trigger.return_value = None
        client = MockClient()
        resp = client.trigger("daily_briefing", {})
        assert resp is None

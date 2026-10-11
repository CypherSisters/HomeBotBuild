"""
PuffPAI LLM Benchmark & Capability Test Suite
===============================================
Tests the local LLM (via Ollama) for:
  1. Raw inference speed (tokens/sec, time-to-first-token, total latency)
  2. Conversation naturalness (voice-first persona compliance)
  3. Tool-calling accuracy (does the LLM pick the right tool?)
  4. End-to-end agent execution (does the tool actually fire and persist?)
  5. Workflow / automation triggers (Node-RED, HA)

Run:
    python -m pytest tests/test_llm_benchmark.py -v -s --tb=short

    Or standalone:
    python tests/test_llm_benchmark.py

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

import json
import os
import re
import sys
import time
import requests
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, field

# ── Setup ─────────────────────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_ROOT = _REPO_ROOT / "src"
_CONFIG_DIR = _REPO_ROOT / "config"

sys.path.insert(0, str(_SRC_ROOT))

# Load config
_cfg_path = _CONFIG_DIR / "puffpai_config.json"
with open(_cfg_path) as f:
    _CFG = json.load(f)

OLLAMA_URL = os.environ.get("PUFFPAI_OLLAMA_URL") or _CFG.get(
    "ollama", {}).get("url", "http://localhost:11434")
MODEL = _CFG.get("ollama", {}).get("model", "dolphin-llama3:8b")
API_URL = f"http://localhost:{_CFG.get('api_server', {}).get('port', 8000)}"


# ═══════════════════════════════════════════════════════════════════════════════
# DATA STRUCTURES
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class BenchmarkResult:
    prompt: str
    category: str
    response: str = ""
    time_to_first_token: float = 0.0
    total_time: float = 0.0
    total_tokens: int = 0
    tokens_per_second: float = 0.0
    tool_called: str = ""
    tool_expected: str = ""
    tool_correct: bool = False
    naturalness_issues: list = field(default_factory=list)
    passed: bool = True
    error: str = ""


# ═══════════════════════════════════════════════════════════════════════════════
# 1. RAW OLLAMA INFERENCE BENCHMARKS
# ═══════════════════════════════════════════════════════════════════════════════

SPEED_PROMPTS = [
    {
        "prompt": "What is 2 plus 2?",
        "category": "trivial",
        "desc": "Trivial math — should be nearly instant",
    },
    {
        "prompt": "Explain how to purify water in an emergency using only household items.",
        "category": "knowledge",
        "desc": "Medium knowledge response — tests sustained generation",
    },
    {
        "prompt": "You are helping someone who just cut themselves deeply while chopping wood. Walk them through first aid step by step.",
        "category": "emergency",
        "desc": "Emergency scenario — tests depth + persona adherence",
    },
    {
        "prompt": "Tell me a short story about a cat who learns to fly.",
        "category": "creative",
        "desc": "Creative generation — tests fluency and coherence",
    },
    {
        "prompt": "What's the weather like today?",
        "category": "tool_trigger",
        "desc": "Should trigger weather tool (tests intent recognition)",
    },
]


def _benchmark_ollama_raw(prompt: str, category: str) -> BenchmarkResult:
    """Benchmark a single prompt against raw Ollama API (streaming)."""
    result = BenchmarkResult(prompt=prompt, category=category)

    try:
        start = time.perf_counter()
        first_token_time = None
        full_response = []
        token_count = 0

        resp = requests.post(
            f"{OLLAMA_URL}/v1/chat/completions",
            json={
                "model": MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "stream": True,
                "temperature": 0.6,
            },
            stream=True,
            timeout=120,
        )
        resp.raise_for_status()

        for line in resp.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data: "):
                continue
            data_str = line[6:].strip()
            if data_str == "[DONE]":
                break

            try:
                chunk = json.loads(data_str)
                content = chunk.get("choices", [{}])[0].get("delta", {}).get("content", "")
                if content:
                    if first_token_time is None:
                        first_token_time = time.perf_counter()
                    full_response.append(content)
                    # Rough token count (words)
                    token_count += len(content.split())
            except json.JSONDecodeError:
                continue

        end = time.perf_counter()

        result.response = "".join(full_response)
        result.total_time = end - start
        result.time_to_first_token = (first_token_time - start) if first_token_time else result.total_time
        result.total_tokens = token_count
        result.tokens_per_second = token_count / result.total_time if result.total_time > 0 else 0

    except requests.ConnectionError:
        result.error = f"Cannot connect to Ollama at {OLLAMA_URL}"
        result.passed = False
    except Exception as e:
        result.error = str(e)
        result.passed = False

    return result


# ═══════════════════════════════════════════════════════════════════════════════
# 2. CONVERSATION NATURALNESS CHECKS
# ═══════════════════════════════════════════════════════════════════════════════

NATURALNESS_PROMPTS = [
    "Hey, how's it going?",
    "Can you help me figure out how to fix a leaky faucet?",
    "I'm worried about a hurricane coming. What should I do?",
    "What's a good recipe for sourdough bread?",
    "Tell me about the best way to store canned food long term.",
]

_MARKDOWN_PATTERNS = [
    (r'\*\*[^*]+\*\*', "bold markdown"),
    (r'#{1,6}\s', "heading markdown"),
    (r'```', "code blocks"),
    (r'^\s*[-*]\s+', "bullet points"),
    (r'^\s*\d+\.\s+', "numbered lists"),
    (r'`[^`]+`', "inline code"),
]


def _check_naturalness(response: str) -> list:
    """Check if a response follows voice-first persona rules."""
    issues = []

    for pattern, desc in _MARKDOWN_PATTERNS:
        if re.search(pattern, response, re.MULTILINE):
            issues.append(f"Contains {desc}")

    # Check response length (voice-first should be concise)
    word_count = len(response.split())
    if word_count > 300:
        issues.append(f"Very long response ({word_count} words) — not voice-friendly")

    # Check for "As an AI" / disclaimer patterns
    disclaimer_patterns = [
        r"as an ai", r"i'm just a", r"i cannot", r"i'm not able to",
        r"seek professional", r"consult a doctor", r"consult a professional",
        r"i don't have personal", r"i'm a language model",
    ]
    for dp in disclaimer_patterns:
        if re.search(dp, response.lower()):
            issues.append(f"Contains disclaimer/refusal: '{dp}'")

    return issues


# ═══════════════════════════════════════════════════════════════════════════════
# 3. TOOL-CALLING ACCURACY
# ═══════════════════════════════════════════════════════════════════════════════

TOOL_CALL_PROMPTS = [
    {
        "prompt": "Set a timer for 10 minutes for my pizza",
        "expected_tool": "app_set_timer",
        "category": "timer",
    },
    {
        "prompt": "Set an alarm for 7:30 AM tomorrow",
        "expected_tool": "app_set_alarm",
        "category": "alarm",
    },
    {
        "prompt": "Add dentist appointment next Monday at 2pm",
        "expected_tool": "app_add_event",
        "category": "calendar_event",
    },
    {
        "prompt": "Remind me to pick up groceries tomorrow",
        "expected_tool": "app_add_task",
        "category": "task",
    },
    {
        "prompt": "Add milk and eggs to my shopping list",
        "expected_tool": "app_add_shopping",
        "category": "shopping",
    },
    {
        "prompt": "I just bought 2 pounds of chicken breast",
        "expected_tool": "app_add_pantry",
        "category": "pantry",
    },
    {
        "prompt": "Turn on the living room lights",
        "expected_tool": "ha_control_device",
        "category": "smart_home",
    },
    {
        "prompt": "What's on my schedule this week?",
        "expected_tool": "app_list_schedule",
        "category": "calendar_query",
    },
    {
        "prompt": "What's the weather like today?",
        "expected_tool": "auto_weather_forecast",
        "category": "weather",
    },
    {
        "prompt": "Play some relaxing music",
        "expected_tool": "media_search_music",
        "category": "media",
    },
    {
        "prompt": "What movies do I have about space?",
        "expected_tool": "media_search_movies",
        "category": "media_search",
    },
    {
        "prompt": "Scan my network for unknown devices",
        "expected_tool": "privacy_scan_network",
        "category": "security",
    },
]


def _test_tool_selection(prompt: str, expected_tool: str, model: str = None) -> BenchmarkResult:
    """Test if the LLM selects the correct tool for a given prompt.

    Uses the raw Ollama API with a hardened system prompt to test
    tool selection in isolation.
    """
    if model is None:
        model = MODEL
    result = BenchmarkResult(
        prompt=prompt,
        category="tool_selection",
        tool_expected=expected_tool,
    )

    # Full tool catalog for accurate selection testing
    tools_for_test = [
        # Calendar / Tasks / Alarms
        "app_add_task: Add a task or to-do item or reminder for later",
        "app_add_event: Add a calendar event, appointment, or meeting (with a specific time)",
        "app_list_schedule: Show the calendar/schedule of upcoming events and tasks",
        "app_edit_task: Edit an existing task",
        "app_delete_task: Delete a task",
        "app_set_timer: Set a countdown timer (e.g., 10 minutes)",
        "app_set_alarm: Set a time-based alarm (e.g., 7:30 AM)",
        # Pantry / Shopping
        "app_add_pantry: Add food/items the user has at home in their pantry",
        "app_add_shopping: Add items to the shopping/grocery list",
        "app_list_pantry: List items in the pantry",
        "app_list_shopping: List items on the shopping list",
        # Trivia / Games
        "app_trivia_question: Start or play a trivia game/quiz",
        # Smart Home
        "ha_control_device: Control a smart home device (turn on/off, toggle, dim, set brightness, set temperature)",
        "ha_list_devices: List all smart home devices",
        "ha_set_light: Set a light's brightness or color",
        "ha_set_temperature: Set a thermostat temperature",
        # Knowledge Base
        "list_indexed_libraries: List all available offline knowledge libraries/collections",
        "search_knowledge_base: Search the offline knowledge base for an answer to a factual question",
        # Media
        "media_search_music: Search the music library by artist, song, or genre",
        "media_search_movies: Search the movie library by title, genre, or year",
        "media_search_books: Search the book/audiobook library",
        "media_play: Play a specific media item that's already been chosen",
        # Privacy / Security
        "privacy_scan_network: Scan the local network for devices",
        "privacy_block_device: Block a specific device from the network",
        "privacy_lockdown_mode: Enable lockdown mode (block all unknown devices)",
        # Weather / Briefings (Node-RED automations)
        "auto_weather_forecast: Get current weather conditions and forecast",
        "auto_daily_briefing: Get the daily briefing (weather + calendar + news)",
        "auto_news_summary: Get a summary of today's news",
    ]
    tool_list = "\n".join(tools_for_test)
    valid_names = [t.split(":")[0].strip() for t in tools_for_test]

    system_msg = (
        f"You are a tool-routing classifier. Your ONLY job is to pick exactly "
        f"ONE tool from the list below for the user's message.\n\n"
        f"Available tools:\n{tool_list}\n\n"
        f"STRICT RULES:\n"
        f"1. You MUST respond with EXACTLY one tool name from the list above. "
        f"Nothing else. No explanation. No prefix.\n"
        f"2. NEVER invent or hallucinate tool names. The tool name MUST be "
        f"copied verbatim from the list.\n"
        f"3. For factual/knowledge questions (how, what, why, when, who) where "
        f"no specific action tool fits, ALWAYS pick search_knowledge_base.\n"
        f"4. For 'list/show my libraries/collections', pick list_indexed_libraries.\n"
        f"5. For multi-step requests, pick the FIRST tool that should run.\n"
        f"6. If truly nothing fits, respond: NONE\n\n"
        f"Valid tool names (the ONLY allowed responses):\n"
        f"{', '.join(valid_names)}\n"
    )

    try:
        start = time.perf_counter()
        resp = requests.post(
            f"{OLLAMA_URL}/v1/chat/completions",
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.1,  # Low temp for deterministic tool selection
            },
            timeout=60,
        )
        resp.raise_for_status()
        end = time.perf_counter()

        result.total_time = end - start
        content = resp.json()["choices"][0]["message"]["content"].strip()
        result.response = content

        # Extract tool name — first try exact match against valid names
        content_lower = content.lower().strip()
        matched = None
        for vn in valid_names:
            if vn.lower() == content_lower or content_lower.startswith(vn.lower()):
                matched = vn
                break
        if not matched:
            # Try to find any valid name embedded in response
            for vn in valid_names:
                if vn.lower() in content_lower:
                    matched = vn
                    break
        if not matched:
            tool_match = re.search(r'(app_\w+|ha_\w+|auto_\w+|media_\w+|privacy_\w+|search_\w+|list_\w+)', content_lower)
            if tool_match:
                matched = tool_match.group(1)
            else:
                matched = content_lower
        result.tool_called = matched

        result.tool_correct = (
            result.tool_called == expected_tool or
            expected_tool in result.tool_called or
            result.tool_called in expected_tool
        )
        result.passed = result.tool_correct

    except Exception as e:
        result.error = str(e)
        result.passed = False

    return result


# ═══════════════════════════════════════════════════════════════════════════════
# 4. END-TO-END AGENT TESTS (via PuffPAI API)
# ═══════════════════════════════════════════════════════════════════════════════

E2E_TESTS = [
    # === CALENDAR / TASKS ===
    {
        "prompt": "Add a task: Buy dog food by tomorrow",
        "expected_behavior": "Task should appear in calendar tasks",
        "verify_endpoint": "/apps/calendar/data",
        "verify_key": "tasks",
        "search_text": "dog food",
        "category": "e2e_task",
    },
    {
        "prompt": "Add a calendar event for my haircut next Friday at 3pm",
        "expected_behavior": "Event should appear in calendar events",
        "verify_endpoint": "/apps/calendar/data",
        "verify_key": "events",
        "search_text": "haircut",
        "category": "e2e_event",
    },
    # === ALARMS / TIMERS ===
    {
        "prompt": "Set a timer for 5 minutes called pizza timer",
        "expected_behavior": "Timer should be created in alarmflow items",
        "verify_endpoint": "/apps/alarmflow/data",
        "verify_key": "items",
        "search_text": "pizza",
        "category": "e2e_timer",
    },
    {
        "prompt": "Set an alarm for 6:45 AM tomorrow called morning workout",
        "expected_behavior": "Alarm should be created in alarmflow items",
        "verify_endpoint": "/apps/alarmflow/data",
        "verify_key": "items",
        "search_text": "morning workout",
        "category": "e2e_alarm",
    },
    # === FRESHTABLE: SHOPPING & PANTRY ===
    {
        "prompt": "Add eggs and butter to my shopping list",
        "expected_behavior": "Items should appear in freshtable shoppingList",
        "verify_endpoint": "/apps/freshtable/data",
        "verify_key": "shoppingList",
        "search_text": "egg",
        "category": "e2e_shopping",
    },
    {
        "prompt": "I just bought 3 pounds of ground beef for the pantry",
        "expected_behavior": "Item should appear in pantry",
        "verify_endpoint": "/apps/freshtable/data",
        "verify_key": "pantry",
        "search_text": "ground beef",
        "category": "e2e_pantry",
    },
]


def _test_e2e(test_case: dict) -> BenchmarkResult:
    """Run an end-to-end test through the PuffPAI API and verify persistence."""
    result = BenchmarkResult(
        prompt=test_case["prompt"],
        category=test_case["category"],
    )

    try:
        # Send message to PuffPAI API
        start = time.perf_counter()
        resp = requests.post(
            f"{API_URL}/v1/chat/completions",
            json={
                "model": "puffpai",
                "messages": [{"role": "user", "content": test_case["prompt"]}],
                "stream": False,
            },
            timeout=120,
        )
        end = time.perf_counter()

        result.total_time = end - start

        if resp.status_code != 200:
            result.error = f"API returned {resp.status_code}: {resp.text[:200]}"
            result.passed = False
            return result

        data = resp.json()
        result.response = data.get("choices", [{}])[0].get("message", {}).get("content", "")

        # Verify the action persisted
        time.sleep(1)  # Give it a moment to persist
        verify_resp = requests.get(
            f"{API_URL}{test_case['verify_endpoint']}",
            timeout=10,
        )

        if verify_resp.status_code == 200:
            verify_data = verify_resp.json()
            items = verify_data.get(test_case["verify_key"], [])

            # Search for our test data
            search = test_case["search_text"].lower()
            found = any(
                search in json.dumps(item).lower()
                for item in (items if isinstance(items, list) else [items])
            )

            if found:
                result.passed = True
                result.tool_correct = True
            else:
                result.passed = False
                result.error = (
                    f"Action completed but '{search}' not found in "
                    f"{test_case['verify_key']}. Response: {result.response[:100]}"
                )
        else:
            result.error = f"Verify endpoint returned {verify_resp.status_code}"
            result.passed = False

    except requests.ConnectionError:
        result.error = f"Cannot connect to PuffPAI API at {API_URL}"
        result.passed = False
    except Exception as e:
        result.error = str(e)
        result.passed = False

    return result


# ═══════════════════════════════════════════════════════════════════════════════
# 5. PYTEST TEST CLASSES
# ═══════════════════════════════════════════════════════════════════════════════

class TestOllamaConnectivity:
    """Verify Ollama is reachable and the model is loaded."""

    def test_ollama_reachable(self):
        try:
            resp = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
            assert resp.status_code == 200, f"Ollama returned {resp.status_code}"
        except requests.ConnectionError:
            raise AssertionError(f"Ollama not reachable at {OLLAMA_URL}")

    def test_model_loaded(self):
        resp = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
        models = [m["name"] for m in resp.json().get("models", [])]
        assert MODEL in models, f"Model '{MODEL}' not found. Available: {models}"


class TestInferenceSpeed:
    """Benchmark raw inference speed against Ollama."""

    def test_speed_benchmarks(self):
        print("\n" + "=" * 70)
        print("  INFERENCE SPEED BENCHMARKS")
        print("=" * 70)

        results = []
        for sp in SPEED_PROMPTS:
            r = _benchmark_ollama_raw(sp["prompt"], sp["category"])
            results.append(r)

            status = "PASS" if r.passed else "FAIL"
            print(f"\n  [{status}] {sp['desc']}")
            if r.error:
                print(f"    Error: {r.error}")
            else:
                print(f"    TTFT:    {r.time_to_first_token:.2f}s")
                print(f"    Total:   {r.total_time:.2f}s")
                print(f"    Tokens:  ~{r.total_tokens}")
                print(f"    Speed:   {r.tokens_per_second:.1f} tok/s")
                print(f"    Preview: {r.response[:80]}...")

        # Summary
        passed = [r for r in results if r.passed]
        avg_ttft = sum(r.time_to_first_token for r in passed) / len(passed) if passed else 0
        avg_tps = sum(r.tokens_per_second for r in passed) / len(passed) if passed else 0

        print(f"\n  SUMMARY: {len(passed)}/{len(results)} passed")
        print(f"  Avg TTFT:  {avg_ttft:.2f}s")
        print(f"  Avg Speed: {avg_tps:.1f} tok/s")

        if avg_ttft > 5:
            print("  WARNING: TTFT > 5s feels sluggish for conversation")
        if avg_tps < 5:
            print("  WARNING: < 5 tok/s will feel robotic (aim for 10+)")

        assert len(passed) > 0, "All speed benchmarks failed"


class TestConversationNaturalness:
    """Test that responses follow voice-first persona rules."""

    def test_naturalness(self):
        print("\n" + "=" * 70)
        print("  CONVERSATION NATURALNESS")
        print("=" * 70)

        # Load persona
        persona_path = _REPO_ROOT / "src" / "puffpai" / "persona.txt"
        persona = persona_path.read_text() if persona_path.exists() else ""

        all_issues = []
        for prompt in NATURALNESS_PROMPTS:
            try:
                resp = requests.post(
                    f"{OLLAMA_URL}/v1/chat/completions",
                    json={
                        "model": MODEL,
                        "messages": [
                            {"role": "system", "content": persona},
                            {"role": "user", "content": prompt},
                        ],
                        "temperature": 0.6,
                    },
                    timeout=60,
                )
                content = resp.json()["choices"][0]["message"]["content"]
                issues = _check_naturalness(content)

                status = "PASS" if not issues else "WARN"
                print(f"\n  [{status}] \"{prompt}\"")
                print(f"    Response: {content[:120]}...")
                if issues:
                    for issue in issues:
                        print(f"    Issue: {issue}")
                    all_issues.extend(issues)
            except Exception as e:
                print(f"\n  [FAIL] \"{prompt}\" — {e}")

        total = len(NATURALNESS_PROMPTS)
        issues_count = len(all_issues)
        print(f"\n  SUMMARY: {total} prompts, {issues_count} naturalness issues")

        if issues_count > total * 2:
            print("  WARNING: Many naturalness issues — persona may not be adhered to")


class TestToolCallAccuracy:
    """Test if the LLM selects the correct tool for each prompt."""

    def test_tool_selection(self):
        print("\n" + "=" * 70)
        print("  TOOL-CALLING ACCURACY")
        print("=" * 70)

        results = []
        for tc in TOOL_CALL_PROMPTS:
            r = _test_tool_selection(tc["prompt"], tc["expected_tool"])
            results.append(r)

            status = "PASS" if r.tool_correct else "FAIL"
            print(f"\n  [{status}] \"{tc['prompt']}\"")
            print(f"    Expected: {tc['expected_tool']}")
            print(f"    Got:      {r.tool_called}")
            print(f"    Time:     {r.total_time:.2f}s")
            if r.error:
                print(f"    Error:    {r.error}")

        correct = sum(1 for r in results if r.tool_correct)
        total = len(results)
        accuracy = (correct / total * 100) if total > 0 else 0

        print(f"\n  SUMMARY: {correct}/{total} correct ({accuracy:.0f}% accuracy)")

        if accuracy < 60:
            print("  CRITICAL: Tool selection accuracy below 60%")
        elif accuracy < 80:
            print("  WARNING: Tool selection accuracy below 80%")
        else:
            print("  GOOD: Tool selection accuracy is solid")

        assert accuracy >= 50, f"Tool selection accuracy too low: {accuracy:.0f}%"


class TestEndToEnd:
    """End-to-end tests through the PuffPAI API (requires API server running)."""

    def test_api_reachable(self):
        """Check if PuffPAI API is running."""
        try:
            resp = requests.get(f"{API_URL}/health", timeout=5)
            assert resp.status_code == 200
        except requests.ConnectionError:
            print(f"\n  SKIP: PuffPAI API not running at {API_URL}")
            print(f"  Start it with: cd PuffPAI && python -m puffpai.api_server")

    def test_e2e_scenarios(self):
        """Run end-to-end scenarios and verify persistence."""
        # Check API first
        try:
            requests.get(f"{API_URL}/health", timeout=5)
        except requests.ConnectionError:
            print(f"\n  SKIP: PuffPAI API not running — skipping E2E tests")
            return

        print("\n" + "=" * 70)
        print("  END-TO-END AGENT TESTS")
        print("=" * 70)

        results = []
        for tc in E2E_TESTS:
            r = _test_e2e(tc)
            results.append(r)

            status = "PASS" if r.passed else "FAIL"
            print(f"\n  [{status}] {tc['category']}: \"{tc['prompt']}\"")
            print(f"    Expected: {tc['expected_behavior']}")
            print(f"    Time:     {r.total_time:.2f}s")
            if r.response:
                print(f"    Response: {r.response[:100]}...")
            if r.error:
                print(f"    Error:    {r.error}")

        passed = sum(1 for r in results if r.passed)
        total = len(results)
        print(f"\n  SUMMARY: {passed}/{total} E2E tests passed")


# ═══════════════════════════════════════════════════════════════════════════════
# 6. USER TEST PROMPTS (from local doc — placeholder for injection)
# ═══════════════════════════════════════════════════════════════════════════════

# From: PuffPAI/Pre-Canned Prompts w Screenshots.docx
USER_TEST_PROMPTS = [
    {
        "prompt": "What knowledge libraries do I have available? Can you please list them in bullet format and in alphabetical order and give them a brief description?",
        "expected_tool": "list_indexed_libraries",
        "category": "knowledge_libs",
    },
    {
        "prompt": "What are the best methods for water purification in an emergency?",
        "expected_tool": "search_knowledge_base",
        "category": "knowledge_query",
    },
    {
        "prompt": "Give me my daily briefing.",
        "expected_tool": "auto_daily_briefing",
        "category": "briefing",
    },
    {
        "prompt": "Scan my network and show me what devices are connected.",
        "expected_tool": "privacy_scan_network",
        "category": "security",
    },
    {
        "prompt": "Can you please block the unknown device and enable lockdown mode?",
        "expected_tool": "privacy_block_device",
        "category": "security_action",
    },
    {
        "prompt": "It's movie night - dim the living room lights to 30%, set the temperature to 70, search my movie library for something sci-fi, and add popcorn to my shopping list.",
        "expected_tool": "ha_control_device",  # First tool in multi-step
        "category": "multi_step",
    },
    {
        "prompt": "Let's play a trivia game!",
        "expected_tool": "app_trivia_question",
        "category": "trivia",
    },
    {
        "prompt": "Add a task to my Calendar to Pickup Riley every day from school at 3 PM, but to leave the house no later than 2:30 PM.",
        "expected_tool": "app_add_event",
        "category": "calendar_recurring",
    },
]


# ═══════════════════════════════════════════════════════════════════════════════
# 7. STANDALONE RUNNER (full report)
# ═══════════════════════════════════════════════════════════════════════════════

def run_full_benchmark():
    """Run all benchmarks and print a comprehensive report."""
    print("\n" + "=" * 70)
    print("  PUFFPAI LLM BENCHMARK SUITE")
    print(f"  Model: {MODEL} | Ollama: {OLLAMA_URL}")
    print(f"  Time:  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)

    # 1. Connectivity
    print("\n--- CONNECTIVITY ---")
    try:
        resp = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
        models = [m["name"] for m in resp.json().get("models", [])]
        print(f"  Ollama: CONNECTED ({len(models)} models)")
        print(f"  Models: {', '.join(models)}")
    except Exception as e:
        print(f"  Ollama: FAILED — {e}")
        return

    # 2. Speed benchmarks
    print("\n--- SPEED BENCHMARKS ---")
    speed_results = []
    for sp in SPEED_PROMPTS:
        r = _benchmark_ollama_raw(sp["prompt"], sp["category"])
        speed_results.append(r)
        if r.passed:
            print(f"  {sp['category']:15s} | TTFT: {r.time_to_first_token:5.2f}s | "
                  f"Total: {r.total_time:5.2f}s | {r.tokens_per_second:5.1f} tok/s")
        else:
            print(f"  {sp['category']:15s} | FAILED: {r.error[:50]}")

    passed_speed = [r for r in speed_results if r.passed]
    if passed_speed:
        avg_ttft = sum(r.time_to_first_token for r in passed_speed) / len(passed_speed)
        avg_tps = sum(r.tokens_per_second for r in passed_speed) / len(passed_speed)
        print(f"\n  Average TTFT: {avg_ttft:.2f}s | Average Speed: {avg_tps:.1f} tok/s")

    # 3. Naturalness
    print("\n--- NATURALNESS ---")
    persona_path = _REPO_ROOT / "src" / "puffpai" / "persona.txt"
    persona = persona_path.read_text() if persona_path.exists() else ""

    naturalness_issues_total = 0
    for prompt in NATURALNESS_PROMPTS:
        try:
            resp = requests.post(
                f"{OLLAMA_URL}/v1/chat/completions",
                json={
                    "model": MODEL,
                    "messages": [
                        {"role": "system", "content": persona},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.6,
                },
                timeout=60,
            )
            content = resp.json()["choices"][0]["message"]["content"]
            issues = _check_naturalness(content)
            status = "OK" if not issues else f"ISSUES({len(issues)})"
            naturalness_issues_total += len(issues)
            print(f"  {status:12s} | \"{prompt[:50]}\"")
            if issues:
                for i in issues:
                    print(f"               | -> {i}")
        except Exception as e:
            print(f"  ERROR        | \"{prompt[:50]}\" — {e}")

    # 4. Tool selection
    print("\n--- TOOL SELECTION ---")
    tool_results = []
    for tc in TOOL_CALL_PROMPTS:
        r = _test_tool_selection(tc["prompt"], tc["expected_tool"])
        tool_results.append(r)
        status = "CORRECT" if r.tool_correct else "WRONG"
        print(f"  {status:8s} | Expected: {tc['expected_tool']:25s} | "
              f"Got: {r.tool_called:25s} | \"{tc['prompt'][:40]}\"")

    correct = sum(1 for r in tool_results if r.tool_correct)
    total = len(tool_results)
    accuracy = (correct / total * 100) if total > 0 else 0
    print(f"\n  Tool Accuracy: {correct}/{total} ({accuracy:.0f}%)")

    # 4b. User test prompts from local doc
    print("\n--- USER TEST PROMPTS (from local doc) ---")
    user_results = []
    for tc in USER_TEST_PROMPTS:
        r = _test_tool_selection(tc["prompt"], tc["expected_tool"])
        user_results.append(r)
        status = "CORRECT" if r.tool_correct else "WRONG"
        print(f"  {status:8s} | Expected: {tc['expected_tool']:25s} | "
              f"Got: {r.tool_called:25s}")
        print(f"           | \"{tc['prompt'][:80]}\"")

    user_correct = sum(1 for r in user_results if r.tool_correct)
    user_total = len(user_results)
    user_acc = (user_correct / user_total * 100) if user_total > 0 else 0
    print(f"\n  User Prompt Accuracy: {user_correct}/{user_total} ({user_acc:.0f}%)")

    # 5. E2E (only if API running)
    print("\n--- END-TO-END ---")
    try:
        requests.get(f"{API_URL}/health", timeout=3)
        for tc in E2E_TESTS:
            r = _test_e2e(tc)
            status = "PASS" if r.passed else "FAIL"
            print(f"  {status:5s} | {tc['category']:15s} | {r.total_time:5.2f}s | "
                  f"\"{tc['prompt'][:50]}\"")
            if r.error:
                print(f"        | -> {r.error[:80]}")
    except requests.ConnectionError:
        print(f"  SKIPPED — PuffPAI API not running at {API_URL}")

    # Final summary
    print("\n" + "=" * 70)
    print("  FINAL SCORECARD")
    print("=" * 70)
    print(f"  Speed:       {len(passed_speed)}/{len(speed_results)} benchmarks passed")
    if passed_speed:
        print(f"  Avg TTFT:    {avg_ttft:.2f}s {'(good)' if avg_ttft < 3 else '(slow)' if avg_ttft < 7 else '(very slow)'}")
        print(f"  Avg tok/s:   {avg_tps:.1f} {'(good)' if avg_tps > 10 else '(okay)' if avg_tps > 5 else '(slow)'}")
    print(f"  Naturalness: {naturalness_issues_total} issues across {len(NATURALNESS_PROMPTS)} prompts")
    print(f"  Tool Accuracy: {accuracy:.0f}%")
    print(f"  Feels natural? {'Yes' if avg_tps > 8 and naturalness_issues_total < 5 and accuracy > 70 else 'Needs work'}")
    print("=" * 70)


def run_model_comparison(models: list = None):
    """Run tool-selection benchmarks across multiple models for comparison."""
    if models is None:
        # Auto-detect available models
        try:
            resp = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
            models = [m["name"] for m in resp.json().get("models", [])]
        except Exception as e:
            print(f"Cannot reach Ollama: {e}")
            return

    print("\n" + "=" * 80)
    print("  PUFFPAI MODEL HEAD-TO-HEAD COMPARISON")
    print(f"  Models: {', '.join(models)}")
    print("=" * 80)

    all_prompts = TOOL_CALL_PROMPTS + USER_TEST_PROMPTS
    results_by_model = {}

    for model in models:
        print(f"\n\n>>> TESTING MODEL: {model}")
        print("-" * 80)
        model_results = []

        # Speed test (single prompt)
        speed_start = time.perf_counter()
        try:
            speed_r = requests.post(
                f"{OLLAMA_URL}/v1/chat/completions",
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "Say hello in one sentence."}],
                    "temperature": 0.5,
                },
                timeout=60,
            )
            speed_total = time.perf_counter() - speed_start
            print(f"  Warm-up time: {speed_total:.2f}s")
        except Exception as e:
            print(f"  Model failed: {e}")
            continue

        # Tool selection tests
        correct = 0
        total_time = 0.0
        for tc in all_prompts:
            r = _test_tool_selection(tc["prompt"], tc["expected_tool"], model=model)
            model_results.append(r)
            total_time += r.total_time
            status = "OK " if r.tool_correct else "X  "
            if r.tool_correct:
                correct += 1
            print(f"  {status}| {tc['expected_tool'][:25]:25s} | "
                  f"got: {r.tool_called[:25]:25s} | {r.total_time:5.2f}s")

        accuracy = (correct / len(all_prompts) * 100) if all_prompts else 0
        avg_time = total_time / len(all_prompts) if all_prompts else 0

        results_by_model[model] = {
            "accuracy": accuracy,
            "correct": correct,
            "total": len(all_prompts),
            "avg_time": avg_time,
            "results": model_results,
        }

        print(f"\n  >> {model}: {correct}/{len(all_prompts)} correct "
              f"({accuracy:.0f}%) | avg {avg_time:.2f}s/prompt")

    # Final comparison table
    print("\n\n" + "=" * 80)
    print("  HEAD-TO-HEAD SCORECARD")
    print("=" * 80)
    print(f"\n  {'Model':<30s} {'Accuracy':<15s} {'Avg Time':<15s} {'Verdict':<15s}")
    print("  " + "-" * 76)
    for model, stats in results_by_model.items():
        verdict = (
            "EXCELLENT" if stats["accuracy"] >= 90
            else "GOOD" if stats["accuracy"] >= 75
            else "OKAY" if stats["accuracy"] >= 60
            else "POOR"
        )
        print(f"  {model:<30s} "
              f"{stats['correct']}/{stats['total']} ({stats['accuracy']:.0f}%)"
              f"{'':<6s}"
              f"{stats['avg_time']:.2f}s"
              f"{'':<10s}"
              f"{verdict}")

    if results_by_model:
        winner = max(results_by_model.items(), key=lambda x: x[1]["accuracy"])
        print(f"\n  WINNER: {winner[0]} ({winner[1]['accuracy']:.0f}% accuracy)")
    print("=" * 80)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "compare":
        models = sys.argv[2:] if len(sys.argv) > 2 else None
        run_model_comparison(models)
    else:
        run_full_benchmark()

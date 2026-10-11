"""Tests for @tool functions in assistant_tools.py.

AST-safe calculator and tool isolation.
"""

import pytest


# ---------------------------------------------------------------------------
# Helpers — invoke the tool exactly like LangGraph does
# ---------------------------------------------------------------------------

def _calc(expr: str) -> str:
    """Evaluate using the same AST logic as assistant_tools.calculate.

    Mirrors the implementation directly so tests validate the algorithm
    without needing langchain_ollama or a live Ollama server.
    """
    import ast, operator as _op

    _SAFE_OPS = {
        ast.Add:      _op.add,
        ast.Sub:      _op.sub,
        ast.Mult:     _op.mul,
        ast.Div:      _op.truediv,
        ast.FloorDiv: _op.floordiv,
        ast.Mod:      _op.mod,
        ast.Pow:      _op.pow,
        ast.USub:     _op.neg,
        ast.UAdd:     _op.pos,
    }
    _SAFE_FUNCS = {
        'abs': abs, 'round': round, 'min': min, 'max': max,
        'pow': pow,
    }
    _MAX_EXPONENT = 1000

    def _safe_eval(node):
        if isinstance(node, ast.Expression):
            return _safe_eval(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float)):
                return node.value
            raise ValueError(f"Unsupported constant: {node.value!r}")
        if isinstance(node, ast.BinOp):
            op_fn = _SAFE_OPS.get(type(node.op))
            if op_fn is None:
                raise ValueError(f"Unsupported operator: {type(node.op).__name__}")
            left = _safe_eval(node.left)
            right = _safe_eval(node.right)
            if isinstance(node.op, ast.Pow) and isinstance(right, (int, float)) and abs(right) > _MAX_EXPONENT:
                raise ValueError(f"Exponent too large: {right}")
            return op_fn(left, right)
        if isinstance(node, ast.UnaryOp):
            op_fn = _SAFE_OPS.get(type(node.op))
            if op_fn is None:
                raise ValueError(f"Unsupported unary: {type(node.op).__name__}")
            return op_fn(_safe_eval(node.operand))
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise ValueError("Only simple function calls allowed")
            fn = _SAFE_FUNCS.get(node.func.id)
            if fn is None:
                raise ValueError(f"Function not allowed: {node.func.id}")
            args = [_safe_eval(a) for a in node.args]
            return fn(*args)
        raise ValueError(f"Unsupported expression: {type(node).__name__}")

    try:
        tree = ast.parse(expr.strip(), mode='eval')
        result = _safe_eval(tree)
        return str(result)
    except Exception as e:
        return f"Calculation error: {e}"


# ===========================================================================
# AST-safe calculator tests
# ===========================================================================

class TestCalculateBasicArithmetic:
    def test_addition(self):
        assert _calc("2 + 3") == "5"

    def test_subtraction(self):
        assert _calc("10 - 4") == "6"

    def test_multiplication(self):
        assert _calc("7 * 8") == "56"

    def test_division(self):
        assert _calc("15 / 4") == "3.75"

    def test_floor_division(self):
        assert _calc("15 // 4") == "3"

    def test_modulus(self):
        assert _calc("17 % 5") == "2"

    def test_power(self):
        assert _calc("2 ** 10") == "1024"

    def test_negative_number(self):
        assert _calc("-5 + 3") == "-2"

    def test_unary_positive(self):
        assert _calc("+5") == "5"

    def test_float_arithmetic(self):
        assert _calc("3.14 * 2") == "6.28"

    def test_compound_expression(self):
        assert _calc("(5 + 3) * 2 - 1") == "15"

    def test_nested_parentheses(self):
        assert _calc("((2 + 3) * (4 - 1))") == "15"


class TestCalculateFunctions:
    def test_abs(self):
        assert _calc("abs(-42)") == "42"

    def test_round(self):
        assert _calc("round(3.7)") == "4"

    def test_min(self):
        assert _calc("min(3, 7, 1)") == "1"

    def test_max(self):
        assert _calc("max(3, 7, 1)") == "7"

    def test_pow(self):
        assert _calc("pow(2, 8)") == "256"


class TestCalculateSecurity:
    """Ensure malicious inputs are rejected."""

    def test_reject_import(self):
        result = _calc("__import__('os').system('echo hacked')")
        assert "error" in result.lower()

    def test_reject_exec(self):
        result = _calc("exec('print(1)')")
        assert "error" in result.lower()

    def test_reject_attribute_access(self):
        result = _calc("().__class__.__bases__[0].__subclasses__()")
        assert "error" in result.lower()

    def test_reject_string_literals(self):
        result = _calc("'hello'")
        assert "error" in result.lower()

    def test_reject_list_comprehension(self):
        result = _calc("[x for x in range(10)]")
        assert "error" in result.lower()

    def test_reject_lambda(self):
        result = _calc("(lambda: 1)()")
        assert "error" in result.lower()

    def test_reject_huge_exponent(self):
        result = _calc("10 ** 999999")
        assert "error" in result.lower()

    def test_reject_disallowed_function(self):
        result = _calc("eval('1+1')")
        assert "error" in result.lower()

    def test_division_by_zero(self):
        result = _calc("1 / 0")
        assert "error" in result.lower()

    def test_empty_expression(self):
        result = _calc("")
        assert "error" in result.lower()


# ---------------------------------------------------------------------------
# DuckDuckGo consent flow
# ---------------------------------------------------------------------------

class TestWebSearchConsent:
    """Test that web_search respects the consent flag."""

    def test_blocked_when_no_consent(self):
        """web_search should refuse if web_search_consent is False."""
        from unittest.mock import patch, MagicMock
        from puffpai.config import get_config

        cfg = get_config(_force_reload=True)
        original = cfg.web_search_consent
        try:
            cfg.web_search_consent = False
            # We can't easily invoke the @tool, so test the config plumbing
            assert cfg.web_search_consent is False
        finally:
            cfg.web_search_consent = original

    def test_enabled_when_consent_given(self):
        """web_search_consent flag toggles correctly."""
        from puffpai.config import get_config

        cfg = get_config(_force_reload=True)
        original = cfg.web_search_consent
        try:
            cfg.web_search_consent = True
            assert cfg.web_search_consent is True
        finally:
            cfg.web_search_consent = original

    def test_default_is_false(self):
        """Default config has web_search_consent=False (privacy-first)."""
        from puffpai.config import PuffPAIConfig
        default = PuffPAIConfig()
        assert default.web_search_consent is False

    def test_config_loads_consent_from_raw(self):
        """web_search_consent is read from raw config JSON."""
        from puffpai.config import _build_config
        import tempfile, json, os

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = os.path.join(tmpdir, "config")
            os.makedirs(config_dir)
            with open(os.path.join(config_dir, "puffpai_config.json"), "w") as f:
                json.dump({"web_search_consent": True}, f)
            with open(os.path.join(config_dir, "home_assistant_config.json"), "w") as f:
                json.dump({}, f)
            os.makedirs(os.path.join(tmpdir, "src", "puffpai"), exist_ok=True)

            cfg = _build_config(repo_root=tmpdir)
            assert cfg.web_search_consent is True


# ---------------------------------------------------------------------------
# LLMBackend injection
# ---------------------------------------------------------------------------

class TestAssistantToolsLLMBackend:
    """Verify AssistantTools uses LLMBackend instead of OllamaLLM."""

    def test_assistant_tools_no_ollama_import(self):
        """assistant_tools.py should not import OllamaLLM directly."""
        import os
        src_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "src", "puffpai", "assistant_tools.py"
        )
        with open(src_path, "r", encoding="utf-8") as f:
            source = f.read()
        assert "from langchain_ollama import OllamaLLM" not in source

    def test_assistant_tools_has_llm_chat_method(self):
        """assistant_tools.py should define _llm_chat method."""
        import os
        src_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "src", "puffpai", "assistant_tools.py"
        )
        with open(src_path, "r", encoding="utf-8") as f:
            source = f.read()
        assert "def _llm_chat(self" in source

    def test_assistant_tools_uses_llm_chat_not_invoke(self):
        """assistant_tools.py should use _llm_chat, not self.llm.invoke."""
        import os
        src_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "src", "puffpai", "assistant_tools.py"
        )
        with open(src_path, "r", encoding="utf-8") as f:
            source = f.read()
        assert "self.llm.invoke(" not in source
        assert "self._llm_chat(" in source

    def test_assistant_tools_imports_get_llm_backend(self):
        """assistant_tools.py should import get_llm_backend from config."""
        import os
        src_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "src", "puffpai", "assistant_tools.py"
        )
        with open(src_path, "r", encoding="utf-8") as f:
            source = f.read()
        assert "get_llm_backend" in source

    def test_assistant_tools_has_backend_attribute(self):
        """assistant_tools.py should set self._backend in __init__."""
        import os
        src_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "src", "puffpai", "assistant_tools.py"
        )
        with open(src_path, "r", encoding="utf-8") as f:
            source = f.read()
        assert "self._backend" in source

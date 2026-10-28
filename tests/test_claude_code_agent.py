from app.claude_code_agent import _run_from_result


def test_run_id_comes_from_the_tool_reply():
    block = {"content": [{"type": "text", "text": "[run-05] Run succeeded."}, {"type": "image"}]}
    assert _run_from_result(block) == "run-05"
    assert _run_from_result({"content": "Run limit (6) reached for this request."}) is None

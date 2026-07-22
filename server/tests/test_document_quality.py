import pytest

from server.app.screening.document_quality import assess_text_quality


def test_quality_accepts_normal_and_concise_resumes() -> None:
    normal = assess_text_quality("张三\nPython 后端工程师\n5年 FastAPI 与 PostgreSQL 经验\n本科")
    concise = assess_text_quality("Python\n5年经验")
    assert normal.quality == "good" and normal.reasons == ()
    assert concise.quality == "good" and concise.reasons == ()


def test_quality_distinguishes_empty_and_too_short() -> None:
    empty = assess_text_quality(" \n\t")
    short = assess_text_quality("Hi")
    assert empty.quality == "empty" and empty.reasons == ("empty_text",)
    assert short.quality == "poor" and short.reasons == ("too_short",)


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("Python 后端工程师\ufffd\ufffd\ufffd\ufffd，五年平台开发经验", "replacement_characters"),
        ("Python\x00\x01 后端工程师，五年平台开发经验", "control_characters"),
        ("!@#$%^&*()_+=[]{}<>?/\\|~`!@#$%^&*()", "gibberish_symbols"),
        ("候选人简历\n保密\n保密\n保密\n保密\n保密\n教育经历", "duplicate_line_domination"),
        ("工作经历 2024年09月2022年09月2020年09月 某科技公司", "concatenated_date_runs"),
    ],
)
def test_quality_detects_bounded_damage_reasons(text: str, reason: str) -> None:
    result = assess_text_quality(text)
    assert result.quality == "poor"
    assert reason in result.reasons
    assert len(result.reasons) <= 6
    assert set(result.metrics) == {
        "char_count", "visible_char_count", "line_count", "replacement_ratio",
        "control_ratio", "symbol_ratio", "duplicate_line_ratio", "concatenated_date_runs",
    }
    assert text not in repr(result)


def test_quality_rejects_non_string_input() -> None:
    with pytest.raises(TypeError):
        assess_text_quality(None)  # type: ignore[arg-type]

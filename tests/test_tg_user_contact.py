from services.employer_contact import (
    coalesce_employer_contact_for_deeplink,
    extract_tg_user_display_name,
    is_tg_user_id_contact,
    tg_user_response_instructions_markdown,
    tg_user_vacancy_notice_html,
)
from services.vacancy_card import VacancyCardInput, build_vacancy_preview_html


def test_is_tg_user_id_contact():
    assert is_tg_user_id_contact("tg://user?id=1057604085")
    assert not is_tg_user_id_contact("@boss")


def test_extract_display_name_from_vacancy():
    body = "👉 [Станислав](tg://user?id=1057604085), нужен хелпер"
    assert extract_tg_user_display_name(body, "1057604085") == "Станислав"


def test_coalesce_uses_poster_username():
    assert coalesce_employer_contact_for_deeplink(
        "tg://user?id=1057604085",
        poster_username="stanislav_hr",
    ) == "@stanislav_hr"


def test_tg_user_markdown_has_clickable_link():
    text = tg_user_response_instructions_markdown(
        "tg://user?id=1057604085",
        vacancy_text="👉 [Станислав](tg://user?id=1057604085)",
        draft_text="Здравствуйте",
    )
    assert "[Станислав](tg://user?id=1057604085)" in text
    assert "Здравствуйте" in text


def test_vacancy_preview_tg_user_hidden_by_default():
    inp = VacancyCardInput(
        category_code="helper",
        category_name="Хелпер",
        category_emoji="👷",
        body="👉 [Станислав](tg://user?id=1057604085)",
        freshness="Свежая",
        author_contact="tg://user?id=1057604085",
    )
    html = build_vacancy_preview_html(inp)
    assert "tg://user?id=1057604085" not in html
    assert "Откликнуться" in html


def test_vacancy_preview_tg_user_notice_when_enabled():
    inp = VacancyCardInput(
        category_code="helper",
        category_name="Хелпер",
        category_emoji="👷",
        body="👉 [Станислав](tg://user?id=1057604085)",
        freshness="Свежая",
        author_contact="tg://user?id=1057604085",
    )
    html = build_vacancy_preview_html(inp, show_employer_contact=True)
    assert "tg://user?id=1057604085" in html
    assert "Станислав" in html

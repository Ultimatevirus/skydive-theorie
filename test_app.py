from app import app


def test_practice_page_shows_level_question_and_cards():
    client = app.test_client()
    response = client.get("/practice")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Welk brevet wil je voor oefenen?" in html
    assert "A" in html
    assert "B" in html


def test_level_selection_redirects_to_mode_selection():
    client = app.test_client()
    response = client.post("/practice/select", data={"level": "A"}, follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/practice/mode/A")


def test_mode_selection_shows_practice_options():
    client = app.test_client()
    response = client.get("/practice/mode/A")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Wil je vrij oefenen, of een oefenexamen maken?" in html
    assert "Oefenexamen maken" in html
    assert "Vrij oefenen" in html


def test_exam_mode_sets_full_exam_question_count():
    client = app.test_client()
    response = client.post(
        "/practice/mode/A",
        data={"practice_type": "exam"},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/exam")
    with client.session_transaction() as session:
        assert session["level"] == "A"
        assert session["question_amount"] == 40


def test_free_practice_redirects_to_question_amount_prompt():
    client = app.test_client()
    response = client.post(
        "/practice/mode/A",
        data={"practice_type": "free"},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/practice/free/A")

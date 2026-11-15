"""Chat parity, natural locale prompts, scoped context and explicit what-if saves."""

import pytest

from agrix.chat import interpret


@pytest.mark.parametrize(
    "message,field,value",
    [
        ("What if price drops by 20%?", "price_multiplier", "0.8"),
        ("Et si le rendement baisse de 10% ?", "yield_multiplier", "0.9"),
        ("إلا نقص الثمن ب 20%؟", "price_multiplier", "0.8"),
    ],
)
def test_explicit_sensitivity_assumptions(message, field, value):
    assert str(getattr(interpret(message), field)) == value


def test_optional_interpreter_rejects_invented_amounts_and_unknown_tools(monkeypatch):
    import httpx

    from agrix.chat import _model_intent
    from agrix.config import Settings

    settings = Settings(
        llm_api_url="https://model.invalid/chat", llm_api_key="secret", llm_model="configured-model"
    )
    seen = []
    outputs = iter(
        [
            '{"topic":"what_if","budget_mad":99999}',
            '{"topic":"what_if","tool":"save_plan"}',
            '{"topic":"funding"}',
        ]
    )

    def response(url, **kwargs):
        seen.append(kwargs["json"])
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={"choices": [{"message": {"content": next(outputs)}}]},
        )

    monkeypatch.setattr(httpx, "post", response)
    assert _model_intent("Explain this", "en", settings) is None
    assert _model_intent("Save it", "en", settings) is None
    assert _model_intent("Explain funding", "en", settings).topic == "funding"
    assert "secret" not in str(seen)
    assert "plot_id" not in str(seen)


@pytest.mark.parametrize(
    "message,amount",
    [
        ("What if my budget is 20,000 MAD?", "20000"),
        ("Et si mon budget est de 20 000 MAD ?", "20000"),
        ("إلا كانت الميزانية ديالي 20000 درهم؟", "20000"),
        ("إلا كانت الميزانية ديالي ٢٠٠٠٠ درهم؟", "20000"),
    ],
)
def test_local_interpretation(message, amount):
    assert str(interpret(message).budget_mad) == amount


@pytest.mark.parametrize("locale", ["en", "fr", "ary"])
def test_what_if_reuses_engine_and_requires_explicit_save(client, evaluation_request, locale):
    original = client.post("/rotations/evaluate", json=evaluation_request).json()
    saved = client.post(
        "/plans",
        json={
            "plot_id": original["plot_id"],
            "evaluation_id": original["id"],
            "status": "selected",
        },
    ).json()
    prompts = {
        "en": "What if my budget is 0 MAD?",
        "fr": "Et si mon budget est de 0 MAD ?",
        "ary": "إلا كانت الميزانية ديالي 0 درهم؟",
    }
    response = client.post(
        "/chat",
        json={
            "plot_id": original["plot_id"],
            "evaluation_ids": [original["id"]],
            "message": prompts[locale],
            "locale": locale,
        },
    )
    assert response.status_code == 200, response.text
    answer = response.json()
    changed = answer["what_if"]
    assert changed["status"] == "doesnt_fit"
    assert answer["read_only"] and answer["requires_confirmation"]
    assert changed["input_snapshot"]["request"]["constraints"]["budget_mad"] == "0.00"
    assert (
        answer["facts"][1]["peak_funding_need_mad"]
        == changed["financials"]["whole_plot"]["peak_funding_need"]["value"]
    )
    replay = client.post("/rotations/evaluate", json=changed["input_snapshot"]["request"]).json()
    assert replay["fingerprint"] == changed["fingerprint"]
    assert client.get("/plans/" + saved["id"]).json() == saved
    assert client.get("/evaluations/" + original["id"]).json() == original
    confirmed = client.post(
        "/plans",
        json={"plot_id": original["plot_id"], "evaluation_id": changed["id"], "status": "draft"},
    )
    assert confirmed.status_code == 201


def test_late_rain_does_not_invent_a_delay_or_weather(client, evaluation_request):
    original = client.post("/rotations/evaluate", json=evaluation_request).json()
    payload = {"plot_id": original["plot_id"], "evaluation_ids": [original["id"]], "locale": "ary"}
    response = client.post("/chat", json={**payload, "message": "إلا تعطّل الشتا؟"}).json()
    assert response["what_if"] is None
    delayed = client.post("/chat", json={**payload, "message": "إلا تأخر الشتا ب 30 يوم؟"}).json()
    assert delayed["what_if"] is not None
    assert delayed["what_if"]["input_snapshot"]["request"]["start_date"] == "2025-12-01"
    assert delayed["what_if"]["input_snapshot"]["profile"] == original["input_snapshot"]["profile"]


def test_chat_requires_scope_and_current_revision_for_what_if(client, evaluation_request):
    original = client.post("/rotations/evaluate", json=evaluation_request).json()
    body = {
        "plot_id": original["plot_id"],
        "evaluation_ids": [original["id"]],
        "message": "budget 20000",
        "locale": "en",
    }
    assert client.post("/chat", json=body, headers={"X-Project-ID": "other"}).status_code == 404
    client.patch(
        "/plots/" + original["plot_id"], json={"expected_revision": 1, "name": "New inputs"}
    )
    assert client.post("/chat", json=body).status_code == 409
    summary = client.post("/chat", json={**body, "message": "Explain funding"}).json()
    assert "older plot revision" in summary["message"]


def test_no_evaluation_gives_next_action_and_rejects_invalid_delay(client, plot):
    reply = client.post("/chat", json={"plot_id": plot["id"], "message": "كيفاش نبدا؟"}).json()
    assert reply["facts"] == [] and reply["suggested_actions"]
    invalid = client.post("/chat", json={"plot_id": plot["id"], "message": "payment 9999 days"})
    assert invalid.status_code == 422

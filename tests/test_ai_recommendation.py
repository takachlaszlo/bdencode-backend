from __future__ import annotations

import json

import httpx
import pytest

from bdencode.ai_recommendation import (
    AIRecommendationError,
    AIRecommendationService,
    RecommendationContext,
    recommendation_output_schema,
)
from bdencode.media.profiles import DetailLevel, VideoEncoder, recommended_profile


def _context(*, detail: DetailLevel = DetailLevel.ADVANCED) -> RecommendationContext:
    return RecommendationContext(
        encoder=VideoEncoder.X264,
        detail_level=detail,
        content_type="film",
        scan_facts={
            "disc_kind": "bd",
            "video": {
                "codec": "avc",
                "width": 1920,
                "height": 1080,
                "frame_rate": "24000/1001",
                "field_order": "progressive",
            },
        },
        base_settings=recommended_profile("x264", detail_level=detail),
        quality_priority="maximum",
        target_size_gib=12.5,
        genre="szemcsés noir",
        prompt="Őrizze meg a filmszemcsét.",
    )


def _provider_document(detail: DetailLevel) -> dict[str, object]:
    _, names = recommendation_output_schema(VideoEncoder.X264, detail)
    values = {name: None for name in names}
    values.update({"crf": 16.5, "preset": "slower", "tune": "grain"})
    return {
        "settings": values,
        "temporal_filter": "progressive",
        "summary": "Filmszemcse-megőrző, magas minőségű profil.",
        "rationale": ["Alacsonyabb CRF a finom textúrákhoz."],
        "warnings": ["A CRF nem garantál pontos fájlméretet."],
        "confidence": 0.91,
    }


def test_recommendation_schema_excludes_format_and_bitstream_controls() -> None:
    schema, names = recommendation_output_schema(
        VideoEncoder.X265, DetailLevel.PRO
    )

    assert "crf" in names
    assert "sao" in names
    for locked in (
        "encoder",
        "profile",
        "bit_depth",
        "pixel_format",
        "color",
        "hdr10",
        "aud",
        "repeat_headers",
        "annexb",
    ):
        assert locked not in names
    assert schema["properties"]["settings"]["additionalProperties"] is False
    assert set(schema["properties"]["settings"]["required"]) == set(names)


def test_service_uses_responses_structured_output_and_locally_validates() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("Authorization")
        seen["body"] = json.loads(request.content)
        provider = {
            "model": "gpt-test-snapshot",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": json.dumps(
                                _provider_document(DetailLevel.ADVANCED)
                            ),
                        }
                    ],
                }
            ],
        }
        return httpx.Response(200, request=request, json=provider)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = AIRecommendationService(
            model="gpt-test",
            credential_loader=lambda name: (
                "private-key" if name == "openai-api-key" else ""
            ),
            client=client,
        ).recommend(_context())

    assert seen["authorization"] == "Bearer private-key"
    body = seen["body"]
    assert body["store"] is False
    assert body["text"]["format"]["strict"] is True
    model_input = json.loads(body["input"])
    assert model_input["operator_goal"]["target_size_gib"] == 12.5
    assert model_input["hard_constraints"]["crop"].startswith("automatic")
    assert result.model == "gpt-test-snapshot"
    assert result.settings["crf"] == 16.5
    assert result.settings["preset"] == "slower"
    assert result.settings["tune"] == "grain"
    # Grain coherence is applied by the deterministic local profile builder.
    assert result.settings["qcomp"] == 0.75
    assert result.requires_operator_confirmation is True


def test_service_rejects_non_json_provider_output_without_leaking_secret() -> None:
    secret = "do-not-leak"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "not-json"}],
                    }
                ]
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        service = AIRecommendationService(
            credential_loader=lambda _name: secret,
            client=client,
        )
        with pytest.raises(AIRecommendationError) as error:
            service.recommend(_context())

    assert secret not in str(error.value)


def test_service_rejects_provider_prose_with_the_wrong_shape() -> None:
    invalid = _provider_document(DetailLevel.ADVANCED)
    invalid["warnings"] = "not an array"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={"output_text": json.dumps(invalid)},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        service = AIRecommendationService(
            credential_loader=lambda _name: "secret",
            client=client,
        )
        with pytest.raises(AIRecommendationError, match="invalid shape"):
            service.recommend(_context())

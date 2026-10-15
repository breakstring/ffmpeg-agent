from typing import Annotated

import pytest
from pydantic import TypeAdapter

from media_service import parameters
from media_service.models import OPERATIONS, capabilities
from media_service.parameters import AudioParams, FrameRate, ImageParams, Parameters, VideoParams

PARAMETER_MODELS = tuple(
    model for model in vars(parameters).values()
    if isinstance(model, type) and issubclass(model, Parameters) and model is not Parameters
)


@pytest.mark.parametrize("model", PARAMETER_MODELS, ids=lambda model: model.__name__)
def test_parameter_field_documentation_includes_valid_examples(model):
    schema = model.model_json_schema()
    for name, field in model.model_fields.items():
        documented = schema["properties"][name]
        assert documented.get("description", "").strip(), (model.__name__, name)
        annotation = field.annotation
        if field.metadata:
            annotation = Annotated[annotation, *field.metadata]
        adapter = TypeAdapter(annotation)
        for example in documented.get("examples", []):
            adapter.validate_python(example, strict=True)


@pytest.mark.parametrize("model", [VideoParams, AudioParams, ImageParams])
def test_documented_conditional_defaults_match_normalized_parameters(model):
    checked = 0
    for field, schema in model.model_json_schema()["properties"].items():
        for discriminator in ("codec", "format", "mode", "container"):
            for value, expected in schema.get(f"x-default-by-{discriminator}", {}).items():
                if expected is not None:
                    assert "default" not in schema or schema["default"] is not None
                context = {discriminator: value}
                if model is VideoParams and discriminator == "container" and value == "webm":
                    context["codec"] = "vp9"
                actual = model.model_validate(context).model_dump()[field]
                assert actual == expected, (model.__name__, field, discriminator, value)
                checked += 1
    assert checked, model.__name__


def test_implicit_frame_rate_mode_is_documented_without_changing_numeric_input():
    mode = FrameRate.model_json_schema()["properties"]["mode"]
    assert mode["x-default-by-value"] == {"omitted": "source", "provided": "constant"}
    assert FrameRate().mode == "source"
    assert FrameRate.model_validate({"value": 30}).model_dump() == {
        "mode": "constant", "value": "30/1",
    }


def test_discovered_defaults_and_examples_are_accepted_by_each_operation():
    discovered = capabilities()
    for operation, (_, _, model, _) in OPERATIONS.items():
        contract = discovered[operation]
        schema = contract["params_schema"]
        assert contract["description"] == schema["description"]
        assert schema.get("examples"), operation
        assert contract["default_params"] == model().model_dump()
        assert model.model_validate(contract["default_params"]).model_dump() == (
            contract["default_params"]
        )
        for example in schema.get("examples", []):
            model.model_validate(example)

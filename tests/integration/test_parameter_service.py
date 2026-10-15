import hashlib
import io
import json

import httpx
from PIL import Image
from test_mcp_service import finished, remote_session, serve

from media_service.models import OPERATIONS, JobCreate
from media_service.service import Service

MEDIA_TOOLS = (
    ("probe_media", "media.probe", "media", "report", "sections"),
    ("transcode_video", "video.transcode", "video", "video", "frame_rate"),
    ("transcode_audio", "audio.transcode", "audio", "audio", "sample_rate"),
    ("extract_audio", "video.extract_audio", "video", "audio", "sample_rate"),
    ("extract_frames", "video.extract_frames", "video", "frames", "sampling"),
    ("resize_image", "image.resize", "image", "image", "quality"),
)


def definition(operation, source, params, input_port, output_port):
    return {
        "inputs": {"main": source},
        "steps": [{"id": "process", "operation": operation,
                   "inputs": {input_port: {"source": "main"}}, "params": params}],
        "outputs": {"result": {"step": "process", "output": output_port}},
    }


def assert_safe_parameters(value, private_paths):
    if isinstance(value, dict):
        assert not {"source", "url", "headers", "file_id", "storage_id", "key", "filename"} & value.keys()
        for child in value.values():
            assert_safe_parameters(child, private_paths)
    elif isinstance(value, list):
        for child in value:
            assert_safe_parameters(child, private_paths)
    elif isinstance(value, str):
        assert all(str(path) not in value for path in private_paths)


def resolved_schema(schema, document):
    if isinstance(schema, list):
        return [resolved_schema(item, document) for item in schema]
    if not isinstance(schema, dict):
        # discriminator.mapping 使用引用字符串，需要消除各协议不同的引用前缀。
        if isinstance(schema, str) and schema.startswith("#/"):
            return schema.rsplit("/", 1)[-1]
        return schema
    if "$ref" in schema:
        referenced = document
        assert schema["$ref"].startswith("#/"), schema["$ref"]
        for part in schema["$ref"][2:].split("/"):
            referenced = referenced[part.replace("~1", "/").replace("~0", "~")]
        schema = referenced | {key: value for key, value in schema.items() if key != "$ref"}
    return {key: resolved_schema(value, document) for key, value in schema.items() if key != "$defs"}


def parameter_contract(schema):
    return {key: value for key, value in schema.items() if key not in {"title", "description", "default"}}


def operation_parameter_schemas(definition):
    step = definition["properties"]["steps"]["items"]
    assert "params" not in step.get("required", [])
    branches = step["oneOf"]
    result = {}
    for branch in branches:
        assert "params" not in branch.get("required", [])
        properties = branch["properties"]
        result[properties["operation"]["const"]] = properties["params"]
    assert len(branches) == len(result) == 6
    assert set(result) == set(OPERATIONS)
    return result


async def test_http_and_remote_mcp_discovery_share_parameter_documentation(settings):
    settings.service_token = None
    service = Service(settings)
    async with serve(service) as base, remote_session(base, {}) as session:
        async with httpx.AsyncClient(base_url=base) as client:
            response = await client.get("/openapi.json")
            assert response.status_code == 200
            openapi = response.json()
            response = await client.get("/v1/capabilities")
            assert response.status_code == 200
            capabilities = response.json()
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        mcp_capabilities = await session.call_tool("list_capabilities", {})
        assert not mcp_capabilities.is_error
        assert mcp_capabilities.structured_content["operations"] == capabilities["operations"]
        body = openapi["paths"]["/v1/jobs"]["post"]["requestBody"]["content"]["application/json"]
        http_operations = operation_parameter_schemas(resolved_schema(body["schema"], openapi))
        upload_body = openapi["paths"]["/v1/jobs/upload"]["post"]["requestBody"]["content"]
        upload_job = upload_body["multipart/form-data"]["schema"]["properties"]["job"]
        assert upload_job["contentMediaType"] == "application/json"
        upload_definition = resolved_schema(upload_job["contentSchema"], openapi)
        upload_operations = operation_parameter_schemas(upload_definition)
        inputs = upload_definition["properties"]["inputs"]["additionalProperties"]["anyOf"]
        assert any(item.get("properties", {}).get("kind", {}).get("const") == "upload" for item in inputs)
        assert upload_job["examples"]
        for encoded in upload_job["examples"]:
            example = json.loads(encoded)
            upload_count = 0
            for name, source in example["inputs"].items():
                if source["kind"] == "upload":
                    assert source["part"] == name
                    example["inputs"][name] = {"kind": "uploaded_file", "file_id": "file_" + "0" * 32}
                    upload_count += 1
            assert upload_count
            JobCreate.model_validate(example)
        submit = tools["submit_job"].input_schema
        mcp_operations = operation_parameter_schemas(
            resolved_schema(submit["properties"]["definition"], submit)
        )
        for name, operation, _, _, _ in MEDIA_TOOLS:
            operation += ".v1"
            contract = capabilities["operations"][operation]
            documented = contract["params_schema"]
            expected = parameter_contract(resolved_schema(documented, documented))
            tool = tools[name]
            assert contract["description"] in tool.description
            assert "params" not in tool.input_schema["required"]
            actual = resolved_schema(tool.input_schema["properties"]["params"], tool.input_schema)
            assert parameter_contract(actual) == expected
            assert parameter_contract(http_operations[operation]) == expected
            assert parameter_contract(upload_operations[operation]) == expected
            assert parameter_contract(mcp_operations[operation]) == expected
            assert contract["default_params"] == OPERATIONS[operation][2]().model_dump()
        assert await service.db.one("SELECT COUNT(*) AS count FROM jobs") == {"count": 0}


async def test_http_upload_and_mcp_resize_share_parameters_and_download(settings):
    settings.service_token = None
    service = Service(settings)
    fixture = settings.storages["input"].root / "image_jpeg_portrait_odd.jpg"
    data = fixture.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    params = {"width": 128, "height": 128, "fit": "pad", "background": "#224466",
              "format": "png", "compression_level": 9, "resampling": "nearest"}
    private_paths = (settings.state_dir, settings.work_dir, fixture)
    async with serve(service) as base, remote_session(base, {}) as session:
        async with httpx.AsyncClient(base_url=base) as client:
            uploaded = await client.post("/v1/files", content=data, headers={
                "Content-Type": "application/octet-stream", "Idempotency-Key": "parameters-image-upload",
                "X-File-Sha256": digest, "X-File-Size": str(len(data)),
            })
            assert uploaded.status_code == 201, uploaded.text
            source = {"kind": "uploaded_file", "file_id": uploaded.json()["file_id"]}
            http_accepted = await client.post("/v1/jobs", headers={
                "Idempotency-Key": "parameters-http-image",
            }, json=definition("image.resize.v1", source, params, "image", "image"))
            assert http_accepted.status_code == 202, http_accepted.text
            mcp_accepted = await session.call_tool("resize_image", {
                "source": source, "params": params,
                "idempotency_key": "parameters-mcp-image",
            })
            assert not mcp_accepted.is_error, mcp_accepted
            jobs = [await finished(session, job_id) for job_id in (
                http_accepted.json()["job_id"], mcp_accepted.structured_content["job_id"],
            )]
            assert all(job["status"] == "succeeded" for job in jobs), jobs
            effective = [job["steps"][0]["effective_params"] for job in jobs]
            assert effective[0] == effective[1]
            assert effective[0]["format"] == "png" and effective[0]["compression_level"] == 9
            assert effective[0]["width"] == 128 and effective[0]["height"] == 128
            decoded, artifact_parameters = [], []
            for job in jobs:
                public = await client.get("/v1/jobs/" + job["job_id"])
                assert public.status_code == 200
                assert public.json() == job
                artifact_id = job["outputs"]["result"]["artifact_id"]
                metadata = await client.get("/v1/artifacts/" + artifact_id)
                assert metadata.status_code == 200
                artifact = metadata.json()
                artifact_parameters.append(artifact["media"]["effective_params"])
                downloaded = await client.get(artifact["access"]["http"]["content"])
                assert downloaded.status_code == 200
                assert len(downloaded.content) == artifact["size_bytes"]
                assert hashlib.sha256(downloaded.content).hexdigest() == artifact["sha256"]
                with Image.open(io.BytesIO(downloaded.content)) as result:
                    assert result.format == "PNG" and result.size == (128, 128)
                    assert result.convert("RGB").getpixel((0, 0)) == (34, 68, 102)
                    decoded.append(result.convert("RGB").tobytes())
                assert_safe_parameters(job["steps"][0]["effective_params"], private_paths)
                assert_safe_parameters(artifact["media"]["effective_params"], private_paths)
                assert source["file_id"] not in json.dumps(job)
                assert str(fixture) not in metadata.text + public.text
            assert artifact_parameters[0] == artifact_parameters[1]
            assert decoded[0] == decoded[1]
    assert hashlib.sha256(fixture.read_bytes()).hexdigest() == digest


async def test_all_media_tools_discover_and_reject_the_same_invalid_parameters(settings):
    settings.service_token = None
    service = Service(settings)
    invalid = (
        {"sections": ["format"], "count_frames": True},
        {"codec": "vp9", "container": "mp4"},
        {"codec": "pcm", "rate_control": {"mode": "bitrate", "bitrate_bps": 128000}},
        {"mode": "copy", "sample_rate": 16000},
        {"sampling": {"mode": "timestamps", "timestamps_ms": [0]},
         "image": {"format": "png", "quality": 80}},
        {"format": "png", "quality": 80},
    )
    source = {"kind": "shared_file", "storage_id": "input", "key": "image_jpeg_portrait_odd.jpg"}
    async with serve(service) as base, remote_session(base, {}) as session:
        async with httpx.AsyncClient(base_url=base) as client:
            listed = await session.list_tools()
            tools = {tool.name: tool for tool in listed.tools}
            assert len(tools) == 19
            http_capabilities = await client.get("/v1/capabilities")
            mcp_capabilities = await session.call_tool("list_capabilities", {})
            assert http_capabilities.status_code == 200 and not mcp_capabilities.is_error
            assert http_capabilities.json()["media_parameters"] == (
                mcp_capabilities.structured_content["media_parameters"]
            )
            cases = list(zip(MEDIA_TOOLS, invalid, strict=True))
            cases.extend((MEDIA_TOOLS[index], params) for index, params in (
                (1, {"audio": {"channel_layout": []}}), (2, {"codec": []}),
                (2, {"channel_layout": {}}), (5, {"format": []}),
            ))
            for index, ((name, operation, input_port, output_port, new_field), params) in enumerate(cases):
                schema = tools[name].input_schema
                assert "operation_version" not in schema["properties"]
                assert "params" not in schema["required"]
                assert f'"{new_field}"' in json.dumps(schema)
                assert operation + ".v1" in http_capabilities.json()["operations"]
                assert len(http_capabilities.json()["operations"]) == 6
                http_rejected = await client.post("/v1/jobs", headers={
                    "Idempotency-Key": f"parameters-invalid-http-{index}",
                }, json=definition(operation + ".v1", source, params, input_port, output_port))
                mcp_rejected = await session.call_tool(name, {
                    "source": source, "params": params,
                    "idempotency_key": f"parameters-invalid-mcp-{index}",
                })
                assert http_rejected.status_code == 422, http_rejected.text
                assert mcp_rejected.is_error, mcp_rejected
                assert http_rejected.json()["error"]["code"] == (
                    mcp_rejected.structured_content["error"]["code"]
                ) == "INVALID_REQUEST"


async def test_multipart_parameters_and_mcp_omitted_params_and_single_dimension(settings):
    settings.service_token = None
    service = Service(settings)
    data = (settings.storages["input"].root / "image_jpeg_portrait_odd.jpg").read_bytes()
    async with serve(service) as base, remote_session(base, {}) as session:
        async with httpx.AsyncClient(base_url=base) as client:
            job = definition("image.resize.v1", {"kind": "upload", "part": "main"},
                             {"width": 96, "format": "webp", "quality": 73}, "image", "image")
            accepted = await client.post("/v1/jobs/upload", headers={
                "Idempotency-Key": "parameters-multipart-image",
            }, files=[("job", (None, json.dumps(job), "application/json")),
                      ("main", ("input.jpg", data, "application/octet-stream"))])
            assert accepted.status_code == 202, accepted.text
            result = await finished(session, accepted.json()["job_id"])
            assert result["status"] == "succeeded", result
            assert result["steps"][0]["effective_params"]["quality"] == 73
            artifact = (await client.get(
                "/v1/artifacts/" + result["outputs"]["result"]["artifact_id"]
            )).json()
            content = await client.get(artifact["access"]["http"]["content"])
            assert content.status_code == 200
            with Image.open(io.BytesIO(content.content)) as image:
                assert image.format == "WEBP" and image.width == 96
            source = {"kind": "shared_file", "storage_id": "input", "key": "image_jpeg_portrait_odd.jpg"}
            default_probe = await session.call_tool("probe_media", {
                "source": source, "idempotency_key": "default-probe",
            })
            assert not default_probe.is_error
            default_job = await finished(session, default_probe.structured_content["job_id"])
            assert default_job["status"] == "succeeded", default_job
            assert default_job["steps"][0]["effective_params"]["sections"] == ["format", "streams"]
            metadata = (await client.get(
                "/v1/artifacts/" + default_job["outputs"]["result"]["artifact_id"]
            )).json()
            report = await client.get(metadata["access"]["http"]["content"])
            assert report.json()["type"] == "image"
            single_dimension = await session.call_tool("resize_image", {
                "source": source, "params": {"width": 96}, "idempotency_key": "v1-no-height",
            })
            assert not single_dimension.is_error
            resized = await finished(session, single_dimension.structured_content["job_id"])
            assert resized["status"] == "succeeded"


async def test_http_and_mcp_expose_frame_members_with_media_and_partial_errors(settings):
    settings.service_token = None
    service = Service(settings)
    source = {"kind": "shared_file", "storage_id": "input", "key": "video_h264_640x360.mkv"}
    async with serve(service) as base, remote_session(base, {}) as session:
        accepted = await session.call_tool("extract_frames", {
            "source": source, "idempotency_key": "frame-members-metadata",
            "params": {"sampling": {"mode": "timestamps", "timestamps_ms": [0, 100000]},
                       "image": {"width": 96, "format": "png"}},
        })
        assert not accepted.is_error
        job = await finished(session, accepted.structured_content["job_id"])
        assert job["status"] == "partial"
        artifact_id = job["outputs"]["result"]["artifact_id"]
        members = await session.call_tool("list_artifact_members", {"artifact_id": artifact_id})
        assert not members.is_error
        async with httpx.AsyncClient(base_url=base) as client:
            response = await client.get(f"/v1/artifacts/{artifact_id}/members")
            assert response.status_code == 200
            assert response.json() == members.structured_content
        first, failed = response.json()["items"]
        assert (first["media"]["width"], first["media"]["format"]) == (96, "png")
        assert first["sha256"] and first["error"] is None
        assert failed["media"] is None and failed["error"]["code"] == "TIMESTAMP_OUT_OF_RANGE"

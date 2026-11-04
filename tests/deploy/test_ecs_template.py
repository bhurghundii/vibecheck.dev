"""The ECS Fargate CloudFormation template, checked against its parsed contents.

Nothing here deploys to AWS: the template is read with PyYAML and its resources, parameters
and outputs are asserted on directly. These are pure tests, so no `vibecheck_env` and no
database.

Two pieces of machinery make that possible. `_Loader` teaches PyYAML's safe loader the
CloudFormation short-form tags (`!Ref`, `!Sub`, `!If`, ...) by turning each into the long form
CloudFormation itself expands it to, so the tests see one shape whichever form the template
uses. `_render()` then evaluates the template's conditions for a given set of parameter values:
it drops resources whose `Condition` is false, picks the live branch of every `Fn::If` and
removes `AWS::NoValue`. Requirements that depend on optional parameters (the base URL, the
status token, the listeners) are asserted on the rendered template for each case rather than
on the raw `Fn::If`, so a branch swapped by mistake fails a test instead of passing it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

TEMPLATE_PATH = Path(__file__).resolve().parents[2] / "deploy" / "aws-ecs" / "vibecheck.yaml"

REQUIRED = {"ImageUri": "ghcr.io/example/vibecheck:latest", "BedrockModelId": "us.anthropic.x-v1:0"}
CERT = "arn:aws:acm:eu-west-2:123456789012:certificate/abc"
GITHUB_STATUS_ARN = "arn:aws:secretsmanager:eu-west-2:123456789012:secret:status-AbCdEf"

_NO_VALUE = object()


class _Loader(yaml.SafeLoader):
    pass


def _construct_tag(loader: _Loader, suffix: str, node: yaml.Node) -> dict[str, Any]:
    if isinstance(node, yaml.ScalarNode):
        value: Any = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    if suffix == "Ref":
        return {"Ref": value}
    if suffix == "Condition":
        return {"Condition": value}
    if suffix == "GetAtt" and isinstance(value, str):
        value = value.split(".", 1)
    return {f"Fn::{suffix}": value}


_Loader.add_multi_constructor("!", _construct_tag)


def _template() -> dict[str, Any]:
    return yaml.load(TEMPLATE_PATH.read_text(), Loader=_Loader)


def _params(template: dict[str, Any], given: dict[str, str]) -> dict[str, str]:
    values = {
        name: spec["Default"] for name, spec in template["Parameters"].items() if "Default" in spec
    }
    values.update(given)
    return values


def _value(node: Any, params: dict[str, str], conditions: dict[str, Any]) -> Any:
    """Evaluate the intrinsic functions a condition is allowed to use."""
    if isinstance(node, dict) and len(node) == 1:
        ((fn, arg),) = node.items()
        if fn == "Ref":
            return params[arg]
        if fn == "Condition":
            return _condition(arg, params, conditions)
        if fn == "Fn::Equals":
            return _value(arg[0], params, conditions) == _value(arg[1], params, conditions)
        if fn == "Fn::Not":
            return not _value(arg[0], params, conditions)
        if fn == "Fn::And":
            return all(_value(a, params, conditions) for a in arg)
        if fn == "Fn::Or":
            return any(_value(a, params, conditions) for a in arg)
        if fn == "Fn::Select":
            return _value(arg[1], params, conditions)[int(arg[0])]
        if fn == "Fn::Split":
            return _value(arg[1], params, conditions).split(arg[0])
    return node


def _condition(name: str, params: dict[str, str], conditions: dict[str, Any]) -> bool:
    return bool(_value(conditions[name], params, conditions))


def _resolve(node: Any, truth: dict[str, bool]) -> Any:
    if node == {"Ref": "AWS::NoValue"}:
        return _NO_VALUE
    if isinstance(node, dict):
        if set(node) == {"Fn::If"}:
            name, yes, no = node["Fn::If"]
            return _resolve(yes if truth[name] else no, truth)
        out = {}
        for key, value in node.items():
            resolved = _resolve(value, truth)
            if resolved is not _NO_VALUE:
                out[key] = resolved
        return out
    if isinstance(node, list):
        items = [_resolve(item, truth) for item in node]
        return [item for item in items if item is not _NO_VALUE]
    return node


def _render(**given: str) -> dict[str, Any]:
    """The template as CloudFormation would create it for these parameter values."""
    template = _template()
    params = _params(template, {**REQUIRED, **given})
    conditions = template.get("Conditions", {})
    truth = {name: _condition(name, params, conditions) for name in conditions}
    resources = {
        name: res
        for name, res in template["Resources"].items()
        if "Condition" not in res or truth[res["Condition"]]
    }
    rendered = {**template, "Resources": resources}
    return _resolve(rendered, truth)


def _of_type(rendered: dict[str, Any], kind: str) -> dict[str, dict[str, Any]]:
    return {n: r for n, r in rendered["Resources"].items() if r["Type"] == kind}


def _one(rendered: dict[str, Any], kind: str) -> tuple[str, dict[str, Any]]:
    found = _of_type(rendered, kind)
    assert len(found) == 1, f"expected one {kind}, found {sorted(found)}"
    return next(iter(found.items()))


def _container(rendered: dict[str, Any]) -> dict[str, Any]:
    _, task = _one(rendered, "AWS::ECS::TaskDefinition")
    (container,) = task["Properties"]["ContainerDefinitions"]
    return container


def _environment(rendered: dict[str, Any]) -> dict[str, Any]:
    return {e["Name"]: e["Value"] for e in _container(rendered).get("Environment", [])}


def _secrets(rendered: dict[str, Any]) -> dict[str, Any]:
    return {s["Name"]: s["ValueFrom"] for s in _container(rendered).get("Secrets", [])}


def _statements(role: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        statement
        for policy in role["Properties"].get("Policies", [])
        for statement in policy["PolicyDocument"]["Statement"]
    ]


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def _role(rendered: dict[str, Any], task_property: str) -> dict[str, Any]:
    _, task = _one(rendered, "AWS::ECS::TaskDefinition")
    ref = task["Properties"][task_property]
    name = ref["Fn::GetAtt"][0]
    return rendered["Resources"][name]


def _base_url_in_env_and_output(rendered: dict[str, Any]) -> tuple[Any, Any]:
    return _environment(rendered)["VIBECHECK_BASE_URL"], rendered["Outputs"]["BaseUrl"]["Value"]


def _ingress(group: dict[str, Any]) -> list[tuple[int, int, str]]:
    rules = []
    for rule in group["Properties"].get("SecurityGroupIngress", []):
        source = rule.get("CidrIp") or rule["SourceSecurityGroupId"]["Fn::GetAtt"][0]
        rules.append((rule["FromPort"], rule["ToPort"], source))
    return sorted(rules, key=str)


def _listeners(rendered: dict[str, Any]) -> dict[int, dict[str, Any]]:
    found = _of_type(rendered, "AWS::ElasticLoadBalancingV2::Listener").values()
    return {listener["Properties"]["Port"]: listener["Properties"] for listener in found}


def test_template_is_one_cloudformation_document() -> None:
    documents = list(yaml.load_all(TEMPLATE_PATH.read_text(), Loader=_Loader))
    assert len(documents) == 1
    assert documents[0]["AWSTemplateFormatVersion"] == "2010-09-09"
    assert documents[0]["Resources"]


def test_parameters_match_the_contract() -> None:
    params = _template()["Parameters"]
    assert set(params) == {
        "VpcId",
        "LoadBalancerSubnetIds",
        "ServiceSubnetIds",
        "DatabaseSubnetIds",
        "AssignPublicIp",
        "LoadBalancerScheme",
        "ImageUri",
        "BedrockModelId",
        "CertificateArn",
        "PublicBaseUrl",
        "GitHubStatusTokenSecretArn",
        "AllowedRepos",
    }
    assert params["AssignPublicIp"]["AllowedValues"] == ["ENABLED", "DISABLED"]
    assert params["AssignPublicIp"]["Default"] == "DISABLED"
    assert params["LoadBalancerScheme"]["Default"] == "internet-facing"
    for required in ("ImageUri", "BedrockModelId"):
        assert "Default" not in params[required]
    for optional in (
        "CertificateArn",
        "PublicBaseUrl",
        "GitHubStatusTokenSecretArn",
        "AllowedRepos",
    ):
        assert params[optional]["Default"] == ""


def test_service_runs_one_fargate_task_on_8000_with_30_day_logs() -> None:
    rendered = _render()
    _, service = _one(rendered, "AWS::ECS::Service")
    assert service["Properties"]["LaunchType"] == "FARGATE"
    assert service["Properties"]["DesiredCount"] == 1
    _, task = _one(rendered, "AWS::ECS::TaskDefinition")
    assert task["Properties"]["RequiresCompatibilities"] == ["FARGATE"]
    container = _container(rendered)
    assert [m["ContainerPort"] for m in container["PortMappings"]] == [8000]
    assert service["Properties"]["LoadBalancers"][0]["ContainerPort"] == 8000

    log_group_name, log_group = _one(rendered, "AWS::Logs::LogGroup")
    assert log_group["Properties"]["RetentionInDays"] == 30
    log_options = container["LogConfiguration"]["Options"]
    assert container["LogConfiguration"]["LogDriver"] == "awslogs"
    assert log_options["awslogs-group"] == {"Ref": log_group_name}


def test_container_environment_selects_bedrock() -> None:
    env = _environment(_render())
    assert env["FAKE_GRADER"] == "false"
    assert env["MODEL_PROVIDER"] == "bedrock"
    assert env["BEDROCK_MODEL_ID"] == {"Ref": "BedrockModelId"}
    assert env["AWS_REGION"] == {"Ref": "AWS::Region"}
    assert "VIBECHECK_BASE_URL" in env


def test_base_url_prefers_public_then_https_then_http() -> None:
    public = _render(PublicBaseUrl="https://vibecheck.example.com", CertificateArn=CERT)
    assert _base_url_in_env_and_output(public) == ({"Ref": "PublicBaseUrl"},) * 2

    https = _render(CertificateArn=CERT)
    assert (
        _base_url_in_env_and_output(https) == ({"Fn::Sub": "https://${LoadBalancer.DNSName}"},) * 2
    )

    http = _render()
    assert _base_url_in_env_and_output(http) == ({"Fn::Sub": "http://${LoadBalancer.DNSName}"},) * 2


def test_database_url_and_token_only_reach_container_as_secrets() -> None:
    template = _template()
    rendered = _render()
    env = _environment(rendered)
    secrets = _secrets(rendered)
    for name in ("DATABASE_URL", "VIBECHECK_TOKEN"):
        assert name not in env
        (ref,) = secrets[name].values()
        assert rendered["Resources"][ref]["Type"] == "AWS::SecretsManager::Secret"
    assert not {"DatabaseUrl", "VibecheckToken", "DatabasePassword"} & set(template["Parameters"])
    for output in template["Outputs"].values():
        text = str(output["Value"])
        assert "resolve:secretsmanager" not in text
        assert "SecretString" not in text


def test_vibecheck_token_is_generated_with_at_least_32_chars() -> None:
    rendered = _render()
    ref = _secrets(rendered)["VIBECHECK_TOKEN"]["Ref"]
    props = rendered["Resources"][ref]["Properties"]
    assert "SecretString" not in props
    generated = props["GenerateSecretString"]
    assert "GenerateStringKey" not in generated, (
        "the token must be the whole secret, not a JSON key"
    )
    assert generated["PasswordLength"] >= 32


def test_github_status_token_present_only_with_secret_arn() -> None:
    with_arn = _secrets(_render(GitHubStatusTokenSecretArn=GITHUB_STATUS_ARN))
    assert with_arn["GITHUB_STATUS_TOKEN"] == {"Ref": "GitHubStatusTokenSecretArn"}

    without = _render()
    assert "GITHUB_STATUS_TOKEN" not in _secrets(without)
    assert "GITHUB_STATUS_TOKEN" not in _environment(without)
    _, task = _one(without, "AWS::ECS::TaskDefinition")
    assert "GITHUB_STATUS_TOKEN" not in str(task)


def test_template_never_mentions_static_credentials() -> None:
    text = TEMPLATE_PATH.read_text()
    for forbidden in ("MODEL_API_KEY", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        assert forbidden not in text


def test_task_role_only_invokes_the_configured_model() -> None:
    for model_id in ("anthropic.claude-x-v1:0", "us.anthropic.claude-x-v1:0"):
        rendered = _render(BedrockModelId=model_id)
        role = _role(rendered, "TaskRoleArn")
        assert "ManagedPolicyArns" not in role["Properties"]
        statements = _statements(role)
        assert statements
        for statement in statements:
            assert statement["Effect"] == "Allow"
            assert _as_list(statement["Action"]) == ["bedrock:InvokeModel"]
            assert "*" not in _as_list(statement["Resource"])

        direct = [s for s in statements if "Condition" not in s]
        through_profile = [s for s in statements if "Condition" in s]
        assert [str(r) for s in direct for r in _as_list(s["Resource"])] == [
            str(
                {
                    "Fn::Sub": "arn:${AWS::Partition}:bedrock:${AWS::Region}::foundation-model/${BedrockModelId}"
                }
            ),
            str(
                {
                    "Fn::Sub": "arn:${AWS::Partition}:bedrock:${AWS::Region}:${AWS::AccountId}:inference-profile/${BedrockModelId}"
                }
            ),
        ]
        (profile,) = through_profile
        assert _as_list(profile["Resource"]) == [
            {"Fn::Sub": "arn:${AWS::Partition}:bedrock:*::foundation-model/*"}
        ]
        assert profile["Condition"] == {
            "StringEquals": {
                "bedrock:InferenceProfileArn": {
                    "Fn::Sub": "arn:${AWS::Partition}:bedrock:${AWS::Region}:${AWS::AccountId}:inference-profile/${BedrockModelId}"
                }
            }
        }


def test_task_role_scopes_to_a_model_given_as_an_arn() -> None:
    arn = "arn:aws:bedrock:eu-west-2:123456789012:inference-profile/eu.anthropic.claude-x-v1:0"
    statements = _statements(_role(_render(BedrockModelId=arn), "TaskRoleArn"))
    direct = [s for s in statements if "Condition" not in s]
    assert [r for s in direct for r in _as_list(s["Resource"])] == [{"Ref": "BedrockModelId"}]
    (profile,) = [s for s in statements if "Condition" in s]
    assert profile["Condition"]["StringEquals"]["bedrock:InferenceProfileArn"] == {
        "Ref": "BedrockModelId"
    }


def test_execution_role_reads_only_its_own_secrets() -> None:
    for given, extra in (
        ({}, []),
        ({"GitHubStatusTokenSecretArn": GITHUB_STATUS_ARN}, ["GitHubStatusTokenSecretArn"]),
    ):
        rendered = _render(**given)
        role = _role(rendered, "ExecutionRoleArn")
        assert role["Properties"]["ManagedPolicyArns"] == [
            {
                "Fn::Sub": "arn:${AWS::Partition}:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
            }
        ]
        (statement,) = _statements(role)
        assert _as_list(statement["Action"]) == ["secretsmanager:GetSecretValue"]
        refs = sorted(r["Ref"] for r in _as_list(statement["Resource"]))
        own = sorted(v["Ref"] for k, v in _secrets(rendered).items() if k != "GITHUB_STATUS_TOKEN")
        assert refs == sorted(own + extra)
        for ref in own:
            assert rendered["Resources"][ref]["Type"] == "AWS::SecretsManager::Secret"


def test_database_is_private_encrypted_postgres_16_kept_on_delete() -> None:
    rendered = _render()
    _, db = _one(rendered, "AWS::RDS::DBInstance")
    props = db["Properties"]
    assert props["Engine"] == "postgres"
    assert str(props["EngineVersion"]).split(".")[0] == "16"
    assert props["StorageEncrypted"] is True
    assert props["PubliclyAccessible"] is False
    assert db["DeletionPolicy"] == "Snapshot"
    assert db["UpdateReplacePolicy"] == "Snapshot"
    subnet_group = rendered["Resources"][props["DBSubnetGroupName"]["Ref"]]
    assert subnet_group["Properties"]["SubnetIds"] == {"Ref": "DatabaseSubnetIds"}


def test_database_url_requires_tls() -> None:
    rendered = _render()
    ref = _secrets(rendered)["DATABASE_URL"]["Ref"]
    delimiter, parts = rendered["Resources"][ref]["Properties"]["SecretString"]["Fn::Join"]
    assert delimiter == ""
    url = "".join(part["Fn::Sub"] if isinstance(part, dict) else part for part in parts)
    assert url.startswith("postgresql://")
    assert re.search(r"\?(.*&)?sslmode=require(&|$)", url)


def test_security_groups_allow_only_the_intended_hops() -> None:
    rendered = _render()
    resources = rendered["Resources"]
    _, service = _one(rendered, "AWS::ECS::Service")
    (service_sg,) = service["Properties"]["NetworkConfiguration"]["AwsvpcConfiguration"][
        "SecurityGroups"
    ]
    service_sg_name = service_sg["Fn::GetAtt"][0]
    _, lb = _one(rendered, "AWS::ElasticLoadBalancingV2::LoadBalancer")
    (lb_sg,) = lb["Properties"]["SecurityGroups"]
    lb_sg_name = lb_sg["Fn::GetAtt"][0]
    _, db = _one(rendered, "AWS::RDS::DBInstance")
    (db_sg,) = db["Properties"]["VPCSecurityGroups"]
    db_sg_name = db_sg["Fn::GetAtt"][0]

    assert _ingress(resources[db_sg_name]) == [(5432, 5432, service_sg_name)]
    assert _ingress(resources[service_sg_name]) == [(8000, 8000, lb_sg_name)]
    assert _ingress(resources[lb_sg_name]) == [(443, 443, "0.0.0.0/0"), (80, 80, "0.0.0.0/0")]
    assert not _of_type(rendered, "AWS::EC2::SecurityGroupIngress")


def test_target_group_checks_healthz() -> None:
    _, group = _one(_render(), "AWS::ElasticLoadBalancingV2::TargetGroup")
    assert group["Properties"]["HealthCheckPath"] == "/healthz"
    assert group["Properties"]["Port"] == 8000
    assert group["Properties"]["TargetType"] == "ip"


def test_listeners_follow_certificate() -> None:
    with_cert = _listeners(_render(CertificateArn=CERT))
    assert set(with_cert) == {80, 443}
    assert with_cert[443]["Protocol"] == "HTTPS"
    assert with_cert[443]["Certificates"] == [{"CertificateArn": {"Ref": "CertificateArn"}}]
    assert with_cert[443]["DefaultActions"][0]["Type"] == "forward"
    (redirect,) = with_cert[80]["DefaultActions"]
    assert redirect["Type"] == "redirect"
    assert redirect["RedirectConfig"]["Protocol"] == "HTTPS"
    assert redirect["RedirectConfig"]["Port"] == "443"

    without = _listeners(_render())
    assert set(without) == {80}
    assert without[80]["Protocol"] == "HTTP"
    assert without[80]["DefaultActions"][0]["Type"] == "forward"


def test_outputs_are_exactly_the_contract() -> None:
    rendered = _render()
    outputs = rendered["Outputs"]
    assert set(outputs) == {
        "BaseUrl",
        "VibecheckTokenSecretArn",
        "LogGroupName",
        "ClusterName",
        "ServiceName",
    }
    token_ref = _secrets(rendered)["VIBECHECK_TOKEN"]
    assert outputs["VibecheckTokenSecretArn"]["Value"] == token_ref
    log_group_name, _ = _one(rendered, "AWS::Logs::LogGroup")
    assert outputs["LogGroupName"]["Value"] == {"Ref": log_group_name}
    cluster_name, _ = _one(rendered, "AWS::ECS::Cluster")
    assert outputs["ClusterName"]["Value"] == {"Ref": cluster_name}
    service_name, _ = _one(rendered, "AWS::ECS::Service")
    assert outputs["ServiceName"]["Value"] == {"Fn::GetAtt": [service_name, "Name"]}


def test_allowed_repos_env_present_only_when_set() -> None:
    assert "VIBECHECK_ALLOWED_REPOS" not in _environment(_render())
    env = _environment(_render(AllowedRepos="octo/a,octo/b"))
    assert env["VIBECHECK_ALLOWED_REPOS"] == {"Ref": "AllowedRepos"}


def test_generated_passwords_are_url_safe() -> None:
    # The database password is spliced into a postgresql:// URL, where '@', '/', ':' and '?'
    # would end the userinfo early. Excluding punctuation keeps it to [A-Za-z0-9].
    rendered = _render()
    secrets = _of_type(rendered, "AWS::SecretsManager::Secret")
    generated = [
        s["Properties"]["GenerateSecretString"]
        for s in secrets.values()
        if "GenerateSecretString" in s["Properties"]
    ]
    assert len(generated) == 2
    for spec in generated:
        assert spec["ExcludePunctuation"] is True

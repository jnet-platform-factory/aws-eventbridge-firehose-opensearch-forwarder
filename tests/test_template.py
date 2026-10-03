"""The template's conditional structure: CreateErrorAlerting in particular.

CloudFormation only finds a dangling reference at deploy time -- a Ref to a
resource whose Condition is false fails the changeset, in the one tenant's
account that set the parameter. So the template is resolved here the way
CloudFormation resolves it (parameters -> conditions -> resources, Fn::If
branches picked, AWS::NoValue dropped) across a matrix of parameter sets, and
every surviving reference must land on something that survived too.

No AWS and no SAM needed. The template's short tags (!Ref, !Sub, !If ...) are
read into their long forms by a loader below; nothing here executes them.
"""
import itertools
import re
from pathlib import Path

import pytest
import yaml

TEMPLATE = Path(__file__).resolve().parent.parent / "template.yaml"

GATE = "CreateErrorAlertingResources"

# The error-alert digest: everything CreateErrorAlerting=false must remove.
ALERTING_RESOURCES = {
    "ErrorAlertsRule",
    "ErrorAlertsQueue",
    "ErrorAlertsDLQ",
    "ErrorAlertsQueuePolicy",
    "AlertsSnsTopic",
    "AlertsSnsTopicPolicy",
    "AlertsEmailSubscription",
    "AlertDigestFunction",
    "AlertDigestLogGroup",
    # Watches ErrorAlertsDLQ, so it cannot outlive it.
    "ErrorAlertsDlqDepthAlarm",
}
ALERTING_OUTPUTS = {"AlertsSnsTopicArn", "ErrorAlertsQueueArn"}

# Values for the parameters that have no default. Generic on purpose: this
# repository is public. None of them feeds a condition.
REQUIRED = {
    "OpenSearchEndpoint": "search-example-xxxx.us-east-1.es.amazonaws.com",
    "OpenSearchResourceArn": "arn:aws:es:us-east-1:111122223333:domain/example/*",
    "OpenSearchIndex": "example-events",
}

PSEUDO = {
    "AWS::AccountId",
    "AWS::NoValue",
    "AWS::NotificationARNs",
    "AWS::Partition",
    "AWS::Region",
    "AWS::StackId",
    "AWS::StackName",
    "AWS::URLSuffix",
}

NO_VALUE = object()


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------
class _CfnLoader(yaml.SafeLoader):
    pass


def _construct_tag(loader, suffix, node):
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
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


_CfnLoader.add_multi_constructor("!", _construct_tag)


def load(path=TEMPLATE):
    return yaml.load(path.read_text(), Loader=_CfnLoader)


# --------------------------------------------------------------------------
# Resolving, the way CloudFormation does before it creates anything
# --------------------------------------------------------------------------
def parameter_values(template, overrides):
    values = {name: spec.get("Default") for name, spec in template["Parameters"].items()}
    values.update(REQUIRED)
    values.update({k: v for k, v in overrides.items() if k in template["Parameters"]})
    return {k: "" if v is None else str(v) for k, v in values.items()}


def evaluate_conditions(template, params):
    raw = template.get("Conditions", {})
    done = {}

    def value(node):
        if isinstance(node, dict) and set(node) == {"Ref"}:
            return params[node["Ref"]]
        return str(node)

    def ev(node):
        ((fn, args),) = node.items()
        if fn == "Fn::Equals":
            return value(args[0]) == value(args[1])
        if fn == "Fn::Not":
            return not ev(args[0])
        if fn == "Fn::And":
            return all(ev(a) for a in args)
        if fn == "Fn::Or":
            return any(ev(a) for a in args)
        if fn == "Condition":
            return cond(args)
        raise AssertionError(f"unsupported condition function {fn}")

    def cond(name):
        assert name in raw, f"condition {name!r} is used but not defined"
        if name not in done:
            done[name] = ev(raw[name])
        return done[name]

    for name in raw:
        cond(name)
    return done


def resolve(node, conds):
    """Pick every Fn::If branch and drop every AWS::NoValue."""
    if isinstance(node, dict):
        if set(node) == {"Fn::If"}:
            name, yes, no = node["Fn::If"]
            assert name in conds, f"Fn::If names undefined condition {name!r}"
            return resolve(yes if conds[name] else no, conds)
        if node == {"Ref": "AWS::NoValue"}:
            return NO_VALUE
        out = {}
        for k, v in node.items():
            r = resolve(v, conds)
            if r is not NO_VALUE:
                out[k] = r
        return out
    if isinstance(node, list):
        return [r for r in (resolve(v, conds) for v in node) if r is not NO_VALUE]
    return node


def effective(overrides=None, template=None):
    """(resources, outputs, conditions) as deployed with these parameters."""
    template = template or load()
    params = parameter_values(template, overrides or {})
    conds = evaluate_conditions(template, params)

    def keep(spec):
        name = spec.get("Condition")
        if name is None:
            return True
        assert name in conds, f"Condition {name!r} is not defined"
        return conds[name]

    def strip(spec):
        return resolve({k: v for k, v in spec.items() if k != "Condition"}, conds)

    resources = {k: strip(v) for k, v in template["Resources"].items() if keep(v)}
    outputs = {k: strip(v) for k, v in template.get("Outputs", {}).items() if keep(v)}
    return resources, outputs, conds


_SUB_VAR = re.compile(r"\$\{([^!}][^}]*)\}")


def references(node):
    """Every logical id or parameter that a resolved node points at."""
    found = set()
    if isinstance(node, dict):
        if set(node) == {"Ref"}:
            found.add(node["Ref"])
        elif set(node) == {"Fn::GetAtt"}:
            found.add(node["Fn::GetAtt"][0])
        elif set(node) == {"Fn::Sub"}:
            arg = node["Fn::Sub"]
            text, variables = (arg, {}) if isinstance(arg, str) else (arg[0], arg[1])
            for v in variables.values():
                found |= references(v)
            for name in _SUB_VAR.findall(text):
                name = name.split(".", 1)[0]
                if name not in variables:
                    found.add(name)
        else:
            for k, v in node.items():
                if k == "DependsOn":
                    found |= {v} if isinstance(v, str) else set(v)
                else:
                    found |= references(v)
    elif isinstance(node, list):
        for v in node:
            found |= references(v)
    return found - PSEUDO


def known_targets(template, resources):
    names = set(template["Parameters"]) | set(resources)
    # Resources the SAM transform generates, which the template may name
    # directly: a function's execution role when it does not bring its own.
    for name, spec in resources.items():
        props = spec.get("Properties", {})
        if spec["Type"] == "AWS::Serverless::Function" and "Role" not in props:
            names.add(f"{name}Role")
    return names


# Every combination of the parameters that switch resources in or out.
MATRIX_AXES = {
    "CreateErrorAlerting": ["true", "false"],
    "AlertEmail": ["", "alerts@example.com"],
    "DeliveryMode": ["Lambda", "Firehose"],
    "FirehoseStreamEnabled": ["true", "false"],
    "CreateForwardingRule": ["true", "false"],
    "CreateSearchApi": ["true", "false"],
}
MATRIX = [
    dict(zip(MATRIX_AXES, combo)) for combo in itertools.product(*MATRIX_AXES.values())
]


def _id(params):
    return ",".join(f"{k}={v or 'blank'}" for k, v in params.items())


def _alarms(resources):
    return {k: v for k, v in resources.items() if v["Type"] == "AWS::CloudWatch::Alarm"}


# --------------------------------------------------------------------------
# The parameter
# --------------------------------------------------------------------------
def test_parameter_defaults_to_creating_everything():
    spec = load()["Parameters"]["CreateErrorAlerting"]
    assert spec["Type"] == "String"
    assert spec["Default"] == "true"
    assert sorted(spec["AllowedValues"]) == ["false", "true"]


def test_gate_condition_is_the_parameter():
    conds = load()["Conditions"]
    assert conds[GATE] == {"Fn::Equals": [{"Ref": "CreateErrorAlerting"}, "true"]}
    assert conds["CreateAlertEmailSubscription"] == {
        "Fn::And": [{"Condition": "HasAlertEmail"}, {"Condition": GATE}]
    }


# --------------------------------------------------------------------------
# (a) The default: every alerting resource, exactly as before
# --------------------------------------------------------------------------
def test_default_creates_every_alerting_resource():
    resources, outputs, _ = effective({"AlertEmail": "alerts@example.com"})
    assert ALERTING_RESOURCES <= set(resources)
    assert ALERTING_OUTPUTS <= set(outputs)


def test_default_without_email_creates_everything_but_the_subscription():
    resources, outputs, _ = effective()
    assert ALERTING_RESOURCES - {"AlertsEmailSubscription"} <= set(resources)
    assert "AlertsEmailSubscription" not in resources
    assert ALERTING_OUTPUTS <= set(outputs)


def test_explicit_true_is_identical_to_the_default():
    assert effective({"CreateErrorAlerting": "true"}) == effective()


@pytest.mark.parametrize("mode", ["Lambda", "Firehose"])
def test_default_alarms_still_notify_the_topic(mode):
    alarms = _alarms(effective({"DeliveryMode": mode})[0])
    assert alarms
    for name, spec in alarms.items():
        props = spec["Properties"]
        assert props["AlarmActions"] == [{"Ref": "AlertsSnsTopic"}], name
        if "OKActions" in props:
            assert props["OKActions"] == [{"Ref": "AlertsSnsTopic"}], name


def test_default_digest_is_unchanged():
    props = effective()[0]["AlertDigestFunction"]["Properties"]
    assert props["ReservedConcurrentExecutions"] == {"Ref": "DigestReservedConcurrency"}
    assert props["Environment"]["Variables"]["ALERTS_SNS_TOPIC_ARN"] == {"Ref": "AlertsSnsTopic"}
    assert props["Events"]["ErrorQueue"]["Properties"]["Queue"] == {
        "Fn::GetAtt": ["ErrorAlertsQueue", "Arn"]
    }


# --------------------------------------------------------------------------
# (b) false: every alerting resource, and every reference to one, is gated
# --------------------------------------------------------------------------
def test_every_alerting_resource_is_gated_in_the_template():
    resources = load()["Resources"]
    for name in ALERTING_RESOURCES - {"AlertsEmailSubscription"}:
        assert resources[name].get("Condition") == GATE, name
    assert resources["AlertsEmailSubscription"]["Condition"] == "CreateAlertEmailSubscription"


def test_every_alerting_output_is_gated_in_the_template():
    outputs = load()["Outputs"]
    for name in ALERTING_OUTPUTS:
        assert outputs[name].get("Condition") == GATE, name


@pytest.mark.parametrize("mode", ["Lambda", "Firehose"])
def test_false_creates_none_of_it(mode):
    resources, outputs, _ = effective(
        {"CreateErrorAlerting": "false", "AlertEmail": "alerts@example.com", "DeliveryMode": mode}
    )
    assert not ALERTING_RESOURCES & set(resources)
    assert not ALERTING_OUTPUTS & set(outputs)


@pytest.mark.parametrize("mode", ["Lambda", "Firehose"])
def test_false_leaves_no_reference_to_any_of_it(mode):
    resources, outputs, _ = effective({"CreateErrorAlerting": "false", "DeliveryMode": mode})
    for name, spec in {**resources, **outputs}.items():
        assert not references(spec) & ALERTING_RESOURCES, name


@pytest.mark.parametrize("mode", ["Lambda", "Firehose"])
def test_false_keeps_the_delivery_alarms_without_actions(mode):
    alarms = _alarms(effective({"CreateErrorAlerting": "false", "DeliveryMode": mode})[0])
    assert {"EventBusFailedInvocationsAlarm", "ForwarderErrorAlarm"} <= set(alarms)
    for name, spec in alarms.items():
        assert "AlarmActions" not in spec["Properties"], name
        assert "OKActions" not in spec["Properties"], name


def test_false_does_not_touch_the_forwarder():
    on = effective()[0]
    off = effective({"CreateErrorAlerting": "false"})[0]
    for name in (
        "OpenSearchForwarderFunction",
        "AllPlatformEventsToForwarderRule",
        "ForwarderInvokePermission",
        "OpenSearchForwarderLogGroup",
    ):
        assert off[name] == on[name], name


# --------------------------------------------------------------------------
# Nothing dangles, under any combination
# --------------------------------------------------------------------------
@pytest.mark.parametrize("params", MATRIX, ids=_id)
def test_no_reference_dangles(params):
    template = load()
    resources, outputs, _ = effective(params, template)
    targets = known_targets(template, resources)
    for name, spec in {**resources, **outputs}.items():
        missing = references(spec) - targets
        assert not missing, f"{name} references {sorted(missing)}, absent under {_id(params)}"

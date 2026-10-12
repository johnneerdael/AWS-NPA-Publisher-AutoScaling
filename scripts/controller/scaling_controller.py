"""Scale out before the busiest publisher thread exhausts one vCPU."""

import json
import logging
import math
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import ClientError

LOG = logging.getLogger(__name__)
LOG.setLevel(logging.INFO)
EXPECTED_SAMPLES_PER_MINUTE = 6


@dataclass(frozen=True)
class Config:
    group_name: str
    state_table: str
    enabled: bool = False
    early_threshold: float = 70
    urgent_threshold: float = 80
    spare_threshold: float = 40
    cooldown_seconds: int = 300

    def __post_init__(self):
        if not self.group_name or not self.state_table:
            raise ValueError("Auto Scaling group and state table are required")
        if not (
            0
            <= self.spare_threshold
            < self.early_threshold
            <= self.urgent_threshold
            <= 100
        ):
            raise ValueError("Require 0 <= spare < early <= urgent <= 100")
        if self.cooldown_seconds <= 0:
            raise ValueError("Cooldown must be positive")


@dataclass
class Decision:
    reason: str
    hottest_cpu: float | None = None
    missing_telemetry: int = 0
    required_spares: int = 0
    available_spares: int = 0
    at_capacity_limit: bool = False
    desired_capacity: int | None = None
    scale_out_requested: bool = False


def _valid_cpu(value):
    return (
        isinstance(value, (float, int))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def _valid_samples(samples):
    if not samples:
        return False
    for field in ("cpu", "ready", "healthy", "count"):
        values = samples.get(field, [])
        if len(values) != 2:
            return False
        if any(not _valid_cpu(value) for value in values):
            return False
        if field in ("ready", "healthy") and values != [1, 1]:
            return False
        if field == "count" and min(values) < EXPECTED_SAMPLES_PER_MINUTE:
            return False
    return True


def decide(group, telemetry, config):
    """Make a scale-out-only decision from two complete minute observations."""
    members = group.get("Instances", [])
    current = [
        member["InstanceId"]
        for member in members
        if member["LifecycleState"] == "InService"
        and member["HealthStatus"] == "Healthy"
    ]
    valid = {
        instance: telemetry[instance]
        for instance in current
        if _valid_samples(telemetry.get(instance))
    }
    observed_cpu = []
    for instance in current:
        cpu = telemetry.get(instance, {}).get("cpu", [])
        if len(cpu) == 2 and _valid_cpu(cpu[-1]):
            observed_cpu.append(cpu[-1])
    result = Decision(
        "below_threshold",
        hottest_cpu=max(observed_cpu, default=None),
        missing_telemetry=len(current) - len(valid),
    )
    if len(current) != len(members) or len(current) != group["DesiredCapacity"]:
        result.reason = "capacity_transition"
        return result
    if result.missing_telemetry or not current:
        result.reason = "missing_telemetry"
        result.missing_telemetry = max(1, result.missing_telemetry)
        return result
    if result.hottest_cpu >= config.urgent_threshold:
        result.required_spares = 2
    elif any(
        min(samples["cpu"]) >= config.early_threshold for samples in valid.values()
    ):
        result.required_spares = 1
    else:
        return result
    result.available_spares = sum(
        max(samples["cpu"]) <= config.spare_threshold for samples in valid.values()
    )
    deficit = max(0, result.required_spares - result.available_spares)
    if not deficit:
        result.reason = "headroom_available"
        return result
    desired = group["DesiredCapacity"]
    requested = max(group["MinSize"], desired + deficit)
    target = min(group["MaxSize"], requested)
    result.at_capacity_limit = requested > group["MaxSize"]
    if target <= desired:
        result.reason = "at_capacity_limit"
    else:
        result.reason = "headroom_deficit"
        result.desired_capacity = target
    return result


def _metric_queries(group_name, members):
    queries = []
    identifiers = {}
    for index, instance in enumerate(members):
        for suffix, field, name, statistic, unit in (
            ("c", "cpu", "HottestThreadCPU", "Maximum", "Percent"),
            ("h", "healthy", "CollectorHealthy", "Minimum", "Count"),
            ("r", "ready", "PublisherReady", "Minimum", "Count"),
            ("n", "count", "CollectorHealthy", "SampleCount", "Count"),
        ):
            metric_id = f"m{index}{suffix}"
            identifiers[metric_id] = (instance, field)
            queries.append(
                {
                    "Id": metric_id,
                    "MetricStat": {
                        "Metric": {
                            "Namespace": "NPA/Publisher",
                            "MetricName": name,
                            "Dimensions": [
                                {"Name": "AutoScalingGroupName", "Value": group_name},
                                {"Name": "InstanceId", "Value": instance},
                            ],
                        },
                        "Period": 60,
                        "Stat": statistic,
                        "Unit": unit,
                    },
                    "ReturnData": True,
                }
            )
    return queries, identifiers


def read_telemetry(cloudwatch, group_name, members, end):
    """Read only exact, complete, closed periods for current healthy members."""
    periods = [end - timedelta(minutes=2), end - timedelta(minutes=1)]
    telemetry = {instance: {} for instance in members}
    # GetMetricData accepts at most 500 queries. Each member uses four.
    for offset in range(0, len(members), 100):
        next_offset = offset + 100
        queries, identifiers = _metric_queries(group_name, members[offset:next_offset])
        request = {
            "MetricDataQueries": queries,
            "StartTime": periods[0],
            "EndTime": end,
            "ScanBy": "TimestampAscending",
        }
        values = {metric_id: {} for metric_id in identifiers}
        invalid = set()
        while True:
            response = cloudwatch.get_metric_data(**request)
            for result in response.get("MetricDataResults", []):
                metric_id = result["Id"]
                if metric_id not in identifiers:
                    continue
                timestamps = result.get("Timestamps", [])
                samples = result.get("Values", [])
                if result.get("StatusCode") != "Complete" or len(timestamps) != len(
                    samples
                ):
                    invalid.add(metric_id)
                    continue
                for timestamp, sample in zip(timestamps, samples):
                    if timestamp in periods:
                        if timestamp in values[metric_id]:
                            invalid.add(metric_id)
                        values[metric_id][timestamp] = sample
            if not response.get("NextToken"):
                break
            request["NextToken"] = response["NextToken"]
        for metric_id, (instance, field) in identifiers.items():
            if metric_id not in invalid:
                telemetry[instance][field] = [
                    values[metric_id].get(period) for period in periods
                ]
    return telemetry


def _publish_decision(cloudwatch, config, decision, now):
    cloudwatch.put_metric_data(
        Namespace="NPA/Autoscaling",
        MetricData=[
            {
                "MetricName": name,
                "Dimensions": [
                    {"Name": "AutoScalingGroupName", "Value": config.group_name}
                ],
                "Timestamp": now,
                "Value": value,
                "Unit": unit,
            }
            for name, value, unit in (
                ("HottestPublisherCPU", decision.hottest_cpu, "Percent"),
                ("MissingTelemetry", decision.missing_telemetry, "Count"),
                ("AtCapacityLimit", int(decision.at_capacity_limit), "Count"),
                ("ScaleOutRequested", int(decision.scale_out_requested), "Count"),
                ("ControllerHeartbeat", 1, "Count"),
                (
                    "CapacityTransition",
                    int(decision.reason == "capacity_transition"),
                    "Count",
                ),
            )
            if value is not None
        ],
    )
    payload = asdict(decision)
    LOG.info(json.dumps({"auto_scaling_group": config.group_name, **payload}))
    return payload


def run_controller(config, autoscaling, cloudwatch, dynamodb, now=None):
    """Inspect headroom and reserve a durable cooldown before any capacity write.

    Keep this controller as the only capacity writer. SetDesiredCapacity has no
    compare-and-set operation: external writes can still race after the recheck.
    """
    now = now or datetime.now(timezone.utc)
    now = now.astimezone(timezone.utc)
    end = now.replace(second=0, microsecond=0)
    groups = autoscaling.describe_auto_scaling_groups(
        AutoScalingGroupNames=[config.group_name]
    )["AutoScalingGroups"]
    if len(groups) != 1:
        raise ValueError("Expected exactly one existing Auto Scaling group")
    for page in autoscaling.get_paginator("describe_policies").paginate(
        AutoScalingGroupName=config.group_name
    ):
        if page.get("ScalingPolicies"):
            raise ValueError(
                "Remove existing scaling policies before using this controller"
            )
    for page in autoscaling.get_paginator("describe_scheduled_actions").paginate(
        AutoScalingGroupName=config.group_name
    ):
        if page.get("ScheduledUpdateGroupActions"):
            raise ValueError(
                "Remove scheduled scaling actions before using this controller"
            )
    group = groups[0]
    members = [
        instance["InstanceId"]
        for instance in group.get("Instances", [])
        if instance["LifecycleState"] == "InService"
        and instance["HealthStatus"] == "Healthy"
    ]
    telemetry = read_telemetry(cloudwatch, config.group_name, members, end)
    decision = decide(group, telemetry, config)
    if decision.desired_capacity is None:
        return _publish_decision(cloudwatch, config, decision, now)
    if not config.enabled:
        decision.reason = "observation_only"
        return _publish_decision(cloudwatch, config, decision, now)
    epoch = int(now.timestamp())
    try:
        dynamodb.update_item(
            TableName=config.state_table,
            Key={"AutoScalingGroupName": {"S": config.group_name}},
            UpdateExpression=(
                "SET nextEligibleAt = :until, lastRequestAt = :now, "
                "requestedDesiredCapacity = :desired"
            ),
            ConditionExpression=(
                "attribute_not_exists(nextEligibleAt) OR nextEligibleAt <= :now"
            ),
            ExpressionAttributeValues={
                ":now": {"N": str(epoch)},
                ":until": {"N": str(epoch + config.cooldown_seconds)},
                ":desired": {"N": str(decision.desired_capacity)},
            },
        )
    except ClientError as error:
        if error.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        decision.reason = "cooldown"
        return _publish_decision(cloudwatch, config, decision, now)
    try:
        latest_groups = autoscaling.describe_auto_scaling_groups(
            AutoScalingGroupNames=[config.group_name]
        )["AutoScalingGroups"]
        if len(latest_groups) != 1:
            raise ValueError("Auto Scaling group disappeared after reservation")
        # Retain the cooldown after an external change; never overwrite it.
        if any(
            latest_groups[0][field] != group[field]
            for field in ("DesiredCapacity", "MinSize", "MaxSize")
        ):
            decision.reason = "capacity_changed"
            decision.desired_capacity = None
            return _publish_decision(cloudwatch, config, decision, now)
        autoscaling.set_desired_capacity(
            AutoScalingGroupName=config.group_name,
            DesiredCapacity=decision.desired_capacity,
            HonorCooldown=False,
        )
    except ClientError:
        # Keep the reservation: an AWS timeout may occur after a successful write.
        decision.reason = "scale_out_failed"
        _publish_decision(cloudwatch, config, decision, now)
        raise
    decision.reason = "scale_out_requested"
    decision.scale_out_requested = True
    return _publish_decision(cloudwatch, config, decision, now)


def load_config():
    enabled = os.environ.get("ENABLE_SCALING", "false").lower()
    if enabled not in ("true", "false"):
        raise ValueError("ENABLE_SCALING must be true or false")
    return Config(
        group_name=os.environ["AUTO_SCALING_GROUP_NAME"],
        state_table=os.environ["STATE_TABLE_NAME"],
        enabled=enabled == "true",
        early_threshold=float(os.environ.get("EARLY_CPU_THRESHOLD", "70")),
        urgent_threshold=float(os.environ.get("URGENT_CPU_THRESHOLD", "80")),
        spare_threshold=float(os.environ.get("SPARE_CPU_THRESHOLD", "40")),
        cooldown_seconds=int(os.environ.get("COOLDOWN_SECONDS", "300")),
    )


def lambda_handler(event, context):
    return run_controller(
        load_config(),
        autoscaling=boto3.client("autoscaling"),
        cloudwatch=boto3.client("cloudwatch"),
        dynamodb=boto3.client("dynamodb"),
    )

"""Preventive scaling decisions and AWS adapter contracts."""

import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import boto3
from botocore.exceptions import ClientError
from botocore.stub import Stubber

from scripts.controller import scaling_controller as controller


def group(cpus):
    return {
        "AutoScalingGroupName": "publishers",
        "MinSize": 1,
        "MaxSize": 10,
        "DesiredCapacity": len(cpus),
        "Instances": [
            {
                "InstanceId": instance,
                "LifecycleState": "InService",
                "HealthStatus": "Healthy",
            }
            for instance in cpus
        ],
    }


def telemetry(cpus):
    return {
        instance: {
            "cpu": values,
            "healthy": [1, 1],
            "ready": [1, 1],
            "count": [6, 6],
        }
        for instance, values in cpus.items()
    }


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.config = controller.Config("publishers", "state")

    def decide(self, cpus, **changes):
        asg = group(cpus)
        asg.update(changes)
        return controller.decide(asg, telemetry(cpus), self.config)

    def test_busiest_publisher_triggers_urgent_despite_low_average(self):
        decision = self.decide({"hot": [82, 85], "busy": [42, 45]})
        self.assertEqual(decision.desired_capacity, 4)
        self.assertEqual(decision.required_spares, 2)
        self.assertEqual(decision.hottest_cpu, 85)

    def test_early_requires_two_full_minutes_on_same_publisher(self):
        self.assertEqual(self.decide({"hot": [69, 75]}).reason, "below_threshold")
        self.assertEqual(self.decide({"hot": [70, 75]}).desired_capacity, 2)
        self.assertEqual(
            self.decide({"a": [75, 50], "b": [50, 75]}).reason,
            "below_threshold",
        )

    def test_sticky_hot_publisher_stops_launching_when_spares_ready(self):
        steps = [
            ({"hot": [75, 75]}, 2),
            ({"hot": [75, 75], "spare": [10, 10]}, None),
            ({"hot": [82, 82], "spare": [10, 10]}, 3),
            ({"hot": [82, 82], "spare": [10, 10], "spare2": [5, 5]}, None),
        ]
        for cpus, desired in steps:
            with self.subTest(cpus=cpus):
                self.assertEqual(self.decide(cpus).desired_capacity, desired)

    def test_spare_cpu_must_remain_cool_for_two_periods(self):
        self.assertEqual(
            self.decide({"hot": [75, 75], "spare": [41, 20]}).desired_capacity,
            3,
        )

    def test_invalid_missing_or_unready_telemetry_inhibits(self):
        cpus = {"hot": [90, 90], "other": [5, 5]}
        for field, values in [
            ("cpu", [None, 5]),
            ("cpu", [float("nan"), 5]),
            ("cpu", [5, float("inf")]),
            ("cpu", [-1, 5]),
            ("cpu", [5]),
            ("ready", [1, 0]),
            ("healthy", [0, 1]),
            ("healthy", [2, 1]),
        ]:
            samples = telemetry(cpus)
            samples["other"][field] = values
            with self.subTest(field=field, values=values):
                result = controller.decide(group(cpus), samples, self.config)
                self.assertEqual(result.reason, "missing_telemetry")
                self.assertEqual(result.missing_telemetry, 1)
                self.assertIsNone(result.desired_capacity)

    def test_launching_unhealthy_or_terminating_member_inhibits(self):
        cpus = {"hot": [90, 90]}
        for changes in [
            {"DesiredCapacity": 2},
            {
                "Instances": [
                    {
                        "InstanceId": "hot",
                        "LifecycleState": "Pending",
                        "HealthStatus": "Healthy",
                    }
                ]
            },
            {
                "Instances": [
                    {
                        "InstanceId": "hot",
                        "LifecycleState": "Terminating",
                        "HealthStatus": "Healthy",
                    }
                ]
            },
            {
                "Instances": [
                    {
                        "InstanceId": "hot",
                        "LifecycleState": "InService",
                        "HealthStatus": "Unhealthy",
                    }
                ]
            },
        ]:
            with self.subTest(changes=changes):
                self.assertEqual(
                    self.decide(cpus, **changes).reason, "capacity_transition"
                )

    def test_only_current_members_count_as_spares(self):
        samples = telemetry({"hot": [90, 90], "terminated": [0, 0]})
        result = controller.decide(group({"hot": [90, 90]}), samples, self.config)
        self.assertEqual(result.desired_capacity, 3)

    def test_unknown_latest_cpu_is_missing_instead_of_zero(self):
        cpus = {"hot": [90, None]}
        result = controller.decide(group(cpus), telemetry(cpus), self.config)
        self.assertIsNone(result.hottest_cpu)
        self.assertEqual(result.reason, "missing_telemetry")

    def test_hot_unready_or_unhealthy_sample_remains_visible(self):
        cpus = {"hot": [90, 95]}
        for field in ("ready", "healthy"):
            samples = telemetry(cpus)
            samples["hot"][field] = [1, 0]
            with self.subTest(field=field):
                result = controller.decide(group(cpus), samples, self.config)
                self.assertEqual(result.hottest_cpu, 95)
                self.assertEqual(result.reason, "missing_telemetry")
                self.assertIsNone(result.desired_capacity)

    def test_sparse_spare_sample_does_not_prove_full_minute_headroom(self):
        cpus = {"hot": [90, 95], "spare": [0, 0]}
        samples = telemetry(cpus)
        samples["spare"]["count"] = [1, 6]
        result = controller.decide(group(cpus), samples, self.config)
        self.assertEqual(result.reason, "missing_telemetry")
        self.assertEqual(result.missing_telemetry, 1)
        self.assertIsNone(result.desired_capacity)

    def test_maximum_capacity_reports_unfulfilled_headroom(self):
        result = self.decide({"hot": [90, 90]}, MaxSize=2)
        self.assertEqual(result.desired_capacity, 2)
        self.assertTrue(result.at_capacity_limit)
        result = self.decide({"hot": [90, 90]}, MaxSize=1)
        self.assertIsNone(result.desired_capacity)
        self.assertEqual(result.reason, "at_capacity_limit")


class ConfigurationTests(unittest.TestCase):
    def test_observation_only_is_default(self):
        with patch.dict(
            os.environ,
            {"AUTO_SCALING_GROUP_NAME": "publishers", "STATE_TABLE_NAME": "state"},
            clear=True,
        ):
            config = controller.load_config()
        self.assertFalse(config.enabled)
        self.assertEqual(
            (config.early_threshold, config.urgent_threshold, config.spare_threshold),
            (70, 80, 40),
        )

    def test_invalid_configuration_fails_closed(self):
        for changes in [
            {"early_threshold": 110},
            {"spare_threshold": 80},
            {"cooldown_seconds": 0},
            {"early_threshold": float("nan")},
        ]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                controller.Config("publishers", "state", **changes)
        with patch.dict(os.environ, {"ENABLE_SCALING": "yes"}, clear=True):
            with self.assertRaises(ValueError):
                controller.load_config()


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 12, 12, 3, 25, tzinfo=timezone.utc)
        self.end = self.now.replace(second=0, microsecond=0)
        self.periods = [
            self.end - timedelta(minutes=2),
            self.end - timedelta(minutes=1),
        ]
        self.config = controller.Config("publishers", "state", enabled=True)
        self.clients = {
            name: boto3.client(
                name,
                region_name="eu-west-1",
                aws_access_key_id="testing",
                aws_secret_access_key="testing",  # pragma: allowlist secret
            )
            for name in ("autoscaling", "cloudwatch", "dynamodb")
        }
        self.stubs = {name: Stubber(client) for name, client in self.clients.items()}
        for stub in self.stubs.values():
            stub.activate()
            self.addCleanup(stub.deactivate)

    def asg_response(self, cpus):
        asg = group(cpus)
        asg.update(
            AutoScalingGroupARN=(
                "arn:aws:autoscaling:eu-west-1:123456789012:"
                "autoScalingGroup:123:autoScalingGroupName/publishers"
            ),
            CreatedTime=self.now,
            AvailabilityZones=["eu-west-1a"],
            DefaultCooldown=300,
            HealthCheckType="EC2",
        )
        for instance in asg["Instances"]:
            instance.update(AvailabilityZone="eu-west-1a", ProtectedFromScaleIn=False)
        return asg

    def query_request(self, cpus):
        queries = []
        for index, instance in enumerate(cpus):
            for suffix, name, statistic, unit in [
                ("c", "HottestThreadCPU", "Maximum", "Percent"),
                ("h", "CollectorHealthy", "Minimum", "Count"),
                ("r", "PublisherReady", "Minimum", "Count"),
                ("n", "CollectorHealthy", "SampleCount", "Count"),
            ]:
                queries.append(
                    {
                        "Id": f"m{index}{suffix}",
                        "MetricStat": {
                            "Metric": {
                                "Namespace": "NPA/Publisher",
                                "MetricName": name,
                                "Dimensions": [
                                    {
                                        "Name": "AutoScalingGroupName",
                                        "Value": "publishers",
                                    },
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
        return {
            "MetricDataQueries": queries,
            "StartTime": self.periods[0],
            "EndTime": self.end,
            "ScanBy": "TimestampAscending",
        }

    def seed(self, cpus, mutate=None):
        self.stubs["autoscaling"].add_response(
            "describe_auto_scaling_groups",
            {"AutoScalingGroups": [self.asg_response(cpus)]},
            {"AutoScalingGroupNames": ["publishers"]},
        )
        self.stubs["autoscaling"].add_response(
            "describe_policies",
            {"ScalingPolicies": []},
            {"AutoScalingGroupName": "publishers"},
        )
        self.stubs["autoscaling"].add_response(
            "describe_scheduled_actions",
            {"ScheduledUpdateGroupActions": []},
            {"AutoScalingGroupName": "publishers"},
        )
        results = []
        for index, values in enumerate(cpus.values()):
            for suffix, samples in [
                ("c", values),
                ("h", [1, 1]),
                ("r", [1, 1]),
                ("n", [6, 6]),
            ]:
                results.append(
                    {
                        "Id": f"m{index}{suffix}",
                        "Timestamps": self.periods,
                        "Values": samples,
                        "StatusCode": "Complete",
                    }
                )
        if mutate:
            mutate(results)
        self.stubs["cloudwatch"].add_response(
            "get_metric_data", {"MetricDataResults": results}, self.query_request(cpus)
        )

    def reserve_request(self, target):
        now = int(self.now.timestamp())
        return {
            "TableName": "state",
            "Key": {"AutoScalingGroupName": {"S": "publishers"}},
            "UpdateExpression": (
                "SET nextEligibleAt = :until, lastRequestAt = :now, "
                "requestedDesiredCapacity = :desired"
            ),
            "ConditionExpression": (
                "attribute_not_exists(nextEligibleAt) OR nextEligibleAt <= :now"
            ),
            "ExpressionAttributeValues": {
                ":now": {"N": str(now)},
                ":until": {"N": str(now + 300)},
                ":desired": {"N": str(target)},
            },
        }

    def diagnostics(self, hottest, missing=0, limit=0, requested=0, transition=0):
        self.stubs["cloudwatch"].add_response(
            "put_metric_data",
            {},
            {
                "Namespace": "NPA/Autoscaling",
                "MetricData": [
                    {
                        "MetricName": name,
                        "Dimensions": [
                            {"Name": "AutoScalingGroupName", "Value": "publishers"}
                        ],
                        "Timestamp": self.now,
                        "Value": value,
                        "Unit": unit,
                    }
                    for name, value, unit in [
                        ("HottestPublisherCPU", hottest, "Percent"),
                        ("MissingTelemetry", missing, "Count"),
                        ("AtCapacityLimit", limit, "Count"),
                        ("ScaleOutRequested", requested, "Count"),
                        ("ControllerHeartbeat", 1, "Count"),
                        ("CapacityTransition", transition, "Count"),
                    ]
                    if value is not None
                ],
            },
        )

    def run_controller(self, config=None):
        return controller.run_controller(
            config or self.config, now=self.now, **self.clients
        )

    def assert_consumed(self):
        for stub in self.stubs.values():
            stub.assert_no_pending_responses()

    def test_heartbeat_and_capacity_transition_are_reported(self):
        decision = controller.Decision("capacity_transition")
        self.diagnostics(None, transition=1)
        result = controller._publish_decision(
            self.clients["cloudwatch"], self.config, decision, self.now
        )
        self.assertEqual(result["reason"], "capacity_transition")
        self.assert_consumed()

    def test_reserve_then_scale_with_closed_minute_queries(self):
        self.seed({"i-hot": [75, 75]})
        self.stubs["dynamodb"].add_response("update_item", {}, self.reserve_request(2))
        self.seed_recheck({"i-hot": [75, 75]})
        self.stubs["autoscaling"].add_response(
            "set_desired_capacity",
            {},
            {
                "AutoScalingGroupName": "publishers",
                "DesiredCapacity": 2,
                "HonorCooldown": False,
            },
        )
        self.diagnostics(75, requested=1)
        result = self.run_controller()
        self.assertEqual(result["reason"], "scale_out_requested")
        self.assertTrue(result["scale_out_requested"])
        self.assert_consumed()

    def test_dry_run_never_reads_or_mutates_state_or_capacity(self):
        self.seed({"i-hot": [90, 90]})
        self.diagnostics(90)
        result = self.run_controller(controller.Config("publishers", "state"))
        self.assertEqual(result["reason"], "observation_only")
        self.assertEqual(result["desired_capacity"], 3)
        self.assert_consumed()

    def test_cooldown_or_concurrent_reservation_prevents_mutation(self):
        self.seed({"i-hot": [75, 75]})
        self.stubs["dynamodb"].add_client_error(
            "update_item",
            service_error_code="ConditionalCheckFailedException",
            expected_params=self.reserve_request(2),
        )
        self.diagnostics(75)
        self.assertEqual(self.run_controller()["reason"], "cooldown")
        self.assert_consumed()

    def test_aws_scale_failure_keeps_reservation_and_raises(self):
        self.seed({"i-hot": [75, 75]})
        self.stubs["dynamodb"].add_response("update_item", {}, self.reserve_request(2))
        self.seed_recheck({"i-hot": [75, 75]})
        self.stubs["autoscaling"].add_client_error(
            "set_desired_capacity",
            service_error_code="ResourceContention",
            expected_params={
                "AutoScalingGroupName": "publishers",
                "DesiredCapacity": 2,
                "HonorCooldown": False,
            },
        )
        self.diagnostics(75)
        with self.assertRaises(ClientError):
            self.run_controller()
        self.assert_consumed()

    def test_partial_or_stale_data_prevents_reservation(self):
        for mutation in [
            lambda results: results[0].update(StatusCode="PartialData"),
            lambda results: results[0].update(
                Timestamps=[self.periods[0] - timedelta(minutes=1), self.periods[0]]
            ),
        ]:
            with self.subTest(mutation=mutation):
                self.seed({"i-hot": [90, 90]}, mutation)
                self.diagnostics(None, missing=1)
                self.assertEqual(self.run_controller()["reason"], "missing_telemetry")
                self.assert_consumed()

    def seed_recheck(self, cpus, **changes):
        asg = self.asg_response(cpus)
        asg.update(changes)
        self.stubs["autoscaling"].add_response(
            "describe_auto_scaling_groups",
            {"AutoScalingGroups": [asg]},
            {"AutoScalingGroupNames": ["publishers"]},
        )

    def test_external_capacity_change_is_not_overwritten(self):
        self.seed({"i-hot": [75, 75]})
        self.stubs["dynamodb"].add_response("update_item", {}, self.reserve_request(2))
        self.seed_recheck({"i-hot": [75, 75]}, DesiredCapacity=5)
        self.diagnostics(75)
        self.assertEqual(self.run_controller()["reason"], "capacity_changed")
        self.assert_consumed()

    def test_invalid_readiness_does_not_hide_measured_cpu(self):
        self.seed(
            {"i-hot": [90, 95]},
            lambda results: results[2].update(Values=[1, 0]),
        )
        self.diagnostics(95, missing=1)
        result = self.run_controller()
        self.assertEqual(result["reason"], "missing_telemetry")
        self.assertEqual(result["hottest_cpu"], 95)
        self.assert_consumed()

    def test_sparse_healthy_sample_inhibits_sdk_capacity_write(self):
        self.seed(
            {"i-hot": [90, 95]},
            lambda results: results[3].update(Values=[6, 1]),
        )
        self.diagnostics(95, missing=1)
        self.assertEqual(self.run_controller()["reason"], "missing_telemetry")
        self.assert_consumed()

    def test_current_or_upcoming_scheduled_action_is_rejected(self):
        self.stubs["autoscaling"].add_response(
            "describe_auto_scaling_groups",
            {"AutoScalingGroups": [self.asg_response({"i-hot": [90, 90]})]},
            {"AutoScalingGroupNames": ["publishers"]},
        )
        self.stubs["autoscaling"].add_response(
            "describe_policies",
            {"ScalingPolicies": []},
            {"AutoScalingGroupName": "publishers"},
        )
        self.stubs["autoscaling"].add_response(
            "describe_scheduled_actions",
            {"ScheduledUpdateGroupActions": [{"ScheduledActionName": "weekday"}]},
            {"AutoScalingGroupName": "publishers"},
        )
        with self.assertRaisesRegex(ValueError, "scheduled scaling actions"):
            self.run_controller()
        self.assert_consumed()

    def test_existing_scaling_policy_is_rejected_without_changes(self):
        self.stubs["autoscaling"].add_response(
            "describe_auto_scaling_groups",
            {"AutoScalingGroups": [self.asg_response({"i-hot": [90, 90]})]},
            {"AutoScalingGroupNames": ["publishers"]},
        )
        self.stubs["autoscaling"].add_response(
            "describe_policies",
            {"ScalingPolicies": [{"PolicyName": "old-cpu-policy"}]},
            {"AutoScalingGroupName": "publishers"},
        )
        with self.assertRaisesRegex(ValueError, "existing scaling policies"):
            self.run_controller()
        self.assert_consumed()

    def test_policy_on_later_page_is_rejected(self):
        self.stubs["autoscaling"].add_response(
            "describe_auto_scaling_groups",
            {"AutoScalingGroups": [self.asg_response({"i-hot": [90, 90]})]},
            {"AutoScalingGroupNames": ["publishers"]},
        )
        self.stubs["autoscaling"].add_response(
            "describe_policies",
            {"ScalingPolicies": [], "NextToken": "page2"},
            {"AutoScalingGroupName": "publishers"},
        )
        self.stubs["autoscaling"].add_response(
            "describe_policies",
            {"ScalingPolicies": [{"PolicyName": "old-cpu-policy"}]},
            {"AutoScalingGroupName": "publishers", "NextToken": "page2"},
        )
        with self.assertRaisesRegex(ValueError, "existing scaling policies"):
            self.run_controller()
        self.assert_consumed()

    def test_state_service_failure_never_changes_capacity(self):
        self.seed({"i-hot": [75, 75]})
        self.stubs["dynamodb"].add_client_error(
            "update_item",
            service_error_code="ProvisionedThroughputExceededException",
            expected_params=self.reserve_request(2),
        )
        with self.assertRaises(ClientError):
            self.run_controller()
        self.assert_consumed()

    def test_metric_pagination_preserves_both_required_periods(self):
        request = self.query_request({"i-hot": [75, 75]})
        ids = ["m0c", "m0h", "m0r", "m0n"]
        first = [
            {
                "Id": metric_id,
                "StatusCode": "Complete",
                "Timestamps": [self.periods[0]],
                "Values": [
                    (
                        75
                        if metric_id.endswith("c")
                        else (6 if metric_id.endswith("n") else 1)
                    )
                ],
            }
            for metric_id in ids
        ]
        second = [
            {
                "Id": metric_id,
                "StatusCode": "Complete",
                "Timestamps": [self.periods[1]],
                "Values": [
                    (
                        80
                        if metric_id.endswith("c")
                        else (6 if metric_id.endswith("n") else 1)
                    )
                ],
            }
            for metric_id in ids
        ]
        self.stubs["cloudwatch"].add_response(
            "get_metric_data",
            {"MetricDataResults": first, "NextToken": "next"},
            request,
        )
        self.stubs["cloudwatch"].add_response(
            "get_metric_data",
            {"MetricDataResults": second},
            {**request, "NextToken": "next"},
        )
        samples = controller.read_telemetry(
            self.clients["cloudwatch"], "publishers", ["i-hot"], self.end
        )
        self.assertEqual(
            samples,
            {
                "i-hot": {
                    "cpu": [75, 80],
                    "healthy": [1, 1],
                    "ready": [1, 1],
                    "count": [6, 6],
                }
            },
        )
        self.assert_consumed()


if __name__ == "__main__":
    unittest.main()

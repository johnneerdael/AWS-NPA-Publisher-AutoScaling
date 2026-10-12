"""Safety contracts for the add-on, without provisioning AWS resources."""

from pathlib import Path
import unittest

import yaml


class CloudFormationLoader(yaml.SafeLoader):
    """Preserve CloudFormation short-form intrinsics for contract inspection."""


def intrinsic(loader, tag, node):
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node)
    else:
        value = loader.construct_mapping(node)
    return {tag if tag == "Ref" else f"Fn::{tag}": value}


CloudFormationLoader.add_multi_constructor("!", intrinsic)


class ThreadScalingTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1]
        path /= "templates/publisher-thread-scaling.yaml"
        loader = CloudFormationLoader(path.read_text())
        try:
            cls.template = loader.get_single_data()
        finally:
            loader.dispose()
        cls.resources = cls.template["Resources"]

    def resource(self, resource_type):
        matches = [
            resource
            for resource in self.resources.values()
            if resource["Type"] == resource_type
        ]
        self.assertEqual(len(matches), 1)
        return matches[0]

    def test_observation_only_add_on_has_no_competing_scaling_policy(self):
        params = self.template["Parameters"]
        self.assertEqual(params["EnableScaling"]["Default"], "false")
        self.assertEqual(params["EnableScaling"]["AllowedValues"], ["true", "false"])
        self.assertNotIn("Default", params["AutoScalingGroupName"])
        self.assertNotIn("Default", params["ControllerCodeBucket"])
        self.assertNotIn("Default", params["ControllerCodeKey"])
        forbidden = {
            "AWS::AutoScaling::AutoScalingGroup",
            "AWS::AutoScaling::ScalingPolicy",
            "AWS::AutoScaling::LifecycleHook",
            "AWS::EC2::Instance",
            "AWS::EC2::LaunchTemplate",
        }
        self.assertFalse(
            forbidden.intersection(r["Type"] for r in self.resources.values())
        )
        self.assertNotIn("ASGAverageCPUUtilization", str(self.template))

    def test_controller_serializes_requests_and_uses_selected_artifact(self):
        function = self.resource("AWS::Lambda::Function")["Properties"]
        self.assertEqual(function["Handler"], "scaling_controller.lambda_handler")
        self.assertEqual(function["Runtime"], "python3.12")
        self.assertEqual(function["Timeout"], 45)
        self.assertEqual(function["ReservedConcurrentExecutions"], 1)
        self.assertEqual(function["Code"]["S3Bucket"], {"Ref": "ControllerCodeBucket"})
        self.assertEqual(function["Code"]["S3Key"], {"Ref": "ControllerCodeKey"})
        version = function["Code"]["S3ObjectVersion"]["Fn::If"]
        self.assertEqual(
            version[1:], [{"Ref": "ControllerCodeVersion"}, {"Ref": "AWS::NoValue"}]
        )
        env = function["Environment"]["Variables"]
        for name, parameter in {
            "AUTO_SCALING_GROUP_NAME": "AutoScalingGroupName",
            "ENABLE_SCALING": "EnableScaling",
            "EARLY_CPU_THRESHOLD": "EarlyCPUThreshold",
            "URGENT_CPU_THRESHOLD": "UrgentCPUThreshold",
            "SPARE_CPU_THRESHOLD": "SpareCPUThreshold",
            "COOLDOWN_SECONDS": "CooldownSeconds",
        }.items():
            self.assertEqual(env[name], {"Ref": parameter})
        table = self.resource("AWS::DynamoDB::Table")
        self.assertEqual(table["DeletionPolicy"], "Retain")
        self.assertEqual(table["UpdateReplacePolicy"], "Retain")
        self.assertEqual(table["Properties"]["BillingMode"], "PAY_PER_REQUEST")
        self.assertTrue(table["Properties"]["SSESpecification"]["SSEEnabled"])
        self.assertEqual(
            table["Properties"]["KeySchema"],
            [{"AttributeName": "AutoScalingGroupName", "KeyType": "HASH"}],
        )
        self.assertIn("STATE_TABLE_NAME", env)
        schedule = self.resource("AWS::Events::Rule")["Properties"]
        self.assertEqual(schedule["ScheduleExpression"], "rate(1 minute)")
        self.assertEqual(
            schedule["Targets"][0]["RetryPolicy"],
            {"MaximumEventAgeInSeconds": 60, "MaximumRetryAttempts": 0},
        )
        invocation = self.resource("AWS::Lambda::EventInvokeConfig")["Properties"]
        self.assertEqual(invocation["MaximumEventAgeInSeconds"], 60)
        self.assertEqual(invocation["MaximumRetryAttempts"], 0)

    def test_role_can_only_scale_selected_group_and_write_diagnostic_namespace(self):
        role = self.resource("AWS::IAM::Role")["Properties"]
        self.assertNotIn("ManagedPolicyArns", role)
        statements = [
            statement
            for policy in role["Policies"]
            for statement in policy["PolicyDocument"]["Statement"]
        ]
        actions = {action for statement in statements for action in statement["Action"]}
        self.assertEqual(
            actions,
            {
                "cloudwatch:GetMetricData",
                "cloudwatch:PutMetricData",
                "autoscaling:DescribeAutoScalingGroups",
                "autoscaling:DescribePolicies",
                "autoscaling:DescribeScheduledActions",
                "autoscaling:SetDesiredCapacity",
                "dynamodb:UpdateItem",
                "logs:CreateLogStream",
                "logs:PutLogEvents",
            },
        )
        for statement in statements:
            if "autoscaling:SetDesiredCapacity" in statement["Action"]:
                self.assertEqual(
                    statement["Resource"],
                    {
                        "Fn::Sub": (
                            "arn:${AWS::Partition}:autoscaling:${AWS::Region}:"
                            "${AWS::AccountId}:autoScalingGroup:*:"
                            "autoScalingGroupName/${AutoScalingGroupName}"
                        )
                    },
                )
            if "cloudwatch:PutMetricData" in statement["Action"]:
                self.assertEqual(
                    statement["Condition"],
                    {"StringEquals": {"cloudwatch:namespace": "NPA/Autoscaling"}},
                )
            if "dynamodb:UpdateItem" in statement["Action"]:
                self.assertNotEqual(statement["Resource"], "*")
            if "logs:PutLogEvents" in statement["Action"]:
                self.assertNotEqual(statement["Resource"], "*")
        logs = self.resource("AWS::Logs::LogGroup")["Properties"]
        self.assertEqual(logs["RetentionInDays"], 30)

    def test_alarms_preserve_missing_data_and_do_not_change_capacity(self):
        alarms = {
            resource["Properties"]["MetricName"]: resource["Properties"]
            for resource in self.resources.values()
            if resource["Type"] == "AWS::CloudWatch::Alarm"
        }
        self.assertEqual(
            set(alarms),
            {
                "MissingTelemetry",
                "AtCapacityLimit",
                "HottestPublisherCPU",
                "ControllerHeartbeat",
                "CapacityTransition",
                "Errors",
            },
        )
        for name, alarm in alarms.items():
            self.assertEqual(
                alarm["TreatMissingData"],
                "breaching" if name == "ControllerHeartbeat" else "missing",
            )
            self.assertEqual(
                alarm["Statistic"],
                "Sum" if name in {"Errors", "ControllerHeartbeat"} else "Maximum",
            )
            self.assertEqual(
                alarm["AlarmActions"]["Fn::If"][2], {"Ref": "AWS::NoValue"}
            )
            self.assertEqual(
                alarm["AlarmActions"]["Fn::If"][1], [{"Ref": "AlarmTopicArn"}]
            )
            if name != "Errors":
                self.assertEqual(alarm["Namespace"], "NPA/Autoscaling")
                self.assertEqual(
                    alarm["Dimensions"],
                    [
                        {
                            "Name": "AutoScalingGroupName",
                            "Value": {"Ref": "AutoScalingGroupName"},
                        }
                    ],
                )
        self.assertEqual(alarms["MissingTelemetry"]["Threshold"], 1)
        self.assertEqual(alarms["AtCapacityLimit"]["Threshold"], 1)
        self.assertEqual(
            alarms["HottestPublisherCPU"]["Threshold"], {"Ref": "UrgentCPUThreshold"}
        )
        heartbeat = alarms["ControllerHeartbeat"]
        self.assertEqual(heartbeat["Threshold"], 1)
        self.assertEqual(heartbeat["ComparisonOperator"], "LessThanThreshold")
        self.assertEqual(heartbeat["Period"], 60)
        self.assertEqual(heartbeat["EvaluationPeriods"], 3)
        self.assertEqual(heartbeat["DatapointsToAlarm"], 3)
        transition = alarms["CapacityTransition"]
        self.assertEqual(transition["Threshold"], 1)
        self.assertEqual(
            transition["ComparisonOperator"], "GreaterThanOrEqualToThreshold"
        )
        self.assertEqual(transition["Period"], 60)
        self.assertEqual(transition["EvaluationPeriods"], 15)
        self.assertEqual(transition["DatapointsToAlarm"], 15)


if __name__ == "__main__":
    unittest.main()

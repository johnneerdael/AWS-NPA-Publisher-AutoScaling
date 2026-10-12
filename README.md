# Preventive autoscaling for NPA publisher threads

This fork adds capacity before the busiest Netskope Private Access publisher
thread saturates one vCPU. Existing sticky connections stay on their publisher;
new connections can use ready publishers with headroom.

The controller uses the highest individual publisher thread CPU. A second idle
core or a low fleet average cannot hide a hot publisher.

| Default condition | Required headroom |
|---|---|
| One publisher at or above 70% in two closed minutes | One ready publisher at or below 40% |
| Any publisher at or above 80% in the latest closed minute | Two ready publishers at or below 40% |
| Enough ready headroom already exists | Hold capacity, even while sticky load stays hot |
| Capacity is launching, telemetry is incomplete, or cooldown is active | Hold and report the reason |
| Required headroom exceeds the ASG maximum | Add only permitted capacity and alert |

The CPU percentages represent **one vCPU**, independent of the instance's core
count. These thresholds are starting values, not a throughput guarantee. Tune
against measured CPU growth and launch-to-ready time. Spare capacity helps only
if the NPA routing layer sends new connections to eligible cool publishers.

## Components

- **Linux host collector:** Reads each publisher thread's user/system CPU time
  under `/proc`. Select one exact executable, optionally within a named Docker
  container. Publish CPU, collector health, and explicit registration readiness
  every 10 seconds. No publisher binary changes are required.
- **Scheduled controller:** Checks current ASG members and closed CloudWatch
  periods every minute. Add only the missing ready headroom. Require six samples
  in each minute, serialize requests, honor a durable cooldown, and respect the
  ASG maximum. Never infer readiness from an idle CPU reading.
- **CloudFormation add-on:** Deploy the controller, state, and alarms against an
  **existing verified publisher ASG**. Default to observation-only. The add-on
  does not create publishers or replace their registration workflow.

Automatic scale-in is deliberately absent. Remove publishers only through a
verified draining procedure. The controller must be the group's sole capacity
writer when enabled; it rejects existing scaling policies and scheduled actions.

## Start here

Read [deployment and qualification](docs/PUBLISHER_THREAD_SCALING.md) for host
installation, readiness integration, permissions, packaging, observation, and
enabling scaling. Read [the design](docs/PUBLISHER_THREAD_SCALING_DESIGN.md) for
constraints and implementation scope.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/cfn-lint templates/publisher-thread-scaling.yaml
python3 scripts/package-controller.py dist/controller.zip
```

Deployment uses `templates/publisher-thread-scaling.yaml`. Upload the locally
built ZIP to an operator-owned regional S3 bucket and pass its immutable key to
the stack. No dependency on an upstream-owned Lambda deployment bucket exists.

## Validation status

Unit and AWS SDK adapter tests cover threshold persistence, sticky-load headroom,
thread/process restarts, Docker selection, missing and sparse telemetry, readiness
expiry, cooldown contention, concurrent capacity changes, and observation safety.
CI also tests real Linux `/proc` CPU sampling, packages the reviewed source, and
checks the supported template and Python code.

A read-only Frankfurt check identified `/home/npa_publisher` inside Docker with
two OS threads and confirmed the collector can sample both. That publisher was
idle during the check. This establishes compatibility with that process layout;
it does not establish loaded throughput, ASG registration, routing, or production
readiness. No AWS stack or tenant configuration was changed.

## Upstream implementation

The original upstream implementation remains at
`templates/npa-publisher-autoscaling.yaml`; its original documentation is in
`docs/DEPLOYMENT_GUIDE.md`, `docs/DEVOPS_NOTES.md`, and
`docs/NPA_AWS_HA_STRATEGIES.md`. Treat these as historical reference.

The original template uses `ASGAverageCPUUtilization` at 70% on a two-vCPU
`t3.large`. A single saturated thread contributes roughly 50% EC2 instance CPU,
so the default can miss saturation. Its checked-in lifecycle Lambda and ZIP
handle CloudWatch log forwarding, not the documented publisher registration
flow; the original template instead references a separately hosted S3 ZIP whose
contents are not verified here. Current cfn-lint also reports E3712 for its
initial zero-capacity ASG. This fork's supported add-on avoids those dependencies
and requires an independently verified publisher lifecycle integration.

See [AWS target tracking metric semantics](https://docs.aws.amazon.com/autoscaling/ec2/userguide/as-scaling-target-tracking.html)
and [EC2 CPU accounting](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/burstable-credits-baseline-concepts.html).

## License

Apache-2.0, inherited from [netskopeoss/AWS-NPA-Publisher-AutoScaling](https://github.com/netskopeoss/AWS-NPA-Publisher-AutoScaling).

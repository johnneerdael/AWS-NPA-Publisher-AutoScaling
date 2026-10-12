# Deploy and qualify preventive thread scaling

## Operating model

Use this add-on with an existing publisher Auto Scaling group whose AMI,
registration, application assignment, and launch lifecycle already work. Use
instance-count desired capacity and one publisher per instance; mixed weighted
or vCPU/memory capacity groups are outside this controller's model.

The busiest publisher's hottest thread is the trigger. At 70% for two closed
one-minute periods, require one ready publisher at or below 40% CPU. At 80% in
the latest closed minute, require two. Count publishers below 40% for both
periods as spare headroom; no second idle core contributes to this count.

For example, readings of `[75, 65]` over both periods require one extra publisher.
After the new publisher becomes eligible and reports `[10, 10]`, `[75, 65, 10]`
already has enough headroom: sticky load on the first publisher does not keep
launching instances. `[85, 65, 10]` requires a second ready spare. These are
examples of policy behavior, not measured NPA routing behavior.

Only this controller may write desired capacity while enabled. Disable/remove
legacy scaling policies and scheduled capacity actions during an intentional
maintenance migration. The controller rejects them even in observation mode.
Pause this controller before an operator or other automation changes capacity.
A fresh ASG read checks desired/min/max just before the write, but AWS provides
no conditional compare-and-set for `SetDesiredCapacity`; an uncoordinated writer
can still race the request. Never run two controller stacks against the same ASG.

Scale-in remains a separate drained maintenance operation. This add-on cannot
prevent termination caused by operators, EC2 failure replacement, Spot events,
or other AWS mechanisms. Low CPU does not prove that sticky sessions can be
terminated safely.

## Identify the publisher on the EC2 host

Run the collector on the **host**, with root procfs access. Do not install it
inside the publisher container or use aggregate `docker stats` as the scaling
metric. The collector reads thread CPU counters, so scheduler migration between
cores does not change its units.

Use `docker ps`, `docker top CONTAINER -eo pid,ppid,comm,nlwp`, and
`sudo readlink /proc/HOST_PID/exe` to identify the forwarding publisher executable.
Do not select the watchdog, diagnostic server, or the entire container.

A read-only check on the user's Frankfurt publisher on 2026-10-12 found the
executable `/home/npa_publisher` and two OS threads. The Docker container was
running with no explicit CPU quota or cpuset restriction. Both measured threads
were idle; loaded behavior was not qualified. Container names and PIDs from that
check are transient and must not be copied into a reusable AMI.

Use a stable container name in launch automation. The collector resolves that
name to the current full Docker ID on every sample, then checks the process's
host cgroup membership. If the name or executable matches no publisher, or more
than one publisher, report unhealthy telemetry rather than guessing.

A read-only inspection requires no boto3 installation or AWS access:

```bash
sudo python3 scripts/publisher_metrics.py --executable /home/npa_publisher --container-name publisher --inspect --samples 3 --interval 2
```

The first sample is a baseline with no CPU reading. Subsequent readings report
`cpu_percent_one_vcpu` and the hottest host thread ID. A CPU reading is user plus
system CPU time divided by monotonic elapsed time, with no CPU-count divisor.
A new/reused thread or process requires a new baseline. Do not treat missing
readings as zero.

## Give the host permission to publish telemetry

The host needs an instance profile with `cloudwatch:PutMetricData` constrained
to namespace `NPA/Publisher`. Attach this policy to the existing publisher EC2
role through the infrastructure that owns that role:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": "cloudwatch:PutMetricData",
    "Resource": "*",
    "Condition": {"StringEquals": {"cloudwatch:namespace": "NPA/Publisher"}}
  }]
}
```

Allow outbound HTTPS to CloudWatch and the package sources used during bootstrap,
or provide regional/private endpoints and a prebuilt collector environment.
Keep credentials in the EC2 instance role, not JSON configuration or the AMI.

The collector resolves `instance_id` and `region` through IMDSv2 when configured
as `auto`. It does not fall back to IMDSv1. Run it on the host so container hop
limits do not affect metadata requests. Explicit identity values are supported
for environments without IMDS; each instance must receive its own instance ID.

## Install the host collector

Create `collector-config.json` with deployment-specific values:

```json
{
  "executable": "/home/npa_publisher",
  "container_name": "publisher",
  "group": "YOUR-PUBLISHER-ASG",
  "instance_id": "auto",
  "region": "auto",
  "ready_file": "/run/npa-metrics/ready.json",
  "ready_ttl": 180,
  "interval": 10
}
```

For a publisher running directly on the host, omit `container_name`. An optional
`pid_file` selects one host PID and still verifies its exact executable.

Install with Python 3.10+ and the platform's venv package available:

```bash
sudo bash scripts/install-thread-metrics.sh collector-config.json
```

The installer copies the collector/config, installs boto3 into an isolated venv,
and enables `npa-thread-metrics.service`. Integrate this installation into the
AMI build or existing launch bootstrap so **every future instance** emits metrics.
Ensure that exactly one collector publishes each instance's metric series; sample
counts are not a substitute for preventing duplicate collectors.

## Integrate actual registration readiness

Readiness must confirm that this process is registered with Netskope, assigned
to the intended applications, and eligible for new connections. Neither process
existence, low CPU, EC2 health, nor ASG `InService` alone establishes that fact.

After the existing lifecycle/verifier confirms eligibility, run on the host:

```bash
sudo /opt/npa-thread-metrics/venv/bin/python /opt/npa-thread-metrics/publisher_metrics.py --config /etc/npa-thread-metrics.json --mark-ready
```

Have the eligibility verifier renew this acknowledgement every 60 seconds **only
while eligibility is still confirmed**, and clear `/run/npa-metrics/ready.json`
when eligibility is lost. Do not use an unconditional cron renewal. The marker
records the selected PID, procfs start time, and verification time; it expires
after 180 seconds and becomes invalid immediately when the publisher restarts.
The marker is an integration interface, not an included Netskope API verifier.
Implement and test the verifier against the tenant's actual supported API/health
contract before enabling scaling. The upstream checked-in Lambda is unsuitable
for this purpose.

The collector emits `HottestThreadCPU` (Percent), `PublisherReady` (Count), and
`CollectorHealthy` (Count) into `NPA/Publisher`, with exactly these dimensions:
`AutoScalingGroupName` and `InstanceId`. Publish every 10 seconds. CPU timestamps
reflect collection time, not eventual SDK completion. Sampling skips missed
intervals instead of generating a burst of catch-up samples.

The controller reads both newest closed one-minute buckets and requires six
health samples per bucket, health/readiness minima of 1, and valid CPU maxima.
A whole or partial telemetry gap, unknown thread baseline, expired readiness,
or delivery delay prevents the instance being treated as usable headroom.
The conservative current implementation inhibits capacity writes until **all**
current healthy InService members have complete ready telemetry; it emits
`MissingTelemetry` so this condition is actionable. A pending/unhealthy member
instead yields `capacity_transition`; inspect ASG lifecycle events and controller
logs for stuck registration. The CapacityTransition alarm fires after 15
consecutive minutes; missing controller heartbeats alarm after three minutes.
Maintain baseline spare capacity while qualifying
telemetry delivery and readiness latency.

## Package and deploy in observation mode

Build the reviewed controller source into a deterministic ZIP and record its
printed SHA-256:

```bash
python3 scripts/package-controller.py dist/controller.zip
```

Upload to a regional bucket owned by the operator. Use a content-addressed key
and do not overwrite it; optionally supply an S3 object version as well:

```bash
aws s3 cp dist/controller.zip s3://YOUR-BUCKET/npa-thread-scaling/CONTROLLER-SHA256.zip
```

Create `controller-parameters.json`:

```json
[
  {"ParameterKey":"AutoScalingGroupName","ParameterValue":"YOUR-PUBLISHER-ASG"},
  {"ParameterKey":"ControllerCodeBucket","ParameterValue":"YOUR-BUCKET"},
  {"ParameterKey":"ControllerCodeKey","ParameterValue":"npa-thread-scaling/CONTROLLER-SHA256.zip"},
  {"ParameterKey":"EnableScaling","ParameterValue":"false"},
  {"ParameterKey":"EarlyCPUThreshold","ParameterValue":"70"},
  {"ParameterKey":"UrgentCPUThreshold","ParameterValue":"80"},
  {"ParameterKey":"SpareCPUThreshold","ParameterValue":"40"},
  {"ParameterKey":"CooldownSeconds","ParameterValue":"300"}
]
```

Require `0 <= spare < early <= urgent <= 100`. The controller rejects inconsistent
thresholds. Supply `AlarmTopicArn` to connect an existing SNS alert topic.

```bash
aws cloudformation create-stack --stack-name publisher-thread-scaling --template-body file://templates/publisher-thread-scaling.yaml --parameters file://controller-parameters.json --capabilities CAPABILITY_IAM
```

Observation mode reads AWS state and writes diagnostic metrics/logs only. It does
not reserve cooldown or change ASG desired capacity. The stack creates only the
controller, retained state, scheduler, log group, role, and alarms.

No real stack deployment, metric publication, role attachment, readiness renewal,
or tenant changes were performed during development of this fork.

## Qualify before enabling capacity writes

Compare thread CPU readings with actual loaded publisher behavior. Verify all
current and newly launched instances provide complete metric windows and fresh
readiness. Confirm that adding publishers causes **new** connections to use them
while existing sessions stay connected. If a cool eligible publisher receives
no new traffic, investigate routing/eligibility rather than raising ASG capacity.

Measure from a threshold crossing through two-period detection, CloudWatch
delivery, the next controller tick, EC2 launch, registration, and the first new
connection on the added publisher. The 70% early rule may take roughly two to
three minutes to detect sustained load, before provisioning time; the urgent
rule still uses a closed minute. If CPU can saturate sooner, lower thresholds,
maintain more baseline publishers, or redesign for shorter metric windows.

Test missing/stale telemetry, registration failure, process/container restart,
retained hot sticky load, ASG maximum, SDK failures, and cooldown contention in
a nonproduction group. Verify alarm delivery and the absence of scale-in.
Pending capacity cannot prove it will become ready; stuck launches need operator
attention, not unlimited replacement launches.

After qualification, update this stack with `EnableScaling=true`. Pause capacity
writes by changing it back to `false`; that preserves the observed publishers
and desired capacity. Preserve DynamoDB cooldown state when replacing the stack.
On stack deletion its state table is retained and needs deliberate cleanup.

AWS references:

- [CloudWatch metric retrieval](https://docs.aws.amazon.com/AmazonCloudWatch/latest/APIReference/API_GetMetricData.html)
- [Desired capacity behavior](https://docs.aws.amazon.com/autoscaling/ec2/APIReference/API_SetDesiredCapacity.html)
- [IMDSv2 identity access](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/configuring-instance-metadata-service.html)
- [Linux thread CPU fields](https://man7.org/linux/man-pages/man5/proc_pid_stat.5.html)

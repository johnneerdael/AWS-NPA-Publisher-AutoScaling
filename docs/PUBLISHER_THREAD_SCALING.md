# Provision and deploy exact publisher thread scaling

## Signal and policy

Measure the actual busiest publisher thread, not EC2 instance averages or a
scaled approximation. Compute 100 times its user+system CPU-time delta divided
by monotonic elapsed time. One fully occupied vCPU is 100%, independent of the
machine's CPU count or scheduler migration. Select exactly one publisher
executable, optionally within a named Docker container; do not monitor the
watchdog, diagnostic server, or entire container as the publisher.

The collector also emits `CPUCoreUtilization` separately for every logical CPU,
with a `Cpu=cpuN` dimension. This measures each CPU's non-idle/non-iowait fraction
from Linux `/proc/stat`; guest counters are excluded from the total because user
and nice already include them. No aggregate CPU row or core average is emitted.
CPU-core inspection and publisher-thread scaling are distinct measurements.

At 70% thread CPU in each of two closed minutes, require one healthy InService
publisher below or equal to 40% in both periods. At 80% in the latest closed
minute, require two. Existing cool publishers count as headroom. Adding enough
headroom then holds capacity even if sticky existing sessions stay hot.

The controller uses normal current ASG `Healthy`/`InService` state and complete
collector health/CPU samples. The ordinary launch workflow must register new
publishers before putting them InService. An already registered healthy publisher
requires no new tenant-specific eligibility verifier or readiness marker.
Publisher process/thread measurement failures produce unhealthy telemetry, not
fabricated zero CPU. ASG health otherwise follows the group's existing checks.

## Official Marketplace AMI

Use the [Netskope Private Access Publisher listing](https://aws.amazon.com/marketplace/pp/prodview-sv7z6ot7sefiy), sold by Netskope, as the image source.
The listing describes Ubuntu 22.04 x86_64. Regional EC2 metadata verified on
2026-10-12 identifies Marketplace version 140.1.0.11337:

| Region | AMI |
|---|---|
| Frankfurt (`eu-central-1`) | `ami-059e32c80fcfbab6e` |
| Northern Virginia (`us-east-1`) | `ami-085f0a08151861720` |

Both share Marketplace product code `25q3w26r1r9gcd4w1k0x888g2`. AMI IDs are
regional; do not use the Virginia identifier in Frankfurt. The public listing's
cached release information can lag the actual regional image metadata.

To select the verified Frankfurt image explicitly in launch-template input:

```bash
python3 scripts/generate-cloud-init.py --group YOUR-PUBLISHER-ASG --ami-id ami-059e32c80fcfbab6e
```

Without `--ami-id`, generated input contains only UserData and inherits the image
from the selected source launch-template version. Marketplace access/subscription
must be available in the deployment account; the generator does not subscribe,
accept terms, or launch an instance. Reverify regional image availability before
provisioning. This verification established image metadata, not a fresh boot of
the image with the collector installed.

## Provision through cloud-init

Run the collector on the EC2 host with procfs access. Keep the publisher binary
and Docker image unchanged. The publisher executable found by read-only inspection
on Frankfurt is `/home/npa_publisher`.

Generate the provisioning payload locally:

```bash
python3 scripts/generate-cloud-init.py --group YOUR-PUBLISHER-ASG
```

Use `--container-name publisher` if launch automation assigns a stable container
name, and `--executable /absolute/publisher/path` for another image. Without a
container selector, exactly one matching executable must exist on the host.
The collector can find the publisher inside Docker from host procfs.

Output files:

- `dist/publisher-user-data.gz`: compressed cloud-config, under EC2's 16 KiB raw
  user-data limit; cloud-init automatically decompresses it.
- `dist/publisher-user-data.yaml`: readable cloud-config for review/schema checking.
- `dist/publisher-user-data.launch-template.json`: already base64-encoded UserData
  for the EC2 launch-template API.

The payload embeds the reviewed collector source, its configuration, and the
systemd unit. Cloud-init installs Python 3/venv and boto3, then enables and starts
the collector. It reads instance identity through IMDSv2 and publishes using the
existing EC2 instance role. No credentials or tenant API tokens are embedded.
Use an Ubuntu/Debian publisher host with Python 3.10+, cloud-init, package-network
access or a prebuilt dependency environment, and outbound HTTPS to CloudWatch.

Inspect existing launch-template user data before changing it. If it is an
existing cloud-config mapping, preserve it by passing the reviewed local file:

```bash
python3 scripts/generate-cloud-init.py --group YOUR-PUBLISHER-ASG --base-cloud-config existing-user-data.yaml
```

The generator preserves existing packages, write_files, bootcmd, registration
runcmd, and other cloud-config keys. It appends collector setup after existing
runcmd and rejects conflicting collector paths or malformed list fields. Existing
runcmd must complete; do not leave a foreground publisher command blocking it.
Script, MIME, and cloud-boothook inputs require integration in their owning
bootstrap rather than treating them as cloud-config or overwriting them.

Resolve the version currently used by the ASG; do not assume an unreferenced
`$Latest` version has the right bootstrap. After reviewing the generated input,
create a launch-template version inheriting
the existing image, instance profile, networking, and other launch configuration:

```bash
aws ec2 create-launch-template-version --launch-template-id YOUR-LAUNCH-TEMPLATE-ID --source-version ACTIVE-ASG-LAUNCH-VERSION --launch-template-data file://dist/publisher-user-data.launch-template.json
```

Then select that returned version for future ASG launches:

```bash
aws autoscaling update-auto-scaling-group --auto-scaling-group-name YOUR-PUBLISHER-ASG --launch-template LaunchTemplateId=YOUR-LAUNCH-TEMPLATE-ID,Version=NEW-VERSION
```

Use the IaC resource that owns the launch template/group when available, so its
configuration remains authoritative. If the ASG uses a launch configuration or
mixed-instances launch structure, integrate the same UserData through that
owner; the simple command above applies to a direct launch-template ASG.
Updating user data does not install the collector on running instances. Existing
instances must be brought onto the provisioning version through planned drained
replacement. Keep the controller in observation mode until current members
publish complete data. This work generates artifacts only; it does not run the
AWS mutation commands or replace/restart the active publisher.

## Host metric permissions

Supply `PublisherInstanceRoleName` to the controller stack: the existing IAM role
in the publisher EC2 instance profile, not the instance profile's name or ARN.
The template attaches permission for only `cloudwatch:PutMetricData` in namespace
`NPA/Publisher`. Host credentials remain in the EC2 role. Controller permissions
are separate and limited to observations, its state, and the chosen ASG capacity.

The collector sends `HottestThreadCPU` and `CollectorHealthy` with dimensions
`AutoScalingGroupName` and `InstanceId` every 10 seconds. Individual CPU metrics
add `Cpu`. Use exactly one collector per publisher. Preserve collection timestamps
across SDK delays; missed intervals are skipped, never filled with catch-up bursts.
A new/reused thread requires a baseline before a full hottest-thread reading.

The controller requires six health samples in each of both newest closed minutes,
health minima of 1, and valid CPU maxima. Missing, partial, or delayed data inhibits
capacity requests and emits `MissingTelemetry`. Initial boot and process restart
therefore need complete metric windows before counting as usable headroom.

## Package and deploy in observation mode

Build and record the SHA-256 of the reviewed Lambda source:

```bash
python3 scripts/package-controller.py dist/controller.zip
```

Upload to an operator-owned regional bucket under an immutable key:

```bash
aws s3 cp dist/controller.zip s3://YOUR-BUCKET/npa-thread-scaling/CONTROLLER-SHA256.zip
```

Create `controller-parameters.json`:

```json
[
  {"ParameterKey":"AutoScalingGroupName","ParameterValue":"YOUR-PUBLISHER-ASG"},
  {"ParameterKey":"PublisherInstanceRoleName","ParameterValue":"YOUR-PUBLISHER-EC2-ROLE"},
  {"ParameterKey":"ControllerCodeBucket","ParameterValue":"YOUR-BUCKET"},
  {"ParameterKey":"ControllerCodeKey","ParameterValue":"npa-thread-scaling/CONTROLLER-SHA256.zip"},
  {"ParameterKey":"EnableScaling","ParameterValue":"false"},
  {"ParameterKey":"EarlyCPUThreshold","ParameterValue":"70"},
  {"ParameterKey":"UrgentCPUThreshold","ParameterValue":"80"},
  {"ParameterKey":"SpareCPUThreshold","ParameterValue":"40"},
  {"ParameterKey":"CooldownSeconds","ParameterValue":"300"}
]
```

Supply `AlarmTopicArn` to use an existing SNS alert topic. Optionally pin the S3
artifact version with `ControllerCodeVersion`.

```bash
aws cloudformation create-stack --stack-name publisher-thread-scaling --template-body file://templates/publisher-thread-scaling.yaml --parameters file://controller-parameters.json --capabilities CAPABILITY_IAM
```

The controller observes and emits diagnostics by default. It does not reserve
cooldown or change desired capacity until `EnableScaling=true`. The stack attaches
host metric permission but does not install software on existing instances.

Use an instance-count ASG, one publisher per instance, with a working ordinary
launch/registration flow. The controller must be the sole desired-capacity writer;
it rejects other scaling policies and scheduled actions. Pause it for manual
capacity changes. A recheck before writing reduces races, but AWS provides no
conditional capacity update. Do not attach two controllers to the same ASG.

## Qualification and operation

Validate cloud-init on a fresh publisher launch, including role permission,
collector startup, exact process selection, registration, complete metrics, and
new-client distribution. Measure detection plus launch-to-ready time. The early
rule uses two closed minutes and the urgent rule one; lower thresholds or keep
more baseline capacity if saturation can occur sooner than provisioning.

Test restart, missing/sparse telemetry, stuck launches, ASG maximum, cooldown,
and SDK failures. Missing heartbeats alert after three minutes; a capacity
transition stuck for 15 minutes alerts separately. At capacity limits, investigate
traffic and capacity rather than assuming an idle second core supplies headroom.

When evidence supports the chosen thresholds, update with `EnableScaling=true`.
Return to false to pause capacity writes. Existing sticky connections remain on
their publisher; new capacity cannot move them or prevent their traffic increasing.
Use explicit drained maintenance for scale-in. Removing the controller retains
its state table, which needs deliberate cleanup when retired.

A read-only Frankfurt check sampled the selected Docker publisher while idle.
The generated cloud-config also passed that host's cloud-init 26.1 schema validator
using an anonymous memory file, without running provisioning commands. No software
installation, instance monitoring change, publisher restart, or AWS/tenant
configuration change was performed. Fresh-boot and loaded behavior remain
qualification steps rather than claims established by the idle/schema checks.

References:

- [EC2 user data](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/user-data.html)
- [Cloud-init user-data formats](https://docs.cloud-init.io/en/latest/explanation/format/index.html)
- [Linux thread CPU fields](https://man7.org/linux/man-pages/man5/proc_pid_stat.5.html)
- [AWS capacity write behavior](https://docs.aws.amazon.com/autoscaling/ec2/APIReference/API_SetDesiredCapacity.html)

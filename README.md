# Preventive NPA publisher thread autoscaling

Add capacity before the busiest publisher thread saturates one vCPU. Measure
actual publisher thread CPU directly; never use instance-wide CPU averages,
fleet averages, or a multiplier to infer a busy core.

Cloud-init provisions the host collector automatically on new publishers. The
publisher binary and Docker image need no changes. Existing registration and ASG
health remain in use, with no separate tenant eligibility checker or readiness
marker protocol.

| Default condition | Required headroom |
|---|---|
| One publisher reaches 70% in each of two closed minutes | One healthy InService publisher at or below 40% |
| Any publisher reaches 80% in the latest closed minute | Two healthy InService publishers at or below 40% |
| Enough cool publishers already exist | Hold capacity despite persistent sticky load |
| Capacity is launching, metrics are incomplete, or cooldown is active | Hold and report the reason |
| Required headroom exceeds the ASG maximum | Add only permitted capacity and alert |

All thresholds represent actual CPU time of the busiest publisher thread relative
to **one vCPU**. An idle second vCPU never dilutes this signal. Thread migration
between CPUs does not dilute it either. The collector also publishes each logical
CPU separately as `CPUCoreUtilization`, with `Cpu=cpu0`, `Cpu=cpu1`, etc.; those
individual CPU measurements are inspection metrics, not averages.

Existing sticky clients remain connected; new clients can use added publishers.
Maintain headroom rather than repeatedly launching idle instances while existing
sticky sessions stay hot. Tune thresholds against measured CPU growth and
launch-to-ready time, and confirm that new clients spread to added publishers.

## Provision and deploy

Read [deployment and cloud-init integration](docs/PUBLISHER_THREAD_SCALING.md).
Use the official Netskope Marketplace AMI; version 140.1.0.11337 was verified as
`ami-059e32c80fcfbab6e` in Frankfurt and `ami-085f0a08151861720` in Virginia.
The supported controller template is `templates/publisher-thread-scaling.yaml`.
It grants the existing publisher instance role permission to publish the metrics.

Generate cloud-init user data from the reviewed collector source:

```bash
python3 scripts/generate-cloud-init.py --group YOUR-PUBLISHER-ASG
```

The generator creates compressed EC2 user data and launch-template JSON under
`dist/`. It embeds the collector, JSON configuration, and systemd unit; cloud-init
installs dependencies and starts collection at boot. Supply `--base-cloud-config`
to preserve an existing cloud-config registration/bootstrap. Never replace
unexamined script or MIME user data with this payload.

The controller attaches to an existing ASG with a working launch/registration
workflow. It defaults to observation-only, uses durable cooldown and launch
controls, and performs scale-out only. It rejects competing policies/schedules
and requires exclusive ownership of desired-capacity writes. Remove publishers
through explicit drained maintenance.

## Development and validation

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/cfn-lint templates/publisher-thread-scaling.yaml
python3 scripts/package-controller.py dist/controller.zip
```

Tests cover exact single-vCPU units, per-CPU separation, busiest-thread selection,
thread/process restart and migration, Docker selection, missing/sparse telemetry,
sticky headroom, cooldown, AWS failures, and capacity limits. Cloud-init tests
verify source embedding, preserved bootstrap, and the EC2 user-data size limit.
CI also exercises real Linux procfs and checks packaging, formatting, lint,
security, secrets, and the template.

Read-only Frankfurt inspection identified `/home/npa_publisher` in Docker, and
sampling worked while it was idle. The generated user data passed the installed
cloud-init schema validator using an anonymous memory file, without executing
provisioning commands. No collector installation, publisher restart, monitoring
change, AWS deployment, or tenant change was performed on that host. Fresh-instance
provisioning, loaded behavior, and traffic distribution still need qualification
before capacity writes are enabled. See [the design](docs/PUBLISHER_THREAD_SCALING_DESIGN.md).

## Upstream implementation

The original `templates/npa-publisher-autoscaling.yaml` and its documentation
remain historical reference. Its 70% `ASGAverageCPUUtilization` target on two-vCPU
instances can miss single-thread saturation. The checked-in lifecycle Lambda/ZIP
forwards CloudWatch logs rather than implementing the advertised registration
workflow. The original template references a separately hosted deployment ZIP
whose contents are unverified here and has a baseline cfn-lint E3712 bootstrap
issue. This controller requires an existing working registration workflow.

## License

Apache-2.0, inherited from [netskopeoss/AWS-NPA-Publisher-AutoScaling](https://github.com/netskopeoss/AWS-NPA-Publisher-AutoScaling).

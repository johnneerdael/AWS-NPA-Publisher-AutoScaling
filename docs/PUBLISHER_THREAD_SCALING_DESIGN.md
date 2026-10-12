# Preventive publisher thread autoscaling

## Purpose and accepted constraints

Prevent outages caused by the busiest publisher saturating its traffic-handling
thread. Measure the busiest publisher thread relative to one vCPU. Ignore spare
cores and fleet-average CPU as scaling triggers. Keep sticky sessions on their
current publisher and provision headroom for new connections before saturation.
Use 70% as the early threshold and 80% as the urgent threshold, configurable.

## Architecture

Attach a new, scale-out-only controller to an existing, verified publisher Auto
Scaling group. Keep publisher registration, app assignment, and termination in
the existing lifecycle integration. The upstream checked-in Lambda source and
ZIP do not implement the advertised registration workflow; do not silently
reuse them or invent unverified Netskope API contracts.

A Linux sidecar samples each thread under /proc for exactly one explicitly
selected publisher executable. Compute 100 * delta(user + system CPU seconds)
/ delta(monotonic seconds), with no division by CPU count. Publish the hottest
thread CPU, collector health, and registration readiness per instance every
10 seconds. Require six health samples in each closed minute, never fabricated
zeros or bursts of catch-up samples. Unknown new-thread baselines are unhealthy. Bind readiness to the selected process PID and start time; mark it
only after the lifecycle integration has verified Netskope registration and
application eligibility. Expire the proof after 180 seconds and renew from an
actual eligibility verifier every 60 seconds. Require re-marking after restart.

A scheduled Lambda runs every minute. Read only current ASG members and complete
one-minute CloudWatch periods; reject stale, missing, invalid, or unready samples
as usable headroom. At 70% reached in each of two periods, maintain at least one
ready publisher below 40% CPU. At 80% for one period, maintain at least two.
The highest individual publisher CPU triggers the rule, never an average.
Existing cool eligible publishers count as headroom; this includes currently
eligible publishers, not just instances newly launched by the controller. Persistent hot sticky
publishers do not trigger more launches while sufficient headroom exists.

Suppress additional requests while capacity is launching, telemetry/readiness
is incomplete, or a durable cooldown is active. Use DynamoDB conditional state
updates and Lambda reserved concurrency to serialize capacity requests. Respect
ASG maximum capacity and emit diagnostic metrics for missing telemetry and
insufficient headroom at the limit. Reject existing scaling policies and scheduled actions so an old
average-CPU scale-in policy cannot undo the new controller. Re-read capacity after reserving cooldown and cancel if it changed. Require the
controller to be the exclusive capacity writer; AWS has no conditional capacity
update. Never intentionally reduce desired capacity. Deployment defaults to observation-only until integration is verified.

## Scope and limits

Do not deploy to AWS or change tenant applications in this work. Deliver source,
a CloudFormation add-on, local installation and packaging tools, tests, CI, and
an operator guide in the fork. Do not claim registration, routing, latency,
load handling, or session draining has been production-qualified. Traffic
must reach cool publishers for spare capacity to help. CPU thresholds require
qualification against actual launch-to-ready time and CPU growth. Scale-in
remains an explicit drained maintenance operation.

## Implementation units

- [x] Collector: scripts/publisher_metrics.py, scripts/install-thread-metrics.sh,
      requirements.txt, tests/test_publisher_metrics.py. Test first: single-core
      units, hottest thread selection, CPU migration independence, process and
      thread restart, missing/ambiguous processes, readiness process binding,
      no fabricated zero CPU on read errors, metric dimensions.
- [x] Controller: scripts/controller/scaling_controller.py,
      tests/test_scaling_controller.py. Test first: busiest publisher triggers
      despite low average, 70/80 persistence, available spare suppression,
      pending/readiness/freshness gating, cooldown/concurrency, maximum capacity,
      dry run, conflicting policy rejection, SDK adapter calls and error paths.
- [x] Infrastructure: templates/publisher-thread-scaling.yaml,
      tests/test_thread_scaling_template.py. Integrate scheduled controller,
      scoped IAM, retained state, alarms, safe defaults, artifact inputs.
- [x] Integration: README and deployment guide, artifact packaging, enforce
      tests/lint/security/template checks in CI. Verify a synthetic Linux
      publisher process and review the complete change before publishing.

## Verification

Run Python unit and adapter tests, Linux collector smoke tests in CI, cfn-lint,
Black/Flake8/Bandit, secret detection, and git diff --check. Inspect the resulting
artifact and deployment parameters. Production acceptance requires real AWS
metrics, verified publisher eligibility, new-connection distribution, fault
injection, and measured provisioning latency before enabling scaling.

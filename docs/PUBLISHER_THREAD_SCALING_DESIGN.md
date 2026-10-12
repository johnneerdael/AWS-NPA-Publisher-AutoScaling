# Preventive exact publisher CPU autoscaling

## Requirements

Prevent outages from the busiest publisher saturating its traffic-handling
thread. Use direct thread CPU relative to one vCPU at 70% early and 80% urgent.
Never use two-core or fleet averages, EC2 instance CPU, or CPU-count multiplication
to infer the signal. Keep existing sticky clients connected and add headroom
for new connections. Publish logical CPU readings separately for inspection.

Provision host metric collection through cloud-init during publisher launch.
Change no publisher binary or Docker image. Use the existing launch/registration
workflow and ordinary ASG health; require no separate tenant-specific eligibility
checker, readiness marker, or expiring eligibility proof.

## Components

A Linux collector selects exactly one publisher executable on the host,
optionally restricted to a Docker container. Sample each current thread's
user/system CPU counters against monotonic time. A new or reused thread needs a
baseline; missing or invalid measurements never become zero CPU. Per-CPU Linux
counters are separate observations and never averaged. Send actual hottest-thread
CPU and collector health every 10 seconds, preserving timestamps and skipping
missed intervals rather than publishing catch-up bursts.

A scheduled Lambda checks current ASG members and both newest closed minutes.
Require six health samples per minute, healthy measurement minima, and CPU maxima.
At 70% reached in both minutes, maintain one healthy InService publisher below 40%.
At 80% in the latest minute, maintain two. Existing cool publishers count as
headroom; sticky hot sessions cannot cause unlimited launches while that headroom
exists. Use normal existing launch/registration gates before InService.

Suppress requests during launch/replacement, incomplete telemetry, and durable
cooldown. Serialize writes, honor the maximum, reject other policies/schedules,
and recheck desired/min/max before the write. Require exclusive capacity ownership
because AWS offers no compare-and-set operation. Never intentionally scale in.
Emit diagnostics and alarms for missing data, controller failure, stuck capacity
transition, heartbeat loss, urgent CPU, and insufficient capacity at the limit.

Cloud-init embeds the reviewed collector, JSON config, and systemd unit. Install
runtime dependencies and start metrics at boot. Generate deterministic compressed
EC2 user data, enforce the raw size limit, preserve existing cloud-config keys
and bootstrap commands, and reject path/list conflicts. Expose launch-template
JSON without silently replacing existing script or MIME bootstrap.

CloudFormation deploys the controller against an existing ASG and attaches a
namespace-scoped publishing policy to the existing publisher instance role.
Default to observation mode. No AWS mutation or guest installation is performed
as part of source development.

## Implementation and verification

- [x] Collector: exact busiest-thread units, Docker/process selection, restart and
      migration behavior, independent per-CPU observations, sparse/error handling.
- [x] Controller: busiest-publisher thresholds, sticky headroom, normal ASG health,
      complete sample counts, launch transitions, limits, cooldown and writer races.
- [x] Infrastructure: namespace-scoped host permission, scoped controller IAM,
      retained state, scheduler, alarms, source artifact inputs, safe default.
- [x] Provisioning: cloud-init source embedding, preserved bootstrap, service startup,
      deterministic gzip/launch-template JSON, raw EC2 user-data limit.
- [x] Validation: Python and SDK adapter tests, Linux procfs CI, CloudFormation lint,
      formatting, security, secret scans, shell syntax, packaging and source review.

Read-only Frankfurt checks establish idle process-layout compatibility and
cloud-init schema acceptance, not fresh-boot or loaded qualification. Before
capacity writes, verify actual provisioning, registration, current fleet metrics,
new-client distribution, detection/provisioning latency, and failure behavior.

> Historical upstream reference. Use [the thread-scaling guide](PUBLISHER_THREAD_SCALING.md) for this fork’s supported add-on. The original lifecycle bundle is unverified and its average-CPU policy can miss a saturated publisher thread.

# High Availability Strategies for Netskope Private Access Publishers in AWS

## Table of Contents
- [Overview](#overview)
- [Core HA Principles](#core-ha-principles)
- [AWS-Specific HA Strategies](#aws-specific-ha-strategies)
- [Capacity Planning and Scaling](#capacity-planning-and-scaling)
- [Network Requirements](#network-requirements)
- [Deployment Best Practices](#deployment-best-practices)
- [Example Architectures](#example-architectures)
- [Monitoring and Maintenance](#monitoring-and-maintenance)
- [References](#references)

---

## Overview

Netskope Private Access (NPA) Publishers provide secure, zero-trust network access to private applications and resources. Deploying Publishers with high availability (HA) in AWS ensures continuous access to critical applications even during infrastructure failures, maintenance windows, or unexpected outages.

This document outlines proven HA strategies specifically for AWS deployments, based on Netskope's official documentation and AWS best practices.

---

## Core HA Principles

### Minimum Redundancy Requirement

**Deploy at least a pair of Publishers for each Private App** to provide high-availability access.[^1]

This is the fundamental HA strategy recommended by Netskope. The NPA architecture automatically distributes traffic across available Publishers without requiring additional configuration.

### Key HA Characteristics

- **Automatic Load Distribution**: NPA automatically distributes user connections across all healthy Publishers assigned to a Private App
- **Transparent Failover**: If a Publisher becomes unavailable, new connections automatically route to healthy Publishers
- **Stateless Architecture**: Publishers don't maintain session state, enabling seamless failover
- **User-Agnostic**: Publishers are agnostic to the number of users traversing them[^2]

---

## AWS-Specific HA Strategies

### 1. Multi-Availability Zone (Multi-AZ) Deployment

**Strategy**: Deploy Publishers across multiple AWS Availability Zones within a region.

**Benefits**:
- Protection against AZ-level failures
- Improved latency by placing Publishers closer to applications
- Compliance with zone redundancy requirements

**Implementation**:
```
Region: us-east-1
├─ Availability Zone: us-east-1a
│  ├─ Publisher-1 (t3.medium)
│  └─ Publisher-2 (t3.medium)
│
└─ Availability Zone: us-east-1b
   ├─ Publisher-3 (t3.medium)
   └─ Publisher-4 (t3.medium)
```

**Minimum Recommendation**:
- **2 AZs** with at least **1 Publisher per AZ** (total: 2 Publishers)
- **Production Environments**: **2 AZs** with **2 Publishers per AZ** (total: 4 Publishers)

### 2. VPC and Subnet Architecture

**Strategy**: Deploy Publishers in private subnets across multiple AZs with proper network connectivity.

**Key Considerations**:
- Publishers should be deployed in the **same VPC** as the applications when possible[^3]
- Publishers require **L3 reachability** to Private Apps (can be in different VPCs/networks)
- Use **private subnets** with NAT Gateway for outbound internet access
- Ensure Publishers can reach both internal apps and Netskope cloud services

**Network Topology**:
```
VPC: 10.0.0.0/16
│
├─ AZ: us-east-1a
│  ├─ Private Subnet: 10.0.1.0/24 (Publishers)
│  ├─ Private Subnet: 10.0.2.0/24 (Applications)
│  └─ Public Subnet: 10.0.3.0/24 (NAT Gateway)
│
└─ AZ: us-east-1b
   ├─ Private Subnet: 10.0.11.0/24 (Publishers)
   ├─ Private Subnet: 10.0.12.0/24 (Applications)
   └─ Public Subnet: 10.0.13.0/24 (NAT Gateway)
```

### 3. Instance Configuration

**EC2 Instance Requirements**[^2]:

| Component | Specification |
|-----------|---------------|
| **Instance Type** | t3.medium (minimum) |
| **Architecture** | x86_64 |
| **vCPUs** | 2 |
| **Memory** | 4 GB RAM |
| **Storage** | 16 GB HDD |
| **OS** | Ubuntu 22.04 LTS |

**Important Notes**:
- **CentOS-based Publishers are deprecated** - migrate to Ubuntu-based Publishers[^3]
- Use **AWS Marketplace AMI** for simplified deployment[^3]
- Azure Publishers require **30GB HDD** instead of 16GB

### 4. Auto Scaling Groups (Optional)

While Publishers don't natively support AWS Auto Scaling Groups, you can implement a semi-automated approach:

**Manual Scaling Pattern**:
1. Deploy initial Publisher pool across AZs
2. Monitor capacity metrics (throughput, connections)
3. Add Publishers manually when approaching limits
4. Reboot existing Publishers during maintenance window to redistribute load[^2]

---

## Capacity Planning and Scaling

### Publisher Capacity Limits[^2]

| Metric | Limit per Publisher |
|--------|---------------------|
| **Throughput** | ~500 Mbps |
| **Concurrent TCP/UDP Connections** | 32,000 per IP destination |
| **Max Publishers per Tenant** | 1,000 (default: 100) |

### User Capacity by Application Type[^2]

The number of concurrent users a Publisher can support depends on the application type:

| Application Type | Connections per User | Max Concurrent Users |
|------------------|----------------------|----------------------|
| **Web Applications** | 6 (browser limit) | ~5,333 users |
| **SSH/SQL** | 1 | ~32,000 users |
| **FTP** | 2 | ~16,000 users |

### Scaling Strategy

**When to Add Publishers**:
- Approaching **80% of throughput capacity** (400 Mbps)
- Approaching **80% of connection capacity** (25,600 connections)
- Latency increases or performance degradation observed
- Adding new Private Apps with significant user base

**How to Scale**:
1. Deploy new Publisher instances in additional AZs or existing AZs
2. Assign new Publishers to the same Private Apps
3. During maintenance window, **reboot original Publishers** to redistribute users evenly[^2]
4. Verify even distribution via Netskope UI

**Formula for Publisher Count**:
```
Required Publishers = CEILING(
    MAX(
        Total Users / Users per Publisher,
        Total Throughput Mbps / 500 Mbps,
        Total Connections / 32000
    )
)
```

Add **+100% for HA** (multiply by 2) and **+20% for growth buffer** (multiply by 1.2).

---

## Network Requirements

### Required Outbound Access[^2]

Publishers require outbound HTTPS (TCP 443) access to:

| Destination | Purpose |
|-------------|---------|
| `gateway.npa.<tenant-domain>` | NPA Gateway |
| `stitcher.npa.<tenant-domain>` | Publisher Control Plane |
| `ns-<tenant-ID>.<POP-name>.npa.<tenant-domain>` | Management Plane |
| `addon-<customer-tenant-url>` | Feature flags, IDP enrollment |
| `*.docker.com`, `*.docker.io` | Publisher updates (container images) |
| `docker-images-prod.6aa30f8b08e16409b46e0173d6de2f56.r2.cloudflarestorage.com` | Publisher updates (R2 storage) |
| `*.ubuntu.com` | OS updates |
| `gateway.gslb.goskope.com` | GSLB gateway |
| `dns.google` (8.8.8.8, 8.8.4.4) | DNS-over-HTTPS for datacenter discovery |

### Firewall Rules[^2]

**Inbound**:
- **SSH (TCP 22)**: From admin subnets only (for management)

**Outbound**:
- **DNS (UDP 53)**: To DNS resolvers
- **HTTPS (TCP 443)**: To destinations listed above
- **HTTP (TCP 80)**: To `*.ubuntu.com` (OS updates)
- **Application Ports**: TCP/UDP ports required by Private Apps

### AWS Security Groups

**Publisher Security Group (Inbound)**:
```
Rule 1: SSH
- Protocol: TCP
- Port: 22
- Source: Admin CIDR (e.g., 10.0.0.0/16)
- Description: SSH access for management

Rule 2: Application Traffic (if applicable)
- Protocol: TCP/UDP
- Port: App-specific
- Source: Application Security Group
- Description: App-to-Publisher communication
```

**Publisher Security Group (Outbound)**:
```
Rule 1: HTTPS
- Protocol: TCP
- Port: 443
- Destination: 0.0.0.0/0
- Description: Netskope cloud services

Rule 2: DNS
- Protocol: UDP
- Port: 53
- Destination: VPC DNS or 8.8.8.8/32, 8.8.4.4/32
- Description: DNS resolution

Rule 3: Application Access
- Protocol: TCP/UDP
- Port: App-specific
- Destination: Application Security Group
- Description: Publisher-to-App communication
```

### DNS Configuration[^2]

Publishers require DNS resolution for:
- **Internal apps**: `myapp.example.com`
- **Netskope services**: `*.npa.<tenant-domain>`
- **Public endpoints**: Docker, Ubuntu mirrors

**Recommended**:
- Use **VPC DNS** (Amazon Route 53 Resolver) for internal resolution
- Ensure **public DNS access** (8.8.8.8, 8.8.4.4) for external resolution
- If using custom DNS, ensure it can resolve both internal and external names

---

## Deployment Best Practices

### 1. Use AWS Marketplace AMI[^3]

**Benefits**:
- Pre-configured Ubuntu 22.04 LTS environment
- Optimized for NPA workloads
- Simplified deployment process
- Regular updates via AWS Marketplace

**Deployment Steps**:
1. Navigate to AWS Marketplace and search for "Netskope Private Access Publisher"
2. Launch EC2 instance with AMI
3. Select **t3.medium** instance type
4. Configure VPC, subnet, and security groups
5. (Optional) Add Publisher registration token to User Data for automated registration
6. Launch instance
7. Verify "Connected" status in Netskope UI

### 2. Automate Publisher Registration

**User Data Script** (optional, not required for AWS China):
```bash
#!/bin/bash
# Publisher token (passed during instance launch)
YOUR_PUBLISHER_TOKEN
```

Add token to **Advanced Details > User Data** during instance launch.[^3]

**Alternative**: SSH to Publisher and run:
```bash
sudo ./npa_publisher_wizard -token <YOUR_TOKEN>
```

### 3. Enable Auto-Updates[^3]

- Configure **Auto-Update Profiles** in Netskope UI
- Assign profile during Publisher creation
- Schedules automatic Publisher software updates
- Reduces maintenance overhead

**Path**: Settings > Security Cloud Platform > Publishers > New Publisher > Select Auto-Update Profile

### 4. Implement Tagging Strategy

Use AWS tags for Publisher management:

```
Name: npa-publisher-use1a-001
Environment: production
Application: erp-system
AZ: us-east-1a
ManagedBy: netskope
AutoUpdate: enabled
```

### 5. Monitor Publisher Health[^3]

**Netskope UI Monitoring**:
- Navigate to **Settings > Security Cloud Platform > Publishers**
- Verify **Status: Connected**
- Check **Version** for update compliance
- Review **Connected Apps** count

**AWS CloudWatch Integration** (manual setup):
- Monitor EC2 instance metrics (CPU, network, disk)
- Set up CloudWatch alarms for instance health
- Create custom metrics for Publisher-specific monitoring

### 6. Disaster Recovery Planning

**Backup Strategy**:
- Publishers are **stateless** - no data backup required
- Document Publisher configuration (VPC, subnets, security groups)
- Store registration tokens securely (AWS Secrets Manager)
- Maintain runbooks for rapid deployment

**Recovery Process**:
1. Launch new Publisher instances from AMI
2. Configure networking (VPC, subnets, security groups)
3. Register with stored tokens
4. Assign to Private Apps
5. Verify connectivity

**RTO/RPO**:
- **RTO**: 10-15 minutes (time to launch and register new Publisher)
- **RPO**: N/A (stateless architecture)

---

## Example Architectures

### Architecture 1: Basic HA (Small Deployment)

**Use Case**: Small organization, single application, 100-500 users

```
AWS Region: us-east-1
│
├─ AZ: us-east-1a
│  └─ Publisher-1 (t3.medium)
│     └─ Private Subnet: 10.0.1.0/24
│
└─ AZ: us-east-1b
   └─ Publisher-2 (t3.medium)
      └─ Private Subnet: 10.0.11.0/24

Capacity:
- Throughput: 1,000 Mbps
- Users: ~10,000 concurrent (web apps)
- Failover: Single AZ failure tolerated
```

### Architecture 2: Production HA (Medium Deployment)

**Use Case**: Mid-size organization, multiple applications, 1,000-5,000 users

```
AWS Region: us-east-1
│
├─ AZ: us-east-1a
│  ├─ Publisher-1 (t3.medium) - Private Subnet: 10.0.1.0/24
│  └─ Publisher-2 (t3.medium) - Private Subnet: 10.0.1.0/24
│
├─ AZ: us-east-1b
│  ├─ Publisher-3 (t3.medium) - Private Subnet: 10.0.11.0/24
│  └─ Publisher-4 (t3.medium) - Private Subnet: 10.0.11.0/24
│
└─ AZ: us-east-1c
   └─ Publisher-5 (t3.medium) - Private Subnet: 10.0.21.0/24

Capacity:
- Throughput: 2,500 Mbps (5 × 500 Mbps)
- Users: ~26,000 concurrent (web apps)
- Failover: Multi-AZ failure tolerated (60% capacity remains with 2 AZ failures)
```

### Architecture 3: Enterprise HA (Large Deployment)

**Use Case**: Large enterprise, many applications, 10,000+ users, global workforce

```
Primary Region: us-east-1
│
├─ AZ: us-east-1a
│  ├─ Publisher Pool 1: 4× t3.medium (App Group 1)
│  └─ Publisher Pool 2: 4× t3.medium (App Group 2)
│
├─ AZ: us-east-1b
│  ├─ Publisher Pool 1: 4× t3.medium (App Group 1)
│  └─ Publisher Pool 2: 4× t3.medium (App Group 2)
│
└─ AZ: us-east-1c
   ├─ Publisher Pool 1: 4× t3.medium (App Group 1)
   └─ Publisher Pool 2: 4× t3.medium (App Group 2)

DR Region: us-west-2 (Standby)
├─ AZ: us-west-2a (2× t3.medium per app group)
└─ AZ: us-west-2b (2× t3.medium per app group)

Capacity (Primary):
- Total Publishers: 24
- Throughput: 12,000 Mbps
- Users: ~64,000 concurrent (web apps)
- Failover: Geographic redundancy with DR region
```

### Architecture 4: Hybrid Cloud HA

**Use Case**: Hybrid deployment, applications in AWS and on-premises

```
AWS Region: us-east-1
├─ AZ: us-east-1a
│  └─ Publisher-AWS-1 (t3.medium) → AWS Apps (VPC)
│
└─ AZ: us-east-1b
   └─ Publisher-AWS-2 (t3.medium) → AWS Apps (VPC)

On-Premises Data Center
├─ Publisher-DC-1 (VMware ESXi) → On-Prem Apps
└─ Publisher-DC-2 (VMware ESXi) → On-Prem Apps

Configuration:
- AWS Publishers: Access AWS-hosted apps (RDS, EC2 apps)
- On-Prem Publishers: Access data center apps (mainframes, databases)
- All Publishers: Registered to same tenant, separate Private App definitions
```

---

## Monitoring and Maintenance

### Health Monitoring

**Netskope UI Checks**:
- **Status**: Should be "Connected" (green)
- **Version**: Should match latest or approved version
- **Last Seen**: Should be within last few minutes
- **Connected Apps**: Verify correct app assignments

**AWS CloudWatch Metrics**:
- **CPUUtilization**: Should be < 80%
- **NetworkIn/NetworkOut**: Monitor for anomalies
- **StatusCheckFailed**: Should be 0
- **DiskWriteBytes**: Ensure adequate disk space

### Maintenance Windows

**Publisher Updates**:
- Configure **Auto-Update Profiles** with appropriate maintenance windows
- Netskope updates Publishers in rolling fashion to maintain availability
- Monitor update status in Netskope UI after maintenance window

**Manual Maintenance**:
1. Identify Publishers needing maintenance
2. Verify at least **N-1 Publishers** remain healthy (where N = total Publishers)
3. Perform maintenance on **one Publisher at a time**
4. Verify "Connected" status before proceeding to next Publisher
5. Reboot if necessary to apply OS-level updates

### Troubleshooting

**Publisher Not Connecting**:
1. Check security group rules (outbound HTTPS, DNS)
2. Verify DNS resolution (`nslookup stitcher.npa.<tenant-domain>`)
3. Check Publisher logs: `/var/log/npa_publisher.log`[^4]
4. Verify registration token is valid
5. Ensure NTP is synchronized

**Performance Degradation**:
1. Check CPU/memory/disk usage on EC2 instance
2. Verify network throughput isn't maxed (500 Mbps limit)
3. Check connection count (32K limit per destination)
4. Add more Publishers if approaching capacity limits

**Uneven Load Distribution**:
1. Reboot Publishers during maintenance window[^2]
2. Verify all Publishers have same version
3. Check Publisher health status
4. Ensure all Publishers assigned to same Private App

---

## References

[^1]: Netskope Documentation - Deploy a Publisher
https://docs.netskope.com/en/deploy-a-publisher/
*"Using at least a pair of Publishers for each Private App is recommended so they can provide high-availability access."*

[^2]: Netskope Documentation - Requirements and Recommendations
https://docs.netskope.com/en/requirements-and-recommendations/
*Publisher capacity, sizing, and scaling guidelines*

[^3]: Netskope Documentation - Create a New Publisher
https://docs.netskope.com/en/create-a-new-publisher/
*AWS EC2 deployment instructions, AMI configuration, and registration process*

[^4]: Netskope Documentation - Publisher Logs for Troubleshooting
https://docs.netskope.com/en/publisher-logs-for-troubleshooting/
*Publisher troubleshooting and log analysis*

---

## Additional Resources

- **AWS Well-Architected Framework**: https://aws.amazon.com/architecture/well-architected/
- **Netskope Support**: https://support.netskope.com/
- **Netskope Community**: https://community.netskope.com/
- **AWS Marketplace - Netskope Private Access Publisher**: Search "Netskope Private Access Publisher" in AWS Marketplace
- **NewEdge IP Ranges for Allowlisting**: https://support.netskope.com/s/article/NewEdge-Consolidated-List-of-IP-Range-for-Allowlisting

---

## Document Information

**Version**: 1.0
**Last Updated**: 2025-12-07
**Author**: Generated from Netskope Official Documentation
**Maintained By**: Infrastructure Team

**Change Log**:
- 2025-12-07: Initial document creation with HA strategies for AWS NPA Publishers

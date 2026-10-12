> Historical upstream reference. Use [the thread-scaling guide](PUBLISHER_THREAD_SCALING.md) for this fork’s supported add-on. The original lifecycle bundle is unverified and its average-CPU policy can miss a saturated publisher thread.

# DevOps Notes
## Netskope Private Access Publisher with Amazon EC2 Auto Scaling

**Version:** 1.0
**Last Updated:** 2025-12-16

---

## Business Problem Statement

Organizations deploying Netskope Private Access (NPA) face several operational challenges when managing NPA Publishers in AWS environments:

1. **Static Capacity Planning:** Fixed number of publisher instances cannot adapt to fluctuating user demand
2. **Cost Inefficiency:** Over-provisioning to handle peak loads results in unnecessary costs during low-traffic periods
3. **Manual Scaling:** Adding or removing publishers requires manual intervention and API calls to Netskope
4. **High Availability Complexity:** Maintaining publisher redundancy across Availability Zones requires careful coordination
5. **Operational Overhead:** Publisher lifecycle management (registration, deregistration) is time-consuming and error-prone

### Impact
- **Financial:** Wasted infrastructure costs from over-provisioned resources
- **Operational:** Hours spent on manual publisher management tasks
- **Availability:** Risk of service degradation during traffic spikes if publishers are under-provisioned
- **Scalability:** Inability to quickly respond to changing business requirements

---

## Technical Solution Description

This solution provides an **automated, scalable, and cost-optimized** deployment of Netskope Private Access Publishers using native AWS services. The solution eliminates manual publisher management through event-driven automation and provides dynamic scaling based on actual workload demands.

### Core Capabilities

1. **Dynamic Auto Scaling**
   - CPU-based scaling policies automatically adjust publisher capacity
   - Configurable scaling thresholds (default: 70% CPU utilization)
   - Support for minimum, maximum, and desired capacity settings
   - Multi-AZ deployment for high availability

2. **Automated Publisher Lifecycle Management**
   - Automatic publisher registration with Netskope when instances launch
   - Automatic publisher deregistration when instances terminate
   - Automatic private application configuration updates
   - Graceful handling of scaling events via lifecycle hooks

3. **Zero-Touch Operations**
   - Publishers automatically register using AWS Systems Manager
   - No manual API calls required for scaling events
   - Event-driven architecture ensures consistency
   - CloudWatch metrics for monitoring and alerting

4. **Security & Compliance**
   - Publishers deployed without public IP addresses
   - Egress-only network access (no ingress rules)
   - Secrets stored in AWS Secrets Manager with encryption
   - IAM roles follow least privilege principles
   - Access via AWS Systems Manager Session Manager

5. **Cost Optimization**
   - Pay only for capacity you need (scales down during low demand)
   - Configurable capacity limits prevent runaway costs
   - Resource tagging for cost allocation and tracking
   - CPU-based metrics ensure efficient resource utilization

---

## AWS Services Used and Their Roles

### Compute & Scaling
**Amazon EC2 Auto Scaling Group**
- **Purpose:** Manages the fleet of NPA Publisher EC2 instances
- **Function:** Maintains desired capacity, handles instance failures, executes scaling policies
- **Configuration:** Minimum 2 instances (HA), scales based on average CPU utilization

**EC2 Launch Template**
- **Purpose:** Defines instance configuration (AMI, instance type, security, IAM role)
- **Function:** Ensures consistent instance configuration across scaling events
- **Default Instance Type:** t3.large (appropriate for NPA Publisher workload)

**Target Tracking Scaling Policy**
- **Purpose:** Automatically adjusts capacity to maintain target CPU utilization
- **Function:** Adds instances when CPU exceeds target, removes when CPU drops
- **Default Target:** 70% CPU utilization

### Automation & Orchestration
**AWS Lambda (NPACallNetskopeAPIv2LF)**
- **Purpose:** Orchestrates publisher lifecycle with Netskope API
- **Function:**
  - Registers new publishers with Netskope tenant
  - Deregisters terminated publishers
  - Updates private application configurations
  - Manages lifecycle hook completion
- **Runtime:** Python 3.12
- **Trigger:** Amazon EventBridge rules on Auto Scaling lifecycle events

**AWS Lambda (NetskopeNPACustomResourceLF)**
- **Purpose:** CloudFormation custom resource for initial capacity setup
- **Function:** Updates Auto Scaling Group to desired capacity after deployment
- **Runtime:** Python 3.12 (inline code)
- **Trigger:** CloudFormation stack creation/update

**Amazon EventBridge**
- **Purpose:** Routes Auto Scaling lifecycle events to Lambda function
- **Function:** Triggers Lambda on EC2 instance launch and termination events
- **Event Types:**
  - `EC2 Instance-launch Lifecycle Action`
  - `EC2 Instance-terminate Lifecycle Action`

**Auto Scaling Lifecycle Hooks**
- **Purpose:** Pause instance launch/termination for custom actions
- **Function:**
  - LAUNCHING hook: Wait for publisher registration before placing instance in service
  - TERMINATING hook: Wait for publisher deregistration before terminating instance
- **Timeout:** 900 seconds (15 minutes)
- **Default Result:** ABANDON (fail-safe if Lambda fails)

**AWS Systems Manager (Session Manager & Run Command)**
- **Purpose:** Secure instance access and command execution
- **Function:**
  - Execute registration commands on new instances
  - Provide secure shell access without public IPs
  - Monitor instance health and status
- **IAM Policy:** AmazonSSMManagedInstanceCore

### Security & Secrets Management
**AWS Secrets Manager**
- **Purpose:** Securely store Netskope REST API v2 token
- **Function:**
  - Encrypted storage of API credentials
  - Automatic rotation support
  - Access control via IAM policies
  - Can reuse existing secret or create new one
- **Encryption:** AWS KMS (to be added in Phase 2)

**IAM Roles & Policies**
1. **NPAPublisherInstanceRole**
   - Attached to EC2 instances
   - Allows Systems Manager management
   - No outbound API permissions needed

2. **NPACallNetskopeAPIv2LFRole**
   - Attached to lifecycle Lambda function
   - Permissions:
     - Secrets Manager: GetSecretValue, DescribeSecret
     - Auto Scaling: CompleteLifecycleAction
     - SSM: SendCommand, DescribeInstanceInformation, GetCommandInvocation
     - CloudWatch Logs: CreateLogGroup, CreateLogStream, PutLogEvents

3. **NetskopeNPACustomResourceLFRole**
   - Attached to custom resource Lambda
   - Permissions:
     - Auto Scaling: UpdateAutoScalingGroup
     - CloudWatch Logs: CreateLogGroup, CreateLogStream, PutLogEvents

### Networking
**Amazon VPC Security Group**
- **Purpose:** Network-level access control for publisher instances
- **Configuration:**
  - **Ingress:** None (zero inbound rules)
  - **Egress:** Allow all TCP/UDP (443, API endpoints, private apps)
- **Security Posture:** Egress-only, no public internet ingress

**VPC Subnets (User-Provided)**
- **Purpose:** Network placement for publisher instances
- **Requirements:**
  - Minimum 2 subnets in different Availability Zones
  - Outbound internet connectivity (NAT Gateway or IPsec)
  - Access to Netskope NewEdge platform
  - Access to AWS S3 regional endpoint (for CloudFormation)

---

## Architecture Flow

### Deployment Flow
1. User deploys CloudFormation template via AWS Console or CLI
2. CloudFormation creates all infrastructure resources
3. Auto Scaling Group initially created with 0 capacity
4. Custom Resource Lambda updates capacity to desired value
5. Auto Scaling Group launches initial publisher instances
6. Lifecycle hook LAUNCHING pauses instance startup
7. EventBridge triggers Lambda function
8. Lambda uses SSM Run Command to register publisher with Netskope
9. Lambda completes lifecycle hook, instance enters service
10. Private applications are updated with new publisher

### Scaling Up Flow
1. Average CPU utilization exceeds target (e.g., 70%)
2. Scaling policy triggers Auto Scaling Group to add instance
3. Steps 6-10 from Deployment Flow repeat
4. New publisher seamlessly joins existing publisher group

### Scaling Down Flow
1. Average CPU utilization drops below target
2. Scaling policy triggers Auto Scaling Group to terminate instance
3. Lifecycle hook TERMINATING pauses instance termination
4. EventBridge triggers Lambda function
5. Lambda calls Netskope API to deregister publisher
6. Lambda updates private application configurations
7. Lambda completes lifecycle hook
8. Instance terminates

### API Integration Flow
```
Lambda Function → Netskope REST API v2
  ├─ GET /api/v2/infrastructure/publishers (list publishers)
  ├─ POST /api/v2/infrastructure/publishers (register new)
  ├─ DELETE /api/v2/infrastructure/publishers/{id} (deregister)
  ├─ GET /api/v2/steering/apps/private (list private apps)
  └─ PATCH /api/v2/steering/apps/private/{id} (update app config)
```

---

## Integration Points

### Netskope REST API v2
**Authentication:** Bearer token stored in AWS Secrets Manager
**Base URL:** `https://{tenant}.goskope.com/api/v2`
**Rate Limits:** Subject to Netskope API rate limiting
**Required Scopes:**
- Infrastructure management (publishers)
- Application management (private apps)

**Critical Dependencies:**
- Netskope tenant must be accessible from AWS VPC
- API token must have appropriate permissions
- Private application naming convention must be followed

### Naming Convention Requirement
**CRITICAL:** This solution requires a specific naming convention to function properly.

**Rule:** All Netskope Private Applications using this publisher group MUST start their name with the Auto Scaling Group name.

**Example:**
- Auto Scaling Group Name: `MyNPAPublisherGroup1`
- Valid Private App Names:
  - `MyNPAPublisherGroup1-App1`
  - `MyNPAPublisherGroup1-WebServer`
  - `MyNPAPublisherGroup1-Database-API`

**Why:** The Lambda function identifies which private applications belong to this publisher group by matching the name prefix. This allows automatic configuration updates when publishers scale.

### Modifying the Naming Convention

If you need to implement a different naming convention for your organization, you will need to modify the following files:

#### 1. Lambda Function Logic (`scripts/lambda_function.py`)

**Location:** Lines 62 and 193

**Current Implementation:**
```python
if app['app_name'].find(autoscalling_groupname) == -1:
    continue
```

**What it does:** Checks if the private app name contains the Auto Scaling Group name as a substring. If not found (`-1`), the app is skipped.

**Modification Required:** Replace the `.find()` logic with your custom matching pattern.

**Alternative Examples:**

*Suffix-based matching:*
```python
# Match apps ending with ASG name: App1-MyNPAPublisherGroup
if not app['app_name'].endswith(autoscalling_groupname):
    continue
```

*Exact prefix with delimiter:*
```python
# Match apps with exact prefix: MyNPAPublisherGroup_App1
prefix = autoscalling_groupname + "_"
if not app['app_name'].startswith(prefix):
    continue
```

*Tag-based matching (requires API changes):*
```python
# Match apps by custom tag/attribute instead of name
if app.get('publisher_group_tag') != autoscalling_groupname:
    continue
```

*Regex pattern matching:*
```python
import re
# Match apps with pattern: MyNPAPublisherGroup-[alphanumeric]
pattern = f"^{re.escape(autoscalling_groupname)}-[a-zA-Z0-9]+$"
if not re.match(pattern, app['app_name']):
    continue
```

#### 2. Documentation Updates Required

After modifying the Lambda function, update the following documentation files to reflect the new naming convention:

- **README.md** - Lines 101-114 (Naming Convention Requirement section)
- **docs/DEVOPS_NOTES.md** - Lines 229-242 (this section)
- **CloudFormation Template Metadata** - `templates/npa-publisher-autoscaling.yaml` (if parameter descriptions reference naming)

#### 3. Testing the Modified Naming Convention

After making changes, test the following scenarios:

1. **Launch Event:** Create a new private app with the new naming pattern, trigger ASG scale-up, verify publisher is assigned
2. **Terminate Event:** Trigger ASG scale-down, verify publisher is removed from the private app
3. **Non-matching App:** Create an app that doesn't match the pattern, verify it's ignored
4. **Multiple Apps:** Create multiple matching apps, verify all are updated correctly

#### 4. CloudFormation Parameter Validation (Optional)

If you want to enforce naming at deployment time, add validation to the CloudFormation template:

**Location:** `templates/npa-publisher-autoscaling.yaml`

**Example Parameter Constraint:**
```yaml
Parameters:
  AutoScalingGroupName:
    Type: String
    Description: Name of the Auto Scaling Group (must match your naming convention)
    AllowedPattern: "^[a-zA-Z][a-zA-Z0-9-]{1,254}$"
    ConstraintDescription: "Must start with a letter, contain only alphanumeric and hyphens"
```

#### Impact Analysis

**Files Modified:** 1 required (lambda_function.py), 3 optional (documentation)

**Testing Required:** End-to-end scale up/down testing with new naming pattern

**Backward Compatibility:** Changing the naming convention will affect existing private apps. Plan migration carefully.

**Rollback:** Keep the original lambda_function.py backed up. Rollback requires redeploying the Lambda function with the original code.

### AWS Systems Manager
**Purpose:** Execute registration commands on EC2 instances
**Requirements:**
- SSM Agent installed on EC2 instances (included in NPA Publisher AMI)
- VPC endpoint for SSM or NAT Gateway for internet access
- IAM instance profile with SSM permissions

**Commands Executed:**
- Publisher registration with Netskope tenant
- Health checks and status verification

---

## Scalability Characteristics

### Horizontal Scaling
- **Scale Out:** Add instances when demand increases
- **Scale In:** Remove instances when demand decreases
- **Limits:** Configurable min/max capacity (recommended: min 2, max 10)
- **Speed:** New publishers online in ~5-10 minutes (depends on AMI boot time + registration)

### Performance Characteristics
- **Publisher Capacity:** Each t3.large instance supports ~500-1000 concurrent users (varies by workload)
- **Scaling Granularity:** 1 instance at a time
- **Cooldown Period:** 300 seconds (5 minutes) between scaling activities
- **Metric Update Frequency:** 1 minute (CloudWatch metrics)

### Limitations
- **API Rate Limits:** Netskope API calls are subject to tenant rate limits
- **Lifecycle Hook Timeout:** 15 minutes max for registration/deregistration
- **Regional Deployment:** Solution is region-specific (can deploy in multiple regions)
- **Single Publisher Group:** One Auto Scaling Group = One Netskope Publisher Group

---

## High Availability Approach

### Multi-AZ Deployment
- **Requirement:** Minimum 2 subnets in different Availability Zones
- **Benefit:** Protects against AZ-level failures
- **Recommendation:** Deploy across 3 AZs for maximum resilience

### Instance Health Monitoring
- **EC2 Health Checks:** Automatic instance replacement on EC2 health failures
- **Auto Scaling Metrics:** Average CPU across all instances
- **Lifecycle Hooks:** Prevent unhealthy instances from entering service

### Failure Scenarios

| Failure Type | Impact | Recovery |
|--------------|--------|----------|
| Single instance failure | No service impact (other instances handle load) | Auto Scaling launches replacement |
| AZ outage | Reduced capacity (instances in other AZs continue) | Auto Scaling launches instances in healthy AZs |
| Lambda function failure | Publisher not registered | Lifecycle hook times out, instance abandoned, retry with new instance |
| Netskope API unavailable | Publishers cannot register | Lifecycle hook times out, instances wait until API restored |

---

## Security Model

### Network Security
**Defense in Depth:**
1. **No Public IPs:** Publishers deployed in private subnets
2. **No Ingress Rules:** Security group allows no inbound traffic
3. **Egress Only:** Outbound access to Netskope and private apps
4. **NAT Gateway:** Secure outbound internet access (or IPsec tunnel)

### Identity & Access Management
**Principle of Least Privilege:**
- EC2 instances: Only SSM management permissions
- Lambda functions: Only necessary API and service permissions
- No cross-account access (unless explicitly configured)

### Data Protection
- **API Token:** Stored encrypted in Secrets Manager
- **In Transit:** HTTPS for all API calls to Netskope
- **Logs:** CloudWatch Logs encrypted at rest
- **No Sensitive Data:** Publishers do not store user data

### Access Control
- **Instance Access:** AWS Systems Manager Session Manager (no SSH keys needed)
- **API Access:** IAM roles (no long-term credentials)
- **Secret Access:** IAM policy-based access control

---

## Non-Functional Requirements

### Performance
- **Target Availability:** 99.9% (multi-AZ deployment)
- **Scale-Up Time:** < 15 minutes from CPU threshold breach to new capacity
- **Scale-Down Time:** < 10 minutes from low utilization to capacity reduction

### Scalability
- **Horizontal Scaling:** Yes (add/remove instances)
- **Vertical Scaling:** No (requires Launch Template update and instance replacement)
- **Expected Load:** Configurable based on organization size

---

## Cost Model

### Variable Costs (Scale with Usage)
- **EC2 Instances:** t3.large on-demand or reserved instances
  - On-Demand: ~$0.0832/hour per instance
  - 2 instances 24/7/30 days: ~$120/month
  - 10 instances 24/7/30 days: ~$600/month
- **Data Transfer:** Outbound data transfer (first 1GB free, then $0.09/GB)
- **NAT Gateway:** $0.045/hour + $0.045/GB processed (~$33/month + data)

### Fixed Costs
- **Lambda Invocations:** Free tier: 1M requests/month
- **Secrets Manager:** $0.40/secret/month
- **CloudWatch Logs:** $0.50/GB ingested (first 5GB free)

### Monthly Cost Estimation
- **Small Deployment (2-4 instances):** $200-400/month
- **Medium Deployment (4-8 instances):** $400-800/month
- **Large Deployment (8-16 instances):** $800-1600/month

---

## References

- [Netskope Private Access Documentation](https://docs.netskope.com/en/netskope-private-access.html)
- [Netskope REST API v2 Overview](https://docs.netskope.com/en/rest-api-v2-overview-312207.html)
- [Amazon EC2 Auto Scaling User Guide](https://docs.aws.amazon.com/autoscaling/ec2/userguide/)
- [AWS Lambda Developer Guide](https://docs.aws.amazon.com/lambda/latest/dg/)
- [AWS Systems Manager Documentation](https://docs.aws.amazon.com/systems-manager/)

---

**Document Owner:** DevOps Team
**Last Reviewed:** 2025-12-16

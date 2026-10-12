> Historical upstream reference. Use [the thread-scaling guide](PUBLISHER_THREAD_SCALING.md) for this fork’s supported add-on. The original lifecycle bundle is unverified and its average-CPU policy can miss a saturated publisher thread.

\# Deploying Netskope Private Access Publisher with Amazon EC2 Auto Scaling

This document will guide you how to deploy Netskope Private Access Amazon EC2 Auto Scaling.

Netskope Private Access (NPA), a Zero Trust Network Access (ZTNA) solution, seamlessly connects authenticated users anywhere, using any devices to private resources in data centers and public cloud environments. Built on the NewEdge security private cloud, NPA delivers fast and direct application connectivity, ensuring a superior user experience.

To learn more about Netskope Private Access please refer to the [Netskope Private Access](https://www.netskope.com/products/private-access) product overview  and [Netskope Private Access](https://docs.netskope.com/en/netskope-private-access.html) documentation.

Amazon EC2 Auto Scaling helps you maintain application availability and allows you to automatically add or remove EC2 instances according to conditions you define. You can use the fleet management features of EC2 Auto Scaling to maintain the health and availability of your fleet. You can also use the dynamic and predictive scaling features of EC2 Auto Scaling to add or remove EC2 instances. Dynamic scaling responds to changing demand and predictive scaling automatically schedules the right number of EC2 instances based on predicted demand. Dynamic scaling and predictive scaling can be used together to scale faster. To learn more about Amazon EC2 Auto Scaling please refer to the [Amazon EC2 Auto Scaling](https://aws.amazon.com/ec2/autoscaling/) documentation.

Deploying Netskope Private Publisher using Amazon EC2 Auto Scaling group allows you to dynamically respond to the users' traffic demand and to optimize cost of running your Netskope Private Access Publishers' EC2 instances.

## Overview

The solution consists of AWS CloudFormation template NPAPublisherAuto Scalling.yaml which deploys the following main resources:

- Amazon EC2 Auto Scaling group of Netskope Private Publishers in your VPC. This Auto Scaling group has CPU-based Amazon EC2 [Dynamic Scaling Policy](https://docs.aws.amazon.com/autoscaling/ec2/userguide/as-scale-based-on-demand.html) with default CPU utilization target  75% which you can change during the deployment

- AWS Lambda function triggered automatically when new NPA Publisher EC2 instances are added or removed from the EC2 Auto Scaling group. This Lambda function calls [Netskope REST API v2](https://docs.netskope.com/en/rest-api-v2-overview-312207.html)  to provision and deprovision NPA Publisher in your Netskope tenant. It also identifies the [Netskope Private Applications](https://docs.netskope.com/en/private-app-management.html) that are using your Netskope Publisher EC2 Auto Scaling group and updates their configuration accordingly by removing or adding the specific publisher. The Netskope Private Applications naming convention required for this solution to function properly described in the next paragraph below

- Amazon EC2 Auto Scaling NPA Publisher Launching and Terminating Lifecycle Hooks to invoke the above Lambda function

- AWS Secrets Manager secret to store  Netskope REST API v2 access token. You can reuse the same AWS Secrets Manager Secret for multiple NPA Publisher Autos Scaling groups, providing the existing AWS Secrets Manager Secret ARN during the deployment, or choose to create a new AWS Secrets Manager Secret while deploying the solution. When choosing to use the existing Secret assure its resource-based policy allows *secretsmanager:GetSecretValue* and  *secretsmanager:DescribeSecret* actions to the NPACallNetskopeAPIv2LFRole Lambda Function IAM role created by this solution. For more details about AWS Secrets Manager security please refer to the user guide [here](https://docs.aws.amazon.com/secretsmanager/latest/userguide/auth-and-access.html)

- Custom resource AWS Lambda function to update your NPA Publisher Autoscaling group initial capacity during creation

- AWS IAM roles for the EC2 Instances in the Auto Scaling group. This role contains AWS-managed IAM policy AmazonSSMManagedInstanceCore to allow EC2 instance management by the AWS Systems Manager. This is required to provision and deprovision NPA Publisher on your Netskope tenant. For more information about setting Amazon Systems Manager on your account please follow this [link](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-sessions-start.html).

- AWS IAM roles for the NPA Publisher EC2 instances lifecycle management AWS Lambda function and for the custom resource AWS Lambda.

- NetskopeCETaskSecurityGroup will be used by the Netskope CE task


The solution also creates a NetskopeNPACustomResourceLF custom resource AWS Lambda function which is used to update your NPA Publisher Autoscaling group initial capacity.

This solution is a regional solution and can / may be deployed independently in multiple AWS regions. You can build a cross-region redundant NPA Publisher group following the same naming convention below.

## Architecture diagram

![](/docs/media/image001.png)

*Fig 1. Netskope Private Access Publisher with Amazon EC2 Auto Scaling*

## Naming convention

This solution requires a specific naming convention between the Netskope Private Access Publisher EC2 Auto Scaling group and the Netskope Private Applications using this Publishers' group.

Netskope Publishers in the same Publisher group serve all Private Apps assigned to it. When adding a new Publisher to the existing Publishers' group, this new Publisher gets added to all NPA Private Apps assigned to this Publishers' group. This is also true in our Amazon EC2 Auto Scaling setup - NPA Publishers EC2 Auto Scaling group is equivalent to the Netskope Publishers group. All Netskope Private Apps using this Publishers' group will automatically be reconfigured to include new Publishers added to the EC2 Auto Scaling group as well as to exclude Publishers removed from it.

This solution's AWS Lambda function identifies those Netskope Private Apps to be updated by using simple naming convention requiring a Private App name to **start with the NPA Publisher EC2 Auto Scaling Group name**. For example, if your NPA Publisher EC2 Auto Scaling Group name is MyNPAPublisherGroup1 , you can use any Private App name that starts with MyNPAPublisherGroup1 such as:

- MyNPAPublisherGroup1-App1
- MyNPAPublisherGroup1-WebServer
- MyNPAPublisherGroup1-Database-API

and so on.

This naming convention allows you to run multiple NPA Publishers EC2 Auto Scaling groups in the same AWS region or in the different AWS regions while still serving the same Netskope tenant. This also allows you to run multiple NPA Publishers EC2 Auto Scaling groups in one AWS region while serving different Netskope Private Apps in one Netskope tenant. The example on the diagram below illustrates the above

![](/docs/media/image005.png) </br>
*Fig 2. Cross Region Multiple Publishers Groups* </br>


## Deployment

### 1. AMI


1.1. Netskope Private Access Publisher AMI is available from AWS Marketplace:

![](/docs/media/image006.png)

1.1.2. Go to AMI Catalog, AWS Marketplace AMIs and type Netskope in the search bar. Click Select on the Netskope Private Access Publisher.

![](/docs/media/image002.png)

1.1.3. Click Continue:

![](/docs/media/image003.png)

1.1.4. Click Launch Instance with AMI:

![](/docs/media/image004.png)

1.1.5. Copy the AMI ID (ami-xxxxxxxxx) and save it for later.

Note: The AMI ID is region specific. You will need to use the AMI ID for the region where you are deploying the stack. You can also find the AMI ID by navigating to the AMI Catalog in the EC2 console and searching for Netskope Private Access Publisher.

### 2. CloudFormation

1.2.1. Download the CloudFormation template [NPAPublisherAutoscalling.yaml](https://github.com/netskopeoss/AWS-NPA-Publisher-AutoScaling) from this GitHub repository

1.2.2. Navigate to AWS CloudFormation Console, click on **Create stack**, then select **With new resources (standard)**.

1.2.3. Choose **Upload a template file** then click on Choose file. Choose the NPAPublisherAutoscalling.yaml from the directory on your disk where you downloaded it to, click **Open** and then click **Next**.

![](/docs/media/image007.png)

1.2.4. Specify Stack Details:

![](/docs/media/image008.png)

![](/docs/media/image009.png)

**Parameters description**
| Parameter | Description | Example |
| --- | --- | --- |
| Stack name | CloudFormation stack name  | netskope-npa-publisher |
| VPC Id | VPC to deploy NPA Publisher EC2 instances to| vpc-05xxxx29|
| Subnet Ids | Select at least 2 subnets | subnet-0xxxx52, subnet-0xxxx8f |
| AMI ID |NPA Publisher AMI ID you collected at AMI stage| ami-0c55b159cbfafe1f0 |
|Auto Scalling Group Name | Name of your NPA Publisher EC2 Auto Scaling group. Remember the naming convention - Private Apps using this Publisher group MUST have their names started with this name |MyNPAPublisherGroup1|
|Desired Capacity| Desired initial number of the NPA Publishers EC2 instances in Auto Scaling group | 2
|Min Size| Minimum number of the NPA Publishers EC2 instances  in Auto Scaling group| 2|
|Max Size| Maximum number of the NPA Publishers EC2 instances in the Auto Scaling group| 10|
|Instance Type| Instance type. NPA Publisher minimum requirement is 2 vCPU 4GB RAM. Default value t2.large|t2.large|
|Target CPU Utilization | NPA Publisher EC2 instances' target CPU utilization in %, when the average CPU utilization will go above this target Auto Scaling group will add more EC2 instances, when it goes below Auto Scaling group will scale-in|75|
|Netskope Tenant FQDN|Your Netskope tenant FQDN|mytenant.goskope.com|
|Create new AWS Secret (API Token)|When select No you will have to provide the existing secret ARN below. When Yes you will have to provide the Netskope REST API v2 token below. The Lambda function will create a new AWS Secret with this token|Yes|
|Existing Secret Manager ARN|Provide Secret ARN if Create new AWS Secret (API Token) is No||
|Netskope API v2 Token|Provide Netskope REST API v2 token if Create new AWS Secret (API Token) is Yes. Token will be stored in a new AWS Secrets Manager Secret. This parameter is ignored if Create new AWS Secret (API Token) is No||

1.2.5. Click Next

1.2.6 At the Configure Stack options screen click **Next**

1.2.7. Review the Stack Deployment parameters and check "**I acknowledge that AWS CloudFormation might create IAM resources with custom names.**"
![](/docs/media/image010.png)


1.2.8. Click **Submit**

1.2.9. Wait for few minutes for stack creation to complete.

![](/docs/media/image011.png)

1.2.10. When the stack creation successfully completed you should see CREATE_COMPLETE status.

![](/docs/media/image012.png)

1.2.11. Navigate to **Resources** tab and validate all resources got created

 Click on the NPAPublisherSecurityGroup resource and check the Inbound and Outbound rules for the EC2 Auto Scaling Instances:

![](/docs/media/image013.png)![](/docs/media/image014.png)


 Click on the NPACallNetskopeAPIv2LF function and check its Event Trigger:

![](/docs/media/image015.png)


Click on the Auto Scaling group and verify desired capacity was changed to the number you  specified when creating the stack:

![](/docs/media/image012.png)

Navigate to **Instances** to see the NPA Publisher EC2 instances created:

![](/docs/media/image016.png)

1.2.12. Open CloudWatch Logs (from NPACallNetskopeAPIv2LF Lambda Function *Monitor* tab) and verify the publishers were successfully provisioned in your Netskope tenant.

![](/docs/media/image017.png)

1.2.13. Login to your Netskope tenant. Navigate to Setting → Security Cloud Platform → Publishers and verify the new Publishers exist and are registered:

![](/docs/media/image018.png)


1.2.14. Validate the publishers are being added to the Private Apps automatically by adding your Publisher EC2 Auto Scalling Group name to the existing Private Application's name or adding new Application following the naming convention requirements

 ![](/docs/media/image019.png)

Congratulations! Your NPA Publisher EC2 Auto Scalling group deployment is complete.

## Test scaling

To test the Auto Scaling Group scaling you can use the stress utility to stress the publisher instances and trigger the Auto Scaling Group to add more instances. You can also use the AWS Console to manually change the desired capacity of the Auto Scaling Group and verify the publishers are being added and removed from the Private Apps.

## Clean up

To clean up the deployment you can delete the CloudFormation stack. This will delete all the resources created by the stack.

## Troubleshooting

If you encounter any issues during the deployment or operation of the solution, please check the CloudWatch Logs for the Lambda function NPACallNetskopeAPIv2LF. The Lambda function logs all API calls to Netskope and any errors encountered during the execution.

## Support

For support please open an issue in this GitHub repository or contact Netskope support.

## License

This project is licensed under the Apache License 2.0 - see the [LICENSE](../LICENSE) file for details.
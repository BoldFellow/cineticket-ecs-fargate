# AWS Console Guide — CineTicket (ECS Fargate Microservices)

Step-by-step ClickOps walkthrough for building a two-service containerised cinema booking platform. Follow the sections in order — each one depends on the previous.

**Assumed starting point:**
- This project is self-contained — no external VPC stack is required. The click-ops walkthrough (§0 below) or the CloudFormation template (Appendix A) both provision the VPC, subnets, IGW, and route table as part of this deployment.
- AWS CLI is configured: `aws configure` with region `us-east-1`.
- Docker Desktop is installed and running.
- You are working from the `CineTicket - ECS Fargate Microservices/` project folder.

> **Architecture in 30 seconds:** Two Fargate services (Movie + Booking) sit behind a single ALB that uses path-based rules to route `/movies/*` and `/bookings/*` to the right service. The Movie Service caches DynamoDB reads in ElastiCache Redis. The Booking Service does a DynamoDB conditional write to reserve seats, inserts a booking record in RDS PostgreSQL, and publishes an SNS event. A Lambda function picks up the event from SQS and sends a confirmation email via SES. The Booking Service scales to 3 tasks when CPU exceeds 60%. Everything in this guide is built manually — the CloudFormation template in **Appendix A** automates the same setup as a shortcut.

---

## Overview of what you will create

| # | Service | Resource | Name |
|---|---|---|---|
| 0 | VPC | VPC · IGW · 6 subnets · public route table | `cineticket-vpc` |
| 1 | EC2 → Security Groups | ALB, Movie, Booking, Redis, RDS security groups | `cineticket-*-sg` |
| 2 | DynamoDB | Movies catalogue table | `cineticket-movies` |
| 3 | DynamoDB | Seat availability table | `cineticket-seats` |
| 4 | Secrets Manager | RDS credentials secret | `cineticket/db` |
| 5 | RDS | PostgreSQL 16 — booking records | `cineticket-db` |
| 6 | ElastiCache | Redis — movie / seat cache | `cineticket-redis` |
| 7 | SNS | OrderPlaced event topic | `cineticket-order-placed` |
| 8 | SQS | Notification delivery queue | `cineticket-notification-queue` |
| 9 | SQS | Dead-letter queue | `cineticket-notification-dlq` |
| 10 | IAM | Task Execution Role (shared) | `cineticket-ecs-execution-role` |
| 11 | IAM | Movie Service task role | `cineticket-movie-task-role` |
| 12 | IAM | Booking Service task role | `cineticket-booking-task-role` |
| 13 | IAM | Lambda execution role | `cineticket-notification-lambda-role` |
| 14 | Lambda | Email sender on booking events | `cineticket-notification` |
| 15 | ECR | Movie Service registry | `cineticket-movies` |
| 16 | ECR | Booking Service registry | `cineticket-bookings` |
| 17 | ECS | Fargate cluster | `cineticket-cluster` |
| 18 | EC2 → Load Balancers | ALB + target groups + routing rules | `cineticket-alb` |
| 19 | ECS | Movie Service task definition + service | `cineticket-movie-service` |
| 20 | ECS | Booking Service task definition + service | `cineticket-booking-service` |
| 21 | Application Auto Scaling | Booking Service — scale on CPU | 1 → 3 tasks at 60% CPU |
| 22 | S3 | Web UI static website | `cineticket-web-<account-id>` |

---

## 0. Create the VPC and Subnets

This section creates the network foundation: one VPC, an Internet Gateway, six subnets across two AZs (public / private / DB tiers), and a public route table. The private and DB subnets use the VPC's implicit main route table — local routing only, no NAT.

> **Note — no NAT Gateway:** Fargate tasks run in public subnets with **Auto-assign public IP: ENABLED** so they can reach ECR over the internet. This is a deliberate teaching choice for this lab. Private tasks with NAT would cost more and add complexity without changing the architecture lesson.

**Subnet layout (CIDR reference):**

| Subnet | CIDR | AZ | Used by |
|---|---|---|---|
| Public A | `10.30.0.0/24` | AZ-a | ALB, Fargate tasks |
| Public B | `10.30.1.0/24` | AZ-b | ALB, Fargate tasks |
| Private A | `10.30.10.0/24` | AZ-a | Redis |
| Private B | `10.30.11.0/24` | AZ-b | Redis |
| DB A | `10.30.20.0/24` | AZ-a | RDS |
| DB B | `10.30.21.0/24` | AZ-b | RDS |

---

### 0a. Create the VPC

> **Console:** VPC → Your VPCs → **Create VPC**

1. **Resources to create:** VPC only
2. **Name tag:** `cineticket-vpc`
3. **IPv4 CIDR block:** `10.30.0.0/16`
4. **IPv6 CIDR block:** No IPv6 CIDR block
5. **Tenancy:** Default
6. Click **Create VPC**

After creation:
7. Select `cineticket-vpc` → **Actions → Edit VPC settings**
8. Check both:
   - **Enable DNS resolution** (DNS support)
   - **Enable DNS hostnames**
9. Click **Save**

> Both DNS settings are required for Cloud Map private DNS (`cineticket.local`) to resolve service-discovery names.

---

### 0b. Create the Internet Gateway

> **Console:** VPC → Internet Gateways → **Create internet gateway**

1. **Name tag:** `cineticket-igw`
2. Click **Create internet gateway**
3. On the next screen, click **Actions → Attach to VPC** → select `cineticket-vpc` → **Attach internet gateway**

---

### 0c. Create the Six Subnets

> **Console:** VPC → Subnets → **Create subnet**

Select VPC: `cineticket-vpc`, then add all six subnets in one form (click **Add new subnet** after each):

| Subnet name | Availability Zone | IPv4 CIDR |
|---|---|---|
| `cineticket-public-subnet-a` | `us-east-1a` | `10.30.0.0/24` |
| `cineticket-public-subnet-b` | `us-east-1b` | `10.30.1.0/24` |
| `cineticket-private-subnet-a` | `us-east-1a` | `10.30.10.0/24` |
| `cineticket-private-subnet-b` | `us-east-1b` | `10.30.11.0/24` |
| `cineticket-db-subnet-a` | `us-east-1a` | `10.30.20.0/24` |
| `cineticket-db-subnet-b` | `us-east-1b` | `10.30.21.0/24` |

Click **Create subnet**.

**Enable auto-assign public IP on the two public subnets:**

For each of `cineticket-public-subnet-a` and `cineticket-public-subnet-b`:
1. Select the subnet → **Actions → Edit subnet settings**
2. Check **Enable auto-assign public IPv4 address**
3. Click **Save**

---

### 0d. Create the Public Route Table

> **Console:** VPC → Route Tables → **Create route table**

1. **Name:** `cineticket-public-rt`
2. **VPC:** `cineticket-vpc`
3. Click **Create route table**

**Add the default route:**
4. Select `cineticket-public-rt` → **Routes** tab → **Edit routes**
5. Click **Add route:**
   - Destination: `0.0.0.0/0`
   - Target: **Internet Gateway** → select `cineticket-igw`
6. Click **Save changes**

**Associate both public subnets:**
7. **Subnet associations** tab → **Edit subnet associations**
8. Check both `cineticket-public-subnet-a` and `cineticket-public-subnet-b`
9. Click **Save associations**

> The private and DB subnets are intentionally left on the VPC's implicit main route table — local routing only. No explicit private route table is needed.

---

## 1. Security Groups

Security groups are created first because they reference each other (e.g., the Movie SG allows inbound port 8080 from *both* the ALB SG and the Booking SG). Create them in the order below.

> **Console:** EC2 → Network & Security → **Security Groups** → Create security group

---

### 1a. ALB Security Group

1. **Security group name:** `cineticket-alb-sg`
2. **Description:** `CineTicket ALB - allow HTTP from internet`
3. **VPC:** select the VPC you created in §0 (`cineticket-vpc`)
4. **Inbound rules → Add rule:**
   - Type: `HTTP` | Port: `80` | Source: `0.0.0.0/0`
   *(or lock to your classroom IP: `x.x.x.x/32`)*
5. **Outbound rules:** leave the default (all traffic)
6. **Tags → Add tag:** `Name` = `cineticket-alb-sg`
7. Click **Create security group**

---

### 1b. Movie Service Security Group

1. **Security group name:** `cineticket-movie-sg`
2. **Description:** `CineTicket Movie Service - allow 8080 from ALB and Booking Service`
3. **VPC:** select the VPC you created in §0 (`cineticket-vpc`)
4. **Inbound rules → Add two rules:**
   - Type: `Custom TCP` | Port: `8080` | Source: select `cineticket-alb-sg`
   - Type: `Custom TCP` | Port: `8080` | Source: select `cineticket-booking-sg`

   > **Note:** You cannot select `cineticket-booking-sg` yet because it does not exist. Add the first rule now, save the group, and come back after step 1c to add the second rule via **Edit inbound rules**.

5. **Tags → Add tag:** `Name` = `cineticket-movie-sg`
6. Click **Create security group**

---

### 1c. Booking Service Security Group

1. **Security group name:** `cineticket-booking-sg`
2. **Description:** `CineTicket Booking Service - allow 8080 from ALB`
3. **VPC:** select the VPC you created in §0 (`cineticket-vpc`)
4. **Inbound rules → Add rule:**
   - Type: `Custom TCP` | Port: `8080` | Source: select `cineticket-alb-sg`
5. **Tags → Add tag:** `Name` = `cineticket-booking-sg`
6. Click **Create security group**

**Now add the cross-reference on Movie SG:**

> **Console:** EC2 → Security Groups → select `cineticket-movie-sg` → **Edit inbound rules**

7. Click **Add rule:**
   - Type: `Custom TCP` | Port: `8080` | Source: select `cineticket-booking-sg`
8. Click **Save rules**

---

### 1d. Redis Security Group

The Movie Service is the only component that talks to Redis. Port 6379 is only open from the Movie SG.

1. **Security group name:** `cineticket-redis-sg`
2. **Description:** `CineTicket Redis - allow 6379 from Movie Service only`
3. **VPC:** select the VPC you created in §0 (`cineticket-vpc`)
4. **Inbound rules → Add rule:**
   - Type: `Custom TCP` | Port: `6379` | Source: select `cineticket-movie-sg`
5. **Tags → Add tag:** `Name` = `cineticket-redis-sg`
6. Click **Create security group**

---

### 1e. RDS Security Group

The Booking Service is the only component that connects to PostgreSQL. Port 5432 is only open from the Booking SG.

1. **Security group name:** `cineticket-rds-sg`
2. **Description:** `CineTicket RDS - allow 5432 from Booking Service only`
3. **VPC:** select the VPC you created in §0 (`cineticket-vpc`)
4. **Inbound rules → Add rule:**
   - Type: `PostgreSQL` | Port: `5432` | Source: select `cineticket-booking-sg`
5. **Tags → Add tag:** `Name` = `cineticket-rds-sg`
6. Click **Create security group**

---

## 2. DynamoDB Tables

Two DynamoDB tables: one for the movie catalogue (simple primary key on `movie_id`) and one for the seat map (composite key on `movie_id` + `seat_id`). Both use on-demand billing — no capacity planning needed.

> **Console:** Amazon DynamoDB → Tables → **Create table**

---

### 2a. Movies Catalogue Table

1. **Table name:** `cineticket-movies`
2. **Partition key:** `movie_id` (String)
3. **Sort key:** leave blank
4. **Table settings:** Customize settings
5. **Table class:** DynamoDB Standard
6. **Capacity mode:** On-demand
7. Click **Create table** and wait for status `Active`

---

### 2b. Seat Maps Table

The seat map uses a composite key: `movie_id` (partition) + `seat_id` (sort). Every seat for every movie is one item with a `status` attribute (`available` or `booked`). The DynamoDB conditional write in the Booking Service guarantees only one booking wins per seat.

1. **Table name:** `cineticket-seats`
2. **Partition key:** `movie_id` (String)
3. **Sort key:** `seat_id` (String)
4. **Table settings:** Customize settings
5. **Capacity mode:** On-demand
6. Click **Create table** and wait for status `Active`

---

## 3. Secrets Manager — DB Credentials

The Booking Service reads all database connection details (host, port, username, password, database name) from a single Secrets Manager secret at startup. You create the secret now with the username and database name; the host is added after RDS is created in §4c.

> **Console:** AWS Secrets Manager → **Store a new secret**

1. **Secret type:** Other type of secret
2. **Key/value pairs** — switch to **Plaintext** tab and paste:

   ```json
   {"username":"cineticket","dbname":"cineticket","port":5432}
   ```

3. **Encryption key:** `aws/secretsmanager` (default)
4. Click **Next**
5. **Secret name:** `cineticket/db`
6. **Description:** `CineTicket RDS PostgreSQL credentials`
7. Click **Next** → **Next** → **Store**

> You will come back to add `host` and `password` after the RDS instance is created. The Booking Service cannot start without a valid `host`, which is expected — RDS will not be ready either.

---

## 4. RDS PostgreSQL

RDS lives in the DB subnets (local-route-only — no internet, no NAT). The Booking Service reaches it via the private subnet on port 5432, controlled entirely by security groups. Single-AZ, no Multi-AZ — this is a teaching lab.

### 4a. DB Subnet Group

> **Console:** Amazon RDS → Subnet groups → **Create DB subnet group**

1. **Name:** `cineticket-db-subnet-group`
2. **Description:** `CineTicket RDS - DB subnets only`
3. **VPC:** select the VPC you created in §0 (`cineticket-vpc`)
4. **Availability Zones:** select all AZs that have a DB subnet
5. **Subnets:** for each AZ, select the **DB** subnet (not private, not public — `cineticket-db-subnet-a` and `cineticket-db-subnet-b` from §0)
6. Click **Create**

---

### 4b. RDS Instance

> **Console:** Amazon RDS → Databases → **Create database**

1. **Creation method:** Standard create
2. **Engine type:** PostgreSQL
3. **Engine version:** `PostgreSQL 16.13`
4. **Templates:** Free tier *(or Dev/Test if Free tier is unavailable for PostgreSQL 16)*
5. **DB instance identifier:** `cineticket-db`
6. **Master username:** `cineticket`
7. **Credentials management:** Self managed
8. **Master password:** generate a strong password and **copy it** — you will paste it into Secrets Manager next
9. **DB instance class:** `db.t3.micro`
   - Under "Burstable classes": select `db.t3.micro`
10. **Storage:**
    - Storage type: `gp2`
    - Allocated storage: `20` GiB
    - Enable storage autoscaling: **unchecked**
11. **Availability & durability:** Single DB instance (not Multi-AZ)
12. **Connectivity:**
    - VPC: select `cineticket-vpc` (created in §0)
    - DB subnet group: `cineticket-db-subnet-group`
    - Public access: **No**
    - VPC security group: remove `default` → add `cineticket-rds-sg`
    - Availability Zone: No preference
13. **Database authentication:** Password authentication
14. **Additional configuration → Initial database name:** `cineticket`
15. **Backup retention:** `0` days (demo — skip automated backups)
16. Click **Create database**

> RDS takes ~5 minutes to become `Available`.

---

### 4c. Update the Secrets Manager Secret

Once the RDS instance status is **Available**:

1. RDS → Databases → click `cineticket-db`
2. Copy the **Endpoint** (e.g. `cineticket-db.xxxx.us-east-1.rds.amazonaws.com`)
3. Secrets Manager → `cineticket/db` → **Retrieve secret value** → **Edit**
4. Switch to **Plaintext** and replace with the full JSON:

   ```json
   {
     "username": "cineticket",
     "password": "<your-password>",
     "dbname":   "cineticket",
     "port":     5432,
     "host":     "<rds-endpoint>"
   }
   ```

5. Click **Save**

---

## 5. ElastiCache Redis

The Movie Service uses Redis as a cache-aside layer in front of DynamoDB. It stores movie data with a 60-second TTL and invalidates the key whenever a booking or cancellation changes a seat. Redis lives in the private subnets — no internet exposure. The Movie SG is the only SG allowed to reach it.

### 5a. Redis Subnet Group

> **Console:** Amazon ElastiCache → **Subnet groups** → Create subnet group

1. **Name:** `cineticket-redis-subnet-group`
2. **Description:** `CineTicket Redis - private subnets`
3. **VPC:** select the VPC you created in §0 (`cineticket-vpc`)
4. **Subnets:** select both **private** subnets (not DB, not public — `cineticket-private-subnet-a` and `cineticket-private-subnet-b` from §0)
5. Click **Create**

---

### 5b. Redis Cluster

> **Console:** Amazon ElastiCache → **Redis OSS caches** → Create Redis OSS cache

1. **Deployment option:** Design your own cache
2. **Creation method:** Cluster cache
3. **Cluster mode:** Disabled
4. **Name:** `cineticket-redis`
5. **Location:** AWS Cloud
6. **Engine version:** `7.1` *(or latest available — any Redis 6+ works)*
7. **Port:** `6379`
8. **Node type:** `cache.t3.micro`
9. **Number of replicas:** `0` (single node, no replicas — lab only)
10. Under **Connectivity:**
    - Subnet group: `cineticket-redis-subnet-group`
    - Availability zone: No preference
11. Under **Security:**
    - Security groups: remove default → add `cineticket-redis-sg`
    - Encryption at rest: optional (uncheck for simplicity)
    - Encryption in transit: **uncheck** (the app connects without TLS)
12. Click **Next** → **Next** → **Create**

> Redis takes ~2 minutes. Once **Available**, copy the **Primary endpoint** hostname — you need it for the Movie Service task definition in §12a.
> The endpoint looks like: `cineticket-redis.xxxxxx.ng.0001.use1.cache.amazonaws.com`

---

## 6. SNS + SQS — Event Pipeline

After a booking, the Booking Service publishes one SNS message. SNS fans it out to the SQS queue. Lambda polls SQS and sends the confirmation email. The DLQ catches messages that fail three Lambda attempts so they are not lost.

Create resources in this order: DLQ → Queue → Topic → Subscription → Queue Policy.

---

### 6a. Dead-Letter Queue (DLQ)

> **Console:** Amazon SQS → **Queues** → Create queue

1. **Type:** Standard
2. **Name:** `cineticket-notification-dlq`
3. **Visibility timeout:** `60` seconds
4. **Message retention period:** `14` days
5. Click **Create queue**
6. Copy the **DLQ ARN** — you need it when creating the main queue.

---

### 6b. Notification Queue

> **Console:** Amazon SQS → **Queues** → Create queue

1. **Type:** Standard
2. **Name:** `cineticket-notification-queue`
3. **Visibility timeout:** `60` seconds *(must be ≥ Lambda timeout)*
4. **Dead-letter queue:** enable → select `cineticket-notification-dlq` → **Maximum receives:** `3`
5. Click **Create queue**
6. Copy the **Queue ARN** — you need it for the SNS subscription.

---

### 6c. SNS Topic

> **Console:** Amazon SNS → Topics → **Create topic**

1. **Type:** Standard
2. **Name:** `cineticket-order-placed`
3. Click **Create topic**
4. Copy the **Topic ARN** — you need it for the subscription, the queue policy, and the Booking Service task definition.

---

### 6d. SNS → SQS Subscription

> **Console:** SNS → Topics → `cineticket-order-placed` → **Create subscription**

1. **Protocol:** Amazon SQS
2. **Endpoint:** paste the `cineticket-notification-queue` ARN
3. **Enable raw message delivery:** leave unchecked (Lambda expects the SNS envelope)
4. Click **Create subscription**

---

### 6e. SQS Queue Policy (allow SNS to write)

Without this policy, SNS cannot deliver messages to the SQS queue.

> **Console:** SQS → Queues → `cineticket-notification-queue` → **Access policy** tab → **Edit**

Replace the policy JSON with:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": { "Service": "sns.amazonaws.com" },
      "Action": "sqs:SendMessage",
      "Resource": "<queue-arn>",
      "Condition": {
        "ArnEquals": {
          "aws:SourceArn": "<sns-topic-arn>"
        }
      }
    }
  ]
}
```

Replace `<queue-arn>` and `<sns-topic-arn>` with the values you copied above. Click **Save**.

---

## 7. IAM Roles

Four roles are needed: one shared Task Execution Role (used by the ECS agent for ECR pulls and CloudWatch Logs), one task role per service (used by the application code inside the container), and one Lambda execution role.

---

### 7a. Task Execution Role (shared by both ECS services)

This role is assumed by the ECS agent, not your application code. It needs permission to pull images from ECR and write logs to CloudWatch.

> **Console:** IAM → Roles → **Create role**

1. **Trusted entity type:** AWS service
2. **Use case:** Elastic Container Service → **Elastic Container Service Task**
3. Click **Next**
4. **Add permissions:** search for and attach `AmazonECSTaskExecutionRolePolicy`
5. Click **Next**
6. **Role name:** `cineticket-ecs-execution-role`
7. Click **Create role**

---

### 7b. Movie Service Task Role

This role is assumed by code running inside the Movie Service container. It needs DynamoDB read access on both tables.

> **Console:** IAM → Roles → **Create role**

1. **Trusted entity type:** AWS service
2. **Use case:** Elastic Container Service → **Elastic Container Service Task**
3. Click **Next** (skip adding managed policies — we will add an inline policy below)
4. **Role name:** `cineticket-movie-task-role`
5. Click **Create role**

Add an inline policy:

6. Click on `cineticket-movie-task-role` → **Add permissions** → **Create inline policy**
7. Click the **JSON** tab and paste:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "dynamodb:GetItem",
        "dynamodb:Scan",
        "dynamodb:Query"
      ],
      "Resource": [
        "arn:aws:dynamodb:us-east-1:<account-id>:table/cineticket-movies",
        "arn:aws:dynamodb:us-east-1:<account-id>:table/cineticket-seats"
      ]
    }
  ]
}
```

Replace `<account-id>` with your 12-digit AWS account ID (visible in the top-right corner of the console).

8. **Policy name:** `cineticket-movie-policy`
9. Click **Create policy**

---

### 7c. Booking Service Task Role

This role is assumed by code running inside the Booking Service container. It needs to do a DynamoDB conditional write on the seats table, read the DB secret from Secrets Manager, and publish to the SNS topic.

> **Console:** IAM → Roles → **Create role**

1. **Trusted entity type:** AWS service → **Elastic Container Service Task**
2. Click **Next** (no managed policies needed)
3. **Role name:** `cineticket-booking-task-role`
4. Click **Create role**

Add an inline policy:

5. Click on `cineticket-booking-task-role` → **Add permissions** → **Create inline policy** → **JSON** tab:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "dynamodb:UpdateItem",
      "Resource": "arn:aws:dynamodb:us-east-1:<account-id>:table/cineticket-seats"
    },
    {
      "Effect": "Allow",
      "Action": "secretsmanager:GetSecretValue",
      "Resource": "arn:aws:secretsmanager:us-east-1:<account-id>:secret:cineticket/db*"
    },
    {
      "Effect": "Allow",
      "Action": "sns:Publish",
      "Resource": "<sns-topic-arn>"
    }
  ]
}
```

Replace `<account-id>` and `<sns-topic-arn>` with your values.

6. **Policy name:** `cineticket-booking-policy`
7. Click **Create policy**

---

### 7d. Lambda Execution Role

This role is assumed by the notification Lambda. It needs to read and delete messages from the SQS queue, send email via SES, and write logs.

> **Console:** IAM → Roles → **Create role**

1. **Trusted entity type:** AWS service
2. **Use case:** Lambda
3. Click **Next** (no managed policies)
4. **Role name:** `cineticket-notification-lambda-role`
5. Click **Create role**

Add an inline policy:

6. Click on `cineticket-notification-lambda-role` → **Add permissions** → **Create inline policy** → **JSON** tab:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": "arn:aws:logs:*:*:*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "sqs:ReceiveMessage",
        "sqs:DeleteMessage",
        "sqs:GetQueueAttributes"
      ],
      "Resource": "<notification-queue-arn>"
    },
    {
      "Effect": "Allow",
      "Action": "ses:SendEmail",
      "Resource": "*"
    }
  ]
}
```

Replace `<notification-queue-arn>` with the SQS queue ARN from §6b.

7. **Policy name:** `cineticket-notification-policy`
8. Click **Create policy**

---

## 8. Lambda — Notification Function

The Lambda function reads booking events from SQS and sends a confirmation email via SES. It is short-lived, event-triggered, and stateless — exactly the workload Lambda was designed for (contrast with the long-lived Fargate services that hold DB connections).

> **Console:** AWS Lambda → **Create function**

1. **Author from scratch**
2. **Function name:** `cineticket-notification`
3. **Runtime:** Python 3.11
4. **Architecture:** arm64
5. **Permissions:** Use an existing role → `cineticket-notification-lambda-role`
6. Click **Create function**

**Paste the function code:**

7. In the **Code** tab, replace the default `lambda_function.py` content with:

```python
import json, logging, os, boto3

log = logging.getLogger()
log.setLevel(logging.INFO)
ses = boto3.client("ses")
SENDER_EMAIL = os.environ["SENDER_EMAIL"]

def lambda_handler(event, context):
    for record in event["Records"]:
        body    = json.loads(record["body"])
        message = json.loads(body["Message"])
        booking_id     = message.get("booking_id", "N/A")
        movie_id       = message.get("movie_id", "N/A")
        seat_id        = message.get("seat_id", "N/A")
        customer_email = message.get("customer_email")
        if not customer_email:
            continue
        ses.send_email(
            Source=SENDER_EMAIL,
            Destination={"ToAddresses": [customer_email]},
            Message={
                "Subject": {"Data": "Your CineTicket booking is confirmed!"},
                "Body": {"Text": {"Data":
                    f"Booking ID : {booking_id}\n"
                    f"Movie      : {movie_id}\n"
                    f"Seat       : {seat_id}\n\nEnjoy the show!"
                }},
            },
        )
        log.info("sent to %s for booking %s", customer_email, booking_id)
    return {"statusCode": 200}
```

8. Click **Deploy**

**Add the environment variable:**

9. **Configuration** tab → **Environment variables** → **Edit** → **Add environment variable:**
   - **Key:** `SENDER_EMAIL`
   - **Value:** your SES-verified email address (same one used for booking notifications)
10. Click **Save**

**Set the timeout:**

11. **Configuration** tab → **General configuration** → **Edit**
    - **Timeout:** `0` min `30` sec
    - Click **Save**

**Connect the SQS trigger:**

12. **Configuration** tab → **Triggers** → **Add trigger**
    - **Source:** SQS
    - **SQS queue:** `cineticket-notification-queue`
    - **Batch size:** `5`
    - **Enabled:** checked
13. Click **Add**

---

## 9. ECR Repositories and Docker Images

> **Why ECR before ECS?** The ECS task definitions reference a specific image URI in ECR. Build and push the images first so you have the real URI to paste into the task definition. No two-step "deploy placeholder → update" process.

### 9a. Create ECR Repositories

> **Console:** Amazon ECR → Private registry → **Repositories** → Create repository

Create the first repository:
1. **Visibility:** Private
2. **Repository name:** `cineticket-movies`
3. **Image scan settings → Scan on push:** enabled
4. Click **Create repository**

Repeat for the second:
1. **Repository name:** `cineticket-bookings`
2. Click **Create repository**

After both repos exist, click `cineticket-movies` → **View push commands** to see the exact CLI commands pre-filled with your account ID and region.

---

### 9b. Authenticate Docker to ECR

Replace `123456789012` with your AWS Account ID:

```bash
aws ecr get-login-password --region us-east-1 \
  | docker login --username AWS --password-stdin \
    123456789012.dkr.ecr.us-east-1.amazonaws.com
```
Expected output: `Login Succeeded`

---

### 9c. Build and Push the Movie Service Image

> **Apple Silicon (M1/M2/M3/M4):** The ECS task definitions are configured for ARM64 (Graviton). Build natively — no `--platform` flag needed.
> **Intel Mac / Linux x86_64:** Add `--platform linux/arm64` to each `docker build` command, or change the task definition's OS/Architecture to `X86_64` later.

From the `CineTicket - ECS Fargate Microservices/` folder:

```bash
docker build -t cineticket-movies ./app/services/movies

docker tag cineticket-movies:latest \
  123456789012.dkr.ecr.us-east-1.amazonaws.com/cineticket-movies:latest

docker push 123456789012.dkr.ecr.us-east-1.amazonaws.com/cineticket-movies:latest
```

Expected last line: `latest: digest: sha256:... size: ...`

---

### 9d. Build and Push the Booking Service Image

```bash
docker build -t cineticket-bookings ./app/services/bookings

docker tag cineticket-bookings:latest \
  123456789012.dkr.ecr.us-east-1.amazonaws.com/cineticket-bookings:latest

docker push 123456789012.dkr.ecr.us-east-1.amazonaws.com/cineticket-bookings:latest
```

---

## 10. ECS Cluster

The cluster is a logical grouping for services and tasks. With Fargate, there are no EC2 instances to manage — AWS provisions and manages the underlying compute.

> **Console:** Amazon ECS → Clusters → **Create cluster**

1. **Cluster name:** `cineticket-cluster`
2. **Infrastructure:** AWS Fargate (serverless) — ensure this is checked
3. **Monitoring:** leave defaults (enable Container Insights later if desired)
4. Click **Create**

---

## 11. Application Load Balancer

The ALB is the single public entry point for both services. It uses **path-based routing**: requests to `/movies` and `/movies/*` go to the Movie Service target group; requests to `/bookings` and `/bookings/*` go to the Booking Service target group. Any other path gets a fixed 404 response.

Create the target groups first — the ALB listener rules reference them.

---

### 11a. Movie Service Target Group

> **Console:** EC2 → Load Balancing → **Target Groups** → Create target group

1. **Target type:** IP addresses *(Fargate uses the ENI IP — not instance IDs)*
2. **Target group name:** `cineticket-movie-tg`
3. **Protocol:** HTTP | **Port:** `8080`
4. **VPC:** select `cineticket-vpc` (created in §0)
5. **Health checks:**
   - Protocol: HTTP
   - Path: `/health`
   - Healthy threshold: `2`
   - Unhealthy threshold: `3`
   - Timeout: `5`
   - Interval: `30`
   - Success codes: `200`

   > **Common mistake:** The console defaults the health check path to `/`. The apps only respond to `/health` — leave it as `/` and all targets will show Unhealthy.

6. Click **Next** → **Create target group** (do NOT register targets — ECS registers them automatically when the service starts)

---

### 11b. Booking Service Target Group

> **Console:** EC2 → Load Balancing → **Target Groups** → Create target group

1. **Target type:** IP addresses
2. **Target group name:** `cineticket-booking-tg`
3. **Protocol:** HTTP | **Port:** `8080`
4. **VPC:** select `cineticket-vpc` (created in §0)
5. **Health checks:** same as §11a (`/health`, interval 30, threshold 2/3) — change the default `/` path to `/health`
6. Click **Next** → **Create target group**

---

### 11c. Application Load Balancer

> **Console:** EC2 → Load Balancing → **Load Balancers** → Create load balancer → **Application Load Balancer**

1. **Load balancer name:** `cineticket-alb`
2. **Scheme:** Internet-facing

   > **Common mistake:** The first option in the scheme list is "Internal" — selecting it creates an ALB reachable only from inside the VPC. The browser and the S3 web UI cannot reach an internal ALB. Select **Internet-facing**.
3. **IP address type:** IPv4
4. **Network mapping:**
   - VPC: select `cineticket-vpc` (created in §0)
   - Mappings: tick both AZs → for each, select a **public** subnet (`cineticket-public-subnet-a` and `cineticket-public-subnet-b`)
5. **Security groups:** remove `default` → add `cineticket-alb-sg`
6. **Listeners and routing:**
   - Protocol: HTTP | Port: 80
   - Default action: Fixed response → Status code `404`, body `Not found. Use /movies or /bookings`
7. Click **Create load balancer**
8. Once created, copy the **DNS name** (e.g. `cineticket-alb-xxxx.us-east-1.elb.amazonaws.com`) — you need it for the Booking Service environment variable in §12b.

---

### 11d. Listener Routing Rules

> **Console:** EC2 → Load Balancers → `cineticket-alb` → **Listeners** tab → click the HTTP:80 listener → **Manage rules**

Add rule for the Movie Service:

1. Click **Add rule**
2. **Name:** `movies-rule`
3. Click **Next** → **Add condition** → **Path** → `/movies` and `/movies/*` → **Confirm** → **Next**
4. **Routing action:** Forward to target groups → select `cineticket-movie-tg` → **Next**
5. **Priority:** `10`
6. Click **Next** → **Create**

Add rule for the Booking Service:

7. Click **Add rule** again
8. **Name:** `bookings-rule`
9. **Condition:** Path → `/bookings` and `/bookings/*`
10. **Routing action:** Forward to `cineticket-booking-tg`
11. **Priority:** `20`
12. Click **Next** → **Create**

---

## 12. ECS Task Definitions

Task definitions are blueprints: they specify the container image, CPU/memory, environment variables, logging configuration, and the health check command. Create both task definitions before creating the ECS services.

---

### 12a. Movie Service Task Definition

> **Console:** Amazon ECS → Task definitions → **Create new task definition**

**Infrastructure requirements:**
1. **Task definition family:** `cineticket-movies`
2. **Launch type:** AWS Fargate
3. **OS/Architecture:** Linux/ARM64
4. **CPU:** `.25 vCPU`
5. **Memory:** `.5 GB`
6. **Task role:** `cineticket-movie-task-role`
7. **Task execution role:** `cineticket-ecs-execution-role`

**Container:**
8. **Name:** `movie-service`
9. **Image URI:** `123456789012.dkr.ecr.us-east-1.amazonaws.com/cineticket-movies:latest`
10. **Container port:** `8080` | Protocol: `TCP`

**Environment variables** — click **Add environment variable** for each:

| Key | Value |
|---|---|
| `DYNAMODB_TABLE_MOVIES` | `cineticket-movies` |
| `DYNAMODB_TABLE_SEATS` | `cineticket-seats` |
| `REDIS_HOST` | `<your-redis-primary-endpoint>` *(from §5b, hostname only — no port, no `:6379`)* |
| `REDIS_PORT` | `6379` |
| `AWS_REGION` | `us-east-1` |

> **Common mistake:** The ElastiCache console shows the Primary Endpoint as `hostname:6379`. Paste **only the hostname** into `REDIS_HOST`. If you paste the full `hostname:6379` value, the app will try to connect to host `hostname:6379` on port 6379 and the connection will always fail.

**Logging:**
11. **Log collection:** Use log collection → **awslogs**
    - `awslogs-group`: `/ecs/cineticket-movies`
    - `awslogs-region`: `us-east-1`
    - `awslogs-stream-prefix`: `movie`
    - **Create log group:** checked *(creates the CloudWatch log group automatically)*

**Health check:**
12. Expand **Health check** → **Command:**
    ```
    CMD-SHELL,python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')" || exit 1
    ```
    - **Interval:** `30`
    - **Timeout:** `5`
    - **Start period:** `60`
    - **Retries:** `3`

13. Click **Create**

---

### 12b. Booking Service Task Definition

> **Console:** Amazon ECS → Task definitions → **Create new task definition**

**Infrastructure requirements:**
1. **Task definition family:** `cineticket-bookings`
2. **Launch type:** AWS Fargate
3. **OS/Architecture:** Linux/ARM64
4. **CPU:** `.25 vCPU`
5. **Memory:** `.5 GB`
6. **Task role:** `cineticket-booking-task-role`
7. **Task execution role:** `cineticket-ecs-execution-role`

**Container:**
8. **Name:** `booking-service`
9. **Image URI:** `123456789012.dkr.ecr.us-east-1.amazonaws.com/cineticket-bookings:latest`
10. **Container port:** `8080` | Protocol: `TCP`

**Environment variables:**

| Key | Value |
|---|---|
| `DB_SECRET_NAME` | `cineticket/db` |
| `MOVIE_SERVICE_URL` | `http://<your-alb-dns>` *(the DNS name copied in §11c — must start with `http://`, no trailing slash)* |
| `SNS_TOPIC_ARN` | `<your-sns-topic-arn>` *(from §6c)* |
| `DYNAMODB_TABLE_SEATS` | `cineticket-seats` |
| `AWS_REGION` | `us-east-1` |

> **Common mistake:** `MOVIE_SERVICE_URL` must include the `http://` scheme prefix. The ALB DNS name shown in the console does not include it. Pasting `cineticket-alb-xxx.us-east-1.elb.amazonaws.com` without `http://` causes every movie-verification call in the Booking Service to fail with a URL parsing error.

**Logging:**
11. **Log collection:** awslogs
    - `awslogs-group`: `/ecs/cineticket-bookings`
    - `awslogs-region`: `us-east-1`
    - `awslogs-stream-prefix`: `booking`
    - **Create log group:** checked

**Health check:**
12. Same as Movie Service — python3 urllib.request hitting `http://localhost:8080/health`, interval 30, timeout 5, start period 60, retries 3.

13. Click **Create**

---

## 13. ECS Services

Services keep the desired number of tasks running and register them with the ALB target groups. Create the Movie Service first — it must be running before the Booking Service can reach it at the ALB URL.

> **Important:** Both services run in **public subnets** with **Auto-assign public IP: ENABLED**. The VPC has no NAT Gateway, so tasks in private subnets cannot reach ECR to pull images. Public subnets + public IP is the lab-appropriate shortcut.

---

### 13a. Movie Service

> **Console:** ECS → Clusters → `cineticket-cluster` → **Services** tab → **Create**

**Step 1 — Compute configuration:**
1. **Compute options:** Launch type
2. **Launch type:** FARGATE
3. **Platform version:** LATEST

**Step 2 — Deployment configuration:**
4. **Application type:** Service
5. **Task definition — Family:** `cineticket-movies` | **Revision:** LATEST
6. **Service name:** `cineticket-movie-service`
7. **Desired tasks:** `1`

**Step 3 — Networking:**
8. **VPC:** select `cineticket-vpc` (created in §0)
9. **Subnets:** select **both public subnets**
10. **Security group:** remove default → select `cineticket-movie-sg`
11. **Public IP:** ENABLED

**Step 4 — Load balancing:**
12. **Load balancer type:** Application Load Balancer
13. **Load balancer:** `cineticket-alb`
14. **Listener:** use existing → `HTTP 80`
15. **Target group:** use existing → `cineticket-movie-tg`
16. **Health check grace period:** `90` seconds *(gives the container time to start before ALB declares it unhealthy)*

17. Click **Create**

---

### 13b. Booking Service

> **Console:** ECS → Clusters → `cineticket-cluster` → **Services** tab → **Create**

**Step 1 — Compute configuration:**
1. **Compute options:** Launch type
2. **Launch type:** FARGATE
3. **Platform version:** LATEST

**Step 2 — Deployment configuration:**
4. **Application type:** Service
5. **Task definition — Family:** `cineticket-bookings` | **Revision:** LATEST
6. **Service name:** `cineticket-booking-service`
7. **Desired tasks:** `1`

**Step 3 — Networking:**
8. **VPC:** select `cineticket-vpc` (created in §0)
9. **Subnets:** select **both public subnets**
10. **Security group:** remove default → select `cineticket-booking-sg`
11. **Public IP:** ENABLED

**Step 4 — Load balancing:**
12. **Load balancer type:** Application Load Balancer
13. **Load balancer:** `cineticket-alb`
14. **Listener:** use existing → `HTTP 80`
15. **Target group:** use existing → `cineticket-booking-tg`
16. **Health check grace period:** `90` seconds

17. Click **Create**

**Wait for both services to stabilise:**

> **Console:** ECS → Clusters → `cineticket-cluster` → **Services** tab

Both services should show **Running count: 1**, **Desired: 1**. Then:

> **Console:** EC2 → Load Balancing → **Target Groups** → `cineticket-movie-tg` → **Targets** tab

Wait until both target group health statuses show **Healthy** before continuing. This takes ~2–3 minutes after the task starts (health check has a 60-second start period + 2 passing checks at 30-second intervals).

---

## 14. Application Auto Scaling (Booking Service)

The Booking Service is the write-heavy path — it holds RDS connections and processes CPU-intensive requests. Auto Scaling keeps it at 1 task at idle and scales to 3 when sustained CPU exceeds 60%. The Movie Service mostly serves Redis cache hits, so it stays at 1 task.

> **Console:** ECS → Clusters → `cineticket-cluster` → `cineticket-booking-service` → **Configuration and tasks** tab → scroll to **Service auto scaling** → **Update**

1. **Minimum number of tasks:** `1`
2. **Maximum number of tasks:** `3`
3. Click **Add scaling policy**
4. **Policy type:** Target tracking
5. **Scaling policy name:** `cineticket-booking-cpu-scaling`
6. **ECS service metric:** `ECSServiceAverageCPUUtilization`
7. **Target value:** `60`
8. **Scale-out cooldown:** `60` seconds
9. **Scale-in cooldown:** `120` seconds
10. Click **Update**

---

## 15. Seed DynamoDB with Sample Data

The Movie Service has no data until you load it. Run these CLI commands to add three movies and their seat maps.

> **Console:** DynamoDB → Tables → `cineticket-movies` → **Explore items** (verify here after running the commands)

```bash
# Three movies
aws dynamodb batch-write-item --region us-east-1 --request-items '{
  "cineticket-movies": [
    {"PutRequest": {"Item": {
      "movie_id":  {"S": "inception-2024"},
      "title":     {"S": "Inception"},
      "genre":     {"S": "Sci-Fi"},
      "director":  {"S": "Christopher Nolan"},
      "showtimes": {"L": [{"S": "7:00 PM"}, {"S": "10:00 PM"}]}
    }}},
    {"PutRequest": {"Item": {
      "movie_id":  {"S": "dune-2024"},
      "title":     {"S": "Dune: Part Two"},
      "genre":     {"S": "Sci-Fi"},
      "director":  {"S": "Denis Villeneuve"},
      "showtimes": {"L": [{"S": "6:30 PM"}, {"S": "9:30 PM"}]}
    }}},
    {"PutRequest": {"Item": {
      "movie_id":  {"S": "oppenheimer-2024"},
      "title":     {"S": "Oppenheimer"},
      "genre":     {"S": "Drama"},
      "director":  {"S": "Christopher Nolan"},
      "showtimes": {"L": [{"S": "5:00 PM"}, {"S": "8:30 PM"}]}
    }}}
  ]
}'
```

```bash
# Seats for all three movies (rows A–B, seats 1–3)
aws dynamodb batch-write-item --region us-east-1 --request-items '{
  "cineticket-seats": [
    {"PutRequest": {"Item": {"movie_id": {"S": "inception-2024"},    "seat_id": {"S": "A1"}, "status": {"S": "available"}}}},
    {"PutRequest": {"Item": {"movie_id": {"S": "inception-2024"},    "seat_id": {"S": "A2"}, "status": {"S": "available"}}}},
    {"PutRequest": {"Item": {"movie_id": {"S": "inception-2024"},    "seat_id": {"S": "A3"}, "status": {"S": "available"}}}},
    {"PutRequest": {"Item": {"movie_id": {"S": "inception-2024"},    "seat_id": {"S": "B1"}, "status": {"S": "available"}}}},
    {"PutRequest": {"Item": {"movie_id": {"S": "inception-2024"},    "seat_id": {"S": "B2"}, "status": {"S": "available"}}}},
    {"PutRequest": {"Item": {"movie_id": {"S": "dune-2024"},         "seat_id": {"S": "A1"}, "status": {"S": "available"}}}},
    {"PutRequest": {"Item": {"movie_id": {"S": "dune-2024"},         "seat_id": {"S": "A2"}, "status": {"S": "available"}}}},
    {"PutRequest": {"Item": {"movie_id": {"S": "dune-2024"},         "seat_id": {"S": "B1"}, "status": {"S": "available"}}}},
    {"PutRequest": {"Item": {"movie_id": {"S": "oppenheimer-2024"},  "seat_id": {"S": "A1"}, "status": {"S": "available"}}}},
    {"PutRequest": {"Item": {"movie_id": {"S": "oppenheimer-2024"},  "seat_id": {"S": "A2"}, "status": {"S": "available"}}}},
    {"PutRequest": {"Item": {"movie_id": {"S": "oppenheimer-2024"},  "seat_id": {"S": "B1"}, "status": {"S": "available"}}}}
  ]
}'
```

**Verify:** DynamoDB → Tables → `cineticket-seats` → **Explore items** — 11 items, all with `status: available`.

---

## 16. Load the RDS Schema

RDS is in a private subnet with no public access — you cannot connect to it from your laptop directly. The cleanest approach is a one-shot Fargate task using the Booking Service image (which already has `psycopg2` installed) with a Python command override that creates the `bookings` table and exits.

### 16a. Look Up the IDs You Need

You need two values — both visible in the console, or fetch them with the CLI commands below.

**Public Subnet A ID:**
```bash
aws ec2 describe-subnets \
  --filters "Name=tag:Name,Values=*PublicSubnet1*" \
  --query 'Subnets[0].SubnetId' --output text
```

**Booking Security Group ID:**
```bash
aws ec2 describe-security-groups \
  --filters "Name=group-name,Values=cineticket-booking-sg" \
  --query 'SecurityGroups[0].GroupId' --output text
```

---

### 16b. Run the Schema Task via CLI

> **Why CLI and not the ECS console?** The console's Container overrides **Command override** field splits on every comma — the Python command contains commas inside import statements, function calls, and the SQL, so the console breaks the command into garbage arguments. The CLI passes the command as a JSON array so commas inside the script are not interpreted as argument separators.

Replace `<subnet-id>` and `<sg-id>` with the values from §16a, then run:

```bash
aws ecs run-task \
  --cluster cineticket-cluster \
  --task-definition cineticket-bookings \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[<subnet-id>],securityGroups=[<sg-id>],assignPublicIp=ENABLED}" \
  --overrides '{"containerOverrides":[{"name":"booking-service","command":["python3","-c","import boto3,json,psycopg2; s=boto3.client(\"secretsmanager\",region_name=\"us-east-1\").get_secret_value(SecretId=\"cineticket/db\"); d=json.loads(s[\"SecretString\"]); conn=psycopg2.connect(host=d[\"host\"],port=int(d.get(\"port\",5432)),dbname=d[\"dbname\"],user=d[\"username\"],password=d[\"password\"],connect_timeout=10); cur=conn.cursor(); cur.execute(\"CREATE TABLE IF NOT EXISTS bookings (booking_id UUID PRIMARY KEY, movie_id VARCHAR(64) NOT NULL, seat_id VARCHAR(16) NOT NULL, customer_email VARCHAR(255) NOT NULL, booked_at TIMESTAMPTZ DEFAULT NOW())\"); conn.commit(); print(\"Schema applied OK\"); conn.close()"]}]}' \
  --region us-east-1
```

The command returns a task ARN immediately. The task runs for ~20 seconds and stops.

---

### 16c. Verify the Schema Loaded

> **Console:** ECS → Clusters → `cineticket-cluster` → **Tasks** tab

1. Find the task you just ran — wait for status to change from `RUNNING` to `STOPPED`
2. Click the task → **Logs** tab
3. Confirm you see: `Schema applied OK`

> **If the task fails:** Click the task → **Stopped reason** for the error. Common causes:
> - Wrong subnet or public IP not enabled → the task cannot reach ECR to pull the image
> - `cineticket/db` secret missing `host` → update the secret per §4c and retry

---

## 17. SES Email Verification

SES sandbox mode only allows sending to verified addresses. Verify your address so the Lambda can deliver booking confirmations.

> **Console:** Amazon SES → Configuration → **Verified identities**

1. If your email already appears with status **Verified** — skip to §18.
2. Click **Create identity**
   - **Identity type:** Email address
   - **Email address:** the address you entered as the `SENDER_EMAIL` Lambda environment variable
   - Click **Create identity**
3. Check your inbox for an email from AWS → click the verification link
4. Refresh the **Verified identities** page — status changes to **Verified**

> In sandbox mode you can only send *to* verified addresses too. For a real deployment, submit an SES production access request.

---

## 18. S3 Static Website — Web UI Hosting

S3 static website hosting serves the single-page web UI over HTTP, which pairs correctly with the HTTP-only ALB.

> **Why S3 and not Amplify?** Amplify serves your app over HTTPS. Your ALB has no SSL certificate and only listens on HTTP port 80. A browser on an HTTPS page will silently block `fetch()` calls to HTTP URLs (mixed-content policy) — requests appear to go out but receive 0 response headers and the UI shows "Cannot reach ALB". S3 static website hosting uses plain HTTP, matching the ALB.

---

### 18a. Create the S3 Bucket

> **Console:** S3 → **Create bucket**

1. **Bucket name:** `cineticket-web-<your-account-id>` — replace `<your-account-id>` with your 12-digit AWS account ID (makes the name globally unique)
2. **AWS Region:** `us-east-1`
3. **Object Ownership:** leave as **ACLs disabled (recommended)** (default)
4. **Block Public Access settings:** uncheck **Block all public access** → tick the acknowledgement checkbox that appears
5. Leave all other settings as default → **Create bucket**

---

### 18b. Enable Static Website Hosting

> **Console:** S3 → click your `cineticket-web-*` bucket → **Properties** tab → scroll to **Static website hosting** → **Edit**

1. **Static website hosting:** select **Enable**
2. **Hosting type:** leave as **Host a static website**
3. **Index document:** `index.html`
4. Click **Save changes**

After saving, the **Static website hosting** section shows the website endpoint:
```
http://cineticket-web-<account-id>.s3-website-us-east-1.amazonaws.com
```
Copy this URL.

---

### 18c. Attach a Public-Read Bucket Policy

> **Console:** S3 → bucket → **Permissions** tab → **Bucket policy** → **Edit**

Paste the policy below, replacing `<account-id>` with your 12-digit AWS account ID:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "PublicReadGetObject",
      "Effect": "Allow",
      "Principal": "*",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::cineticket-web-<account-id>/*"
    }
  ]
}
```

Click **Save changes**. The **Permissions** tab header changes from "Bucket and objects not public" to "Objects can be public".

---

### 18d. Upload the Web UI

> **Console:** S3 → bucket → **Objects** tab → **Upload**

1. Click **Add files** → select `app/web/index.html` from the project folder
2. Click **Upload**

---

### 18e. Open the Web UI and Connect to the API

1. Go to the **Properties** tab → **Static website hosting** section → click the **Bucket website endpoint** link
2. The CineTicket UI opens in your browser over HTTP
3. In the **API Configuration** card at the top, paste the ALB DNS name:
   - Format: `http://cineticket-alb-xxxxxxxx.us-east-1.elb.amazonaws.com`
   - Include `http://` — the ALB listener is HTTP port 80
   - No trailing slash
4. Click **Save & Connect**
5. A green **Connected ✓** badge confirms the ALB is reachable

---

## 19. End-to-End Verification

Run through all six sub-steps to validate every layer of the architecture.

### 19a. Browse Movies and Seat Map

1. **Browse Movies** card → click **Refresh Movies**
   → Three movie cards appear: Inception, Dune: Part Two, Oppenheimer
   → Toggle **Pretty** / **Raw JSON** in the API Response panel to see both views

2. **Seat Map** card → type `inception-2024` → click **Load Seat Map**
   → Green squares for seats A1–B2, all `available`
   → Click any seat — the **Book a Seat** form auto-fills

---

### 19b. Book a Seat (201 Confirmed)

1. **Book a Seat** card → fill in:
   - **Movie ID:** `inception-2024`
   - **Seat ID:** `A1`
   - **Your Email:** your SES-verified address
2. Click **Book Seat**

**Expected result:**
- Green banner: `✅ Booking confirmed! Check your email in ~10 seconds.`
- Table: `booking_id`, `movie_id`, `seat_id`, `status: confirmed`
- The `booking_id` auto-fills into the **Lookup & Cancel** card

> **Teaching point — 5-step booking flow:** (1) Booking Service called the Movie Service via the ALB to verify the movie exists. (2) DynamoDB conditional write flipped seat A1 from `available` → `booked` — if two requests arrive simultaneously, only one wins. (3) A row was inserted into RDS PostgreSQL. (4) The Movie Service cache was invalidated. (5) An `OrderPlaced` event was published to SNS. All synchronous except the email.

---

### 19c. Race Condition Demo (409 Conflict)

1. In **Book a Seat**, fill in the same `movie_id`, `seat_id`, and email as §19b
2. Click **Book Same Seat Twice** (the yellow button)

**Expected result:**
- Two side-by-side panels:
  - **Request 1:** `HTTP 201 ✅ Won the race`
  - **Request 2:** `HTTP 409 ❌ Lost — DynamoDB conditional write blocked it`

> **Teaching point:** Both requests hit DynamoDB within milliseconds. `ConditionExpression: status = 'available'` means only one update can succeed. The other gets `ConditionalCheckFailedException` → mapped to a clean 409. No locks, no transactions, no double-booking.

---

### 19d. Verify Cache Invalidation

1. **Seat Map** card → type `inception-2024` → click **Load Seat Map**
2. Seat A1 shows **red** (booked) immediately — even if less than 60 seconds have passed

> **Teaching point:** After the DynamoDB write in step 2 of the booking flow, the Booking Service called `POST /movies/inception-2024/invalidate-cache` on the Movie Service. That deleted the Redis key. The next `GET /movies/inception-2024` went back to DynamoDB for fresh data. Without invalidation, the seat would appear green for up to 60 seconds.

---

### 19e. Look Up a Booking

1. **Lookup & Cancel** card — the `booking_id` from §19b should already be filled in
2. Click **Lookup**

**Expected result (Pretty view):** table showing `booking_id`, `movie_id`, `seat_id`, `customer_email`, `booked_at` timestamp — this data comes from RDS PostgreSQL.

---

### 19f. Cancel a Booking

1. With the `booking_id` in the **Lookup & Cancel** card, click **Cancel & Release Seat**

**Expected result:**
- Amber banner: `🔓 Booking cancelled — seat returned to the available pool.`
- Table: `status: seat returned to pool`

2. Reload the seat map → seat A1 is **green** again

> **Teaching point:** Cancel reverses the booking in three steps: (1) DynamoDB seat status flipped back to `available` (unconditional — the booking record in RDS is our authority that we own this seat). (2) Booking row deleted from RDS. (3) Cache invalidated.

---

### 19g. Email Notification

- Check your inbox — the confirmation email should arrive within ~10 seconds of §19b
- **Subject:** `Your CineTicket booking is confirmed!`

> **Teaching point:** The booking API returned `201` immediately. The email arrived seconds later. The Booking Service published an SNS event and moved on — Lambda picked it up from SQS independently. This is why notifications should never block an HTTP response.

> **If no email arrives:**
> 1. SQS → `cineticket-notification-queue` → **Send and receive messages** → **Poll for messages**: if messages sit here, Lambda is not being triggered — check the event source mapping is enabled.
> 2. SQS → `cineticket-notification-dlq` → **Poll for messages**: messages here mean Lambda failed 3 times — check Lambda logs next.
> 3. Lambda → `cineticket-notification` → **Monitor** → **View CloudWatch logs**: look for `MessageRejected` — means the recipient is not SES-verified (§17).

---

## 20. CloudWatch Logs

> **Console:** CloudWatch → **Log groups**

### 20a. Movie Service — Cache Pattern

1. Click `/ecs/cineticket-movies` → open the latest log stream
2. Look for:
   - `cache MISS` — first read after a key expires or is invalidated
   - `cache HIT` — subsequent reads within the 60-second TTL
   - `cache invalidated` — triggered by a booking or cancellation from the Booking Service

### 20b. Booking Service — 5-Step Flow

1. Click `/ecs/cineticket-bookings` → open the latest log stream
2. After a successful booking: five sequential log lines, one per step of the booking flow

### 20c. Enable Container Insights (optional)

> **Console:** ECS → Clusters → `cineticket-cluster` → **Update cluster**

1. **Container Insights:** toggle **On** → click **Update**

After a few minutes: CloudWatch → **Container Insights** → **ECS Clusters** → select `cineticket-cluster` → view per-service CPU, memory, and network graphs.

---

## 21. Auto-Scaling Demo

> **What to observe:** Booking Service scales from 1 task to 2–3 tasks when CPU exceeds 60% for a sustained period.

Open two terminal windows side by side.

**Window 1 — watch the task count:**

```bash
watch -n 5 "aws ecs describe-services \
  --cluster cineticket-cluster \
  --services cineticket-booking-service \
  --query 'services[0].runningCount' --output text"
```

**Window 2 — generate load** (install: `brew install hey`):

```bash
ALB="cineticket-alb-xxxxxxxx.us-east-1.elb.amazonaws.com"

hey -z 120s -c 50 \
  -m POST \
  -H "Content-Type: application/json" \
  -d '{"movie_id":"dune-2024","seat_id":"B1","customer_email":"load@example.com"}' \
  http://$ALB/bookings
```

> After ~60–90 seconds of sustained CPU above 60%, ECS triggers the target tracking policy and adds tasks. The count climbs 1 → 2 → 3 in Window 1 and in the ECS console.
>
> **Teaching point — independent scaling:** Only the write-heavy Booking Service scales. The Movie Service coasts on Redis cache hits. This is the operational benefit of microservices over a monolith — you scale only the component under pressure.

Also verify in the console:

> **Console:** ECS → Clusters → `cineticket-cluster` → `cineticket-booking-service` → **Configuration and tasks** tab → **Auto scaling** section → **Target tracking policies**

The current value and alarm state are visible here. You can also watch the Application Auto Scaling activity under:

> **Console:** Application Auto Scaling → **Scalable targets** → search for `cineticket-booking-service` → **Scaling activities** tab

---

## 22. Cleanup

Delete resources in reverse-dependency order. The ECR repos were created manually (outside the main resource set) and must be emptied first.

### 22a. Empty and Delete the S3 Web Bucket

> **Console:** S3 → `cineticket-web-<account-id>`

1. Click **Empty** → type `permanently delete` → click **Empty**
2. After the bucket is empty, click **Delete bucket** → type the full bucket name → confirm → **Delete bucket**

---

### 22b. Remove Application Auto Scaling

> **Console:** ECS → Clusters → `cineticket-cluster` → `cineticket-booking-service` → **Configuration and tasks** tab → **Update service**

1. **Service auto scaling:** set **Minimum** and **Maximum** both to `0`, delete the scaling policy → **Update**

---

### 22c. Delete ECS Services

> **Console:** ECS → Clusters → `cineticket-cluster` → **Services** tab

1. Select `cineticket-booking-service` → **Delete** → set desired count to `0` → confirm → Delete
2. Repeat for `cineticket-movie-service`

---

### 22d. Delete ECS Cluster

> **Console:** ECS → Clusters → select `cineticket-cluster` → **Delete cluster** → confirm

---

### 22e. Delete the ALB and Target Groups

> **Console:** EC2 → Load Balancing → **Load Balancers** → select `cineticket-alb` → **Actions → Delete**

> **Console:** EC2 → Load Balancing → **Target Groups**

1. Select `cineticket-movie-tg` → **Actions → Delete**
2. Select `cineticket-booking-tg` → **Actions → Delete**

---

### 22f. Delete ECR Images and Repositories

> **Console:** ECR → Private registry → **Repositories**

1. Click `cineticket-movies` → select all images → **Delete** → confirm
2. Back → select `cineticket-movies` repository → **Delete** → confirm
3. Repeat for `cineticket-bookings`

Or via CLI (faster):

```bash
aws ecr batch-delete-image --region us-east-1 --repository-name cineticket-movies \
  --image-ids "$(aws ecr list-images --region us-east-1 --repository-name cineticket-movies \
    --query 'imageIds' --output json)"

aws ecr batch-delete-image --region us-east-1 --repository-name cineticket-bookings \
  --image-ids "$(aws ecr list-images --region us-east-1 --repository-name cineticket-bookings \
    --query 'imageIds' --output json)"

aws ecr delete-repository --region us-east-1 --repository-name cineticket-movies --force
aws ecr delete-repository --region us-east-1 --repository-name cineticket-bookings --force
```

---

### 22g. Delete Lambda and Messaging

> **Console:** Lambda → Functions → `cineticket-notification` → **Actions → Delete**

> **Console:** SQS → Queues

1. Select `cineticket-notification-queue` → **Delete** → confirm
2. Select `cineticket-notification-dlq` → **Delete** → confirm

> **Console:** SNS → Topics → select `cineticket-order-placed` → **Delete**

---

### 22h. Delete RDS and ElastiCache

> **Console:** Amazon RDS → Databases → select `cineticket-db` → **Actions → Delete**

- Uncheck **Create final snapshot** (lab cleanup)
- Uncheck **Retain automated backups**
- Type `delete me` → click **Delete**

> **Console:** Amazon ElastiCache → Redis OSS caches → select `cineticket-redis` → **Delete**

After RDS and Redis are deleted:

> **Console:** RDS → Subnet groups → `cineticket-db-subnet-group` → **Delete**

> **Console:** ElastiCache → Subnet groups → `cineticket-redis-subnet-group` → **Delete**

---

### 22i. Delete Secrets Manager, DynamoDB, and IAM

> **Console:** Secrets Manager → `cineticket/db` → **Delete secret** *(30-day recovery window — tick "Disable waiting period" if available)*

> **Console:** DynamoDB → Tables

1. Select `cineticket-movies` → **Delete** → confirm
2. Select `cineticket-seats` → **Delete** → confirm

> **Console:** IAM → Roles — delete each role:
- `cineticket-ecs-execution-role`
- `cineticket-movie-task-role`
- `cineticket-booking-task-role`
- `cineticket-notification-lambda-role`

---

### 22j. Delete Security Groups

> **Console:** EC2 → Network & Security → **Security Groups**

Delete in this order (dependent groups first):
1. `cineticket-rds-sg`
2. `cineticket-redis-sg`
3. `cineticket-booking-sg`
4. `cineticket-movie-sg`
5. `cineticket-alb-sg`

---

## Appendix A — CloudFormation Shortcut

The template `cfn/template.yaml` in this repo automates everything in §1–§13 plus auto scaling into a single stack. Use it to skip the manual ClickOps steps and get to the seeding and testing stages faster.

**What the template provisions:**
All five security groups · both DynamoDB tables · Secrets Manager secret + SecretTargetAttachment (auto-populates RDS host/port into the secret) · RDS PostgreSQL + DB subnet group · ElastiCache Redis + Redis subnet group · SNS topic + SQS queue + SQS DLQ + SQS queue policy + SNS subscription · Lambda function with inline code · Lambda event source mapping · Task Execution Role + Movie Task Role + Booking Task Role + Lambda role · ECS Cluster · Movie task definition + Movie ECS service · Booking task definition + Booking ECS service · ALB + both target groups + HTTP listener + two routing rules · Application Auto Scaling on the Booking Service · CloudWatch log groups · S3 web bucket with static website hosting and public-read policy.

**What the template does NOT provision** (still manual):
- ECR repositories and Docker image builds (§9)
- DynamoDB seed data (§15)
- RDS schema (§16)
- SES email verification (§17)
- Uploading `app/web/index.html` to the S3 bucket (§18d)

---

### Using the Template

**Step 1 — Create ECR repos and push images first** (§9 above):

```bash
# Create repos
aws ecr create-repository --repository-name cineticket-movies --region us-east-1
aws ecr create-repository --repository-name cineticket-bookings --region us-east-1

# Authenticate, build, push (replace 123456789012 with your account ID)
aws ecr get-login-password --region us-east-1 | docker login --username AWS \
  --password-stdin 123456789012.dkr.ecr.us-east-1.amazonaws.com

docker build -t cineticket-movies ./app/services/movies
docker tag cineticket-movies:latest 123456789012.dkr.ecr.us-east-1.amazonaws.com/cineticket-movies:latest
docker push 123456789012.dkr.ecr.us-east-1.amazonaws.com/cineticket-movies:latest

docker build -t cineticket-bookings ./app/services/bookings
docker tag cineticket-bookings:latest 123456789012.dkr.ecr.us-east-1.amazonaws.com/cineticket-bookings:latest
docker push 123456789012.dkr.ecr.us-east-1.amazonaws.com/cineticket-bookings:latest
```

**Step 2 — Deploy the stack with real image URIs:**

> **Console:** CloudFormation → **Create stack** → With new resources (standard)

1. Click **Upload a template file** → select `cfn/template.yaml` → **Next**
2. **Stack name:** `cineticket`
3. Fill in parameters:

   | Parameter | Value |
   |---|---|
   | **VpcCidr** | `10.30.0.0/16` (default — change only if it conflicts with existing VPCs in your account) |
   | **PublicSubnetACidr** | `10.30.0.0/24` (default) |
   | **PublicSubnetBCidr** | `10.30.1.0/24` (default) |
   | **PrivateSubnetACidr** | `10.30.10.0/24` (default) |
   | **PrivateSubnetBCidr** | `10.30.11.0/24` (default) |
   | **DbSubnetACidr** | `10.30.20.0/24` (default) |
   | **DbSubnetBCidr** | `10.30.21.0/24` (default) |
   | **MovieImageUri** | `123456789012.dkr.ecr.us-east-1.amazonaws.com/cineticket-movies:latest` |
   | **BookingImageUri** | `123456789012.dkr.ecr.us-east-1.amazonaws.com/cineticket-bookings:latest` |
   | **NotificationEmail** | Your SES-verified email address |
   | **DBMasterUsername** | `cineticket` (default) |
   | **AllowedAlbCidr** | `0.0.0.0/0` (or your classroom IP `/32`) |

4. Click **Next** → **Next**
5. Under **Capabilities:** check **I acknowledge that AWS CloudFormation might create IAM resources with custom names**
6. Click **Submit**

Stack takes ~12 minutes (`CREATE_IN_PROGRESS` → `CREATE_COMPLETE`). RDS is the slowest resource. Watch the **Events** tab for live progress.

**Step 3 — Note stack outputs:**

> **Console:** CloudFormation → Stacks → `cineticket` → **Outputs** tab

| Output Key | Used in |
|---|---|
| `AlbDnsName` | Paste into the web UI as API Base URL |
| `S3WebsiteUrl` | Open in browser to access the web UI |
| `ECSClusterName` | ECS console navigation |
| `DynamoMoviesTable` | Seed data CLI commands |
| `DynamoSeatsTable` | Seed data CLI commands |

**Step 4 — Continue from §15** (DynamoDB seed, RDS schema, SES, upload `index.html` to S3 per §18d, verification).

**Cleanup via template:**

```bash
aws cloudformation delete-stack --stack-name cineticket --region us-east-1
```

The stack deletes all resources it created. Two manual steps first:
1. Empty the S3 web bucket before deleting the stack — CloudFormation cannot delete a non-empty bucket: S3 → `cineticket-web-<account-id>` → **Empty** → confirm.
2. ECR repos (created manually) must be emptied and deleted separately (§22f).

---

## Appendix B — Troubleshooting

### ECS tasks keep stopping immediately

> **Console:** ECS → Clusters → `cineticket-cluster` → Service → **Tasks** tab → click a **Stopped** task

- **Stopped reason: `CannotPullContainerError`** — two causes:
  - **Cannot reach ECR:** Confirm the subnet is **public** and **Auto-assign public IP** is **ENABLED**.
  - **Platform mismatch (`image Manifest does not contain descriptor matching platform 'linux/amd64'`):** Your Docker image was built for ARM64 (e.g., on Apple Silicon) but the task definition OS/Architecture is set to `X86_64`. Fix: set the task definition's OS/Architecture to **Linux/ARM64**, or rebuild the image with `--platform linux/amd64`.
- **Stopped reason: container health check failed** — the container started but `GET /health` failed. Click the **Logs** tab and look for a Python exception at startup (most likely a missing or malformed environment variable).
- **Stopped reason: essential container exited** — gunicorn crashed. Check logs for an import error or a boto3 call that failed at startup.

---

### Web UI shows "Cannot reach ALB"

The browser cannot connect to the ALB at all — this is different from a 502/503 response.

- **ALB scheme is Internal:** Check EC2 → Load Balancers → `cineticket-alb` → **Scheme** field. If it shows "internal" the ALB is only reachable inside the VPC. ALB scheme cannot be changed after creation — delete the ALB and recreate it with **Scheme: Internet-facing**. Target groups survive the delete; recreate only the ALB and its listener/rules.
- **Security group blocks port 80:** Confirm `cineticket-alb-sg` has an inbound rule for TCP 80 from `0.0.0.0/0`.

---

### ALB returns 502 Bad Gateway

The ALB reached a target but the connection was refused or the response was invalid.

> **Console:** EC2 → Load Balancing → **Target Groups** → select a target group → **Targets** tab

- If targets show **Initial** or **Unhealthy:** the container is still starting up — wait 90 seconds and refresh. The container health check has a 60-second start period, plus 2 passing checks at 30-second intervals.
- If targets show **Healthy** and 502 still occurs: the application is running but throwing an exception on this specific request — check the ECS task logs.

---

### ALB returns 503 Service Unavailable

No healthy targets are registered — the ECS service has 0 running tasks.

> **Console:** ECS → Service → **Events** tab

Look for deployment or placement failure messages. Common cause: the task definition's IAM task role is missing a required permission (e.g., DynamoDB access).

---

### Booking Service returns "could not reach Movie Service"

The Booking Service calls `http://<ALB_DNS>/movies/{id}` to verify a movie exists before reserving a seat. If this fails:

1. Confirm the Movie Service is healthy: EC2 → Target Groups → `cineticket-movie-tg` → **Targets** tab → status **Healthy**
2. Confirm `MOVIE_SERVICE_URL` in the Booking Service task definition starts with `http://` and has no trailing slash
3. Confirm the `cineticket-alb-sg` allows inbound HTTP (port 80) from `0.0.0.0/0` — the Booking Service task reaches the ALB over the internet (both run in public subnets, so traffic leaves and re-enters via the ALB's public DNS)

---

### Emails not arriving after a booking

Work down the SNS → SQS → Lambda → SES chain:

1. **SES verified?** → SES → Verified identities → `Status: Verified` for your email address
2. **Messages in queue?** → SQS → `cineticket-notification-queue` → **Send and receive messages** → **Poll for messages** — if messages are here, Lambda is not consuming them. Check that the event source mapping is enabled: Lambda → `cineticket-notification` → **Configuration → Triggers**
3. **Messages in DLQ?** → SQS → `cineticket-notification-dlq` → **Poll for messages** — 3 Lambda failures land here
4. **Lambda errors?** → Lambda → `cineticket-notification` → **Monitor** → **View CloudWatch logs** — look for `MessageRejected: Email address is not verified`

Most common error: the recipient email used in the booking form was not verified in SES.

---

### Booking health check failing (503 from /health)

The Booking Service `/health` endpoint runs `SELECT 1` against RDS. If it returns 503:

1. Confirm the Secrets Manager secret `cineticket/db` has all five fields: `host`, `username`, `password`, `dbname`, `port`. An incomplete secret causes a startup crash that looks identical to a health check failure.
2. Confirm `cineticket-rds-sg` inbound rules include TCP 5432 from `cineticket-booking-sg` — verify on the RDS SG, not the Booking SG.
3. Confirm the task definition's task role is `cineticket-booking-task-role` (not the execution role). The `secretsmanager:GetSecretValue` permission is on the task role, not the execution role.

---

### One-shot schema task fails

> **Console:** ECS → Tasks → click the stopped task → **Stopped reason**

- **CannotPullContainerError** → subnet is not public, or public IP is not enabled — retry with Public Subnet A and ENABLED
- **Python error: KeyError 'host'** → the Secrets Manager secret was not updated with the RDS endpoint (§4c)
- **Connection refused / timeout** → confirm the `cineticket-rds-sg` has a rule allowing 5432 from `cineticket-booking-sg`

# MedExtract AI - AWS processing stack (HIPAA-eligible services only)
#
#   S3 (KMS) --notification(drop/)--> SQS --> Lambda worker --> Textract + Bedrock
#                                      |                       --> DynamoDB results
#                                      +--> DLQ --> CloudWatch alarm
#
# The FastAPI service can run on ECS Fargate / App Runner using the same image
# (Dockerfile) and the IAM policy below; it writes to incoming/ and enqueues.

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = { source = "hashicorp/aws", version = ">= 5.40" }
  }
}

provider "aws" {
  region = var.region
  default_tags { tags = { Project = "medextract-ai", Environment = var.environment, DataClass = "PHI" } }
}

locals { name = "medextract-${var.environment}" }

data "aws_caller_identity" "me" {}

# ------------------------------------------------------------------ KMS
resource "aws_kms_key" "phi" {
  description             = "${local.name} PHI encryption"
  enable_key_rotation     = true
  deletion_window_in_days = 30
}

# ------------------------------------------------------------------- S3
resource "aws_s3_bucket" "docs" {
  bucket = "${local.name}-docs-${data.aws_caller_identity.me.account_id}"
}

resource "aws_s3_bucket_public_access_block" "docs" {
  bucket                  = aws_s3_bucket.docs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "docs" {
  bucket = aws_s3_bucket.docs.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "docs" {
  bucket = aws_s3_bucket.docs.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.phi.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "docs" {
  bucket = aws_s3_bucket.docs.id
  rule {
    id     = "retention"
    status = "Enabled"
    filter {}
    transition {
      days          = 30
      storage_class = "STANDARD_IA"
    }
    expiration { days = var.retention_days }
  }
}

resource "aws_s3_bucket_policy" "tls_only" {
  bucket = aws_s3_bucket.docs.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.docs.arn, "${aws_s3_bucket.docs.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}

# ------------------------------------------------------------------ SQS
resource "aws_sqs_queue" "dlq" {
  name                      = "${local.name}-dlq"
  kms_master_key_id         = aws_kms_key.phi.id
  message_retention_seconds = 1209600
}

resource "aws_sqs_queue" "jobs" {
  name                       = "${local.name}-jobs"
  kms_master_key_id          = aws_kms_key.phi.id
  visibility_timeout_seconds = 960 # > 6x Lambda timeout guidance
  redrive_policy             = jsonencode({ deadLetterTargetArn = aws_sqs_queue.dlq.arn, maxReceiveCount = 3 })
}

resource "aws_sqs_queue_policy" "s3_to_sqs" {
  queue_url = aws_sqs_queue.jobs.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "s3.amazonaws.com" }
      Action    = "sqs:SendMessage"
      Resource  = aws_sqs_queue.jobs.arn
      Condition = { ArnEquals = { "aws:SourceArn" = aws_s3_bucket.docs.arn } }
    }]
  })
}

# Batch ingestion (fax server, SFTP, EHR export) drops files under drop/
resource "aws_s3_bucket_notification" "drop" {
  bucket = aws_s3_bucket.docs.id
  queue {
    queue_arn     = aws_sqs_queue.jobs.arn
    events        = ["s3:ObjectCreated:*"]
    filter_prefix = "drop/"
  }
  depends_on = [aws_sqs_queue_policy.s3_to_sqs]
}

# ------------------------------------------------------------- DynamoDB
resource "aws_dynamodb_table" "results" {
  name         = "${local.name}-results"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "document_id"

  attribute {
    name = "document_id"
    type = "S"
  }
  attribute {
    name = "status"
    type = "S"
  }
  attribute {
    name = "created_at"
    type = "S"
  }

  global_secondary_index {
    name            = "status-index"
    hash_key        = "status"
    range_key       = "created_at"
    projection_type = "ALL"
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.phi.arn
  }
  point_in_time_recovery { enabled = true }
}

# ------------------------------------------------------------------ IAM
resource "aws_iam_role" "worker" {
  name = "${local.name}-worker"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = ["lambda.amazonaws.com", "ecs-tasks.amazonaws.com"] }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy" "worker" {
  role = aws_iam_role.worker.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject"], Resource = "${aws_s3_bucket.docs.arn}/*" },
      { Effect = "Allow", Action = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:SendMessage"], Resource = aws_sqs_queue.jobs.arn },
      { Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:Query", "dynamodb:Scan"], Resource = [aws_dynamodb_table.results.arn, "${aws_dynamodb_table.results.arn}/index/*"] },
      { Effect = "Allow", Action = ["kms:Decrypt", "kms:GenerateDataKey"], Resource = aws_kms_key.phi.arn },
      { Effect = "Allow", Action = ["textract:AnalyzeDocument", "textract:StartDocumentAnalysis", "textract:GetDocumentAnalysis"], Resource = "*" },
      { Effect = "Allow", Action = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"], Resource = var.bedrock_model_arns },
      { Effect = "Allow", Action = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"], Resource = "*" },
    ]
  })
}

# --------------------------------------------------------------- Lambda
resource "aws_cloudwatch_log_group" "worker" {
  name              = "/aws/lambda/${local.name}-worker"
  retention_in_days = 90
  kms_key_id        = aws_kms_key.phi.arn
}

resource "aws_lambda_function" "worker" {
  function_name = "${local.name}-worker"
  role          = aws_iam_role.worker.arn
  package_type  = "Image"
  image_uri     = var.worker_image_uri
  timeout       = 150
  memory_size   = 1024
  architectures = ["x86_64"]
  kms_key_arn   = aws_kms_key.phi.arn

  environment {
    variables = {
      MEDX_ENVIRONMENT        = var.environment
      MEDX_OCR_PROVIDER       = "textract"
      MEDX_LLM_PROVIDER       = "bedrock"
      MEDX_BEDROCK_MODEL_ID   = var.bedrock_model_id
      MEDX_S3_BUCKET          = aws_s3_bucket.docs.bucket
      MEDX_KMS_KEY_ID         = aws_kms_key.phi.arn
      MEDX_STORAGE_BACKEND    = "dynamodb"
      MEDX_DYNAMODB_TABLE     = aws_dynamodb_table.results.name
      MEDX_REDACT_PHI_IN_LOGS = "true"
    }
  }
  depends_on = [aws_cloudwatch_log_group.worker]
}

resource "aws_lambda_event_source_mapping" "jobs" {
  event_source_arn                   = aws_sqs_queue.jobs.arn
  function_name                      = aws_lambda_function.worker.arn
  batch_size                         = 5
  maximum_batching_window_in_seconds = 5
  function_response_types            = ["ReportBatchItemFailures"]
  scaling_config { maximum_concurrency = var.max_concurrency } # protects Bedrock/Textract quotas
}

# ----------------------------------------------------------- Monitoring
resource "aws_cloudwatch_metric_alarm" "dlq_not_empty" {
  alarm_name          = "${local.name}-dlq-not-empty"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.dlq.name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  alarm_actions       = var.alarm_sns_topic_arn == "" ? [] : [var.alarm_sns_topic_arn]
}

resource "aws_cloudwatch_metric_alarm" "worker_errors" {
  alarm_name          = "${local.name}-worker-errors"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.worker.function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 2
  threshold           = 5
  comparison_operator = "GreaterThanThreshold"
  alarm_actions       = var.alarm_sns_topic_arn == "" ? [] : [var.alarm_sns_topic_arn]
}

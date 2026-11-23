variable "region" {
  type    = string
  default = "us-east-1"
}

variable "environment" {
  type    = string
  default = "dev"
}

variable "worker_image_uri" {
  description = "ECR image built from Dockerfile.lambda"
  type        = string
}

variable "bedrock_model_id" {
  description = "Bedrock model or inference-profile ID enabled in this account/region"
  type        = string
  default     = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
}

variable "bedrock_model_arns" {
  description = "ARNs the worker may invoke (foundation models and/or inference profiles)"
  type        = list(string)
  default     = ["arn:aws:bedrock:*::foundation-model/anthropic.*", "arn:aws:bedrock:*:*:inference-profile/*"]
}

variable "retention_days" {
  description = "Raw document retention; align with your records-retention policy"
  type        = number
  default     = 2555 # ~7 years
}

variable "max_concurrency" {
  type    = number
  default = 10
}

variable "alarm_sns_topic_arn" {
  type    = string
  default = ""
}

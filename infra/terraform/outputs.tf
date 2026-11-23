output "bucket" { value = aws_s3_bucket.docs.bucket }
output "queue_url" { value = aws_sqs_queue.jobs.url }
output "dlq_url" { value = aws_sqs_queue.dlq.url }
output "results_table" { value = aws_dynamodb_table.results.name }
output "kms_key_arn" { value = aws_kms_key.phi.arn }
output "worker_role_arn" { value = aws_iam_role.worker.arn }

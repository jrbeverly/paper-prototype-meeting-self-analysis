resource "aws_s3_bucket" "artifacts" {
  bucket        = local.bucket_name
  force_destroy = var.artifact_bucket_force_destroy
}

resource "aws_s3_bucket_ownership_controls" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  rule {
    id     = "abort-incomplete-multipart-uploads"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = var.abort_incomplete_multipart_days
    }
  }

  depends_on = [aws_s3_bucket_versioning.artifacts]
}

data "aws_iam_policy_document" "artifact_bucket" {
  statement {
    sid    = "DenyInsecureTransport"
    effect = "Deny"

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.artifacts.arn,
      "${aws_s3_bucket.artifacts.arn}/*",
    ]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  policy = data.aws_iam_policy_document.artifact_bucket.json

  depends_on = [aws_s3_bucket_public_access_block.artifacts]
}

resource "aws_s3_object" "source_bundle" {
  bucket = aws_s3_bucket.artifacts.id
  key    = var.source_bundle_key
  source = var.source_bundle_path
  etag   = filemd5(var.source_bundle_path)

  content_type           = "application/zip"
  server_side_encryption = "AES256"
  source_hash            = filebase64sha256(var.source_bundle_path)

  depends_on = [
    aws_s3_bucket_server_side_encryption_configuration.artifacts,
    aws_s3_bucket_versioning.artifacts,
  ]
}

resource "aws_ssm_parameter" "config" {
  name        = local.config_parameter_name
  description = "Non-secret JSON configuration for ${local.resource_prefix} scheduled reconcilers"
  type        = "String"
  value = jsonencode(merge(
    jsondecode(var.config_json),
    {
      artifactBucket = aws_s3_bucket.artifacts.bucket
      activePrefix   = var.meetings_prefix
    },
  ))
  data_type = "text"
  tier      = "Standard"
}

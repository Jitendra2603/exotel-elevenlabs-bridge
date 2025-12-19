#!/bin/bash

# AWS Deployment Script for Exotel-ElevenLabs Bridge
# This script builds a container and deploys it to AWS App Runner with full IAM setup

# Exit on error
set -e

# Configuration
SERVICE_NAME="exotel-bridge"
REGION="us-east-1" # Change as needed
ECR_REPO_NAME="exotel-bridge-repo"
ROLE_NAME="AppRunnerECRAccessRole"

# Check for required environment variables
if [ -z "$ELEVENLABS_AGENT_ID" ]; then
    echo "Error: ELEVENLABS_AGENT_ID is not set."
    echo "Please set it: export ELEVENLABS_AGENT_ID=your_agent_id"
    exit 1
fi

if [ -z "$ELEVENLABS_API_KEY" ]; then
    echo "Warning: ELEVENLABS_API_KEY is not set."
fi

# Check if AWS CLI is installed
if ! command -v aws &> /dev/null; then
    echo "Error: AWS CLI is not installed. Please install it first."
    exit 1
fi

# Get AWS Account ID
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
if [ -z "$ACCOUNT_ID" ]; then
    echo "Error: Could not retrieve AWS Account ID. Ensure 'aws configure' is set up."
    exit 1
fi

echo "Deploying $SERVICE_NAME to AWS account $ACCOUNT_ID in $REGION..."

# 1. Setup IAM Role for App Runner
echo "Checking IAM role: $ROLE_NAME..."
if ! aws iam get-role --role-name "$ROLE_NAME" > /dev/null 2>&1; then
    echo "Creating IAM role: $ROLE_NAME..."
    
    TRUST_POLICY='{
      "Version": "2012-10-17",
      "Statement": [
        {
          "Effect": "Allow",
          "Principal": {
            "Service": "build.apprunner.amazonaws.com"
          },
          "Action": "sts:AssumeRole"
        }
      ]
    }'
    
    aws iam create-role \
        --role-name "$ROLE_NAME" \
        --assume-role-policy-document "$TRUST_POLICY"
    
    echo "Attaching AWSAppRunnerServicePolicyForECRAccess to role..."
    aws iam attach-role-policy \
        --role-name "$ROLE_NAME" \
        --policy-arn "arn:aws:iam::aws:policy/service-role/AWSAppRunnerServicePolicyForECRAccess"
    
    # Give AWS some time to propagate IAM changes
    echo "Waiting for IAM propagation..."
    sleep 10
else
    echo "IAM role $ROLE_NAME already exists."
fi

ROLE_ARN="arn:aws:iam::$ACCOUNT_ID:role/$ROLE_NAME"

# 2. Create ECR Repository if it doesn't exist
echo "Checking ECR repository: $ECR_REPO_NAME..."
if ! aws ecr describe-repositories --repository-names "$ECR_REPO_NAME" --region "$REGION" > /dev/null 2>&1; then
    echo "Creating ECR repository..."
    aws ecr create-repository --repository-name "$ECR_REPO_NAME" --region "$REGION"
fi

# 3. Login to ECR
echo "Logging into ECR..."
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com"

# 4. Build and Push Image
echo "Building container image..."
IMAGE_URL="$ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/$ECR_REPO_NAME:latest"
docker build -t "$ECR_REPO_NAME" .
docker tag "$ECR_REPO_NAME:latest" "$IMAGE_URL"

echo "Pushing image to ECR..."
docker push "$IMAGE_URL"

# 5. Deploy to App Runner
echo "Checking for existing App Runner service..."
SERVICE_ARN=$(aws apprunner list-services --region "$REGION" --query "ServiceSummaryList[?ServiceName=='$SERVICE_NAME'].ServiceArn" --output text)

if [ -z "$SERVICE_ARN" ] || [ "$SERVICE_ARN" == "None" ]; then
    echo "Creating new App Runner service..."
    aws apprunner create-service \
        --service-name "$SERVICE_NAME" \
        --region "$REGION" \
        --source-configuration "{
            \"AuthenticationConfiguration\": {
                \"AccessRoleArn\": \"$ROLE_ARN\"
            },
            \"ImageRepository\": {
                \"ImageIdentifier\": \"$IMAGE_URL\",
                \"ImageConfiguration\": {
                    \"Port\": \"10002\",
                    \"RuntimeEnvironmentVariables\": [
                        { \"Name\": \"ELEVENLABS_AGENT_ID\", \"Value\": \"$ELEVENLABS_AGENT_ID\" },
                        { \"Name\": \"ELEVENLABS_API_KEY\", \"Value\": \"$ELEVENLABS_API_KEY\" }
                    ]
                },
                \"ImageRepositoryType\": \"ECR\"
            }
        }"
else
    echo "Updating existing App Runner service: $SERVICE_ARN"
    aws apprunner update-service \
        --service-arn "$SERVICE_ARN" \
        --region "$REGION" \
        --source-configuration "{
            \"ImageRepository\": {
                \"ImageIdentifier\": \"$IMAGE_URL\",
                \"ImageConfiguration\": {
                    \"Port\": \"10002\"
                }
            }
        }"
fi

echo -e "\n=========================================="
echo "Deployment initiated!"
echo "Service: $SERVICE_NAME"
echo "Region: $REGION"
echo "=========================================="
echo "You can monitor the status with:"
echo "aws apprunner list-services --region $REGION --query \"ServiceSummaryList[?ServiceName=='$SERVICE_NAME']\""
echo -e "==========================================\n"

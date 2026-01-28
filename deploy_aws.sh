#!/bin/bash

# AWS Deployment Script for Exotel-ElevenLabs Bridge
# This script builds a container and deploys it to AWS App Runner with full IAM setup

# Exit on error
set -e

# Configuration
SERVICE_NAME="exotel-bridge"
AWS_REGION="${AWS_REGION:-ap-south-1}"  # Default to Mumbai for lower latency to India
ECR_REPO_NAME="exotel-bridge-repo"
ROLE_NAME="AppRunnerECRAccessRole"

# ElevenLabs region configuration
# Options: default, us, eu, india
# For India residency, use "india" which connects to api.in.residency.elevenlabs.io
ELEVENLABS_REGION="${ELEVENLABS_REGION:-india}"

# Check for required environment variables
if [ -z "$ELEVENLABS_AGENT_ID" ]; then
    echo "Error: ELEVENLABS_AGENT_ID is not set."
    echo "Please set it: export ELEVENLABS_AGENT_ID=your_agent_id"
    exit 1
fi

if [ -z "$ELEVENLABS_API_KEY" ]; then
    echo "Warning: ELEVENLABS_API_KEY is not set."
fi

echo "=============================================="
echo "ElevenLabs Configuration:"
echo "  Agent ID: $ELEVENLABS_AGENT_ID"
echo "  Region:   $ELEVENLABS_REGION"
case "$ELEVENLABS_REGION" in
    "india")
        echo "  API URL:  wss://api.in.residency.elevenlabs.io"
        ;;
    "eu")
        echo "  API URL:  wss://api.eu.residency.elevenlabs.io"
        ;;
    "us")
        echo "  API URL:  wss://api.us.elevenlabs.io"
        ;;
    *)
        echo "  API URL:  wss://api.elevenlabs.io"
        ;;
esac
echo "=============================================="

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

echo "Deploying $SERVICE_NAME to AWS account $ACCOUNT_ID in $AWS_REGION..."

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
if ! aws ecr describe-repositories --repository-names "$ECR_REPO_NAME" --region "$AWS_REGION" > /dev/null 2>&1; then
    echo "Creating ECR repository..."
    aws ecr create-repository --repository-name "$ECR_REPO_NAME" --region "$AWS_REGION"
fi

# 3. Login to ECR
echo "Logging into ECR..."
aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "$ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com"

# 4. Build and Push Image
echo "Building container image..."
IMAGE_URL="$ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/$ECR_REPO_NAME:latest"
docker build -t "$ECR_REPO_NAME" .
docker tag "$ECR_REPO_NAME:latest" "$IMAGE_URL"

echo "Pushing image to ECR..."
docker push "$IMAGE_URL"

# 5. Deploy to App Runner
echo "Checking for existing App Runner service..."
SERVICE_ARN=$(aws apprunner list-services --region "$AWS_REGION" --query "ServiceSummaryList[?ServiceName=='$SERVICE_NAME'].ServiceArn" --output text)

if [ -z "$SERVICE_ARN" ] || [ "$SERVICE_ARN" == "None" ]; then
    echo "Creating new App Runner service..."
    aws apprunner create-service \
        --service-name "$SERVICE_NAME" \
        --region "$AWS_REGION" \
        --source-configuration "{
            \"AuthenticationConfiguration\": {
                \"AccessRoleArn\": \"$ROLE_ARN\"
            },
            \"ImageRepository\": {
                \"ImageIdentifier\": \"$IMAGE_URL\",
                \"ImageConfiguration\": {
                    \"Port\": \"10002\",
                    \"RuntimeEnvironmentVariables\": {
                        \"ELEVENLABS_AGENT_ID\": \"$ELEVENLABS_AGENT_ID\",
                        \"ELEVENLABS_API_KEY\": \"$ELEVENLABS_API_KEY\",
                        \"ELEVENLABS_REGION\": \"$ELEVENLABS_REGION\"
                    }
                },
                \"ImageRepositoryType\": \"ECR\"
            }
        }"
else
    echo "Updating existing App Runner service: $SERVICE_ARN"
    aws apprunner update-service \
        --service-arn "$SERVICE_ARN" \
        --region "$AWS_REGION" \
        --source-configuration "{
            \"AuthenticationConfiguration\": {
                \"AccessRoleArn\": \"$ROLE_ARN\"
            },
            \"ImageRepository\": {
                \"ImageIdentifier\": \"$IMAGE_URL\",
                \"ImageConfiguration\": {
                    \"Port\": \"10002\",
                    \"RuntimeEnvironmentVariables\": {
                        \"ELEVENLABS_AGENT_ID\": \"$ELEVENLABS_AGENT_ID\",
                        \"ELEVENLABS_API_KEY\": \"$ELEVENLABS_API_KEY\",
                        \"ELEVENLABS_REGION\": \"$ELEVENLABS_REGION\"
                    }
                },
                \"ImageRepositoryType\": \"ECR\"
            }
        }"
fi

# Wait for service to be ready and get the URL
echo -e "\nWaiting for service to be ready..."
sleep 5

SERVICE_URL=$(aws apprunner describe-service \
    --service-arn "$(aws apprunner list-services --region "$AWS_REGION" --query "ServiceSummaryList[?ServiceName=='$SERVICE_NAME'].ServiceArn" --output text)" \
    --region "$AWS_REGION" \
    --query "Service.ServiceUrl" \
    --output text 2>/dev/null || echo "")

echo -e "\n=============================================="
echo "Deployment initiated!"
echo "=============================================="
echo "Service:     $SERVICE_NAME"
echo "AWS Region:  $AWS_REGION"
echo "EL Region:   $ELEVENLABS_REGION"
if [ -n "$SERVICE_URL" ] && [ "$SERVICE_URL" != "None" ]; then
    echo "----------------------------------------------"
    echo "WebSocket URL for Exotel:"
    echo "  wss://$SERVICE_URL/media"
    echo ""
    echo "Health Check:"
    echo "  https://$SERVICE_URL/health"
fi
echo "=============================================="
echo ""
echo "Monitor deployment status:"
echo "  aws apprunner list-services --region $AWS_REGION --query \"ServiceSummaryList[?ServiceName=='$SERVICE_NAME']\""
echo ""
echo "View logs:"
echo "  aws apprunner list-operations --service-arn <SERVICE_ARN> --region $AWS_REGION"
echo "=============================================="

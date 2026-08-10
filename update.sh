#!/bin/bash

# Alert Manager Update Script
# Pulls latest code, rebuilds Docker containers, and starts services

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Logging
log() {
    echo -e "${BLUE}[$(date '+%Y-%m-%d %H:%M:%S')]${NC} $1"
}

error() {
    echo -e "${RED}[ERROR]${NC} $1" >&2
}

success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

# Get script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

log "Starting Alert Manager update process..."

# Check if git repo
if [ ! -d ".git" ]; then
    error "Not a git repository. Please run this script from the project root."
    exit 1
fi

# Check for uncommitted changes
if ! git diff-index --quiet HEAD --; then
    warning "You have uncommitted changes:"
    git status --short
    read -p "Continue anyway? (y/N): " -r
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        log "Update cancelled"
        exit 0
    fi
fi

# Pull latest changes
log "Pulling latest changes from git..."
git pull

if [ $? -ne 0 ]; then
    error "Git pull failed"
    exit 1
fi

success "Code updated successfully"

# Check if docker-compose is available
if ! command -v docker-compose &> /dev/null; then
    error "docker-compose not found. Please install Docker Compose."
    exit 1
fi

# Stop running containers
log "Stopping existing containers..."
docker-compose down

# Build containers
log "Building Docker containers (this may take a few minutes)..."
docker-compose build --no-cache

if [ $? -ne 0 ]; then
    error "Docker build failed"
    exit 1
fi

success "Containers built successfully"

# Start containers with profiles
log "Starting primary containers ..."
docker-compose --profile primary up -d

if [ $? -ne 0 ]; then
    error "Failed to start containers"
    exit 1
fi

success "Containers started successfully"

# Show running containers
log "Running containers:"
docker-compose ps

# Show logs tail
echo ""
log "Recent logs (Ctrl+C to exit):"
docker-compose logs --tail=20 --follow

exit 0

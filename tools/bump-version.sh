#!/bin/bash

# Version Bump Script for Alert Manager
# Usage: ./tools/bump-version.sh [major|minor|patch]
#
# This script:
# 1. Reads current version from VERSION file
# 2. Increments the specified version component
# 3. Updates VERSION file
# 4. Updates version badges in README.md
# 5. Updates version in CLAUDE.md
# 6. Displays the new version
#
# After running this script, you should:
# 1. Update CHANGELOG.md with the new version section
# 2. Commit the changes: git commit -am "Bump version to X.Y.Z"
# 3. Tag the commit: git tag vX.Y.Z
# 4. Push with tags: git push && git push --tags

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Check if argument provided
if [ $# -eq 0 ]; then
    echo -e "${RED}Error: No version bump type specified${NC}"
    echo "Usage: $0 [major|minor|patch]"
    echo ""
    echo "Examples:"
    echo "  $0 patch   # 1.1.0 -> 1.1.1"
    echo "  $0 minor   # 1.1.0 -> 1.2.0"
    echo "  $0 major   # 1.1.0 -> 2.0.0"
    exit 1
fi

BUMP_TYPE=$1

# Validate bump type
if [[ ! "$BUMP_TYPE" =~ ^(major|minor|patch)$ ]]; then
    echo -e "${RED}Error: Invalid bump type '$BUMP_TYPE'${NC}"
    echo "Valid types: major, minor, patch"
    exit 1
fi

# Get project root (one directory up from tools/)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VERSION_FILE="$PROJECT_ROOT/VERSION"
README_FILE="$PROJECT_ROOT/README.md"
CLAUDE_FILE="$PROJECT_ROOT/CLAUDE.md"

# Check if VERSION file exists
if [ ! -f "$VERSION_FILE" ]; then
    echo -e "${RED}Error: VERSION file not found at $VERSION_FILE${NC}"
    exit 1
fi

# Read current version
CURRENT_VERSION=$(cat "$VERSION_FILE")
echo -e "${BLUE}Current version: ${NC}$CURRENT_VERSION"

# Parse version components
if [[ ! "$CURRENT_VERSION" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)$ ]]; then
    echo -e "${RED}Error: Invalid version format in VERSION file${NC}"
    echo "Expected format: MAJOR.MINOR.PATCH (e.g., 1.2.3)"
    exit 1
fi

MAJOR="${BASH_REMATCH[1]}"
MINOR="${BASH_REMATCH[2]}"
PATCH="${BASH_REMATCH[3]}"

# Increment version based on bump type
case "$BUMP_TYPE" in
    major)
        MAJOR=$((MAJOR + 1))
        MINOR=0
        PATCH=0
        ;;
    minor)
        MINOR=$((MINOR + 1))
        PATCH=0
        ;;
    patch)
        PATCH=$((PATCH + 1))
        ;;
esac

NEW_VERSION="$MAJOR.$MINOR.$PATCH"
echo -e "${GREEN}New version:     ${NC}$NEW_VERSION"
echo ""

# Confirm with user
read -p "Update version to $NEW_VERSION? (y/n) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo -e "${YELLOW}Version bump cancelled${NC}"
    exit 0
fi

# Update VERSION file
echo "$NEW_VERSION" > "$VERSION_FILE"
echo -e "${GREEN}✓${NC} Updated VERSION file"

# Update README.md version badge
if [ -f "$README_FILE" ]; then
    sed -i "s/version-[0-9]\+\.[0-9]\+\.[0-9]\+-blue/version-$NEW_VERSION-blue/g" "$README_FILE"
    echo -e "${GREEN}✓${NC} Updated README.md version badge"
else
    echo -e "${YELLOW}⚠${NC} README.md not found, skipping"
fi

# Update CLAUDE.md version and date
if [ -f "$CLAUDE_FILE" ]; then
    CURRENT_DATE=$(date +%Y-%m-%d)
    sed -i "s/\*\*Version:\*\* [0-9]\+\.[0-9]\+\.[0-9]\+/**Version:** $NEW_VERSION/g" "$CLAUDE_FILE"
    sed -i "s/\*\*Last Updated:\*\* [0-9]\{4\}-[0-9]\{2\}-[0-9]\{2\}/**Last Updated:** $CURRENT_DATE/g" "$CLAUDE_FILE"
    echo -e "${GREEN}✓${NC} Updated CLAUDE.md version and date"
else
    echo -e "${YELLOW}⚠${NC} CLAUDE.md not found, skipping"
fi

echo ""
echo -e "${GREEN}Version bump complete!${NC}"
echo ""
echo "Next steps:"
echo "1. Update CHANGELOG.md with new version section"
echo "2. Review changes: git diff"
echo "3. Commit: git commit -am 'Bump version to $NEW_VERSION'"
echo "4. Tag: git tag v$NEW_VERSION"
echo "5. Push: git push && git push --tags"

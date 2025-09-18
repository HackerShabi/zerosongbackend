#!/bin/bash

# Install Python dependencies
pip install --upgrade pip
pip install -r requirements.txt

# Download Spleeter models (optional - they'll be downloaded on first use)
# This can help reduce cold start times
echo "Build completed successfully"
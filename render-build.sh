#!/usr/bin/env bash
# exit on error
set -o errexit

# Install OS dependencies required by underlying libraries (MarkItDown, python-pptx, etc.)
apt-get update && apt-get install -y libmagic1 libgl1 poppler-utils zlib1g-dev libjpeg-dev tesseract-ocr

pip install -r requirements.txt

# forms/

This directory holds the licensed blank ACORD PDF templates:

- `acord25_2016.pdf`  — ACORD 25 (2016/03) blank Certificate of Liability Insurance
- `acord101.pdf`      — ACORD 101 (2008/01) Additional Remarks Schedule (drivers/trailers)

**Upload both PDFs here before deploying.** They are binary files and must be
uploaded via GitHub's "Upload files" UI or `git` command-line — not the web
editor. Set Render env vars `ACORD25_TEMPLATE_PATH=forms/acord25_2016.pdf`
and `ACORD101_TEMPLATE_PATH=forms/acord101.pdf` after upload.

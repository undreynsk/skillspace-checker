# skillspace-checker

**English** | [Русский](README.ru.md)

CLI tool that checks students' Python homework on a Skillspace school:
downloads works waiting for review, runs the code against the examples from the task
in an isolated Docker container and lets the teacher accept the work from the console.

## How it works

- Logs in through the site form (headless Chromium, Playwright) and calls the internal Skillspace API.
- Tests are the input / output example tables from the task text.
- Each test runs in a throwaway `python:3.14-slim` container: no network, 64 MB RAM,
  2 s time limit, read-only filesystem. `input()` prompts are suppressed so only the result is compared.
- For every work the tool shows the task, the student's code and the verdict, then asks
  `Accept? [y/N]` and an optional comment.

## Requirements

- Windows with WSL2 Ubuntu and Docker Engine inside it (`sudo apt install docker.io`)
- uv

## Run

    cp .env.example .env    # fill in URL, school id, email and password
    uv run playwright install chromium
    uv run checker.py

`auth.py` is a helper used to discover the API: it logs in with a visible browser
and records requests to `logs/` with credentials masked.

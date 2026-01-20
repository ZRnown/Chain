# Repository Guidelines

## Project Structure & Module Organization
- `src/` contains the bot’s core modules (entry point in `src/main.py`).
- `config/tasks.json` stores MTProto clients and scheduled task definitions.
- `state.json` is auto-generated runtime state; do not edit manually.
- `sessions/` holds Telethon session files.
- `requirements.txt` lists Python dependencies.

## Build, Test, and Development Commands
- `pip install -r requirements.txt` installs runtime dependencies.
- `python -m src.main` runs the Telegram bot locally.
- `LOG_LEVEL=DEBUG python -m src.main` enables verbose logging for troubleshooting.

## Coding Style & Naming Conventions
- Python code uses 4-space indentation and standard PEP 8 conventions.
- Use `snake_case` for functions/variables, `PascalCase` for classes, and `UPPER_SNAKE` for constants.
- Keep configuration and state changes in `config/` and `state.json` behind bot commands where possible.

## Testing Guidelines
- No automated test suite is present in this repository.
- If you add tests, prefer `pytest` and place them under `tests/` with `test_*.py` names.
- For manual checks, use bot commands like `/ca <chain> <address>` and review logs.

## Commit & Pull Request Guidelines
- Recent commit history uses generic messages like "update" with no formal convention.
- Please use short, imperative summaries (e.g., "Add GMGN fallback") and include context in the PR description.
- PRs should explain config changes and link related issues/tasks when applicable.

## Security & Configuration Tips
- Do not commit `.env`, `state.json`, or `sessions/*.session` files.
- Required runtime settings live in `.env` (e.g., `TG_BOT_TOKEN`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`).
- Time windows and task behavior are UTC+8; document any changes to scheduling logic.

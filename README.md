# Templatea Backend API

A FastAPI service that wraps the Templatea pipeline for downloading, processing, and rendering Instagram reels. It exposes workspace artifacts, template metadata, and orchestration controls over HTTP + WebSocket.

## Features
- Indexes generated workspaces in SQLite for fast listing and lookup.
- Trigger downloads via the existing `orchestrator.py` pipeline with template selection.
- Serves every artifact (videos, OCR text, logs) with HTTP range support for media files.
- Streams live status updates over WebSockets and broadcasts step transitions.
- Manages template manifests and renderer dispatch through a registry.
- Ships with Docker and basic test coverage.

## Prerequisites
- Python 3.11+
- ffmpeg, yt-dlp on `PATH`
- Optional APIs used by existing scripts: `GEMINI_API_KEY`, `PERPLEXITY_API_KEY`

## Local Setup
```bash
python -m venv .venv
. .venv/Scripts/activate  # Windows PowerShell: . .venv/Scripts/Activate.ps1
pip install --upgrade pip
pip install -r requirements.txt
```

Export required environment variables (for example in PowerShell):
```powershell
$env:API_KEY = "super-secret"
$env:GEMINI_API_KEY = "..."   # optional, required for OCR step
$env:PERPLEXITY_API_KEY = "..."  # optional
```

Run the API:
```bash
uvicorn api.app:APP --host 0.0.0.0 --port 8000
```

## Template Rendering CLI
Render a single video against any template manifest without running the whole pipeline:
```bash
python cli/template_renderer.py --input input.mp4 --output output.mp4 \
  --choice-file workspace/<id>/03_choice/choice.txt \
  --template-root templates/clean_ad \
  --override text.color="#ff00ff"
```

Key options:
- `--text` or `--choice-file` supplies the caption text (choice file is read and stripped).
- `--template-root` points to the folder containing `template.json` and assets (defaults to `templates/default`).
- `--override key=value` applies shallow overrides to the template configuration; dotted keys reach nested values and values accept JSON literals.
- `--dry-run` prints the resolved payload without invoking ffmpeg/Pillow, helpful for validation.

The CLI uses the same `TemplateEngine` that powers the orchestrator, so pipeline behaviour is unchanged.

Key endpoints:
- `GET /api/v1/workspaces` - list indexed workspaces
- `GET /api/v1/workspaces/{id}` - retrieve metadata + artifact URLs
- `POST /api/v1/reels` - trigger a new download (requires `X-API-Key` header)
- `POST /api/v1/workspaces/{id}/choice` - set manual/AI copy and re-render
- `GET /api/v1/templates` – enumerate available templates
- `WS /ws/workspace/{id}?api_key=...` – live status stream

## Running Tests
```bash
pytest
```
Two integration tests are skipped automatically if FastAPI is not installed; installing requirements enables the full suite.

## Docker
Build the image and run locally:
```bash
docker build -t templatea-api -f docker/Dockerfile .
docker run --rm -p 8000:8000 \
  -e API_KEY=super-secret \
  -e GEMINI_API_KEY=$GEMINI_API_KEY \
  -e PERPLEXITY_API_KEY=$PERPLEXITY_API_KEY \
  -v $(pwd)/workspace:/app/workspace \
  -v $(pwd)/db:/app/db \
  templatea-api
```

Or use docker-compose:
```bash
docker-compose up --build
```

On container start, the entrypoint runs `python -m api.db_init` to ensure the SQLite schema is present before launching Uvicorn.

## Template Registry
Template manifests live in `templates/*.json`. Each manifest declares an `id`, `module`, and defaults. The API loads these manifests at runtime and resolves renderer callables using `api/template_registry.py`.

### Text casing configuration
Each template can optionally force the rendered caption into a specific casing by setting `text.casing_mode` inside `template.json`. Supported values are:
- `original` (default)
- `upper`, `uppercase`, or `all_caps`
- `lower`, `lowercase`, or `all_lower`
- `sentence`, `sentence_case`, or `capitalize`
- `title`, `titlecase`, or `title_case`

The value can also be supplied via overrides when invoking the renderer; the transformation is applied before layout, highlights, and compositing.

## Notes
- The API writes and serves from `workspace/` and indexes metadata in `db/workspaces.sqlite`.
- The orchestration step updates workspace metadata with the chosen `template_id` for replayability.
- WebSocket status messages combine periodic polling with event bus updates emitted after orchestration steps.

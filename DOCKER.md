# Templatea - Docker Deployment

Run the Templatea video processing backend on any machine with Docker.

## Quick Start

```bash
# 1. Clone the repository
git clone https://github.com/Aryan-d13/templatea.git
cd templatea

# 2. Create environment file
cp .env.example .env
# Edit .env with your API keys

# 3. Build and run
docker-compose up -d

# 4. Check status
docker-compose logs -f
```

The API will be available at `http://localhost:8000`

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `API_KEY` | No | Authentication key for API access |
| `GEMINI_API_KEY` | Yes | Google Gemini API key for AI features |
| `PERPLEXITY_API_KEY` | No | Perplexity API for research |
| `GROQ_API_KEY` | No | Groq API for fast inference |

## Commands

```bash
# Build image
docker-compose build

# Start container
docker-compose up -d

# View logs
docker-compose logs -f api

# Stop container
docker-compose down

# Rebuild and restart
docker-compose up -d --build
```

## Connecting Mobile App

1. Find your machine's IP address:
   - Windows: `ipconfig` → Look for IPv4
   - Linux/Mac: `hostname -I` or `ifconfig`

2. In the mobile app, enter: `http://YOUR_IP:8000`

## Data Persistence

- `./workspace/` - Video workspaces and outputs
- `./db/` - SQLite database
- `./__video_assets/` - Downloaded video assets

## Troubleshooting

**Port 8000 already in use:**
```bash
docker-compose down
# Or change port in docker-compose.yml
```

**Build fails:**
```bash
docker-compose build --no-cache
```

**Check container health:**
```bash
docker-compose ps
curl http://localhost:8000/health
```

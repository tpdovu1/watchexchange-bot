# Watchexchange Bot

A Reddit watch listing monitor that watches r/Watchexchange for under-market deals and alerts you via Discord.

## Features

- **RSS-based monitoring** - Polls Reddit RSS feed for new posts
- **AI-powered scoring** - Uses MiniMax API to evaluate if a watch is under market price
- **Keyword filtering** - Filters for specific watch brands (Omega, Rolex, Seiko, etc.)
- **Special rules** - Automatically matches Seiko under $1000 and any watch mentioning "1993"
- **Multi-channel Discord alerts** - Sends to different Discord webhooks based on match quality:
  - **ALERT** - Best deals (keyword match + under market + score >= 8)
  - **TEST** - Keyword matches that didn't qualify for ALERT
  - **ALL** - All posts with a price
- **Daily CSV logging** - Saves elite deals to daily CSV files
- **Automated CSV upload** - Uploads previous day's CSV to Discord at a scheduled time

## Requirements

- Python 3.12+
- Discord webhooks (optional)
- MiniMax API key (for AI scoring)

## Installation

1. Clone the repository:
```bash
git clone https://github.com/tpdovu1/watchexchange-bot.git
cd watchexchange-bot
```

2. Create a virtual environment:
```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

3. Install dependencies:
```bash
pip install -r requirements.txt
```

4. Copy the example environment file:
```bash
cp .env.example .env
```

5. Edit `.env` with your configuration (see Environment Variables below).

## Running Locally

```bash
python watcher.py
```

## Deployment to Railway

### Option 1: Deploy from GitHub

1. Push your code to GitHub
2. Go to [railway.app](https://railway.app)
3. Create a new project → "Deploy from GitHub repo"
4. Select your repository
5. Add environment variables in Railway dashboard
6. Deploy!

### Option 2: Deploy with Docker

```bash
docker build -t watchexchange-bot .
docker run -d --env-file .env watchexchange-bot
```

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `ALERT_DISCORD_WEBHOOK_URL` | Discord webhook for best deals (keyword + under market + score >= 8) | - |
| `TEST_DISCORD_WEBHOOK_URL` | Discord webhook for keyword matches | - |
| `ALL_DISCORD_WEBHOOK_URL` | Discord webhook for all posts with price | - |
| `CSV_DISCORD_WEBHOOK_URL` | Discord webhook for daily CSV upload | - |
| `MINIMAX_API_KEY` | MiniMax API key for AI scoring | - |
| `SUBREDDIT` | Subreddit to watch | `Watchexchange` |
| `KEYWORDS` | Comma-separated keywords to match | `omega,rolex,speedmaster,grand seiko,cartier,tudor,jlc,jaeger lecoultre,breitling,audemars piguet,ap,seamaster,patek,patek phillipe` |
| `CHECK_INTERVAL_SECONDS` | How often to check RSS (seconds) | `120` |
| `MAX_POST_AGE_MINUTES` | Only process posts younger than this | `60` |
| `CSV_UPLOAD_HOUR` | Hour to upload daily CSV (0-23) | `0` |
| `CSV_UPLOAD_MINUTE` | Minute to upload daily CSV (0-59) | `5` |
| `CSV_DIRNAME` | Directory name for CSV files | `daily_csv` |

## How It Works

1. **Fetch RSS** - Retrieves latest posts from Reddit RSS feed
2. **Filter by age** - Skips posts older than `MAX_POST_AGE_MINUTES`
3. **Extract price** - Looks for price in title/summary, or fetches OP comments if not found
4. **Keyword match** - Checks if post matches configured keywords
5. **AI scoring** - Sends to MiniMax API to evaluate if under market
6. **Route to Discord** - Sends to appropriate webhook based on score
7. **CSV logging** - Saves high-scoring deals (score > 8) to daily CSV

## Special Matching Rules

- **Seiko under $1000** - Any Seiko watch priced under $1000 automatically matches
- **1993 mentions** - Any watch mentioning "1993" automatically matches
- **Keyword matching** - Uses whole-word matching (won't match "ap" in "lap")

## Files

- `watcher.py` - Main bot script
- `Dockerfile` - Docker container configuration
- `requirements.txt` - Python dependencies
- `.env.example` - Environment variable template
- `daily_csv/` - Directory for daily CSV logs (created automatically)

## License

MIT

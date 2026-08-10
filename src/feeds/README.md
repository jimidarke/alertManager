# Feed Aggregator Service

This service automatically fetches data from RSS feeds and public APIs, converts them to Alert Manager payloads, and publishes them to the MQTT `alerts/input` topic for routing.

## Architecture

```
Feed Aggregator
├── Scheduler (APScheduler)
│   ├── News Feed (30 min intervals)
│   ├── Joke Feed (120 min intervals)
│   ├── Fact Feed (60 min intervals)
│   └── Trivia Feed (240 min intervals)
│
└── MQTT Publisher → alerts/input → Alert Router → Pathways
                                                    ├── LED Sign
                                                    └── File Logger
```

## Feed Modules

### BaseFeed (Abstract Class)
All feed handlers inherit from `BaseFeed`, which provides:
- **Error handling**: Retry once, send warning alert on failure
- **MQTT publishing**: Publish all fetched items to `alerts/input`
- **Consistent logging**: Standard logging format across all feeds

### NewsFeed
- **Source**: RSS feeds (default: CBC News Top Stories)
- **Method**: XML parsing with ElementTree
- **Output**: One alert per news story (5 stories)
- **Format**:
  - `title`: Story headline
  - `message`: First 150 chars of description (if different from title) + `<a href>` "read more" link
  - `metadata.link`: URL to full article
- **HTML Handling**:
  - Strips HTML tags and decodes entities from description
  - Adds clickable "read more" link in message for HTTP gateway log viewer

### JokeFeed
- **Source**: JokeAPI (https://v2.jokeapi.dev)
- **Method**: REST API with batch request
- **Output**: One alert per joke (5 jokes)
- **Format**: Joke text with newlines replaced by spaces
- **Handles**: Both single-part and two-part jokes

### FactFeed
- **Source**: API Ninjas Facts API
- **Method**: Sequential REST API calls with delay
- **Output**: One alert per fact (5 facts)
- **Rate Limiting**: 2-second delay between requests
- **Requires**: API_NINJAS_KEY environment variable

### TriviaFeed
- **Source**: API Ninjas Trivia API
- **Method**: REST API with random category selection
- **Output**: One alert with question and answer
- **Format**: "Q: {question} A: {answer}"
- **Categories**: Configurable list (science, general, geography, etc.)
- **Requires**: API_NINJAS_KEY environment variable

## Configuration

Feed configuration is stored in the main config file (`docker.json`, `main.json`, etc.):

```json
{
  "feed_aggregator": {
    "enabled": true,
    "mqtt": {
      "broker": "mqtt-broker",
      "port": 1883,
      "topic": "alerts/input"
    },
    "client_name": "feed-updates",
    "feeds": {
      "news": {
        "enabled": true,
        "interval_minutes": 30,
        "source": "feed-aggregator/rss-news",
        "rss_url": "https://ctvnews.ca/rss/TopStories",
        "num_items": 5,
        "category": "news",
        "level": "info"
      }
    }
  }
}
```

## Deployment

### Docker Compose
```bash
# Start feed aggregator with other services
docker-compose --profile feeds,logging,http up -d

# Rebuild after code changes
docker-compose build --no-cache alert-router
docker-compose --profile feeds restart
```

### Environment Variables
Required in `.env`:
```bash
API_NINJAS_KEY=your-api-key-here
```

## Error Handling

1. **First Attempt**: Fetch data from API/RSS
2. **If Fails**: Wait 5 seconds
3. **Retry**: Try again
4. **If Both Fail**:
   - Send warning alert to `alerts/input`
   - Skip current cycle
   - Try again at next scheduled interval

Error alerts include:
- `feed_type`: Which feed failed
- `error_message`: Human-readable error
- `retry_attempted`: Always `true`
- `original_error`: First attempt error message

## Adding New Feeds

1. Create new feed handler in `src/feeds/`
2. Inherit from `BaseFeed`
3. Implement `fetch()` method returning list of payloads
4. Add configuration to `docker.json`
5. Import and initialize in `feed_aggregator.py`

Example:
```python
from feeds.base_feed import BaseFeed
from utils.payload_builder import build_alert_payload

class WeatherFeed(BaseFeed):
    def fetch(self) -> List[Dict[str, Any]]:
        # Fetch weather data
        response = requests.get(self.config['api_url'])
        data = response.json()

        # Build alert payload
        payload = build_alert_payload(
            level=self.config['level'],
            category=self.config['category'],
            title="Weather Alert",
            message=f"Temperature: {data['temp']}°C",
            source=self.config['source'],
            client=self.client_name,
            metadata={"feed_type": "weather"}
        )

        return [payload]
```

## Logging

- **Operational Logs**: `/data/logs/ops/alert-feed-aggregator.{date}.log`
- **Alert Content**: `/data/logs/file/feed-updates.{date}.log` (via file-logger)
- **Rotation**: Weekly (Monday-Sunday)

## Testing

```bash
# Test locally without Docker
python3 src/feed_aggregator.py --config data/config/test.json

# View logs in Docker
docker logs alert-feed-aggregator -f

# Test specific feed manually
python3 -c "from feeds.news_feed import NewsFeed; print(NewsFeed({...}).fetch())"
```

## Compliance

All feed alerts comply with CLIENTNODE.md specification:
- ✅ Unix epoch timestamp (integer)
- ✅ Valid level (critical, warning, notice, info, debug)
- ✅ Valid category (security, system, weather, automation, personal, network, application, news, business, hardware, environmental)
- ✅ String source (not object)
- ✅ Client name for routing
- ✅ Optional metadata object

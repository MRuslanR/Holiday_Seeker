# Holiday_Seeker

A tool for the automated collection, verification, and distribution of information regarding official public holidays and non-working days in various countries. The system aggregates data from multiple public APIs, utilizes LLM (via OpenRouter) for data deduplication and fact-checking, stores results in a local SQLite database, and sends reports via Telegram and Email.

## Features

1. **Data Collection:** Aggregation of information from three sources (API-Ninjas, Nager.Date, OpenHolidaysAPI).
2. **LLM Processing:**
   - Deduplication: Merging similar entries from different sources.
   - Fact-checking: Verifying with a model whether a specific holiday is an official non-working day.
3. **Storage:** Local SQLite database (holidays.db).
4. **Reporting:**
   - Generation of summary Excel reports for all tracked countries.
   - HTML composition for Email notifications.
5. **Interface:**
   - Telegram bot for management, instant date checks, and monthly reports.
   - Automatic task scheduler (Cron-like) for monthly execution.

## Requirements

- Python 3.11+
- Accounts and API keys:
  - OpenRouter (access to Perplexity/LLM models)
  - API Ninjas
  - Telegram Bot Token
  - SMTP server (for Email notifications)

## Installation and Setup

### 1. Environment Variables (.env)
The project includes a template file named `envExample`.

Rename `envExample` to `.env` and fill in the required fields with your specific API keys, database paths, and email server settings.

### 2. Configuration File (config.xlsx)
The project root must contain an Excel file named `config.xlsx` with two sheets:
- **Countries**: The first column must contain two-letter country codes (ISO Alpha-2), for example: US, RU, KZ, DE.
- **Emails**: The first column must contain the list of email recipients for notifications.

### 3. Docker Deployment

Build the image:
```docker build -t holidays-bot .```

Run the container (mounting a volume for database persistence):
```docker run -d --name holidays-bot```

### 4. Manual Execution
Install dependencies:
```pip install -r requirements.txt```

Run the bot:
```python bot.py```



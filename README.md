# BTC DCA Scheduler

Bitcoin Dollar Cost Averaging (DCA) automation tool with Line notification.

## Setup

1. Clone the repository
2. Create virtual environment: `python -m venv venv`
3. Activate virtual environment: `source venv/bin/activate`
4. Install dependencies: `pip install -r requirements.txt`
5. Copy `.env.example` to `.env` and fill in your configuration
6. Run the application

## Configuration

Copy `.env.example` to `.env` and configure:
- LINE_NOTIFY_TOKEN: Your Line Notify token
- Other necessary API keys and configurations

## Usage

```bash
python main.py

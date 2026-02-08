import datetime
import json

LOG_PATH = '../logs/agent_events.log'

def capture_event(event_type, event_data):
    timestamp = datetime.datetime.utcnow().isoformat()
    with open(LOG_PATH, 'a') as f:
        f.write(f"[{timestamp}] {event_type}: {json.dumps(event_data)}\n")
